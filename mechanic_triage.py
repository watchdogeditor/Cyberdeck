"""
Mechanic v1 LLM-session half — diagnose-only triage.

The Mechanic v0 supervisor (mechanic.py) reaps orphaned claude
subprocesses when the deck dies. v1 adds a one-shot LLM session that
spawns AFTER subprocess cleanup, reads the just-died deck's log file
+ relevant deck source, and produces a structured triage report
explaining what happened. Read-only — no autonomous fixes (that's
v2).

Per the maintbot design doc, three activation paths exist; v1
implements only one:

  ✅ unclean-exit triage   — deck died with no shutdown / eject
                             close_reason recorded → fire triage
  ⏸ stale-heartbeat triage — deck PID alive but TUI wedged.
                             Filed for v1.5 — needs careful design
                             around "the deck might still be writing
                             to its log while we read it" race.
  ⏸ deliberate summon      — netrunner UI button. Filed for v2 —
                             needs UI plumbing.

Substrate: same `claude -p` clean-spawn recipe as the Advisor
(Family A), with one difference — the mechanic NEEDS read-only
filesystem access (Read, Glob, Grep) to inspect the log file and
deck source. Other tools (Bash, Write, Edit, etc.) are explicitly
disallowed: triage v1 is read-only.

Key design decisions:

  - **Module owns its own claude-spawn**, separate from
    construct.py / advisor.py / watchdog.py. Different surface
    (read deck logs as primary input), different output shape
    (structured Markdown report), different lifecycle (one-shot
    per deck-death, not interactive). Sharing infra here would
    couple unrelated concerns.

  - **System prompt is Family-A-shaped**: full replace via
    `--system-prompt-file` (the multi-line argv truncation gotcha
    bites every spawn site that uses argv for prompts). Mechanic-
    specific role description, architecture vocabulary the
    triage needs, output format spec, "read-only, no autonomous
    fixes" constraint.

  - **Env-var belt suppresses CLAUDE.md auto-load**: the mechanic
    is a sibling process, not a deck role. We give it explicit
    architecture context in the system prompt rather than free-
    riding on the deck's project memory. Cheaper (cache-stable)
    and avoids leaking in-flight design notes into triage
    reports the netrunner might share.

  - **Output goes to disk + stderr summary**: triage report
    written next to the source log as
    `<deck>/logs/<basename>-triage.md`. Mechanic's stderr also
    gets a one-line summary so the netrunner sees something
    useful without opening the file. Both are written even if
    the LLM call failed (the failure itself is useful triage).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Caliber: sonnet + medium. Triage is reasoning-heavy (correlate
# log markers, identify cause from Python tracebacks, compare
# against gotchas in the system prompt) but not novel-synthesis
# territory. Same caliber as Advisor — proven adequate for
# instruction-following on narrowly-scoped tasks.
TRIAGE_MODEL = "sonnet"
TRIAGE_EFFORT = "medium"


# Per-question hard timeout. Triage is one-shot; the model reads
# the log, optionally walks deck source via Read/Glob/Grep, and
# writes a structured report. 180s gives room for the file walk
# without leaving stuck subprocesses behind on a wedged claude.
DEFAULT_TIMEOUT = 180.0


# Read-only tool subset. The mechanic can:
#   - Read the crash log (primary input)
#   - Glob/Grep the deck source for relevant context
#   - Read referenced files (e.g. the line where a traceback
#     points)
# It CANNOT Bash, Write, Edit, NotebookEdit, run web requests,
# spawn subagents, etc. v1 is diagnose-only by design.
TRIAGE_ALLOWED_TOOLS = "Read,Glob,Grep"


# Bytes cap for log content read into the user prompt. The deck's
# per-launch logs can be 1-5 MB on long sessions; passing the
# whole thing as user content would blow budget. Mechanic's job
# is to look at the END of the log (where the crash happened),
# so we tail-N the file. The model can use Read tool to fetch
# more context if it needs the head.
LOG_TAIL_BYTES = 200_000


@dataclass(frozen=True)
class TriageRequest:
    """One triage activation.

    `log_path` is the just-died deck's log file (typically
    `<deck>/logs/cyberdeck-<ts>.log`). The mechanic reads the tail
    of this file as primary input. `clean_close_reason` mirrors
    what the supervisor recorded — None / unset / non-shutdown
    means "unclean exit, triage worth running"; presence of
    "shutdown" or "eject" means the deck closed normally and
    triage is a no-op.

    `deck_pid`, `tracked_subprocesses`, `subprocesses_killed` carry
    forward what the supervisor already knows about the cleanup
    pass. The triage prompt mentions these so the report can
    reflect "we cleaned up N orphans" alongside whatever the log
    tells us.

    `deck_source_dir` is the directory the model can Read/Glob/Grep
    against (the deck's source tree, where Python code + canon
    docs live). Defaults to None — when unset, the model only has
    the log content.

    `report_dir` is where to write the triage report. Defaults to
    the log's parent directory (next to `latest.log`). The naming
    scheme is `<log-basename>-triage.md`.

    `wedge_kill_context` is set by the supervisor when v1.5
    stale-heartbeat triage fires — the deck PID was alive at
    detection time, the heartbeat had gone stale, and the
    supervisor force-killed the deck (either because
    --auto-triage-on-stale was set, or because the netrunner
    confirmed via the interactive prompt). The triage prompt
    surfaces this context so the report can reason about
    "deck wedged then killed" vs. "deck crashed naturally" —
    same final state on disk (no log_footer) but different
    causes worth distinguishing in the diagnostic.
    """
    log_path: Path
    clean_close_reason: Optional[str] = None
    deck_pid: Optional[int] = None
    tracked_subprocesses: int = 0
    subprocesses_killed: int = 0
    deck_source_dir: Optional[Path] = None
    report_dir: Optional[Path] = None
    claude_bin: str = "claude"
    timeout: float = DEFAULT_TIMEOUT
    wedge_kill_context: Optional[str] = None


@dataclass
class TriageResult:
    """Result of one triage call.

    `success` mirrors the subprocess exit code (0 → True, anything
    else → False). `report_text` is the model's output (or the
    error message on failure). `report_path` is the written file
    on disk (when we got far enough to write one). `summary_line`
    is a one-line stderr-friendly recap (~80 chars).

    The supervisor uses summary_line for its stderr output; the
    netrunner reads report_path for the full story.
    """
    success: bool
    report_text: str
    report_path: Optional[Path] = None
    summary_line: str = ""
    error: Optional[str] = None
    elapsed_s: float = 0.0


# System prompt for the mechanic v1 LLM session. Family A — full
# replace via --system-prompt-file. Carries the architecture
# vocabulary the triage needs (since we're killing CLAUDE.md
# auto-load via the env-var belt) and the output format spec.
MECHANIC_SYSTEM_PROMPT = """\
You are the Mechanic of a Cyberdeck. The deck just died — your job
is to read the crash log and produce a structured triage report
the netrunner can act on.

