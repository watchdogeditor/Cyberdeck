"""
Construct: a managed Claude Code subprocess that streams events.

This is the atomic unit of the cyberdeck. One construct = one task,
one context, one isolated OS process. The daemon (later) spawns these;
the watchdog (later) reads their event streams; the user injects into
them via session-resume (later).

Milestone Zero scope: prove the subprocess + event-stream plumbing works.
Everything downstream of that (daemon, watchdog, TUI, wiring, routing)
builds on this foundation.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Profile is referenced by type annotation only. Importing eagerly
    # would create a cycle (profiles.py is intentionally upstream of
    # most of the deck) and isn't needed for runtime — Construct just
    # reads .name, .recommended_tools, and .default_construct_addendum
    # off whatever Profile-shaped object it's handed.
    from profiles import Profile


class ConstructState(Enum):
    STARTING = "starting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    KILLED = "killed"


# Baseline toolset every construct gets unless explicitly narrowed by
# a profile or the spawn caller. Single source of truth — referenced
# both by Construct.__init__ as the default and by the TUI's Tools
# tab to display what's actually in effect. Don't duplicate this list
# elsewhere; import the constant.
#
# Inclusion rationale:
#   Bash       — run arbitrary commands (build, test, etc.)
#   Read       — read existing files
#   Write      — create NEW files (was missing in earlier versions;
#                constructs couldn't produce file deliverables, only
#                chat text)
#   Edit       — modify existing files
#   Glob       — find files by pattern (cross-platform; Bash `find`
#                behaves differently on Windows)
#   Grep       — search file contents (same rationale as Glob)
#   WebSearch  — search the web for relevant pages
#   WebFetch   — pull the contents of a known URL. Distinct from
#                WebSearch; constructs frequently need both (search to
#                find candidates, fetch to read them). Missing this
#                caused silent failures because the tool was denied at
#                --allowedTools rather than failing loudly.
#   TodoWrite  — agent's internal task list. Useful for multi-step
#                work; harmless if unused.
DEFAULT_TOOLS: tuple[str, ...] = (
    "Bash", "Read", "Write", "Edit", "Glob", "Grep",
    "WebSearch", "WebFetch", "TodoWrite",
)


@dataclass
class Event:
    """A single parsed event from a construct's stream.

    Deliberately loose: Claude Code's stream-json schema is documented but
    may evolve, and we want this shell to survive schema drift. The raw
    dict is preserved so downstream code (watchdog, tripwires, logs) can
    reach into whatever details it needs.
    """
    construct_id: str
    timestamp: float
    kind: str  # classified high-level bucket; see classify_event()
    raw: dict


class EventKind:
    """Constants for `classify_event` return values.

    Class-as-namespace pattern (Pythonic; see tkinter / Qt). Values are
    plain strings so the open-ended return path (raw type pass-through
    for shapes the deck doesn't have a special case for) keeps working
    — classify_event still returns bare strings, just sourced from
    these constants for the recognized cases.

    Downstream switch-style consumers (display formatters, watchdog,
    future tripwire DSL) should compare against these constants rather
    than literal strings — a typo on a literal is a silent dead branch;
    a typo on EventKind.FOO_BAR is an AttributeError at import.
    """
    SYSTEM_INIT = "system_init"
    SYSTEM_RESULT = "system_result"
    SYSTEM = "system"
    RESULT = "result"
    RATE_LIMIT = "rate_limit"
    TOOL_USE = "tool_use"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    USER = "user"
    ASSISTANT = "assistant"
    OTHER = "other"


def classify_event(raw: dict) -> str:
    """Bucket a raw stream-json event into a high-level kind.

    We accept whatever Claude Code emits and map it to a short label the
    rest of the system can switch on. Recognized shapes return an
    `EventKind` constant; unknown shapes pass through the raw type
    string (or `EventKind.OTHER` if the type field is missing entirely)
    rather than crashing.
    """
    t = raw.get("type", "unknown")

    if t == "system":
        subtype = raw.get("subtype", "")
        if subtype == "init":
            return EventKind.SYSTEM_INIT
        if subtype == "result":
            return EventKind.SYSTEM_RESULT
        return EventKind.SYSTEM

    # Top-level result event (distinct from system/result subtype)
    if t == "result":
        return EventKind.RESULT

    # Rate limit / quota events — worth routing explicitly since the
    # watchdog will care about these separately from normal flow.
    if t == "rate_limit_event":
        return EventKind.RATE_LIMIT

    if t in ("user", "assistant"):
        content = raw.get("message", {}).get("content", [])
        if isinstance(content, list):
            # A single message can contain multiple content blocks
            # (thinking + text, text + tool_use, etc). We classify by
            # the most "significant" block present, in priority order.
            types = [b.get("type") for b in content if isinstance(b, dict)]
            if "tool_use" in types:
                return EventKind.TOOL_USE
            if "tool_result" in types:
                return EventKind.TOOL_RESULT
            if "thinking" in types:
                return EventKind.THINKING
        return t  # plain user/assistant text — same string as EventKind.USER/ASSISTANT

    return t or EventKind.OTHER


class Construct:
    """One Claude Code subprocess, one task, one event stream."""

    def __init__(
        self,
        task: str,
        tools: Optional[list[str]] = None,
        permission_mode: str = "default",
        cwd: Optional[str] = None,
        claude_bin: str = "claude",
        construct_id: Optional[str] = None,
        extra_args: Optional[list[str]] = None,
        stdin_prompt: Optional[str] = None,
        resume_session_id: Optional[str] = None,
        profile: Optional["Profile"] = None,
        deck_addendum: Optional[str] = None,
        settings_path: Optional[str] = None,
    ):
        self.id = construct_id or f"cx-{uuid.uuid4().hex[:8]}"
        self.task = task
        # Profile metadata. Stored so listeners (TUI panes, logs,
        # finalize meta events) can show which profile was in effect.
        # The profile's effect on this construct is purely soft: its
        # system-prompt addendum is appended at command-build time,
        # and recommended_tools (if any) get surfaced in the addendum
        # as a "for this kind of work, prefer X / Y / Z" hint. The
        # profile does NOT narrow the tool set — runtime tool gating
        # lives entirely in the deck-global brake (see brake_state.py
        # and brake_hook.py).
        self.profile = profile
        self.profile_name: Optional[str] = profile.name if profile else None
        # Deck-wide system-prompt addendum. Independent of profile;
        # describes deck-control utilities (the cyberdeck dispatcher
        # script in <home>/tools/deck/cyberdeck.py) that all
        # constructs can invoke regardless of profile. Joined with
        # the profile addendum (if any) at command-build time.
        self.deck_addendum = deck_addendum
        # Tool resolution priority:
        #   explicit `tools` kwarg     (caller knows best)
        #   > DEFAULT_TOOLS            (deck baseline)
        # Profiles do NOT narrow tools — they recommend, the brake
        # enforces. This used to be a three-tier resolution; the
        # middle tier (profile.allowed_tools) was dropped when brake
        # state moved out of profiles into the deck-global layer.
        if tools is not None:
            self.tools = list(tools)
        else:
            self.tools = list(DEFAULT_TOOLS)
        self.permission_mode = permission_mode
        self.cwd = cwd
        self.claude_bin = claude_bin
        self.extra_args = extra_args or []
        # When set, prompt is piped through stdin rather than passed as
        # -p's argument. Essential for long or multiline prompts that
        # break Windows command-line arg parsing (the daemon's system
        # prompt was hitting this). If both stdin_prompt and task are
        # set, stdin_prompt wins and task is ignored.
        self.stdin_prompt = stdin_prompt
        # Auto-route multiline tasks through stdin. Windows command-line
        # parsing mangles argv values containing literal newlines and
        # bracketed segments — claude receives a corrupted -p value and
        # silently treats it as a no-op (exits 0, emits zero stream-json
        # events). This bit the inject-followup path: the framed task
        # ("[Netrunner halted...]\n\nWait, about a bear!") arrived as
        # mush at the subprocess. Daemon turns dodge this by setting
        # stdin_prompt explicitly; constructs spawned by the TUI
        # historically didn't, so multiline tasks vanished. Promoting
        # any newline-containing task to stdin_prompt makes the route
        # automatic without callers having to remember.
        if self.stdin_prompt is None and task and "\n" in task:
            self.stdin_prompt = task
        # Server-side session_id to resume. When set, --resume <id> is
        # passed to claude, and the new task rides on top of an existing
        # session (warm or otherwise). Set by SessionPool consumers to
        # reuse pre-warmed sessions; None means fresh spawn.
        self.resume_session_id = resume_session_id
        # Path to a transient claude --settings JSON file. When set,
        # passed via `--settings <path>` at spawn so claude installs
        # the brake hook for this construct. Generated by Fleet at
        # spawn time from the current deck-global brake state via
        # brake_state.make_spawn_settings(). None means no hook (YOLO
        # brake or no brake plumbing wired up — both run unrestricted).
        # Stored on the instance so Fleet can clean it up after the
        # construct finalizes.
        self.settings_path = settings_path

        self.state = ConstructState.STARTING
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._started_at: Optional[float] = None
        self._exit_code: Optional[int] = None
        self._stderr_buf: bytes = b""
        self._finalized: bool = False
        # Intent flag for kill(): set immediately when a kill is
        # requested, even though `state` only flips to KILLED after the
        # process is confirmed dead. This split lets wait() know "don't
        # overwrite the eventual KILLED with DONE/FAILED based on exit
        # code" the moment kill() is called, while keeping the visible
        # state honest about whether the kill has actually completed.
        self._kill_requested: bool = False
        # Server-side session_id for this conversation. When resuming
        # a warm session, we know it up front; otherwise it's captured
        # from the `system_init` event when the subprocess starts up.
        # Used by SessionManager and SessionPool for --resume routing.
        self.session_id: Optional[str] = self.resume_session_id
        # Capture the construct's output in layers so we never report
        # back an empty summary if the construct actually did something:
        #   1. `_result_field`: Claude Code's result.result (cleanest)
        #   2. `_last_assistant_text`: most recent assistant text block
        #   3. `_last_tool_result`: most recent tool_result content
        # `final_output` (property) returns the first non-empty in order.
        # We keep all three so even if the construct ends on a tool call
        # with no text summary, the daemon still sees *something* real
        # instead of an empty outcome.
        self._result_field: str = ""
        self._last_assistant_text: str = ""
        self._last_tool_result: str = ""
        # File paths this construct created via the Write tool. Captured
        # from `Write` tool_use events as they stream. Useful so users
        # can tell at a glance what a construct produced — otherwise
        # file creation is invisible in the TUI until someone opens
        # the folder in Explorer.
        self._files_written: list[str] = []

    # ---- lifecycle ------------------------------------------------------

    def _build_command(self, claude_bin: str) -> list[str]:
        # claude_bin is the resolved path the caller already located via
        # shutil.which (or equivalent). Required arg, no fallback to
        # self.claude_bin: the resolved path is the only one safe to
        # hand to create_subprocess_exec on Windows, and threading it
        # through the API removes the temptation to ever skip resolving.
        cmd = [claude_bin]
        # When piping prompt via stdin, omit the prompt arg so claude
        # reads from stdin. Otherwise, pass the task as -p's argument.
        if self.stdin_prompt is not None:
            cmd += ["-p"]
        else:
            cmd += ["-p", self.task]
        cmd += [
            "--output-format", "stream-json",
            "--verbose",  # stream-json often requires verbose per Claude Code docs
        ]
        if self.tools:
            cmd += ["--allowedTools", ",".join(self.tools)]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        # Profile-driven system prompt addendum, plus the deck-wide
        # addendum that describes deck-control utilities. Joined with
        # a paragraph break, then collapsed to single spaces for
        # Windows argv-safety. Empty addenda are skipped — only emit
        # --append-system-prompt if there's actual content to append.
        #
        # Newlines in either addendum get collapsed to spaces before
        # passing as argv. Same Windows argv-mangling story that bit
        # us with multiline -p values: cmd.exe / CreateProcess garbles
        # multiline argv. Authors should write addendums as a single
        # paragraph or accept that paragraph breaks become spaces;
        # the meaning survives, the formatting doesn't.
        addenda: list[str] = []
        if self.profile is not None:
            # Profile's free-text steering. Empty string means "no
            # additional steering for this profile" — skip cleanly.
            profile_addendum = self.profile.default_construct_addendum.strip()
            if profile_addendum:
                addenda.append(profile_addendum)
            # Recommended tools — surface as a soft signal. The model
            # gets told "for this kind of work, prefer X / Y / Z";
            # the construct still has access to all default tools, so
            # this is a steering nudge, not a cap. Skipped when the
            # profile didn't declare any (empty tuple = no opinion).
            if self.profile.recommended_tools:
                rec = ", ".join(self.profile.recommended_tools)
                addenda.append(
                    f"Recommended tools for this profile: {rec}. "
                    f"Prefer these unless the task clearly needs "
                    f"something else."
                )
        if self.deck_addendum:
            deck_addendum = self.deck_addendum.strip()
            if deck_addendum:
                addenda.append(deck_addendum)
        if addenda:
            joined = "\n\n".join(addenda)
            addendum_arg = " ".join(joined.split())
            cmd += ["--append-system-prompt", addendum_arg]
        if self.resume_session_id is not None:
            # Resume an existing session. Saves subprocess startup time
            # (the system prompt is already cached server-side) and
            # primes any existing conversation state.
            cmd += ["--resume", self.resume_session_id]
        if self.settings_path is not None:
            # Brake-hook config file. Per real-deck verification on
            # claude 2.1.118, --settings <path> is the per-invocation
            # mechanism for installing PreToolUse hooks; the JSON at
            # this path points at brake_hook.py with the current
            # brake passed as argv. The hook decides allow/deny per
            # tool call. YOLO brake skips this entirely (settings_path
            # stays None and no --settings is passed).
            cmd += ["--settings", str(self.settings_path)]
        cmd += self.extra_args
        return cmd

    @property
    def pid(self) -> Optional[int]:
        """OS pid of the underlying claude subprocess, or None if it
        hasn't been spawned yet (or already collected). Surfaced so the
        Mechanic supervisor can track live subprocess pids without
        reaching past `_proc`'s underscore."""
        return self._proc.pid if self._proc is not None else None

    async def spawn(self) -> None:
        """Start the subprocess. Safe to call once."""
        if self._proc is not None:
            raise RuntimeError(f"construct {self.id} already spawned")

        # Resolve the binary explicitly so Windows PATHEXT (.cmd, .ps1,
        # .exe) and Unix PATH both get handled correctly. Passing the
        # bare name to create_subprocess_exec is flaky on Windows.
        resolved = shutil.which(self.claude_bin)
        if resolved is None:
            raise FileNotFoundError(
                f"could not locate {self.claude_bin!r} on PATH. "
                f"On Windows, try closing/reopening your terminal after "
                f"`npm install -g @anthropic-ai/claude-code`, or pass an "
                f"explicit path (e.g. the output of `npm config get prefix`)."
            )

        cmd = self._build_command(resolved)
        self._started_at = time.time()

        stdin_mode = (
            asyncio.subprocess.PIPE if self.stdin_prompt is not None else None
        )
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=stdin_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

        # Feed the stdin prompt now that the subprocess is up. Close
        # stdin afterward so claude sees EOF and starts processing.
        if self.stdin_prompt is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.write(self.stdin_prompt.encode("utf-8"))
                await self._proc.stdin.drain()
                self._proc.stdin.close()
            except Exception:
                # Subprocess could die between spawn and stdin write
                # on pathological input; we'll see the state in wait().
                pass

        self.state = ConstructState.RUNNING

    async def events(self) -> AsyncIterator[Event]:
        """Yield parsed events until stdout closes.

        This method only handles streaming. Callers must call wait() to
        get the terminal state, exit code, and stderr — either after the
        iterator completes, or in a try/finally if they may break early.
        """
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError(f"construct {self.id} not spawned")

        while True:
            line = await self._proc.stdout.readline()
            if not line:
                break  # EOF: process closed stdout
            try:
                raw = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                # Garbage line — log and skip. Real Claude Code shouldn't
                # do this but robustness is cheap here.
                continue

            # Opportunistically capture the final-result text as events
            # flow past. Three layers of fallback so we never show the
            # daemon an empty outcome when the construct actually did
            # work.
            rtype = raw.get("type")
            if rtype == "system" and raw.get("subtype") == "init":
                # Capture the server-side session_id on first init event.
                # Used by SessionManager to track every spawned session,
                # and eventually by the session pool to --resume warmly.
                sid = raw.get("session_id")
                if sid and self.session_id is None:
                    self.session_id = sid
            elif rtype == "result":
                # Layer 1: Claude Code's clean result.result field. This
                # is what the CLI itself considers the "final answer."
                result_text = raw.get("result")
                if isinstance(result_text, str) and result_text.strip():
                    self._result_field = result_text
            elif rtype == "assistant":
                # Layer 2: most recent assistant text block. Some tasks
                # interleave text + tool calls; last text wins as the
                # closest-to-summary output we have.
                for block in raw.get("message", {}).get("content", []):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text = block.get("text", "")
                        if text.strip():
                            self._last_assistant_text = text
                    elif btype == "tool_use" and block.get("name") == "Write":
                        # Track file creation for UI visibility.
                        fp = block.get("input", {}).get("file_path", "")
                        if fp and fp not in self._files_written:
                            self._files_written.append(fp)
            elif rtype == "user":
                # Layer 3: tool_results come back as user messages with
                # type="tool_result". Capture the most recent one so if
                # the construct ends on "Bash: grep ..." with no text
                # summary, the daemon still sees the output.
                for block in raw.get("message", {}).get("content", []):
                    if (isinstance(block, dict)
                            and block.get("type") == "tool_result"):
                        content = block.get("content", "")
                        # tool_result content can be a string or a list
                        # of content blocks (for multi-part results).
                        if isinstance(content, list):
                            parts = []
                            for b in content:
                                if (isinstance(b, dict)
                                        and b.get("type") == "text"):
                                    parts.append(b.get("text", ""))
                            content = "\n".join(parts)
                        if isinstance(content, str) and content.strip():
                            # Cap per-tool-result at 500 chars so a
                            # massive file dump doesn't dominate.
                            self._last_tool_result = content[:500]

            yield Event(
                construct_id=self.id,
                timestamp=time.time(),
                kind=classify_event(raw),
                raw=raw,
            )

    async def wait(self, timeout: Optional[float] = None) -> ConstructState:
        """Wait for the subprocess to exit and return the terminal state.

        Idempotent — safe to call multiple times, safe to call whether or
        not events() was drained. Waits for the process to exit, drains
        stderr for post-mortem, and transitions state to a terminal value
        (preserving KILLED if already set by kill()).

        If `timeout` is provided and the process doesn't exit within it,
        we escalate to force-kill and mark the state FAILED. This exists
        because on Windows, subprocesses occasionally orphan themselves
        (child processes spawned by Node.js/Claude Code staying alive
        even after the parent exits, holding stdout open). Without a
        timeout, Fleet shutdown would wedge in `_consume`'s finally
        block and produce a traceback pointing at this line on Ctrl-C.
        """
        if self._finalized:
            return self.state
        if self._proc is None:
            return self.state

        try:
            if timeout is not None:
                self._exit_code = await asyncio.wait_for(
                    self._proc.wait(), timeout=timeout
                )
            else:
                self._exit_code = await self._proc.wait()
        except asyncio.TimeoutError:
            # Process won't die on its own. Force-kill and move on.
            # We don't await wait() again here — kill() does its own
            # bounded wait with escalation, and if even that fails,
            # the subprocess is truly wedged and nothing we do matters.
            await self.kill(timeout=1.0)
            # kill() now sets state = KILLED only on confirmed death.
            # If kill() didn't make it that far (e.g. the wait inside
            # kill itself timed out and the process is truly wedged),
            # mark this as FAILED so we don't leave a stale STARTING
            # /RUNNING state on a finalized construct.
            if self.state != ConstructState.KILLED:
                self.state = ConstructState.FAILED
            self._finalized = True
            return self.state

        # Drain stderr. Narrow catch: only IO-level errors are expected
        # here. Anything else is a real bug we want surfaced.
        if self._proc.stderr is not None:
            try:
                self._stderr_buf = await self._proc.stderr.read()
            except (OSError, asyncio.IncompleteReadError):
                pass

        # Don't overwrite a kill-in-progress with DONE/FAILED. The
        # kill() may not have flipped state to KILLED yet (it does so
        # only after confirming the process is dead), but the intent
        # was set the moment kill() was called, so respect it.
        if not self._kill_requested:
            if self._exit_code == 0:
                self.state = ConstructState.DONE
            else:
                self.state = ConstructState.FAILED

        self._finalized = True
        return self.state

    async def kill(self, timeout: float = 2.0) -> None:
        """SIGTERM first; escalate to SIGKILL if it won't die.

        State transition order: `_kill_requested` flips immediately so
        wait() knows not to overwrite with DONE/FAILED. `state` flips
        to KILLED only after the process is confirmed dead (or after
        ProcessLookupError, which means it died on its own — same
        end-state). If even the SIGKILL escalation doesn't return,
        state stays at whatever it was; the wait() caller (or the
        finalize path) sees not-KILLED and can treat that as FAILED.
        """
        if self._proc is None:
            return
        self._kill_requested = True
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._proc.kill()
            try:
                await self._proc.wait()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass  # already gone
        self.state = ConstructState.KILLED

    # ---- introspection --------------------------------------------------

    @property
    def runtime(self) -> float:
        if self._started_at is None:
            return 0.0
        return time.time() - self._started_at

    @property
    def exit_code(self) -> Optional[int]:
        return self._exit_code

    @property
    def stderr(self) -> str:
        return self._stderr_buf.decode("utf-8", errors="replace")

    @property
    def final_output(self) -> str:
        """The construct's final output, combining text and file creation
        signals so callers always see SOMETHING tangible when the
        construct actually did work:

          - Text signal (in priority order):
              1. The `result` event's `result` field (Claude Code's own
                 "final answer" — cleanest when present)
              2. The last `assistant` message's text block
              3. The last `tool_result` content, marked as a fallback

          - File signal: if `files_written` is non-empty, a `(files
             written: ...)` line is appended regardless of text signal.
             This is critical for daemons — without it, a construct
             that writes a design doc but emits no text summary looks
             indistinguishable from a construct that did nothing at all,
             and the daemon will keep respawning trying to get output.

        Returns empty string only if the construct genuinely produced
        no text output AND wrote no files — which means something
        actually went wrong.
        """
        parts: list[str] = []

        # Text signal
        if self._result_field:
            parts.append(self._result_field)
        elif self._last_assistant_text:
            parts.append(self._last_assistant_text)
        elif self._last_tool_result:
            parts.append(
                "(no summary text; last tool output: "
                + self._last_tool_result
                + ")"
            )

        # File signal — always append if any files were written, even
        # when we already have text. Daemons need to know BOTH.
        if self._files_written:
            files_str = ", ".join(self._files_written)
            parts.append(f"(files written: {files_str})")

        return "\n".join(parts)

    @property
    def files_written(self) -> list[str]:
        """File paths this construct created via the Write tool, in the
        order they were created. Empty list if no files were written."""
        return list(self._files_written)
