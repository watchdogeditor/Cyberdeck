"""
Shared display/formatting helpers for console output.

Two summary functions live here, doing different jobs:

- summarize() — verbose, full event description for the per-construct
  log (visible when a pane is expanded). Shows every block in a multi-
  block message. Used by the non-TUI demo entry points and by the TUI
  for the expanded log view.

- summarize_for_activity() — short, verb-form description of *what
  the construct is currently doing* for the always-visible "› ..." line
  on each pane. Filters down to the most actionable signal (tool_use
  > thinking > text) so a glance tells you what's happening rather
  than what just got streamed.

When Textual lands the chatlog (Phase B1), it'll feed off the same
event stream and use a third format — one-line-per-event with verbs.
"""
from __future__ import annotations

from typing import Optional

from construct import EventKind


def summarize(raw: dict, *, untruncated: bool = False) -> str:
    """One-line human-readable summary of a stream-json event.

    `untruncated=True` raises the per-block sanity cap from 500 to 5000
    chars and the unknown-event fallback cap likewise. Used by the
    ExpandModal pane-log re-render so long thinking blocks and big
    tool_result bodies show in full instead of being chopped at the
    same limit the live pane uses."""
    t = raw.get("type")

    if t == "system":
        sub = raw.get("subtype", "?")
        sid = raw.get("session_id", "?")
        if sub == "result":
            return f"result session={sid} duration={raw.get('duration_ms', '?')}ms"
        return f"{sub} session={sid}"

    if t == "result":
        is_err = raw.get("is_error", False)
        dur = raw.get("duration_ms", "?")
        sub = raw.get("subtype", "?")
        return f"{sub} err={is_err} duration={dur}ms"

    if t == "rate_limit_event":
        info = raw.get("rate_limit_info", {})
        status = info.get("status", "?")
        kind = info.get("rateLimitType", "?")
        return f"{status} type={kind}"

    if t in ("user", "assistant"):
        content = raw.get("message", {}).get("content", [])
        if isinstance(content, list) and content:
            parts = [
                render_block(b, untruncated=untruncated)
                for b in content
                if isinstance(b, dict)
            ]
            return " | ".join(p for p in parts if p)
    # Unknown event type — fall back to a sanity-capped repr.
    cap = 5000 if untruncated else 500
    s = str(raw)
    return s[:cap] + ("..." if len(s) > cap else "")


def render_block(block: dict, *, untruncated: bool = False) -> str:
    """Render a single content block to a short string.

    "Short" is relative — we used to chop at 100 chars to keep things
    legible in narrow panes, but with W/A/S/D horizontal scroll and
    the expand modal (z on a focused log) the netrunner can always
    see the full content. The 500-char caps here are sanity bounds
    against megabyte tool results, not stylistic limits.

    `untruncated=True` raises the per-block cap from 500 to 5000 for
    the modal re-render path."""
    cap = 5000 if untruncated else 500
    bt = block.get("type")
    if bt == "text":
        txt = block.get("text", "").replace("\n", " ")
        return f"text: {txt[:cap]}" + ("..." if len(txt) > cap else "")
    if bt == "tool_use":
        name = block.get("name", "?")
        inp = block.get("input", {})
        return f"tool_use: {name}({fmt_input(inp, untruncated=untruncated)})"
    if bt == "tool_result":
        c = block.get("content", "")
        if isinstance(c, str):
            c = c.replace("\n", " ")
            return f"tool_result: {c[:cap]}" + ("..." if len(c) > cap else "")
        return "tool_result: (structured)"
    if bt == "thinking":
        thought = block.get("thinking", "").replace("\n", " ")
        return f"thinking: {thought[:cap]}" + ("..." if len(thought) > cap else "")
    return f"[block: {bt or 'unknown'}]"