You have READ-ONLY filesystem access via the Read, Glob, and Grep
tools. You can read the log file directly, and you can walk the
deck source tree (Python modules, design docs) to look up
references the log mentions. You CANNOT execute commands, write
files, edit anything, spawn subagents, fetch web content, or take
any other action. v1 is diagnose-only — recommendations go to the
netrunner; the netrunner decides whether to act.

================================================================
DECK ARCHITECTURE (the vocabulary the log uses)
================================================================

The Cyberdeck is a Textual TUI that orchestrates Claude Code
subprocesses. Four runtime entities you'll see in the log:

  - **Deck** — the Python process running tui.py. Hosts the UI,
    coordinates everything else.
  - **Daemon** — a persistent claude subprocess that decomposes
    the netrunner's goals into actions (spawn / kill / etc.) and
    dispatches them as JSON. One per deck.
  - **Constructs** — task-scoped claude subprocesses that do the
    actual work. Many concurrent. Spawned by daemon or directly
    by netrunner. Identified by `cx-XXXXXXXX` in logs.
  - **Watchdog** — a separate claude subprocess that answers the
    netrunner's questions about fleet activity. Read-only.

Plus the **brake hook** (a PreToolUse hook that gates dangerous
tool calls on a per-spawn settings.json basis), **tripwires**
(deterministic regex matchers that fire on construct events),
the **session pool** (warm claude sessions for fast spawn), and
the **event bus** (the spine; everything the deck does goes
through it).

