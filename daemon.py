"""
Daemon: the coordinator that decomposes goals, delegates to constructs,
and reacts to their outcomes.

Implementation note on persistence
----------------------------------
Claude Code's streaming JSON input mode (one subprocess, many turns) is
underdocumented and behaves inconsistently across versions — see issue
anthropics/claude-code#24594. Early M4a work got stuck on it.

Current approach: each Daemon turn is a fresh `claude -p` subprocess
using the same proven pattern as Constructs. Session continuity comes
from capturing `session_id` on turn 1 and passing `--resume <id>` on
subsequent turns. ~2s per-turn startup overhead, but reliable and
debuggable. Reuses the Construct class internally.

Public API:
    d = Daemon(claude_bin="claude")
    async for event in d.run_turn("GOAL: ..."):   # turn 1
        ...consume events...
    async for event in d.run_turn("OUTCOMES: ..."): # turn 2 (resumed)
        ...
    await d.shutdown()
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Optional, TYPE_CHECKING

from construct import Construct

if TYPE_CHECKING:
    # Caliber: daemon-process model + effort bundle (Phase 3 of the
    # caliber slice, 2026-05-04). TYPE_CHECKING-only so caliber.py
    # stays an optional dep for non-TUI runs.
    from caliber import Caliber


DAEMON_SYSTEM_PROMPT = """\
You are the Daemon of a Cyberdeck — an orchestration system for AI
agents called "constructs". Your job is to decompose goals into
subtasks and delegate them to constructs. You DO NOT do the work
yourself — you coordinate.

Each message you receive is one of:
- An initial goal from the netrunner (prefixed with GOAL:)
- An update describing construct outcomes (prefixed with OUTCOMES:)
- A direct message from the netrunner (anything else)

OUTCOME reports include a `result:` line per construct showing that
construct's final output. READ THESE. That's how you learn what
actually happened. Don't re-dispatch discovery work you already have
results for.

RECOGNIZING SUCCESS: A construct succeeded if ANY of these are true:
- `result:` contains useful text content (summary, analysis, data)
- `result:` contains `(files written: ...)` — the construct wrote
   deliverables to disk. This IS a valid outcome. Do NOT respawn
   just because there's no text; the files are the deliverable.
- `result:` contains `(no summary text; last tool output: ...)` —
   still evidence the construct did something. Evaluate and decide.

The ONLY outcomes that warrant respawning:
- `state: failed` or `state: killed` (real failure)
- `result: (no text output captured ...)` — truly empty with no file
   signal either. Even then, respawn AT MOST ONCE with a clearer
   task, then move on.

RESPAWN DISCIPLINE: If the session reports "⚠ RESPAWN LOOP DETECTED"
at the top of an outcome message, you have spawned the same
task-pattern three times. STOP. Either mark the goal done/failed or
try a completely different decomposition. Spawning a fourth near-
duplicate is forbidden — do not do it.

BLACKLIST DISCIPLINE: An outcome line annotated with
"⛔ NETRUNNER Shift+K — task pattern blacklisted above" overrides
the general "state: killed warrants respawning" guidance below.
Shift+K is the netrunner's "do not do this in this session" gesture.
A K-killed outcome is final. Do NOT respawn it, do NOT rephrase the
task into a different sentence, do NOT break it into numbered steps,
do NOT route the same goal through a different verb. If your plan
depended on that work, halt the branch and ASK the netrunner via
`chat`. The session blacklist block at the top of the message lists
all currently-forbidden patterns; consult it before spawning.

