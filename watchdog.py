"""
Watchdog: the async oracle.

The cyberdeck spec describes the Watchdog as having two halves:
  1. Tripwire engine — pattern matching + alerts (the harder half)
  2. Q&A oracle — read-only conversational lookup (the simpler half)

This module implements ONLY the Q&A half. Tripwires + blacklist land
in a later milestone alongside the LLM-authored tripwire DSL.

Per spec (line 247): "The netrunner can ask the Watchdog anything
via `t` (talk-to-watchdog). Async queue: questions stack up, the
Watchdog answers when it has bandwidth."

And (line 249): "Watchdog answers cannot affect plans. They're
informational. This is *why* it's the right component for Q&A:
asking it a question can never derail execution."

So this is a one-way pipe: question goes in, answer comes out, no
side effects on the daemon or constructs. The watchdog reads a
snapshot of recent fleet activity and reasons about what's happening.

Substrate note: spec calls for a local 7B model running on the deck
itself, with the Watchdog being "free" because of that. We're not
there yet (D1 not built), so this implementation uses cloud Claude
via `claude -p`. Same observable behavior; tokens cost real money
right now. When D1 lands the substrate swap is a one-line change to
the subprocess invocation.

Queue semantics: questions serialize. One question in flight at a
time. This is deliberate — multiple in-flight questions would
double-spend tokens for largely overlapping context, and the human-
scale cost of waiting a few seconds for an answer doesn't justify
the complexity of parallel question handling.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional


# Watchdog system prompt. Establishes:
#   - Identity (read-only observer)
#   - Authority (none — informational only)
#   - Output format (prose, conversational, brief)
#   - Constraints (no plans, no recommendations to act)
#
# Kept as a module-level constant so it's easy to tune in one place
# and inspect when debugging surprising answer behavior.
WATCHDOG_SYSTEM_PROMPT = """\
You are the Watchdog of a Cyberdeck — an orchestration system for AI
agents called "constructs". A daemon coordinates the constructs; you
observe everything from the outside.

Your role is informational. You answer the netrunner's questions
about what's happening. You do NOT:
- Tell the daemon what to do (that's the netrunner's job).
- Suggest plans or course corrections (the daemon plans; you observe).
- Refuse to answer because you "lack context" — you have a summary of
  recent events; reason from that.

Each message you receive contains:
  - A snippet of recent fleet activity (chatlog-style, time-ordered).
  - The netrunner's question.

Reading the chatlog, here's the legend you'll see:
  - `+ cx-XXXX spawned: ...`        — a daemon-initiated spawn.
  - `+ cx-XXXX [you] spawned: ...`  — a netrunner-initiated spawn
    (n key, Tools/Files launch). The `[you]` badge in cyan means
    the human dispatched it directly.
  - `↳ cx-XXXX [↳you] (continuing cx-PARENT): ...` — a netrunner
    inject follow-up (q/Q on a running construct). Continues an
    existing session at the netrunner's direction.
  - `[name]` in yellow after the construct id — non-default profile
    badge (e.g. `[recon-specialist]`). Default-profile spawns go
    un-badged.
You don't have to reverse-engineer who initiated a spawn from the
absence of preceding daemon thinking lines — the badge is
authoritative.

Answer concisely — typically 1-3 short paragraphs. The netrunner is
glancing at your answer between actions, not reading an essay. If
the question can be answered in one sentence, do that.