The log is NDJSON: one JSON record per line, with `kind` field
indicating what happened. Common kinds:
  - `log_header`        — first line; deck version, pid, env, brake
  - `log_footer`        — last line on clean shutdown; close_reason
  - `fleet.spawn`       — construct started; payload has cx + pid
  - `fleet.event`       — construct stream-json events
  - `fleet.finalize`    — construct ended (state: done / killed / failed)
  - `daemon.raw`        — daemon stream events
  - `daemon.thinking`   — daemon's reasoning blocks
  - `daemon.chat`       — daemon's chat output
  - `tripwire.fire`     — a tripwire matched
  - `brake.change`      — brake state changed
  - `chatlog.direct`    — UI-facing chatlog line
  - `pool.*`            — session pool lifecycle

================================================================
TWO TRIAGE PATHS — read the user message to know which fired
================================================================

**Path A — natural deck death (v1).** The deck process exited on
its own (clean shutdown, EJECT, crash, OOM, kill -9, blue
screen). Supervisor's job was reaping orphan claude
subprocesses; the log captures whatever the deck wrote before
death. Look for tracebacks, error records, or the last events
before EOF. This is the common case.

**Path B — supervisor force-killed (v1.5 stale-heartbeat).** The
deck PID was alive but the heartbeat file (`<home>/.cyberdeck/
heartbeat`) hadn't been touched in N seconds. The supervisor
force-killed the deck for diagnosis. The user message contains
a STALE-HEARTBEAT CONTEXT block when this path fires; if you
see that block, the diagnostic question shifts: it's no longer
"why did the deck crash" but "why did the TUI go unresponsive
while the process stayed alive." Common causes:

  - **TUI event loop wedge** — Textual's main loop blocked on
    something (sync I/O, infinite loop, livelock between async
    tasks). Look for the LAST work the deck was doing in the
    log: which goal was active, which constructs were running,
    which daemon turn was in flight. The wedge usually happens
    right after a specific trigger.
  - **Render-pipeline crash** (filed gotcha — Textual `_render`
    shadowing). Symptom is "deck stops drawing, heartbeat
    stops" without a Python traceback in the log because the
    crash is inside Textual's render loop and gets swallowed
    by Textual's own error handling.
  - **Async deadlock between deck-side coroutines** (e.g. fleet
    consume task awaiting an event that never fires).
  - **False positive: machine suspend.** The supervisor's prompt
    is supposed to filter most of these out (heartbeat recovers
    when the machine wakes), but if --auto-triage-on-stale was
    set, we may be triaging a suspend. The STALE-HEARTBEAT
    CONTEXT block notes whether the netrunner confirmed via
    prompt or whether auto-fire skipped the prompt — this
    distinguishes "real wedge" from "supervisor was over-eager."
    If suspend looks plausible (long stretch with NO log
    activity right before kill, no interesting trigger), say
    so; the netrunner can adjust the threshold.

================================================================
TRIAGE METHOD
================================================================

1. **Find the death point.** The log usually contains a Python
   traceback or a final event before the stream goes silent. Look
   for: stderr tracebacks, `severity: error` or `severity:
   critical` records, the last few records before EOF. **Path B
   note:** there will be NO traceback (supervisor killed the
   process); look instead for what the deck was DOING right
   before it stopped writing.

2. **Identify what was running.** Walk back from the death point
   and note the active goal, in-flight constructs, the daemon's
   most recent action, brake state, any tripwires firing
   recently. The point isn't to summarize the whole session —
   it's to capture state at the moment of death.