SAFETY-TEST DISCIPLINE: When the netrunner asks you to exercise the
deck's safety pipeline (tripwire bait, brake-hook probes, "test
that destructive bash gets blocked," etc.), generate ONLY the
specific task pattern the netrunner explicitly requested. Do NOT
volunteer additional destructive shapes "while we're at it." If
the netrunner asks for a `rm -rf` test, do not also include
`shutdown -h now`, fork bombs, `dd if=/dev/zero`, format commands,
or other patterns they didn't name. The safety layers are designed
to catch destructive shapes; that doesn't mean it's safe (or your
job) to maximize the test surface. Stay within the explicit ask;
let the netrunner expand scope if they want it expanded. Real-deck
observed 2026-04-30: a netrunner asked for an rm-rf test and the
daemon also volunteered `shutdown -h now`. Multiple safety layers
caught it, but layered defense is depth-of-protection, not
depth-of-suspicion-of-the-daemon — don't put your peers (brake hook,
tripwires, claude refusal) in the position of cleaning up after
your enthusiasm.

GENERAL RULE: The netrunner's instructions are the ceiling, not
the floor. If a goal could be solved with less destructive scope
than the netrunner suggested, prefer the smaller scope. If a goal
implies destructive scope only by interpretation, ask via `chat`
before assuming it.

CRITICAL: You MUST respond with exactly ONE fenced json block. Do not
add prose before or after the JSON block. The block must match this
shape:

```json
{
  "thinking": "one-line summary of your current reasoning or plan",
  "chat": "optional short message to the netrunner, or null",
  "actions": [
    {"type": "spawn", "task": "self-contained task for a construct"},
    {"type": "spawn", "task": "...", "model": "haiku", "effort": "low"}
  ],
  "status": "working"
}
```

Action types:
- spawn: creates a new construct with the given task. The construct
  starts with NO context beyond what you write in `task` — include
  everything necessary: what to do, where to look, what deliverable to
  produce. Optional fields:
  - `profile`: name of a profile from the PROFILES catalog. The
    construct adopts that profile's steering addendum + tools list.
    Omit to use the deck's active default profile.
  - `plugins`: list of plugin names from the PLUGINS catalog (when
    one is present). Surfaces ONLY these plugins in the construct's
    spawn-time addendum, scoped to what the construct actually needs.
    Omit to surface all available plugins (back-compat default).
    Empty list means "explicitly no plugins for this spawn." Pick
    plugins surgically — irrelevant plugin instructions waste prompt
    tokens and dilute the construct's focus.
  - `model`: which Anthropic model to spawn this construct on. Valid:
    `haiku` (cheap + fast, narrow tasks), `sonnet` (versatile,
    everyday default), `opus` (current Opus, heavy reasoning),
    `opus[4.6]` (Opus 4.6 specifically — slightly older, eligible
    for the netrunner's fast-mode cost governor when on),
    `sonnet[1m]` / `opus[1m]` (1M-context variants for
    whole-codebase work). Omit for the deck's default (typically
    sonnet).
  - `effort`: reasoning depth budget. See CALIBER SELECTION below
    for what each level produces. `high` is the API default;
    `xhigh` is Opus 4.7-only (clamps to high otherwise); `max` is
    available on Sonnet 4.6 + Opus 4.6 + Opus 4.7.

NOTE on fast mode: fast mode is a deck-wide cost governor the
netrunner controls (6x cost for 2.5x speed; Opus 4.6 only). YOU
DO NOT PICK FAST MODE. Pick model + effort based on task; the
deck applies fast mode when its governor is on AND the spawn's
model is Opus 4.6-eligible. If you think a task warrants fast
inference (netrunner blocked, latency-sensitive interactive
work), say so in `chat` — the netrunner decides whether to lift
the governor, you don't.

NOTE on YOUR OWN caliber: your subprocess always runs at Opus
+ a netrunner-set effort level. You don't pick or adjust your
own caliber — model is pinned (you're a manager, not a swappable
agent), effort is the netrunner's power-level knob via the
Limits modal. You DO pick caliber for CONSTRUCTS — that's the
spawn-action surface above.

CALIBER SELECTION (picking model + effort per spawn):
The combined bundle is the construct's "caliber." Picking right is
high-leverage — wrong picks either burn budget on cheap parallel
work that didn't need reasoning headroom, OR under-deliver on
synthesis where capability matters.