def fmt_input(inp: dict, *, untruncated: bool = False) -> str:
    """Format a tool's input dict for display. Per-value cap of 200
    chars (2000 in untruncated mode) — high enough that file paths,
    search patterns, command strings land intact in normal use; low
    enough that the occasional enormous payload (a multi-KB Bash
    heredoc, a giant Edit diff) doesn't blow up the pane log."""
    if not isinstance(inp, dict):
        cap = 2000 if untruncated else 200
        return str(inp)[:cap]
    cap = 2000 if untruncated else 200
    parts = []
    for k, v in inp.items():
        vs = str(v).replace("\n", " ")
        if len(vs) > cap:
            vs = vs[:cap - 3] + "..."
        parts.append(f"{k}={vs}")
    return ", ".join(parts)


# ---- Activity-line formatting (per-pane "what's happening now") -----------


def summarize_for_activity(raw: dict) -> Optional[str]:
    """Return a verb-style description of what this event means for the
    construct's current activity, or None if the event isn't worth
    surfacing (e.g., system_init alone tells the user nothing new).

    The pane's activity line gets a lot more readable when it shows
    "running: grep -r foo ." instead of "tool_use: Bash(command=grep
    -r foo .)". Same information, less ceremony, more glanceable.

    Priority within a multi-block message: tool_use > thinking > text.
    Tool uses are the most actionable signal — they tell you what the
    construct is *doing right now*. Thinking is a fallback for "the
    model is reasoning but hasn't acted yet." Text is the wrapup.
    """
    t = raw.get("type")

    if t == "system":
        sub = raw.get("subtype", "")
        if sub == "init":
            return "starting up..."
        return None

    if t == "result":
        return "failed" if raw.get("is_error", False) else "complete"

    if t in ("user", "assistant"):
        content = raw.get("message", {}).get("content", [])
        if not isinstance(content, list):
            return None
        # Tool use wins — it's the most "what's happening" signal.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                return _describe_tool_use(block)
        # No tool use; fall back to thinking, then text. Tool results
        # are deliberately ignored here — the preceding tool_use already
        # told the user what was being done; the result is just plumbing.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                txt = block.get("thinking", "").strip().replace("\n", " ")
                return f"thinking: {txt[:80]}" if txt else "thinking..."
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                txt = block.get("text", "").strip().replace("\n", " ")
                if txt:
                    return txt[:100]
        return None

    if t == "rate_limit_event":
        info = raw.get("rate_limit_info", {})
        return f"rate limit: {info.get('status', '?')}"

    return None


def _describe_tool_use(block: dict) -> str:
    """Verb-form description of a single tool_use block. Reads more
    naturally than 'tool_use: Read(file_path=foo.py)' for a glance."""
    name = block.get("name", "?")
    inp = block.get("input", {}) or {}
    if not isinstance(inp, dict):
        return f"using {name}"

    if name == "Read":
        return f"reading {_short_path(inp.get('file_path', '?'))}"
    if name == "Write":
        return f"writing {_short_path(inp.get('file_path', '?'))}"
    if name == "Edit":
        return f"editing {_short_path(inp.get('file_path', '?'))}"
    if name == "Bash":
        cmd = (inp.get("command") or "").replace("\n", " ")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"running: {cmd}"
    if name == "Glob":
        return f"globbing {inp.get('pattern', '?')}"
    if name == "Grep":
        return f"grepping for {inp.get('pattern', '?')}"
    if name == "WebSearch":
        q = (inp.get("query") or "?")
        return f"web search: {q[:60]}"
    if name == "WebFetch":
        url = inp.get("url", "?")
        return f"fetching {_short_url(url)}"
    if name == "TodoWrite":
        return "updating todos"
    if name == "NotebookEdit":
        return f"editing notebook {_short_path(inp.get('notebook_path', '?'))}"
    if name == "NotebookRead":
        return f"reading notebook {_short_path(inp.get('notebook_path', '?'))}"
    # Unknown tool — show the name and the first input param value.
    if inp:
        first_key = next(iter(inp), None)
        first_val = str(inp.get(first_key, ""))[:40]
        return f"using {name}({first_key}={first_val})"
    return f"using {name}"