3. **Compare against the deck's filed gotchas.** Use Read on
   `Design Files/cyberdeck-state.md` (look for the "Filed
   gotchas" section) and check whether the symptom matches a
   known landmine. Common categories: Async/subprocess (argv
   truncation, --bare/OAuth, stream-json wedges), Terminal/Textual
   (render-shadowing, widget bookkeeping corruption), File paths,
   etc. If you find a match, NAME IT in your report.

4. **Reason about the cause.** Based on the death point + state +
   gotcha match, propose 1-3 plausible causes ranked by
   confidence. "Confident" means the log explicitly shows the
   mechanism; "speculative" means you're inferring from
   correlated evidence; "unknown" is a valid answer when the log
   genuinely doesn't tell you.

5. **Suggest next steps.** What should the netrunner do? Common
   shapes: "relaunch and observe X", "check Y in the source",
   "investigate Z by spawning a recon construct", "the gotcha at
   <link> says <fix>".

================================================================
OUTPUT FORMAT
================================================================

Markdown. The netrunner reads this in a text editor or in the
deck's file viewer. Structure:

```markdown
# Triage report — <log basename>

## Summary
<1-3 sentence top-line>

## Death point
<what was the last event(s); paste relevant log lines verbatim>

## State at death
- **Goal**: <if known>
- **Brake**: <state>
- **In-flight constructs**: <count + ids>
- **Daemon state**: <last known>
- **Recent tripwire fires**: <if any>

## Plausible causes (ranked)
1. **<cause>** (confidence: high / medium / low)
   <reasoning, citing log evidence>
2. ...

## Filed-gotcha matches
<if any matched; quote the gotcha entry>

## Suggested next steps
<numbered list, concrete actions>

## Cleanup status (from supervisor)
<the supervisor's stderr summary, passed in the user message>
```

Keep total length under ~1200 words. The netrunner may share
this report (with you, with future-you, with another model);
make it self-contained but not bloated.

================================================================
HARD CONSTRAINTS
================================================================

- READ-ONLY. No Bash, no Write, no Edit, no NotebookEdit, no
  WebFetch, no spawning subagents.
- NO AUTOMATIC FIXES. Recommendations go in "Suggested next
  steps"; the netrunner decides.
- HONEST ABOUT UNKNOWNS. "The log doesn't show what caused this"
  is a valid finding. Don't invent causes from thin evidence.
- BRIEF. The netrunner is reading this AFTER something already
  went wrong; respect their time.
"""


def build_user_prompt(req: TriageRequest, log_tail: str) -> str:
    """Compose the user-side prompt for one triage call.

    Carries:
      - The path to the original log file (for Read tool reference)
      - The tail content of the log (primary content)
      - The supervisor's cleanup summary (what we already did)
      - The deck source directory (for Read/Glob/Grep targeting)
    """
    parts = [
        f"DECK CRASH TRIAGE REQUEST",
        f"",
        f"Log file path: {req.log_path}",
        f"Deck PID at death: {req.deck_pid if req.deck_pid is not None else 'unknown'}",
        (
            f"Clean close reason: "
            f"{req.clean_close_reason if req.clean_close_reason else 'NONE — unclean exit'}"
        ),
        f"Tracked subprocesses at death: {req.tracked_subprocesses}",
        f"Subprocesses killed by supervisor: {req.subprocesses_killed}",
    ]
    if req.wedge_kill_context:
        # v1.5 stale-heartbeat path. The deck wasn't dying on its
        # own — its PID was alive but the heartbeat had gone stale
        # (TUI wedged, or machine suspend, or other liveness gap).
        # The supervisor force-killed it. Surface this so the
        # triage knows to diagnose "why did the TUI go unresponsive"
        # rather than "why did the process crash."
        parts.append("")
        parts.append("=" * 64)
        parts.append("STALE-HEARTBEAT (v1.5) CONTEXT — supervisor force-killed:")
        parts.append("=" * 64)
        parts.append(req.wedge_kill_context)
    if req.deck_source_dir:
        parts.append(f"Deck source directory: {req.deck_source_dir}")
        parts.append(
            f"(You can Read / Glob / Grep against this directory. "
            f"Design Files/ has the canon docs including filed gotchas.)"
        )
    parts.append("")
    parts.append("=" * 64)
    parts.append(
        f"LOG TAIL (last {LOG_TAIL_BYTES // 1000}KB; head via Read tool)"
    )
    parts.append("=" * 64)
    parts.append(log_tail)
    parts.append("")
    parts.append("=" * 64)
    parts.append("Produce the triage report per the format in your system prompt.")
    return "\n".join(parts)


def _load_log_tail(log_path: Path, *, cap_bytes: int = LOG_TAIL_BYTES) -> str:
    """Read the last `cap_bytes` of the log file.

    Best-effort: returns "" on read failure. Decodes as UTF-8 with
    error-replace so a half-flushed binary tail doesn't crash the
    triage. Strips a partial first line (we may have started mid-
    record after the seek).
    """
    try:
        size = log_path.stat().st_size
    except Exception:
        return ""
    try:
        with log_path.open("rb") as f:
            if size > cap_bytes:
                f.seek(size - cap_bytes)
            data = f.read()
    except Exception:
        return ""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return ""
    if size > cap_bytes:
        # Drop the partial first line (we likely landed mid-record
        # after the seek). Subsequent lines are intact.
        nl = text.find("\n")
        if nl >= 0:
            text = text[nl + 1:]
    return text


def _build_report_path(req: TriageRequest) -> Path:
    """Where to write the triage report. Default: alongside the
    log file with `-triage.md` suffix."""
    base = req.log_path.stem  # cyberdeck-2026-05-06-195754
    parent = req.report_dir if req.report_dir else req.log_path.parent
    return parent / f"{base}-triage.md"


def _summary_line(report_text: str, max_chars: int = 100) -> str:
    """Extract a one-line stderr-friendly summary from the report.

    Looks for the first non-empty line under "## Summary"; falls
    back to the report's first non-heading paragraph; falls back
    to a generic "triage written" if neither found.
    """
    lines = report_text.splitlines()
    in_summary = False
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if not line.strip():
                continue
            if line.strip().startswith("#"):
                break
            text = line.strip()
            if text:
                return text[:max_chars]
    # Fallback: first non-heading prose
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#") and not text.startswith("```"):
            return text[:max_chars]
    return "triage written"


def run_triage(req: TriageRequest) -> TriageResult:
    """Spawn one `claude -p` subprocess for the triage and return
    the result.

    Synchronous (the supervisor is in its cleanup path; no async
    runtime, just a plain wait). Best-effort throughout: any
    failure path still produces a TriageResult with `success=False`
    and an error message — the supervisor logs the summary either
    way.

    Same clean-spawn recipe as advisor.py (the Family A pattern):
    --system-prompt-file (avoids Windows argv-newline truncation),
    --tools "Read,Glob,Grep" (read-only tooling),
    --disable-slash-commands, --no-session-persistence, env-var
    belt for CLAUDE.md / auto-memory / git-instructions
    suppression. Mechanic's curated system prompt has its own
    architecture vocabulary; no need to free-ride on auto-load.
    """
    started_at = time.time()

    # Resolve binary upfront for a clear error if it's missing.
    bin_path = shutil.which(req.claude_bin) or req.claude_bin

    # Read the tail of the log. If this fails, the triage isn't
    # going to be useful — bail with a clear error so the
    # supervisor can log it.
    log_tail = _load_log_tail(req.log_path)
    if not log_tail:
        return TriageResult(
            success=False,
            report_text=(
                f"# Triage report — {req.log_path.name}\n\n"
                f"## Summary\n\n"
                f"Triage failed: could not read log tail from "
                f"`{req.log_path}`. The file may be missing, "
                f"unreadable, or empty.\n"
            ),
            error="log_tail_read_failed",
            elapsed_s=time.time() - started_at,
            summary_line="triage failed: log unreadable",
        )

    user_prompt = build_user_prompt(req, log_tail)

    # System prompt → temp file. argv-newline-truncation gotcha
    # applies to every spawn site that uses --system-prompt /
    # --append-system-prompt with multi-line content on Windows.
    sysprompt_path: Optional[str] = None
    try:
        fd, sysprompt_path = tempfile.mkstemp(
            suffix=".txt", prefix=f"mechanic-triage-",
        )
        os.close(fd)
        Path(sysprompt_path).write_text(
            MECHANIC_SYSTEM_PROMPT, encoding="utf-8",
        )
    except Exception as e:
        return TriageResult(
            success=False,
            report_text="",
            error=f"failed to write system-prompt file: {e}",
            elapsed_s=time.time() - started_at,
            summary_line=f"triage failed: prompt write {e!r}",
        )

    cmd = [
        bin_path,
        "-p",
        "--system-prompt-file", sysprompt_path,
        "--tools", TRIAGE_ALLOWED_TOOLS,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model", TRIAGE_MODEL,
        "--effort", TRIAGE_EFFORT,
        # Permission mode bypassPermissions because v1's tools are
        # read-only — Read/Glob/Grep don't have side effects, no
        # permission prompts needed for them.
        "--permission-mode", "bypassPermissions",
    ]
    # Allow the model to Read files outside the supervisor's cwd
    # (which is wherever launch.bat fired it). Pass the deck
    # source dir as an additional working directory.
    if req.deck_source_dir:
        cmd += ["--add-dir", str(req.deck_source_dir)]

    env = {
        **os.environ,
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }

    try:
        proc = subprocess.run(
            cmd,
            input=user_prompt.encode("utf-8"),
            capture_output=True,
            timeout=req.timeout,
            env=env,
        )
    except FileNotFoundError:
        return _finalize_failure(
            req, sysprompt_path, started_at,
            f"claude binary not found: {req.claude_bin}",
            "triage failed: claude not found",
        )
    except subprocess.TimeoutExpired:
        return _finalize_failure(
            req, sysprompt_path, started_at,
            f"claude -p timed out after {req.timeout}s",
            "triage failed: timeout",
        )
    except Exception as e:
        return _finalize_failure(
            req, sysprompt_path, started_at,
            f"subprocess spawn failed: {e}",
            f"triage failed: spawn {e!r}",
        )
    finally:
        # Always cleanup the temp prompt file. Best-effort.
        if sysprompt_path:
            try:
                os.unlink(sysprompt_path)
            except Exception:
                pass

    if proc.returncode != 0:
        err_text = proc.stderr.decode("utf-8", errors="replace").strip()
        return _finalize_failure(
            req, None, started_at,
            (
                f"claude exited {proc.returncode}: "
                f"{err_text[:300] if err_text else '(no stderr)'}"
            ),
            f"triage failed: claude exit {proc.returncode}",
        )

    report_text = proc.stdout.decode("utf-8", errors="replace").strip()
    if not report_text:
        return _finalize_failure(
            req, None, started_at,
            "claude returned empty output",
            "triage failed: empty output",
        )

    # Write report to disk. Best-effort — even if the write fails,
    # the report_text is in the result.
    report_path = _build_report_path(req)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
    except Exception as e:
        return TriageResult(
            success=True,
            report_text=report_text,
            report_path=None,
            error=f"report-write failed (text in result): {e}",
            summary_line=_summary_line(report_text),
            elapsed_s=time.time() - started_at,
        )

    return TriageResult(
        success=True,
        report_text=report_text,
        report_path=report_path,
        summary_line=_summary_line(report_text),
        elapsed_s=time.time() - started_at,
    )


def _finalize_failure(
    req: TriageRequest,
    sysprompt_path: Optional[str],
    started_at: float,
    error: str,
    summary_line: str,
) -> TriageResult:
    """Build a TriageResult for failure paths. Writes a stub
    report to disk so the netrunner has SOMETHING to find next
    to the log file even when the LLM call failed."""
    report_text = (
        f"# Triage report — {req.log_path.name}\n\n"
        f"## Summary\n\n"
        f"Triage failed: {error}\n\n"
        f"## Cleanup status (from supervisor)\n\n"
        f"Tracked subprocesses at death: {req.tracked_subprocesses}\n"
        f"Subprocesses killed by supervisor: {req.subprocesses_killed}\n"
    )
    report_path = _build_report_path(req)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
    except Exception:
        report_path = None  # write failure stays silent
    return TriageResult(
        success=False,
        report_text=report_text,
        report_path=report_path,
        error=error,
        summary_line=summary_line,
        elapsed_s=time.time() - started_at,
    )