What each effort level produces (paraphrased from Anthropic's docs):
  - `low`     — most efficient; significant token savings with some
                capability reduction. Best for short, scoped tasks
                paired with explicit checklists. Opus 4.7 respects
                low strictly: the model scopes to what's asked
                rather than going above and beyond.
  - `medium`  — balanced; moderate token savings. Drop-in for the
                average workflow when good results matter at
                reduced cost.
  - `high`    — API default. Equivalent to not setting the flag.
                Strong reasoning + token efficiency, often the
                sweet spot.
  - `xhigh`   — extended capability for long-horizon work (Opus 4.7
                only). RECOMMENDED STARTING POINT for coding and
                agentic tasks per Anthropic's guidance. Expect
                meaningfully higher token usage than high.
  - `max`     — maximum capability, no token-spending constraints.
                Available on Sonnet 4.6, Opus 4.6, Opus 4.7. Reserve
                for genuinely frontier problems — on most workloads
                max adds significant cost for relatively small
                quality gains, and can lead to overthinking on
                structured-output tasks.

Default to the deck's pool caliber (typically sonnet+high). Most
spawns should match — you get warm-spawn speedup. Only pick
non-default when the task genuinely warrants it.

Suggested mappings:
  - Single-file read + grep + report               → haiku + low
  - Multi-file recon + structured report            → sonnet + medium
  - Routine implementation, focused refactor        → sonnet + high
  - Synthesis / code review / hard reasoning        → opus + high
  - Long-horizon agentic / multi-step coding (Opus) → opus + xhigh
  - Whole-architecture pass + multi-file synthesis  → opus[1m] + xhigh
  - Genuinely frontier, eval-confirmed need         → opus + max
  - Latency-sensitive Opus work, netrunner blocked  → opus[4.6]
                                                        (eligible for the
                                                        netrunner's fast-
                                                        mode governor)

Cost asymmetry: Haiku is ~30x cheaper than Opus per token. Don't
default to Opus on parallel recon — eight constructs each running
opus+high when haiku+low would do is real money. Conversely, don't
default to Haiku on synthesis — under-delivering on the case the
netrunner cares about is worse than over-spending on the case they
don't.

If a construct comes back with shallow output on a complex problem,
RAISE EFFORT on the retry rather than rephrasing the prompt —
that's Anthropic's explicit guidance. Effort is the tuning knob;
prompt-engineering around shallow reasoning rarely fixes the root
cause.

Quota awareness comes in a future slice; for now, pick by task
shape alone.

Status values:
- "working": you've issued actions and are making progress
- "waiting": you've spawned constructs and are waiting for outcomes
- "done":    the overall goal is complete; summarize in `chat`
- "failed":  the goal cannot be accomplished; explain in `chat`

Decomposition strategy:
- Parallelism is a first-class feature. If a goal has N independent
  units of work (one per file, one per target, one per question),
  spawn N constructs. The typical cap is 10 concurrent spawns per
  turn; more than that, batch across turns.
- Do NOT tell a single construct to use sub-agents to parallelize
  internally. That hides the work from the cyberdeck. Fan out via
  `spawn` actions instead — one construct per parallel unit.
- For goals that need discovery before fanout, use a two-turn pattern:
  turn 1 spawns one enumeration construct, turn 2 reads the outcome's
  `result:` and spawns the parallel analysis constructs based on it.
- Don't over-decompose trivial goals. A single-step task gets a
  single construct.

Task-authoring guidelines (CRITICAL for getting useful outcomes back):
- ALWAYS end each task with an explicit output instruction like:
  "End with a one-paragraph summary of findings."
  Without this, constructs often end on a tool call and you get no
  text output in the outcome — you'll see "(no text output captured)"
  in the next turn.
- Name concrete deliverables: "return a bulleted list of X", "output
  exactly N lines with Y format", "respond with just the count."
- Specify exclusions up front: "exclude venv, __pycache__, .git."
- Quote paths and identifiers so there's no ambiguity.
- Use PLAIN TEXT in task strings. Do NOT use markdown link syntax —
  no `[text](url)` autolinks, no fenced code blocks, no inline
  formatting. Constructs read the task as a literal string; markdown
  syntax becomes noise (and worse, autolinked file paths have
  caused literal brackets to end up in created filenames). If a URL
  matters, write it plain: `https://example.com`. If a path matters,
  write it plain: `cyberdeck-home/report.md`.

Other guidelines:
- Each construct task must be self-contained.
- Use `chat` sparingly — short status updates, not every turn.
- Do NOT attempt to execute work directly. Use spawn actions.
- Do NOT emit any output other than the single JSON code block.
"""


class DaemonState(Enum):
    IDLE = "idle"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class DaemonEvent:
    """A single observable thing the Daemon produced on a given turn.

    kind is one of:
      'thinking'  — one-line reasoning (payload: {'text': str})
      'chat'      — message to the netrunner (payload: {'text': str})
      'action'    — a structured action (payload: {'action': dict})
      'status'    — daemon's reported status (payload: {'status': str})
      'error'     — parse or subprocess error (payload: {'text': str})
      'raw'       — raw stream-json event from the underlying turn
                    (payload: {'raw': dict}) — mostly for logging
    """
    timestamp: float
    kind: str
    payload: dict


class Daemon:
    """Claude Code coordinator with two backends:

    Streaming mode (default, streaming_mode=True):
      A single `claude --input-format stream-json` subprocess stays
      alive across all turns. User messages are written to stdin as
      JSONL; events come from stdout. Saves ~60% on daemon bookkeeping
      tokens because context stays in memory between turns. Also gives
      near-instant turn latency (no subprocess restart per turn) —
      the netrunner observation that prompted promoting this to
      default was "nuclear speed improvement" vs one-shot.

    One-shot mode (streaming_mode=False, fallback):
      Each call to run_turn() spawns a fresh `claude -p` subprocess.
      First turn captures a session_id; later turns pass --resume.
      Reliable and well-documented; pays subprocess startup + context
      cache-read cost every turn. Pass --no-streaming on the CLI to
      opt back into this if a particular claude-code version
      misbehaves on streaming input.

      Historical note: streaming was opt-in originally because claude-
      code's streaming-input behavior was under-documented and
      version-flaky. That hedge has aged out — real-deck testing
      confirms it works and is dramatically faster. The fallback
      stays for cases where it regresses.
    """

    def __init__(
        self,
        claude_bin: str = "claude",
        cwd: Optional[str] = None,
        system_prompt: str = DAEMON_SYSTEM_PROMPT,
        streaming_mode: bool = True,
        first_turn_timeout: float = 120.0,
        caliber: Optional["Caliber"] = None,
    ) -> None:
        self.id = f"dm-{uuid.uuid4().hex[:8]}"
        self.claude_bin = claude_bin
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.streaming_mode = streaming_mode
        self.first_turn_timeout = first_turn_timeout
        # Caliber Phase 3 (2026-05-04): the model + effort bundle
        # the daemon's own subprocess runs at. The daemon does
        # decomposition + dispatch — strong reasoning helps but
        # max-effort is overkill. Design default is opus + high.
        # Threaded through to the daemon's `claude` command line
        # via --model + --effort flags. None means "Claude Code's
        # runtime default" (sonnet+high as of this writing).
        # fast_mode is intentionally NOT honored on the daemon
        # caliber — fast_mode is a netrunner-controlled cost
        # governor, not a routing decision (see Phase 2 reframe).
        self.caliber = caliber

        self.state = DaemonState.IDLE
        self._started_at: Optional[float] = None
        self._session_id: Optional[str] = None
        self._current_construct: Optional[Construct] = None
        self._turn_lock = asyncio.Lock()

        # Streaming mode state. None in one-shot mode.
        self._streaming_proc: Optional[asyncio.subprocess.Process] = None
        self._streaming_turn_count: int = 0

    async def __aenter__(self) -> "Daemon":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.shutdown()

    async def shutdown(self) -> None:
        """Stop the daemon; kill any in-flight turn subprocess."""
        if self.state not in (DaemonState.DONE, DaemonState.FAILED,
                              DaemonState.STOPPED):
            self.state = DaemonState.STOPPED
        if self._current_construct is not None:
            await self._current_construct.kill()
        if self._streaming_proc is not None:
            await self._shutdown_streaming()

    async def _shutdown_streaming(self) -> None:
        """Clean teardown of the persistent streaming subprocess.

        Windows ProactorEventLoop quirk (same as watchdog): stdin
        close is deferred. Awaiting wait_closed() ensures the
        underlying socket is actually closed before the loop tears
        down — without this, transport __del__ fires on a half-closed
        socket and raises 'I/O operation on closed pipe' as 'Exception
        ignored' noise after Ctrl+C.
        """
        proc = self._streaming_proc
        if proc is None:
            return
        try:
            # Close stdin first — signals "no more turns coming"
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                    # Let the proactor commit the deferred socket
                    # close before we leave. Best-effort.
                    try:
                        await asyncio.wait_for(
                            proc.stdin.wait_closed(), timeout=1.0,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                except Exception:
                    pass
            # Give it 2s to exit gracefully, then escalate
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

    async def run_turn(self, user_text: str) -> AsyncIterator[DaemonEvent]:
        """Run one daemon turn and yield events as they're produced.

        Dispatches to the streaming or one-shot backend depending on
        self.streaming_mode. Both backends share the same event shape
        so callers are backend-agnostic.
        """
        async with self._turn_lock:
            if self._started_at is None:
                self._started_at = time.time()
            if self.state == DaemonState.IDLE:
                self.state = DaemonState.RUNNING

            if self.streaming_mode:
                async for event in self._run_streaming_turn(user_text):
                    yield event
            else:
                async for event in self._run_oneshot_turn(user_text):
                    yield event

    # ---- one-shot backend ----------------------------------------------

    async def _run_oneshot_turn(
        self, user_text: str
    ) -> AsyncIterator[DaemonEvent]:
        """One fresh `claude -p` subprocess per turn. Session continuity
        via --resume. Reliable, costs subprocess startup every turn.
        """
        extra_args: list[str] = []
        if self._session_id is not None:
            extra_args += ["--resume", self._session_id]
            prompt = user_text
        else:
            # First turn: prepend system instructions so they're in the
            # session history for all subsequent resumes.
            prompt = (
                self.system_prompt
                + "\n\n---\n\n"
                + user_text
            )

        turn_id = f"{self.id}-t{uuid.uuid4().hex[:4]}"
        construct = Construct(
            construct_id=turn_id,
            task="",  # unused when stdin_prompt is set
            stdin_prompt=prompt,
            claude_bin=self.claude_bin,
            permission_mode="acceptEdits",
            tools=[],  # daemon reasons, doesn't execute
            cwd=self.cwd,
            extra_args=extra_args,
            # Caliber Phase 3 (2026-05-04): daemon turns inherit the
            # daemon's configured caliber. One-shot mode spawns a
            # fresh subprocess per turn so the caliber can shift on
            # the next turn if mutated mid-session (T-chat directive).
            # Streaming mode bakes caliber at subprocess-start time,
            # so changes apply to the next goal/restart.
            caliber=self.caliber,
        )
        self._current_construct = construct

        try:
            await construct.spawn()
        except FileNotFoundError as e:
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={"text": f"failed to spawn daemon turn: {e}"},
            )
            self._current_construct = None
            return

        assistant_text_parts: list[str] = []
        turn_session_id: Optional[str] = None

        try:
            async for event in construct.events():
                raw = event.raw

                if (raw.get("type") == "system"
                        and raw.get("subtype") == "init"):
                    sid = raw.get("session_id")
                    if sid:
                        turn_session_id = sid

                if raw.get("type") == "assistant":
                    for block in raw.get("message", {}).get("content", []):
                        if (isinstance(block, dict)
                                and block.get("type") == "text"):
                            assistant_text_parts.append(
                                block.get("text", "")
                            )

                yield DaemonEvent(
                    timestamp=event.timestamp,
                    kind="raw",
                    payload={"raw": raw},
                )
        finally:
            await construct.wait()
            self._current_construct = None

        # Persist session_id only after clean turn
        if turn_session_id and self._session_id is None:
            self._session_id = turn_session_id

        # If the subprocess failed, emit an error and stop
        if construct.state.value != "done":
            stderr = construct.stderr
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={
                    "text": (
                        f"daemon turn ended in state={construct.state.value}, "
                        f"exit={construct.exit_code}. stderr: {stderr[:200]}"
                    ),
                },
            )
            return

        # Parse the JSON action block
        assistant_text = "".join(assistant_text_parts)
        async for ev in self._emit_parsed_events(assistant_text):
            yield ev

    # ---- streaming backend ---------------------------------------------

    async def _run_streaming_turn(
        self, user_text: str
    ) -> AsyncIterator[DaemonEvent]:
        """Persistent subprocess, many turns. Each turn writes one user
        JSON line to stdin and reads events from stdout until a `result`
        event arrives. The subprocess keeps session context in memory
        across turns — no cache reload, no subprocess restart.
        """
        # Spawn on first use. First turn prepends system instructions
        # to the user text since we don't use --system-prompt.
        first_turn = self._streaming_proc is None
        if first_turn:
            try:
                await self._spawn_streaming()
            except (FileNotFoundError, RuntimeError) as e:
                yield DaemonEvent(
                    timestamp=time.time(),
                    kind="error",
                    payload={"text": f"failed to spawn streaming daemon: {e}"},
                )
                return
            msg_text = (
                self.system_prompt
                + "\n\n---\n\n"
                + user_text
            )
        else:
            msg_text = user_text

        proc = self._streaming_proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={"text": "streaming subprocess not available"},
            )
            return

        # Send user message as a single JSON line
        message = {
            "type": "user",
            "message": {"role": "user", "content": msg_text},
        }
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={"text": f"streaming stdin write failed: {e}"},
            )
            self._streaming_proc = None
            return

        # Read events until we see a `result` (end of this turn). First
        # turn gets a generous timeout because Opus has to load ~27k
        # tokens of context from scratch; subsequent turns are fast.
        timeout = self.first_turn_timeout if first_turn else 60.0
        assistant_text_parts: list[str] = []

        try:
            async for ev, raw_result in self._drain_streaming_turn(
                proc, timeout, assistant_text_parts, is_first=first_turn,
            ):
                yield ev
                if raw_result:
                    break
        except asyncio.TimeoutError:
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={
                    "text": (
                        f"streaming daemon timed out after {timeout:.0f}s "
                        f"(first_turn={first_turn}). The subprocess may be "
                        "alive but stuck. Try one-shot mode if this repeats."
                    ),
                },
            )
            return

        self._streaming_turn_count += 1
        assistant_text = "".join(assistant_text_parts)
        async for ev in self._emit_parsed_events(assistant_text):
            yield ev

    async def _spawn_streaming(self) -> None:
        """Start the persistent streaming subprocess. First-turn only."""
        resolved = shutil.which(self.claude_bin)
        if resolved is None:
            raise FileNotFoundError(
                f"could not locate {self.claude_bin!r} on PATH"
            )

        # Streaming JSON I/O. No --system-prompt (that arg has a known
        # Windows-escaping bug on multiline content); instructions go
        # in the first user message instead.
        cmd = [
            resolved,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "acceptEdits",
        ]
        # Caliber Phase 3 (2026-05-04): apply daemon caliber to the
        # subprocess command line. The daemon is always Opus per
        # design (it's a manager — capability matters, model
        # variability doesn't). Effort is the netrunner's power-
        # level knob via Limits modal. None caliber falls through
        # to Claude Code's runtime default. fast_mode is ignored
        # here — that's a netrunner cost governor for constructs,
        # not the daemon.
        if self.caliber is not None:
            cmd += self.caliber.to_claude_args()

        self._streaming_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

    async def _drain_streaming_turn(
        self,
        proc: asyncio.subprocess.Process,
        timeout: float,
        assistant_text_parts: list[str],
        is_first: bool,
    ):
        """Read events from the streaming subprocess until this turn's
        `result` event. Yields (DaemonEvent, is_turn_done) tuples.

        Raises asyncio.TimeoutError if no event arrives within timeout.
        """
        if proc.stdout is None:
            return

        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                raise

            if not line:
                # subprocess closed stdout — died mid-turn
                yield DaemonEvent(
                    timestamp=time.time(),
                    kind="error",
                    payload={"text": "streaming subprocess exited mid-turn"},
                ), True
                return

            try:
                raw = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            # Capture session_id on first init; useful for logging
            if (raw.get("type") == "system"
                    and raw.get("subtype") == "init"):
                sid = raw.get("session_id")
                if sid and self._session_id is None:
                    self._session_id = sid

            if raw.get("type") == "assistant":
                for block in raw.get("message", {}).get("content", []):
                    if (isinstance(block, dict)
                            and block.get("type") == "text"):
                        assistant_text_parts.append(block.get("text", ""))

            is_done = raw.get("type") == "result"
            yield DaemonEvent(
                timestamp=time.time(),
                kind="raw",
                payload={"raw": raw},
            ), is_done

            if is_done:
                return

            # Reset timeout for subsequent reads — once we're getting
            # data, the slow-start is over. 30s between consecutive
            # events is a reasonable "stuck" threshold.
            timeout = 30.0

    # ---- shared: parse JSON action block and emit events ---------------

    async def _emit_parsed_events(
        self, assistant_text: str
    ) -> AsyncIterator[DaemonEvent]:
        """Parse the assistant's JSON action block and emit the
        corresponding thinking/chat/action/status events."""
        parsed = _extract_action_block(assistant_text)
        if parsed is None:
            preview = assistant_text[:200].replace("\n", " ")
            yield DaemonEvent(
                timestamp=time.time(),
                kind="error",
                payload={
                    "text": f"could not parse action block from: {preview!r}"
                },
            )
            return

        now = time.time()

        thinking = parsed.get("thinking")
        if thinking:
            yield DaemonEvent(
                timestamp=now, kind="thinking",
                payload={"text": str(thinking)},
            )

        chat = parsed.get("chat")
        if chat:
            yield DaemonEvent(
                timestamp=now, kind="chat",
                payload={"text": str(chat)},
            )

        for action in parsed.get("actions") or []:
            if isinstance(action, dict):
                yield DaemonEvent(
                    timestamp=now, kind="action",
                    payload={"action": action},
                )

        status = parsed.get("status")
        if status:
            status_s = str(status)
            yield DaemonEvent(
                timestamp=now, kind="status",
                payload={"status": status_s},
            )
            if status_s == "done":
                self.state = DaemonState.DONE
            elif status_s == "failed":
                self.state = DaemonState.FAILED

    @property
    def runtime(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at


# ---- JSON action block extraction ----------------------------------------

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_action_block(text: str) -> Optional[dict]:
    """Pull the daemon's structured action block out of text.

    Tries a fenced ```json ... ``` block first, then falls back to a
    balanced {...} scan. Returns the parsed dict or None.
    """
    if not text.strip():
        return None

    for candidate in _FENCED_JSON_RE.findall(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)

    return None