def _short_path(path: str) -> str:
    """Trim a path to its last two components for compactness. Full
    path is in the expanded log; the activity line just needs identity."""
    if not path or not isinstance(path, str):
        return "?"
    # Try / first, then \ for Windows paths
    sep = "/" if "/" in path else ("\\" if "\\" in path else None)
    if sep is None:
        return path
    parts = path.split(sep)
    if len(parts) <= 2:
        return path
    return ".../" + "/".join(parts[-2:])


def _short_url(url: str) -> str:
    """Strip protocol and trim long URLs for the activity line."""
    if not url or not isinstance(url, str):
        return "?"
    u = url
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if len(u) > 60:
        u = u[:57] + "..."
    return u


# ---- Chatlog formatting ---------------------------------------------------
#
# B1: a unified one-line-per-event view of everything happening across the
# fleet. Mechanical extraction (no LLM in the loop, zero new tokens) — pure
# rendering of events that already flow through the listener channels.
# Lives as a right-panel tab in the TUI.
#
# Both helpers return Optional[str]:
#   - str: a Rich-markup line ready to write to the chatlog
#   - None: skip this event (it's noise, redundant, or already covered
#           by another line)


def chatlog_format_fleet(fevent, *, untruncated: bool = False) -> "Optional[str]":
    """Render a FleetEvent as a one-line chatlog entry, or None to skip.

    Most fleet events go through here. The job is to produce something
    a netrunner can scan at a glance — *what* happened to *which*
    construct, with enough specificity to act on. Chevron glyphs and
    color coding match the conventions used in the construct panes
    (cyan = clean, orange = killed, blue = redirected, red = failed).

    untruncated=True relaxes the per-event sanity caps from 500 to
    5000 chars. Used by the ExpandModal so long thinking blocks /
    tool results show in full there even when the live chatlog cap
    chops them. Still bounded — multi-MB tool results would explode
    the modal otherwise.
    """
    cap = 5000 if untruncated else 500
    cid = fevent.construct_id
    payload = fevent.payload

    if fevent.kind == "meta":
        ptype = payload.get("type", "")
        if ptype == "spawned":
            task = (payload.get("task") or "").replace("\n", " ")
            parent = payload.get("parent_id")
            if task.startswith("[Netrunner"):
                # Strip the inject framing for chatlog display, same as
                # we do for pane previews. The bracketed framing is for
                # the model, not the netrunner.
                marker = "]\n\n"
                # task already had \n collapsed above, so the marker we
                # actually see is "] " (after replace). Be tolerant.
                idx = task.find("] ")
                if idx != -1:
                    task = task[idx + 2:]
            preview = task[:60] + ("..." if len(task) > 60 else "")
            # Profile badge only for non-default profiles. Default
            # spawns are the baseline behavior; only deviations get
            # visual weight in the chatlog.
            profile_name = payload.get("profile_name")
            profile_badge = ""
            if profile_name and profile_name != "default":
                profile_badge = f" [yellow]\\[{profile_name}][/yellow]"
            # Origin badge — who dispatched this spawn.
            #   daemon    → no badge (default; visually quiet)
            #   netrunner → cyan [you] badge ("you ran n")
            #   inject    → cyan [↳you] badge ("you injected on q/Q")
            # Daemon is the baseline, so it's the absence-of-badge
            # case to match the existing "what's not annotated is
            # routine" pattern. The watchdog's earlier reasoning —
            # "this spawn had no preceding daemon thinking line, so
            # it's netrunner-initiated" — now lands as a glance check.
            origin = payload.get("origin")
            origin_badge = ""
            if origin == "netrunner":
                origin_badge = " [cyan]\\[you][/cyan]"
            elif origin == "inject":
                origin_badge = " [cyan]\\[↳you][/cyan]"
            if parent:
                # Inject follow-up. Distinct visual: continuing-from
                # marker + dim parent reference.
                return (
                    f"[bright_blue]↳[/bright_blue] [b]{cid}[/b]"
                    f"{profile_badge}{origin_badge} "
                    f"[dim](continuing {parent})[/dim]: {preview}"
                )
            return (
                f"[cyan]+[/cyan] [b]{cid}[/b]{profile_badge}{origin_badge} "
                f"spawned: {preview}"
            )

        if ptype == "finalized":
            state = payload.get("state", "?")
            runtime = payload.get("runtime", 0.0)
            files = payload.get("files_written") or []
            file_suffix = f", {len(files)} file(s)" if files else ""
            # Brake-hook denials suffix. When non-empty, append a
            # bracket showing how many tool calls got blocked and
            # which tools — gives the netrunner a glance signal that
            # this construct hit the brake. Watchdog reads the same
            # info from the chatlog snippet.
            denials = payload.get("permission_denials") or []
            if denials:
                # Summarize: "Write×2, Bash×1" — counts per tool name.
                from collections import Counter
                tool_counts = Counter(
                    str(d.get("tool_name", "?"))
                    for d in denials if isinstance(d, dict)
                )
                summary = ", ".join(
                    f"{name}×{n}" if n > 1 else name
                    for name, n in sorted(tool_counts.items())
                )
                denial_suffix = f" [yellow]· brake blocked: {summary}[/yellow]"
            else:
                denial_suffix = ""
            if state == "done":
                return (
                    f"[cyan]✓[/cyan] [b]{cid}[/b] done "
                    f"[dim]({runtime:.1f}s{file_suffix})[/dim]"
                    f"{denial_suffix}"
                )
            if state == "failed":
                return (
                    f"[red]✗[/red] [b]{cid}[/b] failed "
                    f"[dim]({runtime:.1f}s)[/dim]"
                    f"{denial_suffix}"
                )
            if state == "killed":
                return (
                    f"[orange1]×[/orange1] [b]{cid}[/b] killed "
                    f"[dim]({runtime:.1f}s)[/dim]"
                    f"{denial_suffix}"
                )
            # Unknown terminal state — render the literal so we
            # notice if Construct grows a new state.
            return f"[dim]{cid} finalized: {state}[/dim]{denial_suffix}"

        if ptype == "spawn_failed":
            err = payload.get("error", "?")
            return f"[red b]✗[/red b] spawn failed: {err}"

        if ptype == "spawn_blocked":
            # Connection-aware refusal. Distinct from spawn_failed
            # because the cause is environmental (connection state),
            # not the spawn machinery itself. Yellow rather than red:
            # this is "wait, then retry" not "broken."
            reason = payload.get("reason", "blocked")
            task = payload.get("task", "")
            preview = task if len(task) < 40 else task[:37] + "..."
            return (
                f"[yellow b]⊘[/yellow b] spawn blocked: "
                f"[dim]{preview}[/dim] [yellow]· {reason}[/yellow]"
            )

        # Other meta types (run_start, run_end, etc) come through but
        # we don't need them in the chatlog right now.
        return None

    # Non-meta: it's an event from the construct's stream.
    if fevent.kind != "event":
        return None

    event_kind = payload.get("event_kind", "")
    raw = payload.get("raw", {})

    # System init: same boilerplate every spawn. Skip — the spawn line
    # already announced the construct.
    if event_kind == EventKind.SYSTEM_INIT:
        return None

    # Tool result: the tool_use that preceded it already told the
    # netrunner what was happening. Skip to avoid doubling every line.
    if event_kind == EventKind.TOOL_RESULT:
        return None

    # Tool use: render with the verb form we use elsewhere ("reading
    # auth.py", "running: grep ..."). This is the workhorse — most of
    # the chatlog will be these.
    if event_kind == EventKind.TOOL_USE:
        content = raw.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    verb = _describe_tool_use(block)
                    return f"[yellow]{cid}[/yellow] [dim]›[/dim] {verb}"
        return None

    # Thinking: include but truncate aggressively. Thinking is one of
    # the most useful debugging signals — it tells the netrunner what
    # the model is reasoning about before it acts.
    if event_kind == EventKind.THINKING:
        content = raw.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "thinking":
                    txt = block.get("thinking", "").strip().replace("\n", " ")
                    if txt:
                        # Sanity cap (500 default, 5000 in untruncated
                        # mode for the expand modal). Long enough that
                        # most thinking blocks land complete; short
                        # enough that the occasional 50K-char ramble
                        # doesn't blow up the chatlog. With wrap=False
                        # and W/A/S/D scroll, the netrunner can read
                        # the whole thing horizontally if needed.
                        return (
                            f"[dim yellow]{cid}[/dim yellow] "
                            f"[dim italic]thinking:[/dim italic] {txt[:cap]}"
                            + ("..." if len(txt) > cap else "")
                        )
        return None

    # Rate limit: surface only when not 'allowed' — the green-light
    # ones are noise.
    if event_kind == EventKind.RATE_LIMIT:
        info = raw.get("rate_limit_info", {})
        status = info.get("status", "?")
        if status == "allowed":
            return None
        kind = info.get("rateLimitType", "?")
        return (
            f"[yellow b]rate limit[/yellow b] "
            f"[dim]({cid})[/dim] {status} ({kind})"
        )

    # Result: Claude Code emits its own per-turn 'result' event. The
    # user-visible version of this is the meta finalized event. Skip
    # to avoid doubling.
    if event_kind == EventKind.RESULT or event_kind == EventKind.SYSTEM_RESULT:
        return None

    # Plain assistant text / user text events come through too. The
    # final response gets surfaced in the meta finalized payload as
    # final_output; intermediate text is usually less interesting.
    # Skip by default; if a netrunner finds they want it, easy to flip.
    if event_kind in (EventKind.USER, EventKind.ASSISTANT):
        return None

    return None