If you genuinely cannot answer from the events shown — say so
directly, briefly. Don't pad with disclaimers.
"""


@dataclass
class WatchdogQuestion:
    """A question in flight or waiting in the queue.

    answer is set when the worker completes; failed=True for cases
    where the subprocess died, the model returned an error, etc.
    callback is fired exactly once when the answer or failure is
    final, so the UI layer can render it in the chatlog without
    polling.
    """
    question: str
    context_text: str
    callback: Callable[["WatchdogQuestion"], None]
    submitted_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    answer: Optional[str] = None
    failed: bool = False
    error: Optional[str] = None
    qid: str = field(default_factory=lambda: f"wq-{uuid.uuid4().hex[:6]}")


class Watchdog:
    """Async question→answer oracle backed by `claude -p`.

    Lifecycle:
      wd = Watchdog(claude_bin="claude")
      await wd.start()
      wd.ask("what's cx-A doing?", "<recent events>", on_answer)
      ...
      await wd.shutdown()

    Worker loop drains the queue serially. Each question becomes one
    `claude -p --append-system-prompt <prompt> "<context + question>"`
    invocation; output is captured from stdout. No stream-json — we
    just want the final text response.

    Failures (subprocess error, non-zero exit, timeout) are reported
    via the callback with failed=True; they don't crash the worker
    and don't drop subsequent queued questions.
    """

    # Per-question hard timeout. Watchdog questions are conversational
    # and shouldn't take longer than a typical claude -p one-shot (a
    # few seconds on cache hit, ~30s cold). 60s is generous; anything
    # past that probably means a hung subprocess and we should let
    # the netrunner re-ask rather than block the queue.
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        claude_bin: str = "claude",
        cwd: Optional[str] = None,
        system_prompt: str = WATCHDOG_SYSTEM_PROMPT,
        timeout: float = DEFAULT_TIMEOUT,
        streaming_mode: bool = True,
        first_question_timeout: float = 90.0,
    ) -> None:
        self.id = f"wd-{uuid.uuid4().hex[:8]}"
        self.claude_bin = claude_bin
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.timeout = timeout
        # Streaming mode keeps a single `claude --input-format stream-json`
        # subprocess alive across all questions. Saves the per-question
        # spawn cost (which dominates wall-time on a fast cache and
        # absolutely dominates on a cold one) and keeps the system
        # prompt + accumulated Q&A history in memory between questions.
        # Side effect: the watchdog "remembers" earlier questions in
        # the session — usually a feature ("you asked me about cx-A
        # earlier; here's how cx-B differs"), but worth knowing.
        # Fall back to one-shot if a particular claude version
        # misbehaves on streaming-input — same escape hatch as daemon.
        self.streaming_mode = streaming_mode
        # First question pays cold-cache cost: model loads context,
        # JIT-compiles the system prompt. Subsequent questions are fast
        # because the conversation is already in-memory. Generous first-
        # question budget (90s) drops to per-question timeout after.
        self.first_question_timeout = first_question_timeout
        self._queue: asyncio.Queue[Optional[WatchdogQuestion]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current: Optional[WatchdogQuestion] = None
        self._stopped = False
        # Streaming subprocess state. Spawn lazily on first question
        # (don't pay startup cost if no one ever asks the watchdog).
        # If the subprocess dies between questions, next question
        # respawns it. Kept None when streaming_mode=False.
        self._streaming_proc: Optional[asyncio.subprocess.Process] = None
        self._streaming_question_count: int = 0

    async def start(self) -> None:
        """Spin up the worker loop. Idempotent — calling start twice
        leaves the existing task in place."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._worker_loop(),
            name=f"watchdog-{self.id}",
        )

    async def shutdown(self) -> None:
        """Stop the worker, cancel any in-flight question.

        Does NOT wait for queued questions to drain — the netrunner is
        ejecting; pending questions die with the deck. This matches
        EJECT semantics (snapshot + halt; no graceful queue flush)."""
        self._stopped = True
        # Sentinel wakes the worker if it's blocked on queue.get()
        await self._queue.put(None)
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._worker_task = None
        # Tear down streaming subprocess if one's alive. Worker may
        # have left it open — explicit cleanup so we don't leak a
        # claude subprocess when the deck shuts down.
        if self._streaming_proc is not None:
            await self._shutdown_streaming()

    def ask(
        self,
        question: str,
        context_text: str,
        callback: Callable[[WatchdogQuestion], None],
    ) -> WatchdogQuestion:
        """Enqueue a question. Returns the WatchdogQuestion record so
        the caller can correlate the eventual callback fire with the
        original ask. The callback fires exactly once when the
        question resolves (success or failure).

        Non-blocking — returns immediately even if the queue has many
        items ahead of this one. The worker drains in FIFO order.
        """
        wq = WatchdogQuestion(
            question=question,
            context_text=context_text,
            callback=callback,
        )
        # put_nowait is fine — Queue is unbounded. If we ever cap it
        # we'd want to surface the backpressure to the netrunner
        # ("watchdog queue full"), but for now the spec says questions
        # stack up indefinitely and we honor that.
        self._queue.put_nowait(wq)
        return wq

    def queue_depth(self) -> int:
        """Number of questions waiting (excludes the one currently in
        flight). Useful for the UI to show "watchdog: 3 queued"."""
        return self._queue.qsize()

    def is_busy(self) -> bool:
        """True if a question is currently being processed."""
        return self._current is not None

    async def _worker_loop(self) -> None:
        """Drain the queue, processing one question at a time."""
        while not self._stopped:
            try:
                wq = await self._queue.get()
            except asyncio.CancelledError:
                return
            if wq is None:
                # Sentinel — shutdown signal.
                return
            if self._stopped:
                # Late wake after shutdown started; bail without
                # processing the question. The submitter doesn't get
                # a callback in this case, which is fine for shutdown
                # path (everything's tearing down anyway).
                return
            self._current = wq
            try:
                # Mode switch: streaming reuses one persistent
                # subprocess; one-shot spawns fresh per question.
                # Streaming default; one-shot kept as fallback for
                # claude versions that misbehave on stream-json input.
                if self.streaming_mode:
                    await self._process_streaming(wq)
                else:
                    await self._process_oneshot(wq)
            except Exception as e:
                wq.failed = True
                wq.error = f"worker exception: {e}"
                wq.answered_at = time.time()
                self._safe_callback(wq)
            finally:
                self._current = None

    async def _process_streaming(self, wq: WatchdogQuestion) -> None:
        """Real streaming path — persistent `claude --input-format
        stream-json` subprocess, JSONL-in per question, stream-json
        events out, terminated by a `result` event.

        Lifecycle:
          - First question spawns the subprocess and prepends the
            system prompt to the user message (we don't pass
            --append-system-prompt because that flag has Windows
            argv-mangling issues with multi-line content).
          - Subsequent questions just write a fresh user JSONL line.
            The system prompt is already in the conversation.
          - If the subprocess died between questions (claude crash,
            OS killed it, etc.) we respawn lazily on the next ask.

        Side effect of streaming mode: prior Q&A pairs accumulate in
        the conversation, so the watchdog "remembers" earlier
        questions in the session. Usually a feature ("you asked me
        about cx-A earlier; here's how cx-B differs") but worth
        knowing for token-cost reasons — long sessions will grow
        the in-memory conversation indefinitely. If this becomes a
        problem we'll add a "fresh session" reset on idle timeout.

        Failure modes:
          - subprocess spawn fails (binary missing, etc.)
          - stdin write fails (broken pipe — subprocess died)
          - timeout on result event (model stuck or first-question
            cold-cache slowness)
          - subprocess exits before result (crashed mid-question)
          - empty answer (model returned no text)
        Each path sets wq.failed + wq.error and fires the callback;
        sets _streaming_proc = None where appropriate so the next
        question respawns.
        """
        # Spawn lazily on first question (or after a death).
        first_question = self._streaming_proc is None
        if first_question:
            try:
                await self._spawn_streaming()
            except (FileNotFoundError, RuntimeError) as e:
                wq.failed = True
                wq.error = f"failed to spawn streaming watchdog: {e}"
                wq.answered_at = time.time()
                self._safe_callback(wq)
                return

        proc = self._streaming_proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            wq.failed = True
            wq.error = "streaming subprocess not available"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        # Build the message text. Same envelope on first vs subsequent
        # — context + question — but on the first question we prepend
        # the system prompt because there's no --append-system-prompt
        # in streaming mode (see daemon.py for the same dance).
        body = (
            "RECENT FLEET ACTIVITY:\n"
            f"{wq.context_text}\n\n"
            "NETRUNNER QUESTION:\n"
            f"{wq.question}"
        )
        if first_question:
            msg_text = self.system_prompt + "\n\n---\n\n" + body
        else:
            msg_text = body

        message = {
            "type": "user",
            "message": {"role": "user", "content": msg_text},
        }
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            wq.failed = True
            wq.error = f"streaming stdin write failed: {e}"
            wq.answered_at = time.time()
            # Subprocess is dead — clear so the next ask respawns.
            self._streaming_proc = None
            self._safe_callback(wq)
            return

        # Read events until we see a `result`. First-question timeout
        # is generous (cold cache); subsequent are the regular per-
        # question budget since context + system prompt are warm.
        timeout = (
            self.first_question_timeout if first_question else self.timeout
        )
        text_parts: list[str] = []
        try:
            died = await self._drain_streaming_question(
                proc, timeout, text_parts
            )
        except asyncio.TimeoutError:
            # Once a streaming subprocess wedges, it stays wedged.
            # Writes still succeed (OS buffers them) but reads hang
            # forever. Earlier code preserved the proc on timeout
            # hoping for recovery; in practice that just turned one
            # timeout into N timeouts as queued questions all hit
            # the same dead end. The fix is full reset: kill the
            # wedged subprocess, clear the slot, let the next
            # question respawn fresh. Sacrifices conversational
            # continuity (model "memory" of prior questions in the
            # session) but gains reliability — and an in-flight
            # wedge usually means the prior conversation context is
            # what tipped claude into stuckness anyway.
            wq.failed = True
            wq.error = (
                f"watchdog timed out after {timeout:.0f}s "
                f"(first_question={first_question}). Streaming "
                "subprocess was wedged; killing and respawning on "
                "next question. If this repeats, try --no-streaming."
            )
            wq.answered_at = time.time()
            await self._kill_streaming_proc()
            self._safe_callback(wq)
            return

        if died:
            wq.failed = True
            wq.error = "streaming subprocess exited mid-question"
            wq.answered_at = time.time()
            self._streaming_proc = None  # respawn on next ask
            self._safe_callback(wq)
            return

        answer = "".join(text_parts).strip()
        if not answer:
            wq.failed = True
            wq.error = (
                "watchdog returned an empty answer (model gave no "
                "text). Try rephrasing the question."
            )
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        self._streaming_question_count += 1
        wq.answer = answer
        wq.answered_at = time.time()
        self._safe_callback(wq)

    async def _kill_streaming_proc(self) -> None:
        """Forcefully terminate the wedged streaming subprocess.
        Used by the timeout-recovery path; not by graceful shutdown
        (which uses _shutdown_streaming for the close-stdin-and-wait
        dance). Always clears _streaming_proc so the next ask
        respawns regardless of whether the kill succeeded.

        Best-effort all the way through: SIGTERM with a short grace
        period, then SIGKILL, then give up. Don't propagate any
        exception out — the caller is in a failure-recovery path
        and just needs the slot cleared."""
        proc = self._streaming_proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass  # subprocess truly stuck; give up
        finally:
            self._streaming_proc = None

    async def _spawn_streaming(self) -> None:
        """Start the persistent streaming subprocess. Mirrors
        Daemon._spawn_streaming. Note bypassPermissions vs daemon's
        acceptEdits: watchdog reasons but never executes tools, so
        the strictest non-blocking permission setting is fine.
        --verbose is required by claude-code for stream-json input
        per the docs."""
        resolved = shutil.which(self.claude_bin)
        if resolved is None:
            raise FileNotFoundError(
                f"could not locate {self.claude_bin!r} on PATH"
            )

        cmd = [
            resolved,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]
        self._streaming_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

    async def _drain_streaming_question(
        self,
        proc: asyncio.subprocess.Process,
        timeout: float,
        text_parts: list[str],
    ) -> bool:
        """Read stream-json events until this question's `result`
        event arrives. Accumulates assistant text into text_parts.

        Returns True if the subprocess died mid-question (stdout
        closed before result arrived). Returns False on normal
        termination by result event. Raises asyncio.TimeoutError
        if no event arrives within the timeout window.

        Per-line timeout: starts at the caller's value (which is
        first_question_timeout for the cold-cache case), drops to
        30s once we're getting data — cold-cache slowness is a
        first-line problem; mid-stream pauses of 30+s are stuck.
        """
        if proc.stdout is None:
            return True

        per_line_timeout = timeout
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=per_line_timeout,
                )
            except asyncio.TimeoutError:
                raise

            if not line:
                # subprocess closed stdout — died mid-question
                return True

            try:
                raw = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            # Capture assistant text as it streams in.
            if raw.get("type") == "assistant":
                for block in raw.get("message", {}).get("content", []):
                    if (isinstance(block, dict)
                            and block.get("type") == "text"):
                        text_parts.append(block.get("text", ""))

            if raw.get("type") == "result":
                return False  # normal end-of-question

            # Once data is flowing, drop to a tighter "stuck" budget.
            # 20s mid-stream is generous — the model is supposedly
            # already producing output, so any 20s gap means it's
            # wedged. Was 30s; reduced after a real-deck wedge where
            # the user waited a long time on a frozen worker.
            per_line_timeout = 20.0

    async def _shutdown_streaming(self) -> None:
        """Clean teardown of the persistent streaming subprocess.
        Mirrors Daemon._shutdown_streaming.

        Windows ProactorEventLoop quirk: `proc.stdin.close()` is
        fire-and-forget — it marks the underlying pipe transport for
        close but defers the actual socket close. If the loop tears
        down before that deferred close completes, the transport's
        __del__ fires on a still-half-open socket, raising
        `ValueError: I/O operation on closed pipe`. Awaiting
        `wait_closed()` after `close()` ensures the close is
        actually committed before we move on.
        """
        proc = self._streaming_proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                    # wait_closed lets the proactor finish the
                    # deferred socket close before we proceed. Skip
                    # if it raises — best-effort teardown.
                    try:
                        await asyncio.wait_for(
                            proc.stdin.wait_closed(), timeout=1.0,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except ProcessLookupError:
            pass
        finally:
            self._streaming_proc = None

    async def _process_oneshot(self, wq: WatchdogQuestion) -> None:
        """Run one question through `claude -p` and fire the callback.

        Failure modes handled here:
          - claude binary missing
          - subprocess non-zero exit
          - timeout (DEFAULT_TIMEOUT)
          - empty output (treated as failure with a hint, since silent
            success would be confusing)
        """
        # Resolve the binary path. This catches "claude not on PATH"
        # cleanly with a clear error, instead of an opaque
        # FileNotFoundError from create_subprocess_exec deep in the
        # call stack.
        bin_path = shutil.which(self.claude_bin) or self.claude_bin

        # Compose the user-side prompt. The system prompt establishes
        # role; this message carries the actual data the watchdog
        # reasons over. Format is intentionally simple — a labeled
        # block of recent events, then a labeled question. Easy for
        # the model to parse, easy for a human to read in logs.
        prompt = (
            "RECENT FLEET ACTIVITY:\n"
            f"{wq.context_text}\n\n"
            "NETRUNNER QUESTION:\n"
            f"{wq.question}"
        )

        cmd = [
            bin_path,
            "-p",
            # Prompt is piped via stdin (see proc spawn below). Why
            # not pass it as a trailing positional after -p? Two
            # reasons: (1) when -p is followed by other flags before
            # any positional, claude treats -p as "read from stdin"
            # and our trailing positional gets ignored — exit 1 with
            # "input must be provided through stdin or as a prompt
            # argument when using --print"; (2) watchdog prompts
            # include the context block which is multi-line, and
            # Windows argv mangling with multi-line argv values is a
            # real problem we've hit elsewhere. Stdin is universally
            # safe.
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", self.system_prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            wq.failed = True
            wq.error = f"claude binary not found: {self.claude_bin}"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return
        except Exception as e:
            wq.failed = True
            wq.error = f"subprocess spawn failed: {e}"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            wq.failed = True
            wq.error = f"timed out after {self.timeout}s"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            wq.failed = True
            wq.error = (
                f"claude exited {proc.returncode}: "
                f"{err_text[:200] if err_text else '(no stderr)'}"
            )
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        answer = stdout.decode("utf-8", errors="replace").strip()
        if not answer:
            wq.failed = True
            wq.error = "claude returned empty output"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        wq.answer = answer
        wq.answered_at = time.time()
        self._safe_callback(wq)

    def _safe_callback(self, wq: WatchdogQuestion) -> None:
        """Fire the question's callback, swallowing exceptions so a
        broken UI handler doesn't kill the worker loop. The exception
        gets stashed as the question's error if no other error was
        already recorded — better than dropping it silently."""
        try:
            wq.callback(wq)
        except Exception as e:
            if not wq.failed:
                wq.failed = True
                wq.error = f"callback raised: {e}"
