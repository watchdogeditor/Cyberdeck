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
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


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

    `is_partial` is True when the triage timed out but
    partial-recovery wrote a useful report from collected stream
    events (assistant text, thinking blocks, tool calls). Lets
    the supervisor distinguish a USEFUL timeout-recovery report
    from a literal failure stub when surfacing the result to the
    netrunner — the difference matters because partial reports
    contain real diagnostic content the netrunner can act on.

    `session_id` is the claude-side session uuid captured from the
    first system/init stream event. Set when the spawn produced one;
    None on early failures. `run_iterative_triage` reads this to
    drive `--resume <session_id>` for subsequent deepening passes,
    so the model keeps its working context across passes instead
    of re-reading the log from scratch.

    The supervisor uses summary_line for its stderr output; the
    netrunner reads report_path for the full story.
    """
    success: bool
    report_text: str
    report_path: Optional[Path] = None
    summary_line: str = ""
    error: Optional[str] = None
    elapsed_s: float = 0.0
    is_partial: bool = False
    session_id: Optional[str] = None


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

6. **Recommend whether config repair would help.** The deck has a
   v2 "Mechanic repair" mode that scans config files (state.json,
   profile TOMLs, tools.toml) for structural corruption and
   proposes per-file fixes the netrunner approves with one
   keystroke. After your report writes, the supervisor offers to
   run the repair scan; your `## Repair recommendation` section
   sets the prompt's default. Recommend YES when the death
   evidence points at a config-file shape (TOML parse error,
   missing required field, broken state.json, etc.). Recommend NO
   when the failure is in code / network / subprocess / Python
   runtime — repair won't help with those. Recommend NO when the
   log is clean and the deck just shut down normally. Be honest:
   most crashes don't have config-file root causes; the default
   should be NO unless evidence points at config shape.

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

## Repair recommendation
<one short line in this exact shape: "Recommend repair: Y - <one-sentence reasoning>"
or "Recommend repair: N - <one-sentence reasoning>". The supervisor parses
this to set the post-triage prompt's default. Default to N unless the death
evidence specifically implicates a config file (state.json, profile TOML,
tools.toml).>

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


# ---- stream-json event pretty-printer -------------------------------------
#
# Filed 2026-05-06 after netrunner observed that the v1 / v1.5 triage
# spawn went radio-silent for ~2 minutes ("a thousand fucking years"
# was the netrunner's framing). The original `subprocess.run`
# implementation captured stdout end-to-end with no live output, so
# the mechanic window showed nothing while the LLM thought, walked
# files, and assembled the report. Felt like a hang even when it
# wasn't.
#
# Fix: spawn claude with `--output-format stream-json --verbose`,
# read events line-by-line on a daemon thread, pretty-print one
# short line per event to stderr (mechanic's window), collect the
# final result event's text. Same total wall time, but the
# netrunner sees what the model is actually doing — Read tool fires,
# tool result lengths, thinking-block previews, the final result
# event arriving. Live narration of progress.


def _emit(line: str) -> None:
    """Write one line to stderr with flush. Batched flushing would
    defeat the live-narration UX (lines would buffer up and arrive
    in chunks)."""
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _truncate(text: str, n: int = 80) -> str:
    """Single-line truncation for stderr display — collapses
    newlines to spaces, caps at n chars, adds an ellipsis if we
    cut anything."""
    s = " ".join((text or "").split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _print_triage_event(event: dict) -> None:
    """Pretty-print one stream-json event to mechanic's stderr.

    Short human-readable lines, one per significant event. Silent
    on noisy event kinds (rate_limit, partial assistant chunks)
    that don't help the netrunner understand what's happening.
    """
    et = event.get("type")

    if et == "system":
        sub = event.get("subtype")
        if sub == "init":
            model = event.get("model", "?")
            _emit(f"[mechanic.triage] session started · model={model}")
        return

    if et == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                txt = _truncate(block.get("thinking", ""), 100)
                if txt:
                    _emit(f"[mechanic.triage] thinking: {txt}")
            elif btype == "text":
                txt = _truncate(block.get("text", ""), 100)
                if txt:
                    _emit(f"[mechanic.triage] writing: {txt}")
            elif btype == "tool_use":
                tool = block.get("name", "?")
                inp = block.get("input", {}) or {}
                # Tool-specific summary so the netrunner sees WHAT
                # the model is reading / searching. Matters
                # because a stuck triage often shows up as repeated
                # reads of the same file (sign of a confused
                # model that needs a higher caliber or a tighter
                # prompt).
                if tool == "Read":
                    fp = inp.get("file_path", "?")
                    _emit(f"[mechanic.triage] Read: {fp}")
                elif tool == "Glob":
                    pat = inp.get("pattern", "?")
                    where = inp.get("path", "")
                    suffix = f" in {where}" if where else ""
                    _emit(f"[mechanic.triage] Glob: {pat}{suffix}")
                elif tool == "Grep":
                    pat = inp.get("pattern", "?")
                    where = inp.get("path", "")
                    suffix = f" in {where}" if where else ""
                    _emit(f"[mechanic.triage] Grep: {pat!r}{suffix}")
                else:
                    _emit(f"[mechanic.triage] {tool} tool")
        return

    if et == "user":
        # tool_result envelope. Show length only — tool results
        # are often kilobytes (the netrunner doesn't need the full
        # content streamed to their terminal).
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                # content may be [{type:text, text:...}, ...]
                content = "".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict)
                )
            clen = len(str(content or ""))
            is_err = bool(block.get("is_error"))
            tag = "tool error" if is_err else "tool result"
            _emit(f"[mechanic.triage] {tag}: {clen} chars")
        return

    if et == "result":
        result_text = event.get("result", "") or ""
        _emit(
            f"[mechanic.triage] result received "
            f"({len(result_text)} chars)"
        )
        return

    # Other event kinds (rate_limit_event, partial assistant chunks
    # under --include-partial-messages, etc.) are skipped silently
    # to keep stderr signal-to-noise high.


def run_triage(
    req: TriageRequest,
    *,
    resume_session_id: Optional[str] = None,
    user_prompt_override: Optional[str] = None,
) -> TriageResult:
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

    Iterative-triage support (item 0g, 2026-05-07):
      - `resume_session_id`: when set, the spawn adds
        `--resume <session_id>` and SKIPS the system-prompt-file
        flag (claude --resume preserves the prior session's system
        prompt + conversation context). The deepening pass inherits
        the model's working knowledge from pass 1 instead of
        re-reading the log + the gotchas list from scratch.
      - `user_prompt_override`: when set, used as the user-message
        body instead of the default `build_user_prompt(req,
        log_tail)`. Pass-2+ uses this for the deepening directive
        ("go deeper, look at X"); the conversation context already
        has the log content from pass 1.
    """
    started_at = time.time()

    # Resolve binary upfront for a clear error if it's missing.
    bin_path = shutil.which(req.claude_bin) or req.claude_bin

    # Determine the user prompt + whether we need to read the log.
    # Resume passes don't re-read the log (the prior session already
    # has it in context); fresh passes do.
    if user_prompt_override is not None:
        user_prompt = user_prompt_override
    else:
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
    # Skipped when resuming — claude --resume preserves the prior
    # session's system prompt automatically.
    sysprompt_path: Optional[str] = None
    if resume_session_id is None:
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

    cmd = [bin_path, "-p"]
    if resume_session_id is not None:
        cmd += ["--resume", resume_session_id]
    if sysprompt_path is not None:
        cmd += ["--system-prompt-file", sysprompt_path]
    cmd += [
        "--tools", TRIAGE_ALLOWED_TOOLS,
        "--disable-slash-commands",
        # --no-session-persistence is omitted on resume passes
        # because the resume itself reads the persisted session.
        # Fresh passes still set it to keep the cache stable.
        *(["--no-session-persistence"] if resume_session_id is None else []),
        "--model", TRIAGE_MODEL,
        "--effort", TRIAGE_EFFORT,
        # Permission mode bypassPermissions because v1's tools are
        # read-only — Read/Glob/Grep don't have side effects, no
        # permission prompts needed for them.
        "--permission-mode", "bypassPermissions",
        # Stream-json output + verbose so we get one event per
        # line on stdout. The reader thread parses each line and
        # pretty-prints to stderr, giving the netrunner live
        # visibility into what the triage is doing (Read tool
        # fires, thinking-block previews, tool-result lengths,
        # etc.). claude code requires --verbose with stream-json
        # in -p mode.
        "--output-format", "stream-json",
        "--verbose",
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

    # Spawn the subprocess. stdout is captured for stream-json
    # parsing; stderr passes through to mechanic's own stderr so
    # any claude-internal error messages surface to the netrunner
    # immediately. stdin is piped for the user-prompt write.
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,  # passthrough to mechanic stderr
            env=env,
        )
    except FileNotFoundError:
        return _finalize_failure(
            req, sysprompt_path, started_at,
            f"claude binary not found: {req.claude_bin}",
            "triage failed: claude not found",
        )
    except Exception as e:
        return _finalize_failure(
            req, sysprompt_path, started_at,
            f"subprocess spawn failed: {e}",
            f"triage failed: spawn {e!r}",
        )

    # Send the user prompt + close stdin so claude knows that's the
    # whole input. Best-effort — if the subprocess died between
    # spawn and now, broken pipe is silent (the wait() below will
    # reflect the failure).
    try:
        if proc.stdin is not None:
            proc.stdin.write(user_prompt.encode("utf-8"))
            proc.stdin.close()
    except Exception:
        pass

    # Reader thread: pulls stream-json events line-by-line off
    # stdout, pretty-prints each to stderr, AND accumulates
    # in-progress state for partial-recovery on timeout. Running
    # on a daemon thread so the main thread can wait on the
    # subprocess with a timeout — readline() is blocking and
    # would interfere with timeout enforcement otherwise.
    #
    # Partial-recovery state (filed 2026-05-06 after netrunner
    # observed the 180s timeout produced ONLY a stub "triage
    # failed: timed out" report with no actual content). With
    # streaming output we ALREADY have the model's in-progress
    # work in memory — it would be wasteful to throw it away just
    # because the result event didn't arrive in time. Track
    # everything that came through; assemble a partial report on
    # timeout.
    reader_state: dict = {
        "final_text": "",         # set when `result` event arrives
        "events_seen": 0,
        "assistant_text": [],     # text blocks: the in-progress report
        "thinking_blocks": [],    # all thinking strings (debug appendix)
        "tool_calls": [],         # (tool, input_dict) tuples
        "session_id": None,       # claude-side uuid from system/init
    }

    def _reader_thread() -> None:
        try:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                if not raw:
                    break
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                reader_state["events_seen"] += 1
                _print_triage_event(event)

                et = event.get("type")
                if et == "system" and event.get("subtype") == "init":
                    # First event in the stream — carries the
                    # claude-side session uuid we'll need for
                    # --resume on a subsequent deepening pass.
                    sid = event.get("session_id")
                    if isinstance(sid, str) and sid:
                        reader_state["session_id"] = sid
                if et == "result":
                    reader_state["final_text"] = (
                        event.get("result", "") or ""
                    )
                elif et == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []) or []:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type")
                        if bt == "text":
                            t = block.get("text", "")
                            if t:
                                reader_state["assistant_text"].append(t)
                        elif bt == "thinking":
                            t = block.get("thinking", "")
                            if t:
                                reader_state["thinking_blocks"].append(t)
                        elif bt == "tool_use":
                            tool = block.get("name", "?")
                            inp = block.get("input", {}) or {}
                            reader_state["tool_calls"].append((tool, inp))
        except Exception as e:
            _emit(f"[mechanic.triage] reader thread error: {e}")

    reader = threading.Thread(target=_reader_thread, daemon=True)
    reader.start()

    # Wait for the subprocess with timeout. timeout fires only if
    # the model HANGS — normal completion sets returncode and the
    # readline loop exits cleanly when stdout closes.
    timed_out = False
    try:
        proc.wait(timeout=req.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    # Give the reader a moment to drain any remaining events from
    # stdout's pipe buffer. Daemon thread, so worst case it's
    # abandoned at process exit — but the join lets us collect
    # the final result event if it landed just before close.
    reader.join(timeout=5)

    # Cleanup the temp prompt file. Always best-effort.
    if sysprompt_path:
        try:
            os.unlink(sysprompt_path)
        except Exception:
            pass

    if timed_out:
        # Partial-recovery path. The model went over the timeout
        # cap, but streaming output means we have its in-progress
        # work in `reader_state` — assistant text it was writing,
        # thinking blocks it produced, tool calls it made. Assemble
        # a "PARTIAL TRIAGE" report with that content rather than
        # discarding everything and returning only the stub.
        # Filed 2026-05-06 after netrunner observed the timeout
        # path produced "Triage failed: timed out" with NO actual
        # content despite the model having done substantial work.
        partial_report = _build_partial_report(
            req, reader_state, started_at,
        )
        report_path = _build_report_path(req)
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(partial_report, encoding="utf-8")
        except Exception:
            report_path = None
        return TriageResult(
            success=False,
            is_partial=True,
            report_text=partial_report,
            report_path=report_path,
            error=f"claude -p timed out after {req.timeout}s",
            summary_line=(
                f"timed out at {req.timeout:.0f}s — partial report "
                f"saved with {len(reader_state['assistant_text'])} "
                f"text blocks + {len(reader_state['tool_calls'])} "
                f"tool calls"
            ),
            elapsed_s=time.time() - started_at,
            session_id=reader_state.get("session_id"),
        )

    if proc.returncode != 0:
        return _finalize_failure(
            req, None, started_at,
            (
                f"claude exited {proc.returncode} "
                f"(events seen: {reader_state['events_seen']})"
            ),
            f"triage failed: claude exit {proc.returncode}",
        )

    report_text = (reader_state["final_text"] or "").strip()
    if not report_text:
        return _finalize_failure(
            req, None, started_at,
            (
                f"claude returned no result event "
                f"(events seen: {reader_state['events_seen']})"
            ),
            "triage failed: no result event",
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
            session_id=reader_state.get("session_id"),
        )

    return TriageResult(
        success=True,
        report_text=report_text,
        report_path=report_path,
        summary_line=_summary_line(report_text),
        elapsed_s=time.time() - started_at,
        session_id=reader_state.get("session_id"),
    )


def _build_partial_report(
    req: TriageRequest,
    reader_state: dict,
    started_at: float,
) -> str:
    """Assemble a partial triage report from streamed events when
    the subprocess hit the timeout before producing a `result`
    event.

    The streamed events tell us what the model HAD done by the
    deadline: thinking blocks (its reasoning chain so far), tool
    calls (what it read / searched), and any assistant text
    blocks (the in-progress report being written). This is real
    diagnostic value; it would be wasteful to throw it away just
    because the model didn't reach the final assembly step.

    Format mirrors the normal triage report shape so the netrunner
    can still skim it the same way, but with a clear "PARTIAL"
    header and a "WHAT WE HAVE" section listing the captured
    state. Failed-final-step framing rather than completed-but-
    cut-off, because we don't actually know if the in-progress
    text was the final answer or mid-paragraph reasoning.
    """
    elapsed = time.time() - started_at
    text_blocks: list[str] = reader_state.get("assistant_text", [])
    thinking_blocks: list[str] = reader_state.get("thinking_blocks", [])
    tool_calls: list[tuple] = reader_state.get("tool_calls", [])

    lines: list[str] = []
    lines.append(f"# PARTIAL Triage report — {req.log_path.name}")
    lines.append("")
    lines.append("## Status")
    lines.append("")
    lines.append(
        f"**Triage timed out at {req.timeout:.0f}s** "
        f"(elapsed: {elapsed:.1f}s). The model was still working "
        f"when the supervisor killed the subprocess — "
        f"`{len(thinking_blocks)}` thinking block(s), "
        f"`{len(tool_calls)}` tool call(s), "
        f"`{len(text_blocks)}` text block(s) "
        f"captured before the kill. Use the captured state below "
        f"as a partial diagnostic; if it's not enough, re-run "
        f"triage with a longer `--triage-timeout`."
    )
    lines.append("")

    if text_blocks:
        lines.append("## In-progress report (model was writing this)")
        lines.append("")
        lines.append(
            "*This is what the model had typed by the deadline. May "
            "be a complete report cut off at the last paragraph, OR "
            "may be mid-reasoning that hadn't reached the final "
            "structured output yet. Read with that caveat.*"
        )
        lines.append("")
        lines.append("---")
        lines.append("")
        for block in text_blocks:
            lines.append(block.rstrip())
        lines.append("")
        lines.append("---")
        lines.append("")

    if tool_calls:
        lines.append("## Tools the model invoked")
        lines.append("")
        lines.append(
            "*If the same file appears multiple times here, the "
            "model may have been stuck in a re-read loop — sign "
            "that the prompt is unclear or the caliber is too low.*"
        )
        lines.append("")
        for tool, inp in tool_calls:
            if tool == "Read":
                fp = inp.get("file_path", "?")
                lines.append(f"- `Read`: `{fp}`")
            elif tool == "Glob":
                pat = inp.get("pattern", "?")
                where = inp.get("path", "")
                suffix = f" in `{where}`" if where else ""
                lines.append(f"- `Glob`: `{pat}`{suffix}")
            elif tool == "Grep":
                pat = inp.get("pattern", "?")
                where = inp.get("path", "")
                suffix = f" in `{where}`" if where else ""
                lines.append(f"- `Grep`: `{pat!r}`{suffix}")
            else:
                lines.append(f"- `{tool}`")
        lines.append("")

    if thinking_blocks:
        lines.append("## Thinking blocks (model's reasoning chain)")
        lines.append("")
        lines.append(
            "*Captured for debugging — usually the netrunner "
            "doesn't need to read these, but they help when "
            "diagnosing why the model got stuck.*"
        )
        lines.append("")
        for i, t in enumerate(thinking_blocks, 1):
            lines.append(f"### Block {i}")
            lines.append("")
            # Quote-block the thinking so it's visually distinct
            # from the model's actual prose.
            for line in t.splitlines():
                lines.append(f"> {line}")
            lines.append("")

    lines.append("## Cleanup status (from supervisor)")
    lines.append("")
    lines.append(f"- Tracked subprocesses at death: {req.tracked_subprocesses}")
    lines.append(f"- Subprocesses killed by supervisor: {req.subprocesses_killed}")
    if req.clean_close_reason:
        lines.append(f"- Clean close reason: `{req.clean_close_reason}`")
    else:
        lines.append("- Clean close reason: NONE — unclean exit")
    if req.wedge_kill_context:
        lines.append("")
        lines.append("### v1.5 wedge-kill context")
        lines.append("")
        lines.append(req.wedge_kill_context)
    lines.append("")
    lines.append("## Re-running")
    lines.append("")
    lines.append(
        f"To re-run with a longer cap: launch the mechanic with "
        f"`--triage-timeout {req.timeout * 2:.0f}` (or whatever "
        f"feels right). To suppress triage entirely on this branch "
        f"while you debug: `--no-triage`."
    )
    lines.append("")
    return "\n".join(lines)


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


# ---- iterative triage (item 0g, 2026-05-07) -------------------------------
#
# Multi-pass deepening triage. After v1's initial report, optionally prompt
# the netrunner: "Keep delving?". On y, fire another pass via `--resume
# <session_id>` so the model continues with full prior-pass context (no
# re-reading the log + gotchas; just goes deeper). Each pass appends a new
# section to the same report file. Stops on N, max-passes, fail, or
# non-TTY stdin.
#
# Why iterative: real-deck observation 2026-05-06 — pass-1 reports were
# usually correct on the surface but sometimes left the netrunner with
# unanswered "but WHY did X cascade into Y" questions. Forcing the
# netrunner to manually re-spawn a triage with a longer prompt was
# friction. Iterative gives them an interactive deepening surface that
# costs token-by-token instead of upfront.


# Maximum number of deepening passes (v1 = 1 pass; v1+iter = pass 1 + up
# to MAX_DEEPENING_PASSES additional). Soft cap to keep token spend
# bounded — netrunner can override per-launch via `--max-triage-passes`.
DEFAULT_MAX_DEEPENING_PASSES = 4


def _build_deepen_directive(pass_num: int) -> str:
    """User-message body for a deepening pass. Sent verbatim as the
    user's next turn; claude --resume preserves the prior session's
    system prompt and conversation history, so this is just the
    "go further" instruction.

    `pass_num` is the deepening pass number (2 = first deepening pass
    after pass 1; 3 = second deepening; etc.). Used in the heading
    so the appended section is glanceable in the report file.
    """
    return (
        f"DEEPEN. The netrunner read your last triage and wants more "
        f"depth. Go further than your previous pass — pick the most "
        f"speculative cause from your prior 'Plausible causes' list "
        f"and dig until you can promote or eliminate it. Cross-check "
        f"more filed gotchas in cyberdeck-state.md. If your prior "
        f"'Suggested next steps' said 'investigate Z', go investigate "
        f"Z now and report what you found. If your prior report said "
        f"'unknown' anywhere, try to narrow it.\n"
        f"\n"
        f"Append a NEW section to the report under the heading "
        f"`## Deeper analysis (pass {pass_num})`. Don't repeat your "
        f"earlier content; build on it. Same hard constraints apply: "
        f"read-only, no autonomous fixes, honest about unknowns, "
        f"brief."
    )


def prompt_keep_delving(pass_num: int) -> bool:
    """Stderr prompt: "Keep delving? [y/N]". Returns True on y/yes.

    Non-TTY stdin → returns False without prompting. Same policy as
    the doctor's plugin-dep prompt: when there's no human at the
    terminal, don't spin (keeps headless / wall-mount / CI deployments
    sane — they get the v1 single-pass triage and exit, no hang).

    EOFError / KeyboardInterrupt during input() → False (treat as
    decline). Ctrl+C on the supervisor terminal during a triage prompt
    is the netrunner saying "I'm done with this triage flow"; respect
    that.
    """
    if not sys.stdin.isatty():
        return False
    try:
        sys.stderr.write(
            f"\n[mechanic] Pass {pass_num - 1} report written. "
            f"Keep delving? (next pass resumes the same claude "
            f"session and writes a new 'Deeper analysis (pass "
            f"{pass_num})' section). [y/N]: "
        )
        sys.stderr.flush()
        ans = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    except Exception:
        # Any unexpected I/O issue — bail on the iterative path
        # rather than risk a hang. Pass-1 report is preserved.
        return False
    return ans in ("y", "yes")


def _append_pass_to_report(
    report_path: Optional[Path],
    new_text: str,
) -> bool:
    """Append a deepening pass's text to the existing report file.

    Returns True on success, False on any I/O failure. Failure is
    tolerable — the new text is also in the returned TriageResult,
    so the netrunner can recover it from stderr if needed.

    Insertion strategy: append a horizontal-rule separator + the
    new text. The pass's own opening heading (`## Deeper analysis
    (pass N)`) provides the section anchor; we don't add another
    heading layer here. Plain append vs. a more structured rewrite
    keeps this resilient to the model formatting the new pass
    however it wants.
    """
    if report_path is None:
        return False
    try:
        with report_path.open("a", encoding="utf-8") as f:
            f.write("\n\n---\n\n")
            f.write(new_text.rstrip())
            f.write("\n")
        return True
    except OSError:
        return False


def run_iterative_triage(
    req: TriageRequest,
    *,
    max_deepening_passes: int = DEFAULT_MAX_DEEPENING_PASSES,
    prompt_fn: Optional[Callable[[int], bool]] = None,
) -> list[TriageResult]:
    """Run pass 1 + up to `max_deepening_passes` additional passes,
    prompting the netrunner between each via `prompt_fn` (defaults
    to `prompt_keep_delving`). Returns the list of TriageResults
    in order — `[0]` is pass 1, `[1]` is the first deepening pass,
    etc. The list is always non-empty (pass 1 always fires).

    Each deepening pass:
      - Uses `--resume <session_id>` from the prior result to inherit
        the model's full working context (log content, gotchas
        cross-references, prior cause-ranking) instead of re-reading
        from scratch.
      - Sends a short "go deeper" directive (`_build_deepen_directive`)
        instructing the model to write a new `## Deeper analysis
        (pass N)` section.
      - Appends the new text to the existing report file via
        `_append_pass_to_report`.

    Stops early on:
      - `prompt_fn` returns False (netrunner declined / non-TTY).
      - `max_deepening_passes` reached.
      - Pass returned `success=False AND not is_partial` — a real
        failure, no point continuing without a valid session.
      - No `session_id` captured from the prior pass (can't resume).
    """
    if prompt_fn is None:
        prompt_fn = prompt_keep_delving

    # Pass 1 — fresh, standard triage.
    results: list[TriageResult] = []
    pass1 = run_triage(req)
    results.append(pass1)

    # Real failure on pass 1 → no session to resume from. Return
    # the single-result list; supervisor surfaces it the same way
    # as a non-iterative triage.
    if not pass1.success and not pass1.is_partial:
        return results
    if not pass1.session_id:
        # Pass 1 produced output but somehow no session_id (shouldn't
        # happen in practice — system/init fires on every fresh
        # claude -p, including --no-session-persistence ones). Without
        # the id we can't resume; surface pass 1 as final.
        sys.stderr.write(
            "[mechanic] iterative triage: pass 1 didn't surface a "
            "session_id; can't deepen. Returning single-pass result.\n"
        )
        sys.stderr.flush()
        return results

    # Deepening loop.
    current_session_id: Optional[str] = pass1.session_id
    for i in range(max_deepening_passes):
        pass_num = i + 2  # pass 2, 3, 4, ...
        if not prompt_fn(pass_num):
            break

        sys.stderr.write(
            f"[mechanic] iterative: firing deepening pass "
            f"{pass_num} (resume {current_session_id[:8]}…)\n"
        )
        sys.stderr.flush()

        directive = _build_deepen_directive(pass_num)
        next_pass = run_triage(
            req,
            resume_session_id=current_session_id,
            user_prompt_override=directive,
        )
        results.append(next_pass)

        # Append this pass's text to the original report file. Use
        # report_path from pass 1 (or the latest successful pass if
        # available) — the deepening pass's own report_path may
        # have been overwritten by the per-pass write logic.
        original_report_path = pass1.report_path
        if next_pass.report_text:
            _append_pass_to_report(
                original_report_path, next_pass.report_text,
            )

        # Real failure → stop. Partial-recovery on a deepening pass
        # is treated as continuable (the appended text is still
        # diagnostic value), but a hard failure means we've lost
        # the session and should surface what we have.
        if not next_pass.success and not next_pass.is_partial:
            sys.stderr.write(
                f"[mechanic] iterative: pass {pass_num} failed "
                f"({next_pass.error}); stopping deepening loop\n"
            )
            sys.stderr.flush()
            break

        # Update session_id for the next iteration. Claude usually
        # returns the same session_id across resumes within a chain,
        # but some implementations issue a new id when context
        # rotates; track whatever the last pass actually used.
        if next_pass.session_id:
            current_session_id = next_pass.session_id

    return results