def chatlog_format_daemon(devent, *, untruncated: bool = False) -> "Optional[str]":
    """Render a DaemonEvent as a one-line chatlog entry, or None to skip.

    Daemon events are scarcer than fleet events but more important per
    line — they show the netrunner what the coordinator is thinking
    and deciding. Distinct color (green) so they stand out from
    construct-level chatter.

    untruncated=True relaxes per-event caps from 500/200 to 5000/2000
    for the expand modal — same rationale as chatlog_format_fleet."""
    cap_thinking = 5000 if untruncated else 500
    cap_chat = 2000 if untruncated else 200
    cap_error = 2000 if untruncated else 200
    kind = devent.kind
    payload = devent.payload

    if kind == "thinking":
        text = (payload.get("text") or "").strip().replace("\n", " ")
        if not text:
            return None
        return (
            f"[green]daemon[/green] "
            f"[dim italic]thinking:[/dim italic] {text[:cap_thinking]}"
            + ("..." if len(text) > cap_thinking else "")
        )

    if kind == "chat":
        text = (payload.get("text") or "").strip().replace("\n", " ")
        if not text:
            return None
        return (
            f"[green b]daemon[/green b] {text[:cap_chat]}"
            + ("..." if len(text) > cap_chat else "")
        )

    if kind == "status":
        # Status fires every turn. Surface only the meaningful ones —
        # the netrunner doesn't need a "working" line every spawn.
        status = payload.get("status", "?")
        if status in ("done", "failed"):
            color = "cyan" if status == "done" else "red"
            return (
                f"[{color} b]daemon[/{color} b] session {status}"
            )
        return None

    if kind == "error":
        text = payload.get("text", "?").replace("\n", " ")
        return (
            f"[red b]daemon error:[/red b] {text[:cap_error]}"
            + ("..." if len(text) > cap_error else "")
        )

    if kind == "action":
        # The fleet's "+ cx-X spawned" line already announces this from
        # the receiving end. Skipping here avoids the double-count
        # ("daemon: + spawn X" / "+ cx-X: X").
        return None

    # 'raw' and other internal kinds: skip.
    return None
