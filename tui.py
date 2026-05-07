"""
Cyberdeck M3: Textual TUI with keyboard navigation.

One pane per construct, live-updating as events arrive. Keyboard-driven:
Tab cycles focus, number keys jump, Enter expands, `k` kills, `n` (or
Space) spawns a new construct interactively, `q` / Ctrl+C quit.

Run:
    python tui.py "task 1" "task 2" [...]
    CLAUDE_BIN=./mock_claude.py python tui.py "task 1" "task 2"

Env:
    CLAUDE_BIN, CLAUDE_MODE, CYBERDECK_LOG — same as fleet.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

from rich.text import Text
from rich.syntax import Syntax

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    Label, Log, RichLog, Header, Footer, Static, Input, Button,
    TabbedContent, TabPane, ListView, ListItem,
)

from fleet import Fleet, FleetEvent
from construct import Construct, DEFAULT_TOOLS as CONSTRUCT_DEFAULT_TOOLS
from daemon import Daemon, DaemonEvent, DAEMON_SYSTEM_PROMPT
from daemon_session import DaemonSession
from display import (
    summarize,
    summarize_for_activity,
    chatlog_format_fleet,
    chatlog_format_daemon,
)
from profiles import Profile
from profile_registry import ProfileRegistry, ProfileEvent
from tools_registry import ToolRegistry, ToolEvent
from plugins import Plugin
from plugin_registry import PluginRegistry, PluginEvent
from session_manager import SessionManager, SessionPool, PoolEvent
from watchdog import (
    Watchdog,
    WatchdogQuestion,
    Blacklist,
    BlacklistEntry,
    WatchdogHistory,
    _fingerprint as _blacklist_fingerprint,
)
from tripwires import TripwireFire, TripwireAuthoringResult
from advisor import (
    Advisor,
    AdvisorTarget,
    AdvisorTurn,
    ADVISOR_MODEL,
    ADVISOR_EFFORT,
    target_from_tool,
    target_from_plugin,
)
from event_bus import EventBus, DeckEvent
from logger import DeckLogger
from connection_monitor import (
    ConnectionMonitor, ConnectionState, StateChangeEvent,
)
from brake_state import (
    BrakeState, BrakeStateStore, BrakeChangeEvent,
)
# Preferences is the canonical accessor for state.json contents
# (build-plan item 0b, 2026-05-04). brake_state.load_limits /
# save_limits stay exported for any non-tui caller (none today),
# but tui.py uses Preferences exclusively.
from preferences import Preferences
from brake_delay import (
    DelayEntry, DelayResolution, DelayMonitor,
    write_delay_override,
)
from attention import (
    AttentionItem, AttentionKind, AttentionResolved, AttentionResolution,
)
from logger import _serialize_payload
import clipboard
import json


STATE_STYLES = {
    "starting":   "yellow",
    "running":    "green",
    "done":       "cyan",
    "failed":     "red",
    "killed":     "orange1",
    # Inject-only state: the construct was killed mid-flight by an
    # interrupt-inject and its session continues in a new construct.
    # KILLED would also be technically true (the subprocess was
    # SIGTERM'd) but visually misleading — the netrunner pivoted, they
    # didn't give up. Same blue family as the chevron link below.
    "redirected": "bright_blue",
}


# Pane states from which no further action is meaningful: kill is a
# no-op (subprocess is gone), inject would resume a session that's
# either truly done or being continued by another construct (redirect
# follow-up). Keep this in one place — three gates use it and they
# kept drifting out of sync when new terminal-ish states landed.
TERMINAL_PANE_STATES = ("done", "failed", "killed", "redirected")


# RichLog widgets that should support space-to-expand. The set has two
# uses: (1) tells action_primary which RichLogs to route to ExpandModal
# (focused widget id must be in this set), and (2) provides the
# friendly title each one shows up under in the modal header.
#
# Pane logs (#pane_log inside ConstructPane) get expanded via a
# different code path because their id collides across panes — add
# explicit ConstructPane handling in action_primary if/when we want
# per-pane expand.
_EXPANDABLE_RICHLOGS: dict[str, str] = {
    "chatlog_log":  "Chatlog",
    "fleet_log":    "Fleet log",
    "watchdog_log": "Watchdog",
    "daemon_log":   "Daemon",
}


# ---- deck-protocol marker parsing -----------------------------------
# The cyberdeck dispatcher script (installed at <home>/tools/deck/
# cyberdeck.py at startup) emits magic marker lines on its stdout
# when a construct invokes it via Bash. We see those markers as part
# of tool_result text from constructs and act on them server-side
# (update Files panel etc), then strip them from the displayed text
# so the netrunner doesn't see the protocol bytes.
#
# Format (kept loose-prefix-tight-suffix so accidental collisions in
# normal output are vanishingly unlikely):
#   __CYBERDECK::v1::ACTION::PAYLOAD__
# - Version pinned to v1; bumps on wire-format breaks.
# - ACTION is uppercase letters + underscores.
# - PAYLOAD is everything after "::" up to the trailing "__".
#   PAYLOADs may contain arbitrary chars including ":" — the regex
#   uses non-greedy matching to avoid swallowing the closing "__".
import re as _re
_DECK_MARKER_RE = _re.compile(
    r"__CYBERDECK::v1::([A-Z_]+)::(.*?)__",
)


def _extract_deck_markers(text: str) -> tuple[list[tuple[str, str]], str]:
    """Pull all deck-protocol markers out of `text`. Returns
    (events, cleaned_text) where events is a list of
    (action, payload) tuples in occurrence order, and cleaned_text
    is the original with marker substrings removed.

    The text-cleaning is loose: marker lines often appear on a line
    by themselves but might be inline. We just remove the marker
    substring; if that leaves a blank line, downstream display logic
    handles whitespace normally.

    Best-effort. If the regex finds nothing, returns ([], text)
    unchanged."""
    if not text or "__CYBERDECK::" not in text:
        # Fast path: no markers. The substring check avoids regex
        # overhead on the 99%+ of tool_results that don't contain
        # protocol bytes.
        return [], text
    events: list[tuple[str, str]] = []

    def _capture(m: "_re.Match") -> str:
        events.append((m.group(1), m.group(2)))
        return ""

    cleaned = _DECK_MARKER_RE.sub(_capture, text)
    return events, cleaned


DAEMON_STATUS_STYLES = {
    "idle":     "dim",
    "starting": "yellow",
    "working":  "green",
    "waiting":  "yellow",
    "done":     "cyan",
    "failed":   "red",
    "stopped":  "orange1",
}


def _humanize_tokens(n: int) -> str:
    """Compact display for large token counts. 1,234,567 -> '1.2M'."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _wrap_words(words: tuple[str, ...] | list[str], max_width: int) -> list[str]:
    """Greedy word-wrap: pack words into lines no longer than max_width.

    Used by the Tools tab so the displayed tool list adjusts cleanly
    when the underlying constant grows or shrinks. Handles the edge
    case where a single word exceeds max_width by giving it its own
    line — it'll still wrap at the renderer level, but at least the
    other words don't get pulled into the overflow.
    """
    lines: list[str] = []
    current = ""
    for w in words:
        if not current:
            current = w
            continue
        candidate = current + " " + w
        if len(candidate) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def _expandable_title(widget) -> str:
    """Friendly title for the expand modal. Falls back to the widget's
    id (or '(log)' if no id) when not in our registered set — better
    something than nothing if the netrunner finds a way to focus an
    unregistered RichLog."""
    if widget.id and widget.id in _EXPANDABLE_RICHLOGS:
        return _EXPANDABLE_RICHLOGS[widget.id]
    return widget.id or "(log)"


def _resolve_home_dir(explicit: Optional[str]) -> Path:
    """Resolve the cyberdeck home directory and ensure it exists.

    Priority: explicit (CLI arg) > $CYBERDECK_HOME env var >
    `./cyberdeck-home` relative to the current working directory.

    Subdirectories the deck writes into (.cyberdeck/, logs/, etc.) get
    created lazily by the components that own them; this helper just
    guarantees the top-level home dir exists so those don't trip on a
    missing parent.

    The home dir is a SOFT sandbox, not a real one — Bash tool calls
    can `cd` anywhere they like once they're running. The point is to
    make the construct's default Read/Glob/Write surface point at a
    user data area instead of at the deck's own source code.
    """
    if explicit:
        home = Path(explicit).expanduser().resolve()
    else:
        env = os.environ.get("CYBERDECK_HOME")
        if env:
            home = Path(env).expanduser().resolve()
        else:
            home = (Path.cwd() / "cyberdeck-home").resolve()
    home.mkdir(parents=True, exist_ok=True)
    return home


class ProfileListItem(ListItem):
    """A focusable row in the Tools tab's profile list. Stashes a
    reference to the underlying Profile object so action handlers
    (the launch modal in C1g.4) can retrieve the data they need
    without doing a name → registry round-trip on every keypress.

    The visible content is a single Label rendering the row text
    (tier sigil + category/name + tool count). We compose with the
    Label child rather than using ListItem's bare-text constructor
    because we want full markup support."""

    def __init__(self, profile, label_markup: str) -> None:
        super().__init__(Label(label_markup, markup=True))
        self.profile = profile


class PluginListItem(ListItem):
    """A focusable row in the Tools tab's Plugins section. Stashes
    the underlying Plugin object so the launch modal (deferred) and
    z-magnify (which reads README.md inline) can find it without
    a registry round-trip.

    Distinct from ProfileListItem because the underlying types are
    different — Plugin carries a source_dir and a readme blob,
    Profile carries a daemon/construct addendum and recommended_tools."""

    def __init__(self, plugin, label_markup: str) -> None:
        super().__init__(Label(label_markup, markup=True))
        self.plugin = plugin


class ToolListItem(ListItem):
    """A focusable row in the unified Tools tab for a registry-backed
    binary or script tool. Tools-UI Thought of Dave (build-plan item
    0c) shipped 2026-05-04: wires space-launch and z-info on these
    rows.

    Pre-0c the tool rows were bare ListItem(Label(...)) with `.tool`
    set as an arbitrary attribute. Promoting to a real class makes
    `isinstance` dispatch in action_primary / action_expand work
    cleanly — same pattern as PluginListItem and FileListItem.

    Stashes the Tool dataclass so launch/info modals can read
    name + description + command + path + availability without a
    registry round-trip."""

    def __init__(self, tool, label_markup: str) -> None:
        super().__init__(Label(label_markup, markup=True))
        self.tool = tool


class ScriptListItem(ListItem):
    """A focusable row in the Tools tab's Scripts section. Each script
    lives at <home>/tools/<category>/<filename> and is invokable by
    constructs via Bash. C1g.4 wires space here to launch a follow-up
    construct that's been told about this specific script (a tool
    shortcut for "spin up a deck capability").

    Carries the absolute path so the launch wiring can reference it
    without disk re-scan, and category/name separately so display
    can render `<dim>category/</dim><cyan>name</cyan>` consistently
    with how profiles are shown."""

    def __init__(
        self,
        script_path: str,
        category: str,
        script_name: str,
    ) -> None:
        super().__init__(Label(
            f"[dim]{category}/[/dim][cyan]{script_name}[/cyan]",
            markup=True,
        ))
        self.script_path = script_path
        self.category = category
        # Stored as `script_name` not `name` because Textual's
        # Widget.name is a read-only property — we can't shadow it
        # without breaking widget identity.
        self.script_name = script_name


class FileListItem(ListItem):
    """A focusable row in the Files tab — one file touched (read /
    written / edited) by some construct during this run. Carries the
    path and the construct that produced it; C1g.4 wires space here
    to launch a follow-up construct with the file pre-loaded into the
    prompt (`FILE: <path>\\n\\n<your task>`).

    Path display vs. storage: `file_path` is the absolute path as the
    construct emitted it — preserved verbatim so the FILE: envelope
    sent to the next construct can be resolved without ambiguity.
    `display_path` is what the netrunner sees in the panel — shortened
    to `~/...` when the path lives under cyberdeck-home so the narrow
    Files column doesn't waste two thirds of its width on the home
    prefix. The caller (CyberdeckApp._append_files) computes the
    display version with App._shorten_path before constructing.

    De-duplication note: the Files tab can show the same path multiple
    times if multiple constructs touch it. That's intentional today —
    the construct_id column tells the netrunner *who* touched what,
    which is meaningful provenance. When that gets noisy, M5+ can add
    a 'group by path' toggle."""

    def __init__(
        self,
        construct_id: str,
        file_path: str,
        display_path: Optional[str] = None,
    ) -> None:
        # If the caller didn't pre-shorten, just show the raw path —
        # avoids needing home_dir context in the item itself.
        display = display_path if display_path is not None else file_path
        super().__init__(Label(
            f"[cyan]{construct_id}[/cyan]  [dim]→[/dim] {display}",
            markup=True,
        ))
        self.construct_id = construct_id
        self.file_path = file_path        # absolute, for FILE: envelope
        self.display_path = display       # what the netrunner sees


class GoalPane(Static, can_focus=True):
    """Displays the netrunner's current goal. Read-only in M4a; M4b
    will make it editable with propagation to the daemon."""

    DEFAULT_CSS = """
    GoalPane {
        border: round $accent;
        padding: 0 1;
        height: auto;
        min-height: 3;
        margin-bottom: 1;
    }
    GoalPane:focus {
        border: heavy $warning;
        background: $boost;
    }
    GoalPane > #goal_text {
        width: 100%;
        height: auto;
    }
    """

    def __init__(self, goal: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.goal = goal

    def compose(self) -> ComposeResult:
        yield Label("[b]YOUR GOAL[/b]", id="goal_title")
        yield Label(self.goal or "[dim](no goal set)[/dim]", id="goal_text")

    def set_goal(self, goal: str) -> None:
        self.goal = goal
        try:
            self.query_one("#goal_text", Label).update(
                goal or "[dim](no goal set)[/dim]"
            )
        except Exception:
            pass  # widget may not be mounted yet


class AttentionPanel(Static, can_focus=False):
    """Pending approval prompts the netrunner can resolve with X.

    Phase 2 of safety architecture pass slice 3 — consolidates
    proposal-shaped events that need a netrunner X-press to approve
    before they auto-expire. Today renders blacklist proposals
    (critical+bad_enough tripwires file these); future kinds plug
    in via the AttentionItem.kind dispatch on the App side.

    Sits at the top of #main, above the construct pool. When no
    items are pending it collapses to height 0 + display: none so
    it costs nothing visually. When items appear, the panel grows
    auto-height with one row per item: a colored exclamation glyph,
    the title (e.g. "blacklist: cx-... (rm_rf_destructive)"),
    countdown bar, and "press X to approve" hint.

    Not focusable — X dispatch goes through App.action_x_focused
    which checks attention items as a fallback after pane-delay
    matches. Multi-item resolution: if there's only one open item,
    X always lands on it. If there are multiple, X lands on the
    most recent (top of the panel).

    Re-renders fully on every refresh tick (every 100ms via the
    existing _refresh_delay_countdowns timer) so countdown bars
    drain smoothly. Cheap because the panel is small (1-3 items
    typical) and re-rendering a Static is just an update() call.
    """

    DEFAULT_CSS = """
    AttentionPanel {
        height: 0;
        display: none;
        padding: 0 1;
        margin-bottom: 0;
    }
    /* `.-active` flips on whenever there's at least one open item.
     * Heavy magenta border matches the per-pane delay overlay's
     * "this is time-sensitive" semantic — same color for the same
     * meaning, so the netrunner reads both surfaces uniformly. */
    AttentionPanel.-active {
        height: auto;
        display: block;
        border: heavy magenta;
        margin-bottom: 1;
    }
    """

    BAR_CELLS = 20

    def __init__(self, **kwargs) -> None:
        super().__init__("", **kwargs)
        self._items: list[AttentionItem] = []

    def update_items(self, items: list[AttentionItem]) -> None:
        """Replace the panel's contents. Caller passes the current
        full list of pending items; the panel renders all of them.
        Empty list → collapse via .-active class removal."""
        self._items = list(items)
        self.set_class(bool(self._items), "-active")
        self._paint()

    def refresh_countdown(self) -> None:
        """Re-render with current remaining_seconds/progress. Called
        by the App's 100ms refresh tick. No-op when empty."""
        if self._items:
            self._paint()

    def _paint(self) -> None:
        # Method name `_paint` (NOT `_render`) so we don't shadow
        # Textual's Widget._render. Same gotcha that caught
        # DelayListItem in the slice 3 phase 1 first attempt; filed
        # in cyberdeck-state.md.
        if not self._items:
            self.update("")
            return
        lines = ["[b magenta]ATTENTION NEEDED[/b magenta]"]
        for item in self._items:
            remaining = item.remaining_seconds
            progress = item.progress
            filled = max(0, self.BAR_CELLS - int(round(self.BAR_CELLS * progress)))
            bar = "█" * filled + "░" * (self.BAR_CELLS - filled)
            # Title is the kind-specific summary; detail is the
            # longer explanation (truncated). The X hint mirrors
            # the per-pane delay overlay's wording.
            detail = item.detail
            if len(detail) > 80:
                detail = detail[:77] + "..."
            lines.append(
                f"[b]{item.title}[/b]  "
                f"[magenta]{bar}[/magenta]  "
                f"[b]{remaining:.1f}s[/b]"
            )
            lines.append(
                f"[dim]{detail}  ·  press [b]X[/b] to approve[/dim]"
            )
        self.update("\n".join(lines))


class PoolMeter(Static, can_focus=False):
    """Visual indicator of the session pool's fill state.

    Shows a compact bar like `┃■■□┃ 2/3` where:
      ■ = warm session ready to pull (green)
      □ = empty slot — either warming in flight, or genuinely empty
          (dim; the user doesn't need to distinguish these visually,
           the count tells them everything they need to know)

    Output-only: not a focus target. Updates live via update_state()
    as PoolEvents flow in from the SessionPool."""

    DEFAULT_CSS = """
    PoolMeter {
        height: 1;
        padding: 0;
        margin-bottom: 1;
        color: $text;
    }
    """

    # Cell glyphs. Block chars render in any terminal that supports
    # Unicode (which is everything we care about — the deck runs on
    # Linux with a real terminal emulator).
    GLYPH_WARM  = "■"
    GLYPH_EMPTY = "□"

    def __init__(self, **kwargs) -> None:
        # Initialize with a non-empty placeholder; Textual's Visual
        # conversion chokes on truly empty Static content during
        # display-toggle render passes.
        super().__init__(" ", **kwargs)
        self._warm = 0
        self._warming = 0
        self._target = 0
        self._enabled = False

    def set_enabled(self, enabled: bool) -> None:
        """Hide the meter entirely when the pool is disabled (--no-pool).
        No point showing zero-of-zero on a small screen."""
        self._enabled = enabled
        self.display = enabled
        self._refresh_display()

    def update_state(self, warm: int, warming: int, target: int) -> None:
        """Refresh the display from current pool state."""
        self._warm = warm
        self._warming = warming
        self._target = target
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Re-render the visible content based on current state.
        (Named with _refresh prefix instead of _render because Textual's
        Widget._render is part of the rendering pipeline and must return
        a Visual; shadowing it crashes the app.)"""
        if not self._enabled or self._target == 0:
            self.update(" ")  # non-empty placeholder; empty trips Visual conversion
            return
        # Cells fill from the left: warm cells solid, then empty slots.
        # We don't visually distinguish "warming in flight" from "empty"
        # — the count text tells the user how many are ready. Adding a
        # third glyph cluttered the meter without adding info.
        warm_cells = self.GLYPH_WARM * self._warm
        empty_count = max(0, self._target - self._warm)
        empty_cells = self.GLYPH_EMPTY * empty_count

        bar = (
            f"[green]{warm_cells}[/green]"
            f"[dim]{empty_cells}[/dim]"
        )
        self.update(
            f"[b]POOL[/b]  ┃{bar}┃ "
            f"[dim]{self._warm}/{self._target}[/dim]"
        )
        # Force a repaint. Updates triggered from background asyncio
        # tasks (pool warming workers) sometimes don't trigger a visible
        # screen refresh until the user causes another event — explicit
        # refresh ensures the meter ticks live as cells warm up.
        self.refresh()


class DaemonPane(Static, can_focus=False):
    """Shows the daemon's ongoing activity — thinking, chat, status.
    Not focusable: it's an output surface, not an interaction target.
    Daemon interaction lives elsewhere (T to talk, e to edit goal)."""

    DEFAULT_CSS = """
    DaemonPane {
        height: 1fr;
        padding: 0 1;
    }
    DaemonPane > #daemon_header {
        height: 1;
    }
    DaemonPane > #daemon_log {
        height: 1fr;
        border: round $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.status = "idle"

    def compose(self) -> ComposeResult:
        yield Label("[b]DAEMON[/b]  [dim]\\[IDLE][/dim]", id="daemon_header")
        # wrap=False — see WatchdogPane.compose for the full reasoning.
        # Short version: now that this log lives in a TabbedContent,
        # writes can happen while the tab is inactive (size=0), and
        # wrap=True + min_width=1 caches Strips at 1-char-per-line
        # forever. wrap=False sidesteps that. Daemon thinking lines
        # are usually short; long ones get horizontal-scroll, which
        # is acceptable for a glance-only log.
        daemon_log = RichLog(
            id="daemon_log",
            max_lines=200,
            markup=True,
            wrap=False,
        )
        # Daemon log is focusable now (the bottom panel went tabbed
        # and the netrunner needs to be able to focus + magnify the
        # daemon log just like any other RichLog). Earlier this was
        # forced can_focus=False because the bottom region wasn't a
        # focus target at all; that's no longer true.
        yield daemon_log

    def set_status(self, status: str) -> None:
        self.status = status
        style = DAEMON_STATUS_STYLES.get(status, "white")
        try:
            self.query_one("#daemon_header", Label).update(
                f"[b]DAEMON[/b]  [{style}]\\[{status.upper()}][/{style}]"
            )
        except Exception:
            pass

    def write_thinking(self, text: str) -> None:
        self._write_line(f"[dim]› thinking:[/dim] {text}")

    def write_chat(self, text: str) -> None:
        self._write_line(f"[b]chat:[/b] {text}")

    def write_action(self, action: dict) -> None:
        atype = action.get("type", "?")
        if atype == "spawn":
            task = action.get("task", "")
            preview = task if len(task) < 50 else task[:47] + "..."
            self._write_line(f"[green]+[/green] spawn: {preview}")
        else:
            self._write_line(f"[dim]action:[/dim] {action}")

    def write_error(self, text: str) -> None:
        self._write_line(f"[red]error:[/red] {text}")

    def write_line(self, text: str) -> None:
        """Generic write to the daemon log, no formatting prefix."""
        self._write_line(text)

    def _write_line(self, line: str) -> None:
        try:
            self.query_one("#daemon_log", RichLog).write(line)
        except Exception:
            pass


class WatchdogPane(Static, can_focus=False):
    """Bottom-panel tab for the Watchdog Q&A history.

    Mirror of DaemonPane in shape: small status header on top
    (showing queue depth + busy state instead of daemon status), big
    RichLog filling the rest with the back-and-forth Q&A.

    Visual treatment differs from DaemonPane: yellow accent border
    (vs daemon's accent color) reinforces the soft/loud distinction
    between `t` (talk-watchdog, casual) and `T` (talk-daemon, loud)
    even in the panel chrome. Each Q&A pair gets a visual separator
    so you can scroll back through past questions cleanly."""

    DEFAULT_CSS = """
    WatchdogPane {
        height: 1fr;
        padding: 0 1;
    }
    WatchdogPane > #watchdog_header {
        height: 1;
    }
    WatchdogPane > #watchdog_log {
        height: 1fr;
        border: round $warning;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

    def compose(self) -> ComposeResult:
        yield Label(
            "[b]WATCHDOG[/b]  [dim]\\[IDLE][/dim]",
            id="watchdog_header",
        )
        # wrap=False is deliberate. With wrap=True + min_width=1, when
        # the watchdog tab is INACTIVE (default — daemon tab is
        # initial), writes to this log get pre-rendered at the
        # widget's current width, which is 0 or 1 px until the tab
        # activates. At width=1, RichLog wraps at 1 character per
        # line — turning "anything going on?" into eighteen lines of
        # one character each. The user reported seeing exactly this.
        # The pre-rendered Strips are cached, so even after the tab
        # activates and gets a real width, the question stays
        # character-per-line forever.
        #
        # wrap=False sidesteps this: long lines just stay long, and
        # the netrunner can scroll horizontally. In practice this
        # doesn't bite because answers come from the model with
        # paragraph-shaped newlines already in place — wrap was only
        # going to help for unusual cases where the model returned
        # one giant line. Worth the trade-off.
        watchdog_log = RichLog(
            id="watchdog_log",
            max_lines=500,
            markup=True,
            wrap=False,
        )
        yield watchdog_log

    def set_status(self, busy: bool, queue_depth: int) -> None:
        """Update the header's busy/queue annotation. Three states:
          - idle (no questions in flight or queued)
          - thinking (one in flight, none queued)
          - thinking + N queued (one in flight, N waiting)
        Dim color when idle; yellow when active."""
        if not busy and queue_depth == 0:
            text = "[dim]\\[IDLE][/dim]"
        elif busy and queue_depth == 0:
            text = "[yellow]\\[THINKING...][/yellow]"
        else:
            text = (
                f"[yellow]\\[THINKING... · {queue_depth} QUEUED][/yellow]"
            )
        try:
            self.query_one("#watchdog_header", Label).update(
                f"[b]WATCHDOG[/b]  {text}"
            )
        except Exception:
            pass

    def write_question(self, question: str) -> None:
        """A new question was submitted. Visual separator + Q line."""
        self._write_line(
            f"\n[dim]──────[/dim]  "
            f"[yellow]Q:[/yellow] {question}"
        )

    def write_answer(self, answer: str) -> None:
        """An answer arrived. Multi-paragraph answers get rendered
        on their own newlines (preserved here, unlike chatlog where
        they collapse into ¶ glyphs — the dedicated tab has the
        visual budget for proper paragraph breaks)."""
        # Normalize line endings; let RichLog wrap naturally.
        normalized = answer.replace("\r\n", "\n").strip()
        self._write_line(f"[bold]A:[/bold] {normalized}")

    def write_failure(self, error: str) -> None:
        """Watchdog returned an error instead of an answer."""
        self._write_line(f"[red]✗ failed:[/red] {error}")

    def write_history_separator(self, count: int) -> None:
        """Visual marker delimiting replayed prior-session Q&A from
        live current-session Q&A. Called once at startup before any
        prior entries are replayed; the count tells the netrunner
        how much they're looking at.

        Format chosen to be unmistakable but compact: a yellow-dimmed
        rule line with the count, so it reads as 'context, not new.'
        """
        self._write_line(
            f"[dim yellow]──── prior session ({count} entries) ────[/dim yellow]"
        )

    def write_live_session_marker(self) -> None:
        """Bookend after history replay, before live Q&A starts."""
        self._write_line(
            f"[dim yellow]──── live session ────[/dim yellow]"
        )

    def _write_line(self, line: str) -> None:
        try:
            self.query_one("#watchdog_log", RichLog).write(line)
        except Exception:
            pass


class ConstructPane(Static, can_focus=True):
    """One pane per construct: ID, state badge, current activity, event log.

    Starts in compact mode (header + current activity only, ~3 rows).
    Focus with Tab/number keys, expand with Enter, kill with k.
    """

    DEFAULT_CSS = """
    ConstructPane {
        border: round $accent;
        padding: 0 1;
        height: auto;
        margin-bottom: 1;
    }
    ConstructPane:focus {
        border: heavy $warning;
        background: $boost;
    }
    ConstructPane > #pane_header {
        height: 1;
    }
    /* Slice 3: variable-outcome delay overlay. Hidden by default; the
     * `.-delaying` class flips display:block + reserves two rows for
     * the countdown bar + the X-press hint. Pops out automatically
     * with the tool call (DelayMonitor → bus → pane.set_delay) so the
     * netrunner doesn't have to switch tabs to see what's pending. */
    ConstructPane > #pane_delay {
        height: 0;
        display: none;
    }
    ConstructPane.-delaying > #pane_delay {
        height: 2;
        display: block;
        margin-top: 0;
    }
    ConstructPane > #pane_current {
        height: 1;
        color: $text-muted;
    }
    ConstructPane > #pane_log {
        height: 0;
        border-top: dashed $panel;
        margin-top: 0;
        display: none;
    }
    ConstructPane.-expanded > #pane_log {
        height: 12;
        margin-top: 1;
        display: block;
    }
    /* Compact mode: terminal-state panes get out of the way. The
     * activity line vanishes (the construct isn't doing anything
     * anymore — it's done), the border dims, and we trim the spacing
     * below. The header remains, and the link chevrons (if any) stay
     * legible since they're often the most useful info on a finished
     * pane. Compact panes can still be expanded by Space/Enter; the
     * existing -expanded rule still applies on top. */
    ConstructPane.-compact {
        border: round $panel;
        margin-bottom: 0;
        text-style: dim;
    }
    ConstructPane.-compact > #pane_current {
        display: none;
        height: 0;
    }
    ConstructPane.-compact.-expanded > #pane_current {
        display: block;
        height: 1;
    }
    ConstructPane.-compact:focus {
        border: heavy $warning;
        text-style: not dim;
    }
    /* Brake-blocked treatment. Set when the construct's finalized
     * meta event reports non-empty permission_denials — the brake
     * hook caught one or more tool calls. Yellow border (matches
     * the brake indicator's paranoid color) so the pane visually
     * pops out from clean dones. Survives compact mode so a row of
     * finalized constructs stays readable: the blocked ones glow
     * yellow, the rest sit dim. Focus / expanded styles still take
     * priority — :focus already uses $warning, so a focused blocked
     * pane just looks like any other focused pane (correct: focus
     * is the more important signal). */
    ConstructPane.-blocked {
        border: round $warning;
    }
    ConstructPane.-blocked.-compact {
        border: round $warning 60%;
        text-style: not dim;
    }
    /* Blacklist-match treatment. Set when a Shift+K elsewhere added
     * a fingerprint that matches THIS construct's task — the construct
     * keeps running per netrunner direction (no auto-kill — at that
     * point we should be ejecting), but the pane gets visually flagged
     * so the netrunner can decide whether to k it manually. Red border
     * to differentiate from the yellow brake-blocked treatment: brake
     * is "this thing was caught by a static rule" (mechanical),
     * blacklist is "this thing matches what you just told us to ban"
     * (netrunner-authored). Compact + focus rules layer the same way
     * as -blocked. */
    ConstructPane.-blacklisted {
        border: round $error;
    }
    ConstructPane.-blacklisted.-compact {
        border: round $error 60%;
        text-style: not dim;
    }
    /* Slice 3: variable-outcome delay overlay. The pane gets a heavy
     * magenta border while a delay window is open — distinct from
     * yellow (brake-blocked / focused / paranoid indicator), red
     * (blacklisted / EJECT / errors), green (success), and the
     * default $accent. Magenta because the deck doesn't currently
     * use it for anything else, and "this is time-sensitive, act on
     * it or it auto-resolves" is exactly the kind of attention-needed
     * signal that wants its own color slot.
     *
     * Heavy weight (not round) so the overlay reads as MORE urgent
     * than the ambient brake-blocked yellow ring — a delaying pane
     * outranks a finalized blocked pane visually. Focus styles still
     * take priority (focus = $warning heavy yellow, more urgent than
     * a passive delay). */
    ConstructPane.-delaying {
        border: heavy magenta;
    }
    """

    state: reactive[str] = reactive("starting")
    expanded: reactive[bool] = reactive(False)
    # Compact mode is set after a construct has been terminal for a
    # grace period (see CyberdeckApp._compact_terminal_pane). The
    # watcher just toggles the CSS class — actual moving between
    # zones is the App's job since panes don't reach into siblings.
    compact: reactive[bool] = reactive(False)
    # Brake-blocked badge state. Set from set_denials() when the
    # finalize meta event carries a non-empty permission_denials
    # list. Just the rendered summary string, e.g. "Write×2, Bash×1"
    # — empty string means no denials, which suppresses the badge
    # AND the .-blocked class (see watcher below). Reactive so a
    # late finalize event repaints the header without manual call.
    denial_summary: reactive[str] = reactive("")
    # Slice 3 delay overlay. Carries the active DelayEntry when the
    # brake hook is holding a tool call from this construct, None
    # otherwise. The watcher toggles `.-delaying` (CSS pops the
    # overlay row in/out) and refreshes the rendered countdown.
    delay_entry: reactive[Optional[object]] = reactive(None)

    def __init__(
        self,
        construct_id: str,
        task: str,
        injected_from: Optional[str] = None,
        profile_name: Optional[str] = None,
        caliber: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.construct_id = construct_id
        self.cd_task = task
        self.files_written: list[str] = []
        # Profile name in effect for this construct. Shown as a small
        # yellow badge in the header so the netrunner can tell at a
        # glance which steering is active. None or "default" suppresses
        # the badge — only non-default profiles get visual weight.
        self.profile_name: Optional[str] = profile_name
        # Caliber Phase 5 (2026-05-04): the model + effort + fast-mode
        # bundle this construct ran at, as a pre-rendered display
        # string ("haiku·low" / "opus·xhigh" / "opus[4.6]·high·fast").
        # Sourced from the spawned meta event payload — fleet renders
        # `effective_caliber.display()` into the event, the TUI just
        # reads it back. None when no caliber metadata reached the
        # pane (legacy spawns / pool warming subprocesses); the
        # header suffix is suppressed in that case.
        self.caliber: Optional[str] = caliber
        # Inject linkage. injected_from is set at spawn time when this
        # construct is a turn-N+1 follow-up of an injected predecessor.
        # injected_to is set later, when an inject targets THIS construct
        # and a follow-up spawns from it. Either or both can be set
        # (chained injects: A → B → C).
        self.injected_from: Optional[str] = injected_from
        self.injected_to: Optional[str] = None
        # Mount-race guard. Textual's mount() is async — yielded
        # children become query-able after a compositor cycle, not
        # synchronously. For *fresh* constructs the subprocess startup
        # gives mount plenty of time to flush. For *resumed* sessions
        # (inject follow-ups), the server returns events almost
        # instantly because the conversation is cached, and they can
        # arrive before our children compose. In that race window,
        # query_one() raises NoMatches, the fleet listener loop
        # silently swallows the exception, and the pane appears empty
        # forever. The buffer absorbs events that hit before mount,
        # and on_mount flushes them. After mount the buffer stays
        # empty and add_event writes through directly.
        self._mounted: bool = False
        # Pre-mount event queue. Stores 4-tuples of (event_kind,
        # log_summary, activity_summary, raw) — same shape as the
        # raw_event_buffer's records minus log_summary because raw
        # gets re-formatted in render_buffer. on_mount drains this in
        # order through add_event, which writes through to the Log
        # AND appends to the buffer. raw is forwarded through.
        self._pending_events: list[tuple[str, str, Optional[str], Optional[dict]]] = []
        # Total add_event calls so far. Used as a "did this construct
        # actually produce anything" signal in the finalize handler.
        # Zero events from a state=done construct usually means a
        # corrupted prompt got through and claude bounced silently
        # (most often: Windows argv mangling on multiline tasks). We
        # surface this in the pane log so the netrunner sees the
        # anomaly instead of an inscrutably blank pane.
        self._event_count: int = 0
        # Mirror buffer for ExpandModal un-truncated re-render. Stores
        # tuples of (event_kind, log_summary, raw). raw is the full
        # stream-json dict when add_event was called with one — the
        # render_buffer method passes that back through summarize()
        # with untruncated=True so multi-paragraph thinking blocks
        # and big tool_result bodies show in full inside the modal,
        # rather than the same 500-char chop the live pane uses.
        # raw is None for synthetic events (anomaly markers, file
        # lists from set_files_written, etc.) — for those the
        # log_summary already IS the full content (always short),
        # so the buffer just hands it back as-is.
        # maxlen mirrors the live Log widget's max_lines so the modal
        # doesn't show events the live pane has dropped. 200 lines is
        # plenty for human glance review of a single construct's
        # session — overflow falls off the front in either view.
        self._raw_event_buffer: deque = deque(maxlen=200)

    def compose(self) -> ComposeResult:
        yield Label("", id="pane_header")
        # Slice 3 delay overlay. Hidden until set_delay(entry) is
        # called from a brake.delay_opened bus event; the bar drains
        # over delay_window_seconds while the netrunner has a chance
        # to press X. Sits between header and current so the tool
        # being delayed (which shows up in pane_current as a tool_use
        # event) is visually adjacent to the "you can override this"
        # affordance.
        yield Label("", id="pane_delay", markup=True)
        yield Label("", id="pane_current")
        yield Log(id="pane_log", max_lines=200, highlight=False)

    def on_mount(self) -> None:
        self._mounted = True
        self._refresh_header()
        # Drain any events that arrived during the mount race, in
        # order. add_event writes directly now that _mounted is True.
        if self._pending_events:
            buffered = self._pending_events
            self._pending_events = []
            for event_kind, log_summary, activity_summary, raw in buffered:
                self.add_event(
                    event_kind,
                    log_summary,
                    activity_summary,
                    raw=raw,
                )

    def watch_state(self, _old: str, _new: str) -> None:
        self._refresh_header()

    def watch_expanded(self, _old: bool, new: bool) -> None:
        self.set_class(new, "-expanded")

    def watch_compact(self, _old: bool, new: bool) -> None:
        self.set_class(new, "-compact")

    def watch_delay_entry(self, _old: object, new: object) -> None:
        # Toggle the .-delaying class (pops the overlay row open/closed)
        # and immediately render the current state. Refresh ticks
        # afterwards keep the bar drained without re-toggling the class.
        self.set_class(new is not None, "-delaying")
        self._refresh_delay_overlay()

    def set_delay(self, entry: object) -> None:
        """Open the delay overlay for this pane. Called from the App's
        brake.delay_opened handler with a DelayEntry whose construct_id
        matches this pane.

        Idempotent: setting twice for the same delay window just refreshes
        the rendered text. If a different DelayEntry comes in (rare —
        race between back-to-back delay windows), it overwrites cleanly."""
        self.delay_entry = entry

    def clear_delay(self) -> None:
        """Close the delay overlay. Called from the App's
        brake.delay_resolved handler. Safe to call when no delay is open."""
        self.delay_entry = None

    def refresh_delay_countdown(self) -> None:
        """Re-render the overlay's countdown text. Called periodically
        by the App's 100ms refresh timer. No-op when no delay is open."""
        if self.delay_entry is not None:
            self._refresh_delay_overlay()

    def _refresh_delay_overlay(self) -> None:
        """Render the bold-verb + bar + X-press hint into #pane_delay.

        Verb maps from default_action × brake intent:
          - default=allow (only happens under YOLO+delay) → "Running"
            (the call IS about to execute; X interrupts)
          - default=deny  (Default+destructive or Paranoid) → "Redirecting"
            (the call is about to bounce back to the construct as a
            tool_result.is_error; X overrides to approve)

        Bar drains over delay_window_seconds — full at open, empty at
        deadline. EJECT-style 20 cells with █ filled / ░ empty."""
        e = self.delay_entry
        if e is None:
            return
        # Verb based on what the default action is, not the override —
        # netrunner needs to see "what's about to happen" first, with
        # the X-press hint on the second line as the override option.
        if e.default_action == "allow":
            verb = "Running"
            bar_color = "red"   # caution: about to execute under YOLO
            x_action = "block"
        else:
            verb = "Redirecting"
            bar_color = "yellow"   # about to deny; less urgent
            x_action = "approve"
        BAR = 20
        progress = e.progress
        # Bar shows TIME REMAINING — full at open, empties as deadline
        # approaches. Mirrors EjectScreen's countdown bar rendering.
        filled = max(0, BAR - int(round(BAR * progress)))
        bar = "█" * filled + "░" * (BAR - filled)
        remaining = e.remaining_seconds
        text = (
            f"[b]{verb}[/b] in [b]{remaining:.1f}s[/b]  "
            f"[{bar_color}]{bar}[/{bar_color}]\n"
            f"[dim]press [b]X[/b] to {x_action}  "
            f"[bright_black]· brake={e.brake} · "
            f"{e.tool_name}[/bright_black][/dim]"
        )
        try:
            self.query_one("#pane_delay", Label).update(text)
        except Exception:
            # Mount race — overlay not rendered yet. Refresh tick will
            # try again. Safe to drop this paint.
            pass

    def set_injected_to(self, construct_id: str) -> None:
        """Mark this pane as having been redirected to a follow-up
        construct. Refreshes the header so the chevron link appears."""
        self.injected_to = construct_id
        self._refresh_header()

    def set_denials(self, denials: list) -> None:
        """Record the brake-hook denials this construct received.

        Called by the App's finalize handler with the
        permission_denials list off the finalize meta event payload.
        Computes a short summary ("Write×2, Bash×1") and stashes it
        in `denial_summary`; the reactive watcher applies the
        .-blocked CSS class and re-renders the header.

        Empty list (the common case — most constructs don't hit the
        brake) clears the summary and removes the class, so a pane
        that's been re-spawned doesn't carry stale denial state.
        """
        if not denials:
            self.denial_summary = ""
            return
        from collections import Counter
        counts = Counter(
            str(d.get("tool_name", "?"))
            for d in denials if isinstance(d, dict)
        )
        # Render in stable alphabetical order so repeated denials
        # don't flicker the badge text on each repaint.
        self.denial_summary = ", ".join(
            f"{name}×{n}" if n > 1 else name
            for name, n in sorted(counts.items())
        )

    def watch_denial_summary(self, value: str) -> None:
        """React to denial_summary changes by toggling the .-blocked
        CSS class and re-rendering the header. Empty string removes;
        non-empty applies."""
        if value:
            self.add_class("-blocked")
        else:
            self.remove_class("-blocked")
        # Header re-renders even when value is "" because the badge
        # might have been showing previously and needs to disappear.
        try:
            self._refresh_header()
        except Exception:
            # Pre-mount or torn down — ignore.
            pass

    def _refresh_header(self) -> None:
        style = STATE_STYLES.get(self.state, "white")
        header = self.query_one("#pane_header", Label)
        task_preview = self.cd_task if len(self.cd_task) < 60 else self.cd_task[:57] + "..."
        chev = "▼" if self.expanded else "▶"
        file_count = (
            f"  [cyan]→ {len(self.files_written)} file(s)[/cyan]"
            if self.files_written else ""
        )
        # Inject-link annotation. Two arrows because the chain has two
        # ends and a pane can be on either side (or both, in chained
        # injects). Outgoing (↪) points at the follow-up; incoming (↩)
        # points back at the originator. bright_blue matches the
        # REDIRECTED state color so the relationship reads visually.
        inject_link = ""
        if self.injected_to:
            inject_link += f"  [bright_blue]↪ {self.injected_to}[/bright_blue]"
        if self.injected_from:
            inject_link += f"  [bright_blue]↩ {self.injected_from}[/bright_blue]"
        # Profile badge. Skip when there's no profile or it's the
        # default — profiles only show up when they actually deviate
        # from baseline behavior, so the badge is signal not noise.
        profile_badge = ""
        if self.profile_name and self.profile_name != "default":
            profile_badge = f"  [yellow]\\[{self.profile_name}][/yellow]"
        # Caliber Phase 5 (2026-05-04): per-construct caliber suffix.
        # Cyan to match the header's identity color but dim because
        # caliber is metadata, not state — the netrunner reads it
        # secondarily after the [STATE] badge. Suppressed when no
        # caliber metadata reached the pane (legacy spawns).
        caliber_badge = ""
        if self.caliber:
            caliber_badge = (
                f"  [dim cyan]· {self.caliber}[/dim cyan]"
            )
        # Brake-blocked badge. Shows immediately after the state
        # badge (so its color visually amplifies the state's "wait,
        # something's off about this") with the warning glyph and
        # the per-tool count summary. Suppressed when there are no
        # denials, which is the common case. The .-blocked CSS class
        # also fires from the watcher, painting the pane border
        # yellow — the badge tells you WHAT got blocked, the border
        # tells you AT A GLANCE that something did. Two-channel
        # visibility per the netrunner's note ("we need to know
        # what's going on when managing ten constructs").
        denial_badge = ""
        if self.denial_summary:
            denial_badge = (
                f"  [yellow]\\[⚠ blocked: {self.denial_summary}]"
                f"[/yellow]"
            )
        header.update(
            f"[dim]{chev}[/dim] [b]{self.construct_id}[/b]  "
            f"[{style}]\\[{self.state.upper()}][/{style}]"
            f"{denial_badge}{profile_badge}{caliber_badge}  "
            f"[dim]{task_preview}[/dim]{file_count}{inject_link}"
        )

    def add_event(
        self,
        event_kind: str,
        log_summary: str,
        activity_summary: Optional[str] = None,
        *,
        raw: Optional[dict] = None,
    ) -> None:
        """Append to the pane's event log and refresh the activity line.

        log_summary feeds the expanded log (verbose, kept full).
        activity_summary feeds the always-visible "› ..." line. If
        None, the activity line is left alone (e.g., for events like
        tool_result that are noise on the activity line — the
        preceding tool_use was the informative signal).

        raw is the original stream-json dict (or None for synthetic
        events). When provided, the un-truncated re-render path in
        render_buffer can pass it back through summarize(untruncated=
        True) for the ExpandModal. Synthetic events without raw fall
        through to log_summary verbatim.
        """
        # Counted regardless of whether we write through or buffer.
        # The finalize handler reads this to detect "construct
        # produced zero events" (an anomaly worth surfacing).
        self._event_count += 1
        # Mirror buffer for the ExpandModal. Always append, regardless
        # of mount state — the modal pulls from the buffer, not the
        # live Log widget, so even pre-mount events end up in the
        # un-truncated view.
        self._raw_event_buffer.append((event_kind, log_summary, raw))
        # Pre-mount: stash for replay in on_mount. Without this, events
        # for resumed sessions get silently lost to NoMatches exceptions
        # on query_one (children aren't composed yet). After mount, this
        # branch never fires.
        if not self._mounted:
            self._pending_events.append(
                (event_kind, log_summary, activity_summary, raw)
            )
            return
        try:
            log = self.query_one("#pane_log", Log)
            log.write_line(f"{event_kind:14s} {log_summary}")
            if activity_summary is not None:
                current = self.query_one("#pane_current", Label)
                preview = (
                    activity_summary if len(activity_summary) < 120
                    else activity_summary[:117] + "..."
                )
                current.update(f"[dim]›[/dim] {preview}")
        except Exception:
            # Defensive: query_one can still fail in edge cases (pane
            # being torn down, etc.). Don't let one bad write kill the
            # event stream.
            pass

    def render_buffer(self, *, untruncated: bool = False) -> list:
        """Re-render the pane's event buffer as a list of Text objects
        for the ExpandModal. When untruncated is True, raw events get
        run back through summarize(untruncated=True) so long thinking
        blocks and big tool_results show in full. Synthetic events
        (raw=None) pass through with their log_summary verbatim.

        Returns rich.text.Text instances rather than plain strings so
        the modal renders consistently with the chatlog provider."""
        out: list = []
        for event_kind, log_summary, raw in self._raw_event_buffer:
            if raw is not None and untruncated:
                summary = summarize(raw, untruncated=True)
            else:
                summary = log_summary
            out.append(Text(f"{event_kind:14s} {summary}"))
        return out

    def set_files_written(self, files: list[str]) -> None:
        """Update the pane with the final list of files this construct
        created. Also dumps them into the pane log for a clear record.

        These writes go through add_event so the buffer captures them
        — synthetic event (raw=None) so the un-truncated re-render
        keeps the same wording. Without buffering, the modal would
        show every other event but mysteriously skip the file
        manifest, which is one of the more useful things to see in
        full."""
        self.files_written = list(files)
        self._refresh_header()
        if files:
            self.add_event(
                "files",
                f"wrote {len(files)}:",
                activity_summary=None,
            )
            for fp in files:
                # Each file gets its own buffered line — same layout
                # the live log used to write directly.
                self.add_event(
                    "",
                    f"  • {fp}",
                    activity_summary=None,
                )


class NewConstructScreen(ModalScreen[Optional[str]]):
    """Modal prompt for a new construct task. Returns the task string
    on submit, or None if cancelled."""

    CSS = """
    NewConstructScreen {
        align: center middle;
    }
    #dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #dialog > Label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("[b]Spawn new construct[/b]  [dim](Esc to cancel)[/dim]")
            yield Input(placeholder="task for the new construct...", id="task_input")

    def on_mount(self) -> None:
        self.query_one("#task_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        task = event.value.strip()
        self.dismiss(task or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class LaunchScreen(ModalScreen[Optional[str]]):
    """Launch modal triggered by space on a focused list item in the
    Tools or Files tab. Shows a context line describing what's being
    launched (profile name, file path), takes a single task input,
    returns the task string on submit (None on cancel).

    The caller (CyberdeckApp.action_primary) keeps the context object
    around and composes the full launch — for profiles, that means
    spawning with profile=ctx_profile; for files, prepending the
    `FILE: <path>\\n\\n` envelope to the task before spawning. The
    modal itself is context-agnostic past the header text — it only
    collects the user's task input.

    Single-input modal, so no Tab dance needed (the LimitsScreen
    cycle-on-tab fix lives at App level and would handle it for free
    if we ever add fields)."""

    CSS = """
    LaunchScreen {
        align: center middle;
    }
    #launch_dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    #launch_dialog > Label {
        margin-bottom: 1;
        width: 100%;
    }
    #launch_context {
        color: $accent;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, header_markup: str, context_markup: str) -> None:
        """header_markup is the bold title line ("Launch with profile",
        "Launch with file"). context_markup is the highlighted detail
        — profile name + tier + tool count, or file path + originating
        construct. Both are markup strings, rendered as Labels."""
        super().__init__()
        self.header_markup = header_markup
        self.context_markup = context_markup

    def compose(self) -> ComposeResult:
        with Vertical(id="launch_dialog"):
            yield Label(
                f"[b]{self.header_markup}[/b]  "
                f"[dim](Esc to cancel)[/dim]"
            )
            yield Label(self.context_markup, id="launch_context")
            yield Input(
                placeholder="task for the construct...",
                id="task_input",
            )

    def on_mount(self) -> None:
        self.query_one("#task_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        task = event.value.strip()
        self.dismiss(task or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class TalkDaemonScreen(ModalScreen[Optional[str]]):
    """Modal prompt for `T` (talk-to-daemon). Single Input field;
    returns the message string on submit, None on cancel.

    Counterpart to AskWatchdogScreen, deliberately distinct in tone:
      - Watchdog (`t`, lowercase): informational, async, free, yellow.
        "What's going on?" — observer query.
      - Daemon  (`T`, uppercase):  plan-affecting, sync-ish, expensive,
        green/primary. "Stop spawning fetches; switch to summarizing"
        — directive input.

    The visual treatment (primary border vs warning) mirrors the soft/
    loud distinction from the spec's `t` vs `T` design. Color and
    placeholder text reinforce that what you type here will steer the
    daemon's plan, not just sit in a question queue.

    Delivery model: messages are stashed on DaemonSession via
    set_pending_netrunner_message and surface to the daemon at the
    next outcome turn (same break-point as goal updates). If the
    netrunner sends multiple messages before the next break, they
    all stack and get delivered together. Force-push (interrupt
    in-flight turn) is M5+ — for now the wake event keeps idle
    delivery prompt.

    A small queue-depth hint shows pending messages (rare — usually
    zero, occasionally 1-2 if rapid-firing) so the netrunner sees
    "your message will arrive after these N already-queued ones."
    """

    CSS = """
    TalkDaemonScreen {
        align: center middle;
    }
    #talk_dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #talk_dialog > Label {
        margin-bottom: 1;
        width: 100%;
    }
    #talk_queue_hint {
        color: $primary;
    }
    #talk_no_session_hint {
        color: $error;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(
        self,
        pending_count: int = 0,
        session_running: bool = True,
    ) -> None:
        super().__init__()
        self.pending_count = pending_count
        self.session_running = session_running

    def compose(self) -> ComposeResult:
        with Vertical(id="talk_dialog"):
            yield Label(
                "[b]Talk to the Daemon[/b]  "
                "[dim](Esc to cancel · plan-affecting, sync)[/dim]"
            )
            if not self.session_running:
                # Without a session, there's no daemon to talk TO.
                # Surface the constraint clearly rather than letting
                # the netrunner type a message into the void. They
                # can still submit — the message gets dropped; we'll
                # tell them so post-submit too.
                yield Label(
                    "[b red]No daemon session is currently running.[/b red]  "
                    "Press [b]e[/b] to set a goal and start one — your "
                    "message will be lost if you submit now.",
                    id="talk_no_session_hint",
                )
            elif self.pending_count > 0:
                hint = (
                    f"[dim]queue:[/dim] {self.pending_count} message(s) "
                    "already pending delivery to the daemon — yours "
                    "joins them at the next natural break."
                )
                yield Label(hint, id="talk_queue_hint")
            yield Input(
                placeholder=(
                    "course-correct, ask, or add context — "
                    "the daemon weighs this on its next turn..."
                ),
                id="message_input",
            )

    def on_mount(self) -> None:
        self.query_one("#message_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        self.dismiss(message or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AskWatchdogScreen(ModalScreen[Optional[str]]):
    """Modal prompt for `t` (talk-to-watchdog). Single Input field;
    returns the question string on submit, None on cancel.

    Distinct from a daemon chat (`T`, future) by gravity — `t` is
    informational, async, free; `T` is plan-affecting, sync, expensive.
    The visual treatment is intentionally low-key (yellow accent vs
    daemon's green) to reinforce the "casual, not load-bearing"
    framing.

    A small queue-depth hint shows when there are already questions
    in flight so the netrunner knows "your question goes behind 2
    others" without having to peek at the chatlog. If queue is empty
    the hint is suppressed — no point telling them nothing's queued.
    """

    CSS = """
    AskWatchdogScreen {
        align: center middle;
    }
    #ask_dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $warning;
    }
    #ask_dialog > Label {
        margin-bottom: 1;
        width: 100%;
    }
    #ask_queue_hint {
        color: $warning;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, queue_depth: int = 0, busy: bool = False) -> None:
        super().__init__()
        self.queue_depth = queue_depth
        self.busy = busy

    def compose(self) -> ComposeResult:
        with Vertical(id="ask_dialog"):
            yield Label(
                "[b]Ask the Watchdog[/b]  "
                "[dim](Esc to cancel · async, informational)[/dim]"
            )
            # Queue-depth hint only renders when relevant. Computing
            # the wording here keeps the compose method declarative
            # and avoids a watch_*-style update later.
            ahead = self.queue_depth + (1 if self.busy else 0)
            if ahead > 0:
                hint = (
                    f"[dim]queue:[/dim] {ahead} question(s) ahead of "
                    "yours — your answer arrives in the chatlog when "
                    "they finish."
                )
                yield Label(hint, id="ask_queue_hint")
            yield Input(
                placeholder=(
                    "what's cx-A doing? did the daemon respawn anything? ..."
                ),
                id="question_input",
            )

    def on_mount(self) -> None:
        self.query_one("#question_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        self.dismiss(question or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AdvisorScreen(ModalScreen[None]):
    """The Advisor — narrowly-scoped Q&A for one tool or plugin.

    Spec framing (netrunner, 2026-05-05):
        "It is extremely specific — exclusively dedicated to
        informing you about that tool. It does nothing else and
        exclusively takes questions about the tool and its use
        cases. The most you can ask it is 'how can I use it to do
        X' and name other tools in the process."

    Tools-UI Thought of Dave sub-feature 3 (build-plan item 0c).
    Sub-features 1 (space-launch) + 2 (z-info) shipped 2026-05-04;
    this one closes the trio.

    Substrate: one Advisor instance per modal session, holding a
    `claude -p` one-shot subprocess pattern (see advisor.py). Each
    Q is sent fresh; prior Q&As get re-fed in the user prompt so
    follow-ups have context. Closing the modal kills the Advisor
    along with it — no persistence across re-opens. The netrunner
    can ask, learn, and move on; the deck doesn't grow a Q&A archive
    behind their back.

    Caliber: sonnet + medium (forced — see advisor.py). Originally
    haiku/low to keep tool advice cheap, but real-deck testing
    2026-05-05 caught Haiku/low losing track of its scope anchor
    on vague questions ("what is this plugin?" → asked for
    clarification despite being explicitly told the target).
    Sonnet/medium follows the scope rule reliably.

    Visual treatment: cyan accent (matches Tools tab energy), Input
    pinned at the bottom, scrollback above. Q lines yellow, A lines
    plain prose. `Esc` closes; `y` yanks the visible scrollback to
    the clipboard so the netrunner can paste a useful exchange into
    notes. Two non-resolved turns can stack while one's in flight
    (the Advisor serializes via its own asyncio.Lock).
    """

    CSS = """
    AdvisorScreen {
        align: center middle;
    }
    #advisor_dialog {
        width: 90%;
        height: 90%;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    #advisor_title {
        height: 1;
        margin-bottom: 1;
        color: $accent;
    }
    #advisor_subtitle {
        height: 1;
        margin-bottom: 1;
        color: $text-muted;
    }
    #advisor_log {
        height: 1fr;
        background: $surface;
    }
    #advisor_input {
        margin-top: 1;
    }
    #advisor_hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        # `h` toggles closed too — same key opens it from the
        # ExpandModal. Mirrors z/space pattern (open with same
        # key as close).
        Binding("h", "dismiss", "Close", show=False),
        # Yank the scrollback. Useful when the Advisor produces a
        # one-liner the netrunner wants to paste into notes or a
        # construct prompt.
        Binding("y", "copy", "Copy", show=True),
        # Scroll the log. w/s = line scroll, PgUp/PgDn = page scroll
        # — same convention as ExpandModal. The Input widget
        # consumes typing chars while it has focus, so w/s only fire
        # at the screen level when the netrunner has Tab'd out of
        # the input. Mirrors the deck-wide vim-shaped scroll story.
        Binding("w", "scroll_up", "↑", show=False),
        Binding("s", "scroll_down", "↓", show=False),
        Binding("pageup", "page_up", "Page ↑", show=False),
        Binding("pagedown", "page_down", "Page ↓", show=False),
    ]

    def __init__(
        self,
        target: AdvisorTarget,
        sibling_tool_names: tuple[str, ...] = (),
        claude_bin: str = "claude",
    ) -> None:
        super().__init__()
        self.advisor = Advisor(
            target=target,
            sibling_tool_names=sibling_tool_names,
            claude_bin=claude_bin,
        )
        # Pre-render the title once; it doesn't change per session.
        self._title = f"Advisor: {target.name}"
        self._subtitle = (
            f"{target.kind_label} · "
            f"{ADVISOR_MODEL}·{ADVISOR_EFFORT} · "
            f"scope: questions about {target.name} only"
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="advisor_dialog"):
            yield Label(f"[b]{self._title}[/b]", id="advisor_title")
            yield Label(self._subtitle, id="advisor_subtitle")
            yield RichLog(
                id="advisor_log",
                max_lines=2000,
                markup=True,
                wrap=True,
                min_width=40,
                auto_scroll=True,
            )
            yield Input(
                placeholder=(
                    "how can I use it to do X? … (Enter to ask, Esc to close)"
                ),
                id="advisor_input",
            )
            yield Label(
                "[dim]Esc / h close · Enter to ask · "
                "y copy · w/s line · PgUp/PgDn page · "
                "Tab to defocus input[/dim]",
                id="advisor_hint",
            )

    def on_mount(self) -> None:
        # Greet the netrunner so the empty modal isn't visually
        # confusing. The greeting echoes the scope rule the Advisor
        # itself enforces — sets expectations before the first Q.
        log = self._log()
        if log is not None:
            t = self.advisor.target
            avail = (
                "[green]available[/green]"
                if t.available else
                f"[red]unavailable[/red] ({t.unavailable_reason})"
            )
            log.write(
                f"[dim]Advisor scoped to[/dim] [b]{t.name}[/b] "
                f"· {avail}"
            )
            log.write(f"[dim]{t.description}[/dim]")
            log.write("")
            log.write(
                "[dim]Ask anything about how to use this tool. "
                "Off-topic questions are politely refused.[/dim]"
            )
        try:
            self.query_one("#advisor_input", Input).focus()
        except Exception:
            pass

    def _log(self) -> Optional[RichLog]:
        try:
            return self.query_one("#advisor_log", RichLog)
        except Exception:
            return None

    def action_scroll_up(self) -> None:
        # w/s only fire at screen level when Input doesn't have
        # focus (Input consumes typing chars first). Tab defocuses.
        log = self._log()
        if log is not None:
            log.scroll_up(animate=False)

    def action_scroll_down(self) -> None:
        log = self._log()
        if log is not None:
            log.scroll_down(animate=False)

    def action_page_up(self) -> None:
        log = self._log()
        if log is not None:
            log.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        log = self._log()
        if log is not None:
            log.scroll_page_down(animate=False)

    def action_copy(self) -> None:
        """y: yank the visible Q&A exchange to the clipboard.

        Format: plain text, "Q: ... / A: ..." per turn. The netrunner
        wants this when an answer is worth pasting into a construct
        prompt or notes file. Failed turns get a tagged line so the
        copy isn't silently lossy."""
        lines: list[str] = []
        for t in self.advisor.turns:
            lines.append(f"Q: {t.question}")
            if t.failed:
                lines.append(f"A: [advisor error: {t.error}]")
            elif t.answer is not None:
                lines.append(f"A: {t.answer}")
            else:
                lines.append("A: [pending]")
            lines.append("")
        if not lines:
            self.app.notify("nothing to copy yet", severity="warning")
            return
        text = "\n".join(lines).rstrip() + "\n"
        ok, err = clipboard.copy(text)
        if ok:
            self.app.notify(f"yanked {len(text)} chars to clipboard")
        else:
            self.app.notify(f"copy failed: {err}", severity="error")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        question = event.value.strip()
        if not question:
            return
        # Clear the input immediately so the netrunner can keep typing
        # before the answer comes back. Their next thought shouldn't
        # be blocked on the previous one resolving.
        try:
            self.query_one("#advisor_input", Input).value = ""
        except Exception:
            pass
        log = self._log()
        if log is not None:
            log.write(f"[yellow]Q:[/yellow] {question}")
            log.write("[dim]… thinking[/dim]")
        # Fire-and-forget worker. The Advisor serializes its own
        # subprocess work via asyncio.Lock, so even if the netrunner
        # spams Enter, only one claude -p runs at a time per Advisor.
        self.run_worker(
            self._ask_and_render(question),
            exclusive=False,
            group="advisor",
            description=f"advisor question: {question[:60]}",
        )

    async def _ask_and_render(self, question: str) -> None:
        """Worker body: send the question, render the result.

        We don't pass the turn handle through ask() — Advisor.ask()
        appends to its own internal turn list and returns the
        resolved turn. We just render whatever comes back.
        """
        try:
            turn = await self.advisor.ask(question)
        except Exception as e:
            log = self._log()
            if log is not None:
                # The "thinking" line is the most recent in the log;
                # we can't rewrite it cheaply (RichLog doesn't expose
                # line-edit), but appending an error keeps the
                # exchange readable. Same posture as a failed turn
                # below.
                log.write(f"[red]A: advisor crashed: {e}[/red]")
                log.write("")
            return
        log = self._log()
        if log is None:
            return
        if turn.failed:
            log.write(f"[red]A: advisor failed:[/red] {turn.error}")
        else:
            # Render the answer prose. The system prompt asks for
            # plain prose with optional fenced code blocks; both
            # render fine through RichLog with markup=True (the
            # answer text doesn't contain Rich markup tokens, and
            # Markdown's backticks don't conflict with [bracket]
            # markup syntax — RichLog ignores them as plain chars).
            log.write(f"[green]A:[/green] {turn.answer}")
        log.write("")


class GoalSetScreen(ModalScreen[Optional[str]]):
    """Modal prompt for setting (or replacing) the daemon's goal.
    Returns the goal text on submit, or None if cancelled."""

    CSS = """
    GoalSetScreen {
        align: center middle;
    }
    #goal_dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #goal_dialog > Label {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    def __init__(self, current_goal: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self.current_goal = current_goal

    def compose(self) -> ComposeResult:
        with Vertical(id="goal_dialog"):
            title = "Edit goal" if self.current_goal else "Set goal"
            yield Label(f"[b]{title}[/b]  [dim](Esc to cancel, Enter to commit)[/dim]")
            yield Input(
                value=self.current_goal,
                placeholder="what should the daemon do?",
                id="goal_input",
            )

    def on_mount(self) -> None:
        self.query_one("#goal_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        goal = event.value.strip()
        self.dismiss(goal or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class InjectScreen(ModalScreen[Optional[tuple[str, str, str]]]):
    """Compose-and-deliver modal for injection into a focused construct.
    Returns (mode, message, construct_id) on submit, or None on cancel.

    Mode is 'queue' or 'interrupt'. The opener pre-selects mode based on
    which key was pressed (q vs Q); inside the modal, `f` flips mode if
    the netrunner changes their mind mid-thought, matching the spec's
    soft/loud convention.

    The construct_id is threaded through the result so the handler can
    target the *original* construct that was focused at modal-open time.
    Re-reading focus after dismiss is wrong: focus may move while the
    modal is up (a finalize event auto-focuses the next pane, the user
    Tabs while typing, etc.) and the inject would land on the wrong
    construct."""

    CSS = """
    InjectScreen {
        align: center middle;
    }
    #inject_dialog {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #inject_dialog > Label {
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    #inject_mode_label {
        text-style: bold;
    }
    #inject_target_label {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        # No F-key flip-mode binding here. The Input child widget grabs
        # all alphabetic keys for text entry, so a Binding on 'f' would
        # never fire — the user's 'f' would just type the letter into
        # the message. q/Q already pre-select the mode at modal-open
        # time; flipping mid-modal is a feature nobody needs.
    ]

    def __init__(
        self,
        construct_id: str,
        construct_task: str,
        mode: str = "queue",
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.construct_id = construct_id
        self.construct_task = construct_task
        # Validate mode; fall back to queue if caller passed garbage
        self.mode = mode if mode in ("queue", "interrupt") else "queue"

    def compose(self) -> ComposeResult:
        with Vertical(id="inject_dialog"):
            yield Label(self._mode_label_text(), id="inject_mode_label")
            yield Label(
                f"target: [b]{self.construct_id}[/b]  "
                f"[dim]({self._task_preview()})[/dim]",
                id="inject_target_label",
            )
            yield Input(
                placeholder="message to inject...",
                id="inject_input",
            )
            yield Label(
                "[b]Enter[/b] send  ·  [b]Esc[/b] cancel",
            )

    def _task_preview(self) -> str:
        return (
            self.construct_task[:50] + "..."
            if len(self.construct_task) > 50
            else self.construct_task
        )

    def _mode_label_text(self) -> str:
        if self.mode == "interrupt":
            return (
                "[red b]INTERRUPT-INJECT[/red b]  "
                "[dim](kill current work, redirect with new message)[/dim]"
            )
        return (
            "[yellow b]QUEUE-INJECT[/yellow b]  "
            "[dim](deliver at next natural break)[/dim]"
        )

    def on_mount(self) -> None:
        self.query_one("#inject_input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        if not message:
            self.dismiss(None)
            return
        # Include the originally-targeted construct_id so the handler
        # doesn't have to re-derive it from focus (which may have
        # shifted while the modal was up).
        self.dismiss((self.mode, message, self.construct_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class EffortPickerScreen(ModalScreen[Optional[str]]):
    """Reusable effort-level picker — mirrors Claude Code client's
    1-5 effort selector. Press 1-5 to pick low/medium/high/xhigh/max;
    Esc to cancel.

    Used by LimitsScreen for daemon effort selection (Caliber Phase 3,
    2026-05-04). Same modal will surface for manual-construct
    creation when the caliber-pick UI lands — keeping this one
    dedicated and parameterized means we don't duplicate the
    effort-blurb panel in two places.

    Returns the selected effort string ("low" / "medium" / "high" /
    "xhigh" / "max") on dismiss, or None on cancel.

    Effort guidance (paraphrased from Anthropic's docs +
    netrunner's framing 2026-05-04):
      - low/medium/high are mostly LITERAL — model executes the
        task as written without extra reasoning depth
      - xhigh/max shift toward CONCEPTUAL / abstract reasoning —
        model thinks about the problem itself before acting
    """

    CSS = """
    EffortPickerScreen {
        align: center middle;
    }
    #effort_picker {
        width: 80;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #effort_picker_title {
        text-style: bold;
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    /* Each row is a Label with class effort_row (no nested Label
       container). The previous `.effort_row > Label` selector
       didn't match — these rules apply to the Label-as-row directly.
       Width 100% + height auto so longer descriptions wrap cleanly
       inside the 80-wide modal instead of overflowing. */
    .effort_row {
        width: 100%;
        height: auto;
        margin-bottom: 0;
    }
    .effort_current {
        background: $boost;
    }
    """

    BINDINGS = [
        Binding("1", "pick_low", "Low", show=True),
        Binding("2", "pick_medium", "Medium", show=True),
        Binding("3", "pick_high", "High", show=True),
        Binding("4", "pick_xhigh", "xHigh", show=True),
        Binding("5", "pick_max", "Max", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    # Per-level guidance. Tuned to fit on one line inside the
    # 80-wide modal without wrapping (preserves clean column
    # alignment); netrunner can read full Anthropic docs for the
    # long version. Order matters — rendered as 1..5.
    LEVELS: tuple[tuple[str, str], ...] = (
        ("low",
         "fast, cheap; short scoped tasks (literal)"),
        ("medium",
         "balanced; moderate token savings (literal)"),
        ("high",
         "API default; strong reasoning (literal)"),
        ("xhigh",
         "long-horizon coding/agentic (conceptual; Opus 4.7)"),
        ("max",
         "frontier problems; deepest reasoning (abstract)"),
    )

    def __init__(
        self,
        *,
        title: str = "Set Effort",
        current_effort: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.title_text = title
        self.current_effort = current_effort

    def compose(self) -> ComposeResult:
        with Vertical(id="effort_picker"):
            yield Label(f"[b]{self.title_text}[/b]", id="effort_picker_title")
            for i, (level, blurb) in enumerate(self.LEVELS, start=1):
                marker = "★" if level == self.current_effort else " "
                cls = "effort_row"
                if level == self.current_effort:
                    cls = "effort_row effort_current"
                yield Label(
                    f"  [b cyan]\\[{i}][/b cyan]  "
                    f"[b]{level.upper():<7}[/b] {marker} "
                    f"[dim]— {blurb}[/dim]",
                    classes=cls,
                )
            yield Label(
                "[dim]1-5 to pick · Esc to cancel[/dim]",
            )

    def action_pick_low(self) -> None:
        self.dismiss("low")

    def action_pick_medium(self) -> None:
        self.dismiss("medium")

    def action_pick_high(self) -> None:
        self.dismiss("high")

    def action_pick_xhigh(self) -> None:
        self.dismiss("xhigh")

    def action_pick_max(self) -> None:
        self.dismiss("max")

    def action_cancel(self) -> None:
        self.dismiss(None)


class LimitsScreen(ModalScreen[Optional[dict]]):
    """View and adjust runtime caps for the active session.

    Returns a dict of {field_name: new_value} on submit, or None on
    cancel. Caller applies the changes.

    v1 scope: user-triggered viewer/adjuster only. Pause-on-cap-hit
    behavior (auto-open + resume semantics) is deferred until the
    daemon grows a 'paused' state — that bleeds into goal-edit
    territory which we're holding off on. For now: user presses `l`,
    sees current caps, adjusts what they want, accepts. If the
    fleet has ALREADY hit a cap and halted, raising the cap won't
    auto-restart the session — they need to set a new goal."""

    CSS = """
    LimitsScreen {
        align: center middle;
    }
    #limits_dialog {
        width: 100;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #limits_dialog > Label {
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    #limits_title {
        text-style: bold;
    }
    #limits_columns {
        height: auto;
    }
    .limits_col {
        width: 50%;
        height: auto;
        padding: 0 1;
    }
    /* Nested labels inside the columns need width:100% explicitly —
       the `#limits_dialog > Label` rule above only applies to direct
       children. Without these the right column was overflowing the
       50% container instead of wrapping. */
    .limits_col > Label,
    .limits_col_title,
    .power_line {
        width: 100%;
        height: auto;
    }
    .limits_col_title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .limits_row {
        height: 3;
        margin-bottom: 0;
    }
    .limits_row > Label {
        width: 28;
        height: 3;
        content-align: left middle;
    }
    .limits_row > Input {
        width: 14;
    }
    .power_line {
        margin-bottom: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Save", show=True),
        # priority=True so the binding fires even when one of the
        # numeric Input widgets has focus. Without priority, the
        # focused Input swallows the `e` keystroke (it gets validated
        # against type="integer" and silently dropped — the parent
        # screen never sees it). Same pattern as Ctrl+S / Esc, which
        # are already handled by Textual's keymap because they're
        # special keys; bare-letter bindings need the priority hint.
        Binding(
            "e", "open_effort_picker", "Set daemon effort",
            show=True, priority=True,
        ),
        # F toggles the deck-wide fast_mode governor. Same priority
        # rationale as `e` above — Inputs would swallow it otherwise.
        # Toggle is in-modal only; commit happens on Ctrl+S/Enter
        # like every other field. The label updates live so the
        # netrunner sees the new state before submitting.
        Binding(
            "f", "toggle_fast_mode", "Toggle fast mode",
            show=True, priority=True,
        ),
    ]

    def __init__(
        self,
        max_concurrent: int,
        max_total_spawns: int,
        pool_size: int,
        wedge_timeout_seconds: float,
        delay_window_seconds: float,
        daemon_effort: str,
        fast_mode: bool,
        current_live: int,
        current_spawned: int,
        cost_so_far: float,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.initial_max_concurrent = max_concurrent
        self.initial_max_total_spawns = max_total_spawns
        self.initial_pool_size = pool_size
        self.initial_wedge_timeout_seconds = wedge_timeout_seconds
        self.initial_delay_window_seconds = delay_window_seconds
        # Caliber Phase 3 (scoped down, 2026-05-04): daemon effort is
        # the netrunner's power-level knob for the daemon's own
        # subprocess. Model is pinned to opus by design (daemon is a
        # manager, not a swappable agent). Effort applies on next
        # goal start — the streaming subprocess bakes its caliber at
        # spawn time. Modified state lives in `self.daemon_effort`;
        # action_submit reads it back into the result dict.
        self.daemon_effort = daemon_effort
        self.fast_mode = fast_mode
        self.current_live = current_live
        self.current_spawned = current_spawned
        self.cost_so_far = cost_so_far

    def compose(self) -> ComposeResult:
        with Vertical(id="limits_dialog"):
            yield Label("[b]LIMITS[/b]", id="limits_title")
            yield Label(
                f"Currently: [cyan]{self.current_live}[/cyan] live  ·  "
                f"[cyan]{self.current_spawned}[/cyan] total spawned  ·  "
                f"[cyan]${self.cost_so_far:.4f}[/cyan] cost"
            )
            yield Label(
                "[dim]Adjust caps + power levels. Tab between fields, "
                "Enter or Ctrl+S to save, Esc to cancel. "
                "[reverse b] E [/reverse b] daemon effort  ·  "
                "[reverse b] F [/reverse b] fast-mode toggle[/dim]"
            )
            with Horizontal(id="limits_columns"):
                # ---- Left column: numeric caps -----------------------
                with Vertical(classes="limits_col"):
                    yield Label("CAPS", classes="limits_col_title")
                    with Horizontal(classes="limits_row"):
                        yield Label("max concurrent:")
                        yield Input(
                            value=str(self.initial_max_concurrent),
                            id="input_max_concurrent",
                            # type="integer" restricts keystrokes to
                            # digits + optional leading minus.
                            # Editing keys (backspace, arrows, etc.)
                            # still work. Saves us from validating
                            # in action_submit() — bad input can't
                            # be entered in the first place.
                            type="integer",
                        )
                    with Horizontal(classes="limits_row"):
                        yield Label("max total spawns:")
                        yield Input(
                            value=str(self.initial_max_total_spawns),
                            id="input_max_total_spawns",
                            type="integer",
                        )
                    with Horizontal(classes="limits_row"):
                        yield Label("pool size:")
                        yield Input(
                            value=str(self.initial_pool_size),
                            id="input_pool_size",
                            type="integer",
                        )
                    with Horizontal(classes="limits_row"):
                        yield Label("wedge timeout (s):")
                        yield Input(
                            value=str(int(
                                self.initial_wedge_timeout_seconds
                            )),
                            id="input_wedge_timeout_seconds",
                            type="integer",
                        )
                    with Horizontal(classes="limits_row"):
                        yield Label("delay window (s):")
                        yield Input(
                            value=str(int(
                                self.initial_delay_window_seconds
                            )),
                            id="input_delay_window_seconds",
                            type="integer",
                        )
                # ---- Right column: power levels ----------------------
                # Standardized 2026-05-07 (UI-polish micro-pass): both
                # daemon-effort and fast-mode render as parallel rows
                # with a small keycap-styled letter prefix. Pre-fix,
                # daemon-effort had a primary-variant Button while
                # fast-mode was just a Label — visually asymmetric.
                # Now both are Labels with `[reverse b] X [/reverse b]`
                # keycap markers and the same shape.
                with Vertical(classes="limits_col"):
                    yield Label(
                        "POWER LEVELS", classes="limits_col_title",
                    )
                    yield Label(
                        self._daemon_caliber_label_text(),
                        id="daemon_caliber_label",
                        classes="power_line",
                    )
                    yield Label(
                        self._fast_mode_label_text(),
                        id="fast_mode_label",
                        classes="power_line",
                    )
                    yield Label(
                        "[dim]Construct calibers are daemon-controlled "
                        "per task (haiku/sonnet/opus + matching "
                        "effort). Pool warms at sonnet·high; "
                        "non-default-caliber spawns fall through to "
                        "fresh.[/dim]",
                        classes="power_line",
                    )
                    yield Label(
                        "[b]Effort guidance:[/b]\n"
                        "[dim]low/medium/high → mostly literal "
                        "execution. xhigh/max → conceptual/abstract "
                        "reasoning. high is the API default and the "
                        "sweet spot for routine work.[/dim]",
                        classes="power_line",
                    )
            yield Label(
                "[dim]Caps notes: max_concurrent and max_total_spawns "
                "each accept 0 = 'no cap' (burst spawn + cloud quota "
                "is a real cost). pool_size is pre-warmed claude "
                "subprocesses kept hot for snappy spawns; 0 disables. "
                "wedge_timeout is the post-stdout-close ceiling "
                "before fleet force-kills (kill_source=fleet_wedge_"
                "timeout); 0 disables (debug only). delay_window is "
                "the variable-outcome pause UX (slice 3, default 5s) "
                "— brake hook holds interesting tool calls for N "
                "seconds watching for an X-press override. All "
                "persist across restarts.[/dim]"
            )

    def on_mount(self) -> None:
        # Focus the first input so user can immediately type/edit
        try:
            self.query_one("#input_max_concurrent", Input).focus()
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter on any input commits all values
        self.action_submit()

    def action_submit(self) -> None:
        """Parse inputs, validate, return dict of updates to apply.
        On any parse/validation error, reject silently (caller leaves
        state untouched). Defensive design — bad input shouldn't
        break the modal."""
        try:
            mc_str = self.query_one("#input_max_concurrent", Input).value.strip()
            mts_str = self.query_one("#input_max_total_spawns", Input).value.strip()
            ps_str = self.query_one("#input_pool_size", Input).value.strip()
            wt_str = self.query_one("#input_wedge_timeout_seconds", Input).value.strip()
            dw_str = self.query_one("#input_delay_window_seconds", Input).value.strip()
            mc = int(mc_str)
            mts = int(mts_str)
            ps = int(ps_str)
            wt = int(wt_str)
            dw = int(dw_str)
        except (ValueError, AttributeError):
            # Bad input — toast back via dismissal of None. Caller
            # will treat as cancel; user can re-open if desired.
            self.dismiss(None)
            return

        # Lower-bound clamps only. The previous "max_concurrent <= 9"
        # ceiling came from the number-key construct-jump real estate
        # (Ctrl+1-9); netrunner doesn't actually use those bindings,
        # and the upcoming UI rework removes the rationale entirely.
        # Uncapped from 2026-04-30 onward.
        #
        # Semantics:
        #   max_concurrent: must be >= 1 (0 concurrent makes no sense;
        #     0 spawns is what max_total_spawns=0-and-no-spawns gets you)
        #   max_total_spawns: 0 = "no cap"; otherwise must be >= 0
        #   pool_size: 0 = "no warming" (pool effectively disabled);
        #     otherwise the target count of hot pre-warmed sessions
        #   wedge_timeout_seconds: 0 = "no timeout" (debug only — a
        #     real wedge will block fleet shutdown indefinitely);
        #     otherwise the post-stdout-close ceiling before fleet
        #     force-kills with kill_source="fleet_wedge_timeout".
        if mc < 1:
            mc = 1
        if mts < 0:
            mts = 0
        if ps < 0:
            ps = 0
        if wt < 0:
            wt = 0
        if dw < 0:
            dw = 0

        self.dismiss({
            "max_concurrent": mc,
            "max_total_spawns": mts,
            "pool_size": ps,
            "wedge_timeout_seconds": float(wt),
            "delay_window_seconds": float(dw),
            "daemon_effort": self.daemon_effort,
            "fast_mode": self.fast_mode,
        })

    def _daemon_caliber_label_text(self) -> str:
        """Compose the daemon-effort row. Keycap-prefix styling
        matches the fast-mode row below (UI-polish standardization
        2026-05-07). Extracted so the EffortPickerScreen callback
        can re-render in place after a pick without duplicating
        the markup string."""
        return (
            f"[reverse b] E [/reverse b]  Daemon: [b]opus[/b] · "
            f"[cyan b]{self.daemon_effort}[/cyan b]\n"
            f"[dim]model pinned (manager role)[/dim]"
        )

    def _fast_mode_label_text(self) -> str:
        """Compose the fast-mode row. Keycap-prefix styling matches
        the daemon-effort row above. Extracted so
        action_toggle_fast_mode can re-render in place after a flip
        without duplicating the markup string."""
        fm_text = (
            "[green]ON[/green]" if self.fast_mode
            else "[dim]off[/dim]"
        )
        return (
            f"[reverse b] F [/reverse b]  Fast-mode: {fm_text}\n"
            f"[dim]Opus 4.6 only · 6x cost / 2.5x speed[/dim]"
        )

    def action_toggle_fast_mode(self) -> None:
        """Flip the fast-mode governor in-modal. Updates the label
        live; commit happens with the rest of the modal state on
        Ctrl+S / Enter (action_submit puts fast_mode in the dismiss
        dict and _handle_limits_submitted persists via prefs.save).
        Cancel (Esc) discards the toggle along with everything else."""
        self.fast_mode = not self.fast_mode
        try:
            lbl = self.query_one("#fast_mode_label", Label)
            lbl.update(self._fast_mode_label_text())
        except Exception:
            pass

    def action_open_effort_picker(self) -> None:
        """Push the EffortPickerScreen for daemon effort selection.
        Selection persists into self.daemon_effort + the displayed
        Daemon row updates. The actual apply-to-deck happens when
        the netrunner submits the LimitsScreen (Ctrl+S / Enter)."""
        def _picked(result: Optional[str]) -> None:
            if result is None:
                return
            self.daemon_effort = result
            try:
                lbl = self.query_one("#daemon_caliber_label", Label)
                lbl.update(self._daemon_caliber_label_text())
            except Exception:
                pass

        self.app.push_screen(
            EffortPickerScreen(
                title="Set Daemon Effort",
                current_effort=self.daemon_effort,
            ),
            _picked,
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


class YoloConfirmScreen(ModalScreen[bool]):
    """Deliberate-consent confirmation for moving brake to YOLO.

    Mirrors EjectScreen exactly in shape — countdown bar drains over
    CONFIRM_WINDOW_SECS, Space within the window confirms, Esc or
    timeout cancels. Same 3-second window as EJECT for muscle memory;
    the gravity is different (YOLO doesn't kill anything immediately,
    it grants every future spawn full latitude) but the gesture should
    feel identical so the netrunner doesn't have to relearn the
    timing.

    Returns True if confirmed, False otherwise."""

    CSS = """
    YoloConfirmScreen {
        align: center middle;
    }
    #yolo_dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $error;
    }
    #yolo_dialog > Label {
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    #yolo_title {
        color: $error;
        text-style: bold;
    }
    #yolo_countdown {
        color: $warning;
    }
    """

    BINDINGS = [
        Binding("space", "confirm", "Confirm YOLO", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    CONFIRM_WINDOW_SECS = 3.0
    TICK_INTERVAL_SECS = 0.05

    def compose(self) -> ComposeResult:
        with Vertical(id="yolo_dialog"):
            yield Label("⚠  YOLO CONFIRMATION  ⚠", id="yolo_title")
            yield Label(
                "All future construct spawns will run with no brake. "
                "No path filtering, no destructive-command pattern "
                "matching, no plugin gating.\nEject is your only stop.",
            )
            yield Label("", id="yolo_countdown")
            yield Label(
                "[b]SPACE[/b] to confirm  ·  [b]ESC[/b] to cancel",
            )

    def on_mount(self) -> None:
        self._elapsed = 0.0
        self._tick_timer = self.set_interval(
            self.TICK_INTERVAL_SECS, self._tick
        )
        self._refresh_countdown()

    def _tick(self) -> None:
        self._elapsed += self.TICK_INTERVAL_SECS
        if self._elapsed >= self.CONFIRM_WINDOW_SECS:
            self._tick_timer.stop()
            self.dismiss(False)
            return
        self._refresh_countdown()

    def _refresh_countdown(self) -> None:
        remaining = max(0.0, self.CONFIRM_WINDOW_SECS - self._elapsed)
        cell_count = 20
        filled = int(round(cell_count * remaining / self.CONFIRM_WINDOW_SECS))
        bar = "█" * filled + "░" * (cell_count - filled)
        try:
            self.query_one("#yolo_countdown", Label).update(
                f"[red]{bar}[/red]  [dim]{remaining:.1f}s remaining[/dim]"
            )
        except Exception:
            pass

    def action_confirm(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        self.dismiss(True)

    def action_cancel(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        self.dismiss(False)


class BrakeScreen(ModalScreen[Optional["BrakeState"]]):
    """Modal for changing the deck-global brake state.

    Three options; each bound to its first letter (P/D/Y) for fast
    selection. Selecting Paranoid or Default commits immediately.
    Selecting YOLO opens YoloConfirmScreen as a sub-modal — required
    deliberate-consent gesture, since YOLO removes all runtime gating.

    Returns the new BrakeState on confirmed change, or None on
    cancel. The caller (CyberdeckApp._handle_brake_submitted) is
    responsible for actually mutating the BrakeStateStore — this
    modal is purely UI."""

    CSS = """
    BrakeScreen {
        align: center middle;
    }
    #brake_dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $primary;
    }
    #brake_dialog > Label {
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    #brake_title {
        text-style: bold;
    }
    #brake_current {
        color: $text-muted;
    }
    .brake_option {
        height: auto;
        margin-bottom: 0;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=True),
        # First-letter selectors for fast keyboard nav. Lowercase only —
        # this modal is one-handed-friendly like the rest of the deck.
        Binding("p", "select_paranoid", "Paranoid", show=True),
        Binding("d", "select_default", "Default", show=True),
        Binding("y", "select_yolo", "YOLO", show=True),
    ]

    def __init__(self, current: "BrakeState", **kwargs) -> None:
        super().__init__(**kwargs)
        self.current = current

    def compose(self) -> ComposeResult:
        cur = self.current.value.upper()
        with Vertical(id="brake_dialog"):
            yield Label("[b]BRAKE STATE[/b]", id="brake_title")
            yield Label(f"Current: [b]{cur}[/b]", id="brake_current")
            yield Label("")
            # Three options, color-coded by gravity.
            yield Label(
                "[b][yellow]P[/yellow][/b]aranoid  ·  "
                "[yellow]investigate-only; deny Write/Edit/Bash/WebFetch[/yellow]",
                classes="brake_option",
            )
            yield Label(
                "[b][white]D[/white][/b]efault   ·  "
                "[white]most things allowed; deny destructive ops + OS path writes[/white]",
                classes="brake_option",
            )
            yield Label(
                "[b][red]Y[/red][/b]OLO      ·  "
                "[red]no brakes; eject is your only stop[/red]  "
                "[dim](requires confirm)[/dim]",
                classes="brake_option",
            )
            yield Label("")
            yield Label("[dim]Esc to cancel.[/dim]")

    def action_select_paranoid(self) -> None:
        self.dismiss(BrakeState.PARANOID)

    def action_select_default(self) -> None:
        self.dismiss(BrakeState.DEFAULT)

    def action_select_yolo(self) -> None:
        # YOLO requires deliberate consent. Push the confirmation
        # screen on top of this one; if confirmed, dismiss with YOLO,
        # if cancelled, stay open so the netrunner can pick something
        # else or hit Esc.
        def _on_confirm(confirmed: bool) -> None:
            if confirmed:
                self.dismiss(BrakeState.YOLO)
            # else: leave BrakeScreen up. The netrunner backed out of
            # YOLO; they may still want paranoid or default.

        self.app.push_screen(YoloConfirmScreen(), _on_confirm)

    def action_cancel(self) -> None:
        self.dismiss(None)


class KeybindsScreen(ModalScreen[None]):
    """Modal overlay displaying the full keybinds map. Triggered by `?`.
    Lives in the netrunner's pocket: any time you forget a key, ? brings
    it up. Esc dismisses."""

    CSS = """
    KeybindsScreen {
        align: center middle;
    }
    #keybinds_dialog {
        width: 90;
        height: auto;
        max-height: 90%;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    #keybinds_dialog > Label {
        margin-bottom: 1;
    }
    /* Scroll container for the body — when content exceeds dialog
       max-height (e.g. on smaller terminals or after future
       additions), the body scrolls instead of clipping the bottom
       sections. Today's content fits without scrolling, but this
       prevents the regression we just hit. */
    #keybinds_scroll {
        height: 1fr;
        max-height: 100%;
    }
    #keybinds_body {
        height: auto;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("question_mark", "dismiss", "Close", show=False),
        Binding("space", "dismiss", "Close", show=False),
    ]

    # Static map of bindings. Kept here (rather than auto-derived from
    # CyberdeckApp.BINDINGS) because the spec wants logical grouping and
    # human-readable descriptions, not a mechanical key dump.
    # Sections shown in the help modal. Slimmed to what actually
    # works today — stubbed bindings (t/T/E/c/Shift+C/p/r/f, plus N
    # invisible-spawn) are intentionally NOT listed here. They reappear
    # when their action lands, not before — listing them as if they
    # work makes the modal lie to the netrunner. Esc/Enter are listed
    # in NAVIGATION/PRIMARY rather than getting their own MODAL section
    # since they behave the same in modals as everywhere else.
    SECTIONS = [
        ("NAVIGATION", [
            ("w / s", "Scroll focused widget up / down. Pauses "
                      "auto-follow on scroll-up; resumes at bottom."),
            ("a / d", "Scroll focused widget left / right "
                      "(when content overflows)."),
            ("W / S", "Walk focus up / down within current section."),
            ("A / D", "Cross sections (sidebar ↔ main ↔ right)."),
            ("Tab / Shift+Tab", "Cycle focus within current section."),
            ("1–9 / Ctrl+1–9", "Jump to element N in section / "
                                "construct N globally."),
            ("Esc", "Unfocus / cancel modal."),
        ]),
        ("PRIMARY", [
            ("Space", "Primary interact with focused element "
                      "(pane expand toggle, goal edit, future: "
                      "list-item launch)."),
            ("z", "Zoom focused widget — fullscreen reader with "
                   "un-truncated content."),
            ("y", "Yank focused widget's content to the OS "
                  "clipboard. Inside a Zoom modal, yanks the full "
                  "snapshot."),
            ("Y", "Yank focused widget's structured data as JSON "
                  "(raw events, bus snapshot, list-item record). "
                  "Companion to y."),
            ("Enter", "Submit / accept (universal in modals)."),
        ]),
        ("CONSTRUCTS", [
            ("n", "Spawn new construct."),
            ("k / K", "Soft-kill / hard-kill focused construct."),
            ("q / Q", "Queue-inject / interrupt-inject focused "
                      "construct."),
        ]),
        ("RUN CONTROL", [
            ("e", "Edit / set goal."),
            ("l", "Open Limits modal."),
            ("Ctrl+F", "EJECT — emergency halt with confirm modal."),
            ("Ctrl+Q", "Quit."),
            ("?", "Show / dismiss this help."),
        ]),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="keybinds_dialog"):
            yield Label("[b]CYBERDECK KEYBINDS[/b]  [dim](Esc / ? / Space to close)[/dim]")
            with VerticalScroll(id="keybinds_scroll"):
                yield Static(self._build_keybinds_text(), id="keybinds_body")

    def _build_keybinds_text(self) -> str:
        lines = []
        for section, entries in self.SECTIONS:
            lines.append(f"[b]{section}[/b]")
            for key, desc in entries:
                # Pad keys to consistent width for readability
                lines.append(f"  [cyan]{key:<22s}[/cyan] {desc}")
            lines.append("")
        return "\n".join(lines).rstrip()

    def action_dismiss(self) -> None:
        self.dismiss(None)


class EjectScreen(ModalScreen[bool]):
    """Deliberate-consent modal for EJECT. Two-step confirmation with a
    visible countdown: open with Ctrl+F, confirm with Space within
    the window, or Esc / timeout to cancel harmlessly.

    Returns True if confirmed, False otherwise."""

    CSS = """
    EjectScreen {
        align: center middle;
    }
    #eject_dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $error;
    }
    #eject_dialog > Label {
        margin-bottom: 1;
        /* Width: 100% lets the label fill the dialog; height: auto
         * lets it grow vertically when text wraps. Without these,
         * a long line just clips at the right edge. */
        width: 100%;
        height: auto;
    }
    #eject_title {
        color: $error;
        text-style: bold;
    }
    #eject_countdown {
        color: $warning;
    }
    """

    BINDINGS = [
        Binding("space", "confirm", "Confirm EJECT", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    # How long the user has to confirm before the modal auto-cancels.
    # Long enough to be deliberate, short enough that an accidental
    # Ctrl+F doesn't leave a lingering modal.
    CONFIRM_WINDOW_SECS = 3.0

    # Refresh interval for the countdown bar. Smooth-ish without being
    # a CPU drain.
    TICK_INTERVAL_SECS = 0.05

    def compose(self) -> ComposeResult:
        with Vertical(id="eject_dialog"):
            yield Label("⚠  EJECT CONFIRMATION  ⚠", id="eject_title")
            yield Label(
                "This will SIGKILL every running construct, halt the daemon, "
                "and write a postmortem snapshot to disk.\n"
                "There is no resume.",
            )
            yield Label("", id="eject_countdown")
            yield Label(
                "[b]SPACE[/b] to confirm  ·  [b]ESC[/b] to cancel",
            )

    def on_mount(self) -> None:
        self._elapsed = 0.0
        self._tick_timer = self.set_interval(
            self.TICK_INTERVAL_SECS, self._tick
        )
        self._refresh_countdown()

    def _tick(self) -> None:
        self._elapsed += self.TICK_INTERVAL_SECS
        if self._elapsed >= self.CONFIRM_WINDOW_SECS:
            self._tick_timer.stop()
            self.dismiss(False)
            return
        self._refresh_countdown()

    def _refresh_countdown(self) -> None:
        remaining = max(0.0, self.CONFIRM_WINDOW_SECS - self._elapsed)
        # 20-cell progress bar showing time remaining (drains as time elapses)
        cell_count = 20
        filled = int(round(cell_count * remaining / self.CONFIRM_WINDOW_SECS))
        bar = "█" * filled + "░" * (cell_count - filled)
        try:
            self.query_one("#eject_countdown", Label).update(
                f"[red]{bar}[/red]  [dim]{remaining:.1f}s remaining[/dim]"
            )
        except Exception:
            pass

    def action_confirm(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        self.dismiss(True)

    def action_cancel(self) -> None:
        if hasattr(self, "_tick_timer"):
            self._tick_timer.stop()
        self.dismiss(False)


class EjectedScreen(ModalScreen[str]):
    """Post-EJECT screen. Shows the snapshot path and offers two ways
    out: return to idle (cleanup, stay alive) or quit entirely.

    Returns 'idle' or 'quit' to indicate the netrunner's choice."""

    CSS = """
    EjectedScreen {
        align: center middle;
    }
    #ejected_dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: heavy $error;
    }
    #ejected_dialog > Label {
        margin-bottom: 1;
        width: 100%;
        height: auto;
    }
    #ejected_title {
        color: $error;
        text-style: bold;
    }
    #ejected_path {
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("i", "return_idle", "Return to idle", show=True),
        Binding("q", "quit_app", "Quit", show=True),
        # Esc as alias for "return to idle" — the gentler default
        Binding("escape", "return_idle", "", show=False),
    ]

    def __init__(self, snapshot_path: Optional[Path], **kwargs) -> None:
        super().__init__(**kwargs)
        self.snapshot_path = snapshot_path

    def compose(self) -> ComposeResult:
        with Vertical(id="ejected_dialog"):
            yield Label("◆  EJECTED  ◆", id="ejected_title")
            yield Label(
                "All constructs killed. Daemon halted. Fleet drained."
            )
            if self.snapshot_path is not None:
                yield Label(
                    f"Snapshot: {self.snapshot_path}",
                    id="ejected_path",
                )
            else:
                yield Label(
                    "[red]Snapshot write failed (see fleet log)[/red]",
                    id="ejected_path",
                )
            yield Label(
                "[b]I[/b] return to idle  ·  [b]Q[/b] quit",
            )

    def action_return_idle(self) -> None:
        self.dismiss("idle")

    def action_quit_app(self) -> None:
        self.dismiss("quit")


class ExpandModal(ModalScreen[None]):
    """A near-fullscreen reader for any RichLog-shaped widget on the deck.

    Triggered by pressing space while focused on an "expandable" log
    (chatlog, files, tools, fleet log, pane logs). Takes a SNAPSHOT of
    the source widget's content at open time and renders it in a
    larger, comfortably-readable surface so long log lines that get
    chopped off in a narrow panel are readable in full.

    Snapshot, not live mirror — the modal doesn't keep updating as new
    events arrive. Press `r` inside to refresh from the live source,
    or close + reopen. This keeps the implementation simple (no event
    routing, no listener bookkeeping) and matches the netrunner's
    intent: open the modal to READ something, not to monitor.
    """

    CSS = """
    ExpandModal {
        align: center middle;
    }
    #expand_dialog {
        width: 90%;
        height: 90%;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    #expand_title {
        height: 1;
        margin-bottom: 1;
        color: $accent;
    }
    #expand_hint {
        height: 1;
        margin-top: 1;
        color: $text-muted;
    }
    #expand_advisor_hint {
        height: 1;
        margin-bottom: 1;
        color: $accent;
    }
    #expand_body {
        height: 1fr;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        # `z` toggles the modal — same key opens it (action_expand on
        # the App), pressing again closes. Mirrors the muscle memory.
        Binding("z", "dismiss", "Close", show=False),
        # `h` — open the Advisor on the modal's target. Only fires
        # when the modal was opened on a tool or plugin info view
        # (advisor_target set by the caller). Other expand modes
        # (chatlog magnify, file viewer, fleet log magnify) leave
        # advisor_target=None, and the action no-ops with a toast.
        # Tools-UI Thought of Dave sub-feature 3 (build-plan 0c).
        # Modal-scoped per the netrunner's intent: the Advisor is a
        # contextual feature of the expanded view — you read about
        # the tool, then you ask the Advisor about it. Putting `h`
        # at App level was the wrong shape (corrected 2026-05-05).
        Binding("h", "advise", "Advisor", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        # `y` — yank the modal's full content to the clipboard.
        # The magnified view is the prime target surface for copy
        # because it's where the netrunner reads things worth
        # sharing (long thinking blocks, tool_results, file
        # contents). Modal scope; the App-level `y` doesn't reach
        # here because modal screens are exclusive.
        Binding("y", "copy", "Copy", show=True),
        # `Y` — yank structured JSON of the modal's source. When the
        # modal carries a source_widget_id, resolves it to the live
        # widget and dispatches to _extract_json_for_copy (so a
        # magnified chatlog yields the full bus snapshot, etc.).
        # Falls back to lines-as-JSON when there's no source id.
        Binding("Y", "copy_json", "Copy JSON", show=True),
        # Scroll inside the body. The App's own w/s bindings don't
        # reach here because modal screens are exclusive — keys go
        # to the modal's BINDINGS first. Without these, the modal
        # was a dead-end for keyboard-only readers (esp. on long
        # files via the file viewer). w/s line-scroll, PgUp/PgDn
        # page, Home/End jump.
        Binding("w", "scroll_up",       "↑",        show=False),
        Binding("s", "scroll_down",     "↓",        show=False),
        Binding("pageup", "page_up",    "Page ↑",   show=False),
        Binding("pagedown", "page_down","Page ↓",   show=False),
        Binding("home", "scroll_top",   "Top",      show=False),
        Binding("end", "scroll_bottom", "Bottom",   show=False),
    ]

    def __init__(
        self,
        title: str,
        snapshot_lines: list,
        source_widget_id: Optional[str] = None,
        provider: Optional["Callable[[], list]"] = None,
        start_at_end: bool = False,
        advisor_target: Optional[AdvisorTarget] = None,
        advisor_siblings: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.title_text = title
        # Chat-shaped sources (chatlog, daemon log, watchdog log,
        # fleet log, construct pane logs) want the modal to open at
        # the most recent content, not the oldest. Set start_at_end=
        # True at the call site for those; defaults False so
        # random-access list views (files, profiles, scripts) still
        # open at the top. Only applied on initial mount — refresh
        # preserves whatever scroll position the netrunner was at,
        # so a mid-read refresh doesn't jump them away.
        self.start_at_end = start_at_end
        # Advisor target — set by the caller when the modal is
        # opened on a tool or plugin info view (Tools-UI Thought of
        # Dave sub-feature 3, build-plan 0c). When set:
        #   - the `h` binding becomes meaningful — pressing it
        #     dismisses this modal and pushes AdvisorScreen on the
        #     target.
        #   - a prominent "press H for interactive help" hint
        #     renders above the body so the affordance is visible
        #     to the netrunner while they're reading manifest text.
        # When None: `h` no-ops with a toast; the hint is hidden.
        # advisor_siblings is the (name-only) list of OTHER tools
        # the Advisor is allowed to mention — see advisor.py for
        # the scope rules. Caller (action_expand) builds it from
        # tool_registry + plugin_registry with the target's own
        # name filtered out.
        self.advisor_target = advisor_target
        self.advisor_siblings = advisor_siblings
        # The snapshot is a list of pre-rendered lines. Each entry can
        # be either:
        #   - a markup string (from chatlog provider): RichLog parses
        #     [color]...[/color] tags and renders styled.
        #   - a rich.text.Text object (from _snapshot_richlog): RichLog
        #     renders with the embedded Style info — preserves the
        #     original colors of fleet_log entries, the green daemon
        #     pane, etc.
        # RichLog.write() accepts both. The modal doesn't have to
        # disambiguate.
        #
        # Two refresh paths:
        #   1. provider — a closure that re-renders content from
        #      source-of-truth (e.g. chatlog event buffer with
        #      untruncated=True). Takes precedence on refresh.
        #   2. source_widget_id — a registered RichLog id we can
        #      re-snapshot from at refresh time. Used for widgets
        #      whose displayed content IS the truth (fleet_log,
        #      files_list, tools_list — formatted in-place).
        # If neither is set, refresh is a no-op.
        self.snapshot_lines = snapshot_lines
        self.source_widget_id = source_widget_id
        self.provider = provider

    def compose(self) -> ComposeResult:
        with Vertical(id="expand_dialog"):
            yield Label(f"[b]{self.title_text}[/b]", id="expand_title")
            # Tooltip — "press H for interactive help" — only when
            # the modal carries an Advisor target. Sits between the
            # title and the body so it's the first thing the
            # netrunner reads alongside the manifest text. Cyan
            # accent + bold → unmistakably interactive affordance.
            # Hidden when advisor_target is None, so chatlog
            # magnify / file viewer / fleet log magnify all stay
            # visually clean.
            if self.advisor_target is not None:
                yield Label(
                    "[b]press H for interactive help[/b] "
                    f"[dim]· Advisor scoped to "
                    f"{self.advisor_target.name}[/dim]",
                    id="expand_advisor_hint",
                )
            yield RichLog(
                id="expand_body",
                max_lines=10000,
                # markup=True so chatlog color spans render in the
                # modal too (the live chatlog uses markup; provider-
                # rendered lines pass through with their markup intact).
                # Snapshots from non-chatlog widgets contain plain
                # text only — markup=True passes those through fine.
                markup=True,
                wrap=True,
                # Big min_width so even short lines lay out without the
                # one-char-per-row pathology. The modal is wide enough
                # that this is always honored, never floor-clamped.
                min_width=40,
                auto_scroll=False,
            )
            # Hint line at the bottom. `h Advisor` only appears when
            # the modal has a target; otherwise it'd be a misleading
            # affordance.
            base_hint = (
                "Esc / z close · r refresh · y copy · "
                "Y copy-json · w/s scroll · PgUp/PgDn page"
            )
            if self.advisor_target is not None:
                base_hint += " · h Advisor"
            yield Label(f"[dim]{base_hint}[/dim]", id="expand_hint")

    def on_mount(self) -> None:
        self._populate()
        # QOL: chat-shaped views open at the most recent content. Only
        # on initial mount — refresh keeps current scroll position so a
        # mid-read refresh doesn't jump the netrunner away from what
        # they were looking at.
        if self.start_at_end:
            b = self._body()
            if b is not None:
                b.scroll_end(animate=False)

    # ---- scroll actions -----------------------------------------------
    # All scroll actions target #expand_body (the inner RichLog).
    # Best-effort guards because the body might not exist if the modal
    # is mid-mount or torn down — we'd rather no-op than crash on a
    # late keypress.

    def _body(self) -> Optional[RichLog]:
        try:
            return self.query_one("#expand_body", RichLog)
        except Exception:
            return None

    def action_scroll_up(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_up(animate=False)

    def action_scroll_down(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_down(animate=False)

    def action_page_up(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_page_up(animate=False)

    def action_page_down(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_page_down(animate=False)

    def action_scroll_top(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        b = self._body()
        if b is not None:
            b.scroll_end(animate=False)

    def action_copy(self) -> None:
        """y inside the modal: yank the full snapshot to the clipboard.

        Copies the same untruncated content the modal is rendering, not
        just what's visible on screen. The whole point of the magnified
        view is reading the full thing; the copy keybind matches that
        scope.
        """
        text = _snapshot_lines_to_plain_text(self.snapshot_lines)
        if not text:
            # Modal is open but the snapshot is empty (rare but
            # possible — a freshly-spawned construct's pane log
            # before any events arrived, etc.). Don't write a toast
            # to the fleet log; the modal covers it. Bail silently.
            return
        ok, err = clipboard.copy(text)
        # Toast lands on fleet_log behind the modal — the netrunner
        # sees it after dismissing. Failure path is more important
        # than success path here (the success is invisible-but-
        # working; the failure is invisible-and-broken).
        try:
            app = self.app
            if isinstance(app, CyberdeckApp):
                if ok:
                    app._toast(
                        f"copy: yanked {len(text)} chars to clipboard"
                    )
                else:
                    app._toast(f"copy: failed — {err}")
        except Exception:
            pass

    def action_copy_json(self) -> None:
        """Y inside the modal: yank structured JSON of the source.

        Resolves source_widget_id back to the live widget and
        dispatches to _extract_json_for_copy — so the magnified
        chatlog yields the full bus snapshot, the magnified fleet
        log yields its lines, etc. When there's no source id
        (file viewer, text view), falls back to a JSON array of
        the rendered lines so the keybind never silently no-ops.
        """
        text: Optional[str] = None
        if self.source_widget_id is not None:
            try:
                source = self.app.query_one(
                    f"#{self.source_widget_id}",
                )
                text = _extract_json_for_copy(source)
            except Exception:
                text = None

        if text is None:
            # Fallback: JSON array of the modal's rendered lines.
            # Useful for file viewers and the like where there's no
            # structured backing widget. Lossy vs. the source path
            # but better than no-op.
            lines: list[str] = []
            for entry in self.snapshot_lines:
                if isinstance(entry, Text):
                    lines.append(entry.plain)
                elif isinstance(entry, str):
                    try:
                        lines.append(Text.from_markup(entry).plain)
                    except Exception:
                        lines.append(entry)
                else:
                    lines.append(str(entry))
            text = json.dumps(
                {
                    "surface": "modal",
                    "title": self.title_text,
                    "lines": lines,
                },
                indent=2, default=str, ensure_ascii=False,
            )

        ok, err = clipboard.copy(text)
        try:
            app = self.app
            if isinstance(app, CyberdeckApp):
                if ok:
                    app._toast(
                        f"copy-json: yanked {len(text)} chars to clipboard"
                    )
                else:
                    app._toast(f"copy-json: failed — {err}")
        except Exception:
            pass

    def action_advise(self) -> None:
        """h: open the Advisor on the modal's target.

        Only meaningful when advisor_target was set at modal-open
        time (tools/plugins z-info path). Other expand modes
        (chatlog magnify, file viewer, fleet log magnify) leave
        advisor_target=None — pressing h there is a no-op with a
        toast, since the affordance is absent from the hint line
        and a quiet failure is the right shape.

        Flow: dismiss this modal first, then push AdvisorScreen on
        the App. Stacking AdvisorScreen on top of ExpandModal would
        work but feels wrong — the netrunner is going from "reading
        about the tool" to "talking to the tool's Advisor"; that's
        a context shift, not a sub-modal. Esc out of the Advisor
        returns to the panel they were on, not back into the
        expanded view.
        """
        if self.advisor_target is None:
            try:
                self.app.notify(
                    "Advisor only available on tool / plugin info views",
                    severity="warning",
                )
            except Exception:
                pass
            return
        target = self.advisor_target
        siblings = self.advisor_siblings
        # Snapshot the App reference before dismiss — Textual's
        # ModalScreen detaches self.app during dismiss(), so we
        # need to grab it now.
        app = self.app
        claude_bin = getattr(app, "claude_bin", "claude")
        self.dismiss()
        if app is not None:
            app.push_screen(AdvisorScreen(
                target=target,
                sibling_tool_names=siblings,
                claude_bin=claude_bin,
            ))

    def action_refresh(self) -> None:
        """Re-fetch content. Uses the provider if registered (re-renders
        from raw source), else re-snapshots from the named widget,
        else no-op."""
        if self.provider is not None:
            try:
                self.snapshot_lines = self.provider()
            except Exception:
                pass
            self._populate()
            return
        if self.source_widget_id is None or self.app is None:
            return
        try:
            source = self.app.query_one(
                f"#{self.source_widget_id}", RichLog
            )
        except Exception:
            return
        self.snapshot_lines = _snapshot_richlog(source)
        self._populate()

    def _populate(self) -> None:
        """Render the current snapshot into the modal's body."""
        try:
            body = self.query_one("#expand_body", RichLog)
        except Exception:
            return
        body.clear()
        for line in self.snapshot_lines:
            body.write(line)


def _snapshot_richlog(widget) -> list:
    """Pull the visible content out of a log widget into a list of
    renderables suitable for write to another RichLog. Used by
    ExpandModal at open time and on refresh.

    Returns a list of `rich.text.Text` objects (NOT plain strings) so
    style information from the source widget is preserved when
    rendered in the modal. RichLog.write accepts both str and Rich
    renderables; Text objects render as-styled, no markup re-parse
    needed.

    Two source-widget shapes are handled:
      - RichLog: widget.lines is list[Strip], each Strip iterates
        Segments with .text + .style. We append each segment to a
        Text object preserving its Style → modal shows original
        colors/styles intact.
      - Log: widget.lines is list[str] (no styling exists on these).
        Wrapped in plain Text with no style.

    The two-shape support lets the same helper work for the
    ConstructPane inner pane_log (a Log) and any focusable RichLog
    (chatlog, fleet_log, files_list, tools_list)."""
    out: list = []
    for line in widget.lines:
        if isinstance(line, str):
            # Log widget — plain text, no style to preserve.
            out.append(Text(line.rstrip()))
            continue
        # RichLog Strip — collapse segments into a styled Text. Each
        # segment carries text + a Style; we append both to keep the
        # original color/bold/dim info that the live widget shows.
        text_obj = Text()
        try:
            for seg in line:
                text_obj.append(seg.text, style=seg.style or "")
        except (AttributeError, TypeError):
            # Defensive fallback if Textual changes Strip's iteration
            # contract — better something readable than a crash.
            text_obj = Text(str(line).rstrip())
        # Trim trailing whitespace from the end of the line.
        text_obj.rstrip()
        out.append(text_obj)
    return out


def _snapshot_lines_to_plain_text(snapshot: list) -> str:
    """Flatten a list of snapshot entries (markup strings or rich.text.Text
    objects, the same shapes ExpandModal accepts) into one plain-text
    blob suitable for the OS clipboard.

    Markup tags get stripped (`Text.from_markup(s).plain`), Text objects
    contribute their .plain. Joined with newlines so the netrunner gets
    the same line structure they were reading.
    """
    parts: list[str] = []
    for entry in snapshot:
        if isinstance(entry, Text):
            parts.append(entry.plain)
        elif isinstance(entry, str):
            try:
                parts.append(Text.from_markup(entry).plain)
            except Exception:
                # Bad markup (unbalanced bracket etc.) — fall back to
                # the raw string. Worse than parsed but better than
                # losing the line entirely.
                parts.append(entry)
        else:
            # Unexpected shape; coerce defensively.
            parts.append(str(entry))
    return "\n".join(parts)


def _extract_text_for_copy(widget) -> Optional[str]:
    """Pull plain-text content from a focused widget for the `y` copy
    keybind. Duck-typed dispatch matches the surface map of `action_expand`
    — every widget the netrunner can magnify is also one they can copy
    from.

    Returns None when the widget has no meaningful text payload (focus
    is on a layout container, an unmapped surface, etc.). Caller toasts
    that case rather than copying an empty string."""
    # ConstructPane → re-render the raw event buffer untruncated. Same
    # provider the magnified view uses, so what the netrunner sees in
    # `z` is what they get on the clipboard via `y`.
    if isinstance(widget, ConstructPane):
        lines = widget.render_buffer(untruncated=True)
        return "\n".join(line.plain for line in lines if isinstance(line, Text))

    # Chatlog gets its untruncated provider (re-renders from the bus
    # snapshot). Same untruncated text the magnified view shows.
    if isinstance(widget, RichLog) and widget.id == "chatlog_log":
        try:
            app = widget.app
            if isinstance(app, CyberdeckApp):
                snapshot = app._render_chatlog_buffer(untruncated=True)
                return _snapshot_lines_to_plain_text(snapshot)
        except Exception:
            pass
        # Fall through to the generic RichLog path on any failure.

    # Generic RichLog / Log — snapshot the live widget. fleet_log,
    # daemon_log, watchdog_log, files_list, tools_list all hit this.
    if isinstance(widget, (RichLog, Log)):
        snapshot = _snapshot_richlog(widget)
        return _snapshot_lines_to_plain_text(snapshot)

    # ListView: copy whatever the highlighted item represents. Path for
    # files, profile/script files (their on-disk path); the netrunner
    # most often wants to paste a path into another shell or editor.
    if isinstance(widget, ListView):
        highlighted = widget.highlighted_child
        if isinstance(highlighted, FileListItem):
            return highlighted.file_path
        if isinstance(highlighted, ProfileListItem):
            return str(highlighted.profile.source_path)
        if isinstance(highlighted, ScriptListItem):
            return str(highlighted.script_path)
        return None

    # Plain Static / Label — best-effort renderable text extraction.
    # Won't always produce something useful, but better than failing
    # silently on the goal pane / sidebar info.
    try:
        renderable = getattr(widget, "renderable", None)
        if renderable is None:
            return None
        if isinstance(renderable, Text):
            return renderable.plain
        return Text.from_markup(str(renderable)).plain
    except Exception:
        return None


def _extract_json_for_copy(widget) -> Optional[str]:
    """Pull structured (JSON) content from a focused widget for the `Y`
    copy keybind. Companion to `_extract_text_for_copy`: same surface
    map, but each surface returns its underlying data shape instead of
    rendered text.

    Returns a pretty-printed JSON string ready for the clipboard, or
    None when the widget has no meaningful structured payload.

    The shapes:
      - ConstructPane: raw event buffer (list of stream-json events)
      - Chatlog: bus snapshot (DeckEvents serialized via the same
        path the file logger uses, so the JSON shape matches what
        Mechanic / external tools already parse)
      - Generic RichLog/Log: rendered lines as a JSON array (no
        structured backing — fleet_log, daemon_log etc. could be
        filtered bus snapshots in a future pass; for now treat them
        like any other log surface)
      - ListView item: dict of the item's structured fields
    """
    # ConstructPane — dump the raw event buffer. Each entry already
    # carries the original stream-json dict alongside the kind + summary;
    # repackage as a list of typed records so a downstream consumer
    # (another Claude session, a debugger, jq, etc.) can iterate without
    # touching the deck's display formatting.
    if isinstance(widget, ConstructPane):
        events = []
        for kind, summary, raw in widget._raw_event_buffer:
            events.append({
                "kind": kind,
                "summary": summary,
                "raw": _serialize_payload(raw),
            })
        record = {
            "surface": "construct_pane",
            "construct_id": widget.construct_id,
            "state": getattr(widget, "state", None),
            "events": events,
        }
        return json.dumps(record, indent=2, default=str, ensure_ascii=False)

    # Chatlog — full bus snapshot, same record shape DeckLogger writes
    # to the per-launch file logs. Reuses _serialize_payload so dataclass
    # payloads (FleetEvent / DaemonEvent / BlacklistEntry / etc.) come
    # out as plain dicts.
    if isinstance(widget, RichLog) and widget.id == "chatlog_log":
        try:
            app = widget.app
            if isinstance(app, CyberdeckApp):
                events = []
                for event in app.bus.snapshot():
                    events.append({
                        "ts": getattr(event, "timestamp", None),
                        "kind": getattr(event, "kind", "unknown"),
                        "source": getattr(event, "source", ""),
                        "construct_id": getattr(
                            event, "construct_id", None,
                        ),
                        "severity": getattr(event, "severity", "info"),
                        "text": getattr(event, "text", None),
                        "payload": _serialize_payload(
                            getattr(event, "payload", None),
                        ),
                    })
                return json.dumps(
                    {"surface": "chatlog", "events": events},
                    indent=2, default=str, ensure_ascii=False,
                )
        except Exception:
            pass
        # Fall through to generic-RichLog path on any failure.

    # Generic RichLog / Log — no structured backing per line. Best we
    # can do: dump the rendered lines as a JSON array. Useful for
    # fleet_log / daemon_log / watchdog_log when the netrunner wants
    # the visible text in a structured form (e.g. "split by line, send
    # to a script").
    if isinstance(widget, (RichLog, Log)):
        snapshot = _snapshot_richlog(widget)
        lines = []
        for line in snapshot:
            if isinstance(line, Text):
                lines.append(line.plain)
            else:
                lines.append(str(line))
        return json.dumps(
            {
                "surface": "log",
                "widget_id": getattr(widget, "id", None),
                "lines": lines,
            },
            indent=2, default=str, ensure_ascii=False,
        )

    # ListView highlighted items — emit the item's underlying fields
    # as JSON. Useful for piping a profile / file path / script entry
    # into another tool.
    if isinstance(widget, ListView):
        highlighted = widget.highlighted_child
        if isinstance(highlighted, FileListItem):
            return json.dumps({
                "surface": "list_item",
                "kind": "file",
                "path": highlighted.file_path,
                "display_path": getattr(highlighted, "display_path", None),
            }, indent=2, default=str, ensure_ascii=False)
        if isinstance(highlighted, ProfileListItem):
            p = highlighted.profile
            return json.dumps({
                "surface": "list_item",
                "kind": "profile",
                "data": _serialize_payload(p),
            }, indent=2, default=str, ensure_ascii=False)
        if isinstance(highlighted, ScriptListItem):
            return json.dumps({
                "surface": "list_item",
                "kind": "script",
                "category": getattr(highlighted, "category", None),
                "name": getattr(highlighted, "script_name", None),
                "path": str(getattr(highlighted, "script_path", "")),
            }, indent=2, default=str, ensure_ascii=False)
        return None

    return None


class CyberdeckApp(App):
    """M3 TUI: keyboard-driven cyberdeck over the fleet."""

    # How long a pane stays in its full state after entering a terminal
    # state before getting compacted and pushed to the bottom. Long
    # enough for a glance at the final result; short enough that the
    # netrunner's active pane list stays uncluttered. Tweakable; not
    # currently surfaced as a CLI flag because no one will ever touch it.
    COMPACT_DELAY_SECS = 3.0

    CSS = """
    Screen {
        layout: vertical;
    }
    #top_area {
        layout: horizontal;
        height: 1fr;
    }
    #sidebar {
        width: 32;
        background: $panel;
        border-right: solid $primary;
        padding: 1;
    }
    #sidebar_info {
        margin-bottom: 1;
    }
    #sidebar_log_label {
        margin-top: 1;
    }
    #fleet_log {
        height: 1fr;
        margin-top: 1;
    }
    #main {
        padding: 1;
        width: 1fr;
    }
    #right_panel {
        width: 34;
        background: $panel;
        border-left: solid $primary;
        padding: 0 1;
    }
    #right_panel TabbedContent {
        height: 1fr;
    }
    #files_list, #tools_list, #chatlog_log {
        height: 1fr;
        padding: 0 1;
    }
    #daemon_bar {
        height: 11;
        border-top: solid $primary;
        background: $panel;
        padding: 0 1;
    }
    /* Right-panel content lists (Chatlog / Files / Tools) get a heavy
     * border when focused so the netrunner can clearly see they're "in"
     * the panel. WASD or Tab navigation lands focus here directly. */
    #files_list:focus, #tools_list:focus, #chatlog_log:focus {
        border: heavy $warning;
    }
    /* Bottom-panel logs get the same treatment so the focus state
     * reads the same across the deck. */
    #daemon_log:focus, #watchdog_log:focus {
        border: heavy $warning;
    }
    """

    BINDINGS = [
        # Quit. ctrl+q is plenty — we don't need a redundant ctrl+c
        # (which historically maps to SIGINT in shells; the deck
        # interprets it the same as ctrl+q if anyone wires it back,
        # but having one canonical quit key reduces footer clutter).
        Binding("ctrl+q", "quit", "Quit"),

        # EJECT — emergency halt with deliberate-consent confirmation.
        # Priority so it fires even if some other binding would consume
        # Ctrl+F. The modal it opens still requires a deliberate confirm.
        Binding("ctrl+f", "open_eject", "EJECT", show=False, priority=True),

        # Section nav (WASD, no wrap)
        # WASD navigation (no wrap). The model:
        #   w / a / s / d — "act on what you have." Lowercase keys
        #     work WITHIN the focused widget. On a RichLog: scroll up/
        #     down/left/right by line/column. On a list (future
        #     C1g.2/3): step between items. On a ConstructPane: scroll
        #     its inner pane_log. The most common navigation verb in
        #     normal use, so it gets the easy keys.
        #   W / A / S / D — "go somewhere else." Uppercase keys MOVE
        #     focus. W/S walks prev/next focusable in the current
        #     section. A/D crosses sections horizontally
        #     (sidebar ↔ main ↔ right_panel). The amplifier metaphor:
        #     shift = bigger leap.
        #
        # Capital letters not "shift+w" because (a) it matches the
        # existing Q/K/T pattern and (b) it's a single keycode for
        # eventual custom-hardware input scenarios where there's no
        # modifier-state to track.
        Binding("w", "scroll_up",     "↑ in",       show=False),
        Binding("a", "scroll_left",   "← in",       show=False),
        Binding("s", "scroll_down",   "↓ in",       show=False),
        Binding("d", "scroll_right",  "→ in",       show=False),
        Binding("W", "list_up",       "↑ focus",    show=False),
        Binding("A", "section_left",  "← section",  show=False),
        Binding("S", "list_down",     "↓ focus",    show=False),
        Binding("D", "section_right", "→ section",  show=False),

        # Focus cycle within section
        # Focus cycle within section. Priority so Textual's built-in
        # focus traversal (which would walk into TabbedContent's tab
        # buttons) doesn't eat the keypress before us.
        Binding("tab", "focus_next_in_section", "Next", show=False, priority=True),
        Binding("shift+tab", "focus_prev_in_section", "Prev", show=False, priority=True),

        # Element jump within section
        Binding("1", "jump(1)", show=False),
        Binding("2", "jump(2)", show=False),
        Binding("3", "jump(3)", show=False),
        Binding("4", "jump(4)", show=False),
        Binding("5", "jump(5)", show=False),
        Binding("6", "jump(6)", show=False),
        Binding("7", "jump(7)", show=False),
        Binding("8", "jump(8)", show=False),
        Binding("9", "jump(9)", show=False),

        # Global construct jump (Ctrl+1-9)
        Binding("ctrl+1", "jump_construct(1)", show=False),
        Binding("ctrl+2", "jump_construct(2)", show=False),
        Binding("ctrl+3", "jump_construct(3)", show=False),
        Binding("ctrl+4", "jump_construct(4)", show=False),
        Binding("ctrl+5", "jump_construct(5)", show=False),
        Binding("ctrl+6", "jump_construct(6)", show=False),
        Binding("ctrl+7", "jump_construct(7)", show=False),
        Binding("ctrl+8", "jump_construct(8)", show=False),
        Binding("ctrl+9", "jump_construct(9)", show=False),

        # Primary actions
        #   space — primary "interact" with focused element. Today:
        #     toggles ConstructPane expand/collapse, edits goal pane.
        #     Once C1g.4 lands, it'll also launch a construct from a
        #     focused list item (Tools/Files).
        #   z — universal "zoom focused widget." Opens ExpandModal on
        #     any RichLog or routes to the inner pane_log when a
        #     ConstructPane is focused. Single-keycode (works in every
        #     terminal, GPIO-friendly) — we tried shift+space but most
        #     terminals don't transmit a distinct shift+space event,
        #     so it was a no-op for the netrunner even though it
        #     worked in synthetic pilot tests.
        Binding("space", "primary", "Primary"),
        Binding("enter", "primary", "", show=False),
        Binding("z", "expand", "Zoom", show=False),
        # `y` — yank focused widget's content to the OS clipboard.
        # Sidesteps Ctrl+C-as-copy on Windows, which dropped a SIGINT
        # into every child claude subprocess (2026-04-30 autopsy
        # filed in cyberdeck-state.md → Filed gotchas). Single
        # keycode for the hardware story; vim-yank semantic. The
        # only collision is `y` for YOLO inside BrakeScreen, which
        # is modal-scoped (modals don't inherit App BINDINGS) — no
        # conflict in practice.
        Binding("y", "copy_focused", "Copy", show=False),
        # `Y` (shift+y) — yank structured JSON of the focused widget.
        # Companion to lowercase y. Same surface map; each surface
        # produces its underlying data shape instead of rendered text.
        # Use case: pasting fleet activity into another tool (jq,
        # another Claude session, a debugger) where the rendered text
        # has already lost the structure we want to operate on.
        Binding("Y", "copy_focused_json", "Copy JSON", show=False),
        Binding("escape", "unfocus", "Unfocus", show=False),

        # Construct interaction (soft/loud pairs)
        Binding("q", "queue_inject", "Q-inject", show=False),
        Binding("Q", "interrupt_inject", "I-inject", show=False),
        Binding("k", "kill_focused", "Kill"),
        Binding("K", "hard_kill_focused", "Hard-kill", show=False),
        # Slice 3: X is the deck-wide approval / override key. For an
        # open delay window, X flips the brake hook's default action
        # (approve a deny-default, interrupt an allow-default). Future
        # netrunner-prompt surfaces (blacklist proposals, daemon-
        # requested captures, pause-mode launch) will reuse the same
        # key — "press X" is the universal "act on this prompt"
        # gesture. Z stays for zoom; X is approve/execute.
        Binding("x", "x_focused", "X-ecute", show=False),
        Binding("X", "x_focused", "X-ecute", show=False),

        # Daemon / goal
        Binding("t", "talk_watchdog", "Ask", show=False),
        Binding("T", "talk_daemon", "Talk", show=False),
        Binding("e", "edit_goal", "Goal"),
        Binding("E", "toggle_daemon_pause", "Pause", show=False),

        # Spawn (visible/invisible pair)
        Binding("n", "new_construct", "New"),
        Binding("N", "new_construct_invisible", "New(inv)", show=False),

        # Routing / wiring
        Binding("r", "wire_route", "Wire", show=False),

        # Plugins / tools
        Binding("c", "plugin_quickfire", "Capture", show=False),
        Binding("C", "plugin_picker", "", show=False),
        Binding("p", "toggle_airgap", "Airgap", show=False),
        Binding("l", "open_limits", "Limits", show=False),
        Binding("b", "open_brake", "Brake", show=False),

        # Help / keybinds overlay
        Binding("question_mark", "show_keybinds", "Help"),
    ]

    def __init__(
        self,
        tasks: list[str],
        claude_bin: str,
        permission_mode: str,
        log_dir: Optional[Path] = None,
        log_level: str = "info",
        goal: Optional[str] = None,
        daemon_bin: Optional[str] = None,
        max_concurrent: int = 10,
        max_total_spawns: int = 30,
        streaming_mode: bool = True,
        use_pool: bool = True,
        pool_size: int = 5,
        wedge_timeout_seconds: float = 30.0,
        delay_window_seconds: float = 5.0,
        home_dir: Optional[Path] = None,
        profiles_dir: Optional[Path] = None,
        default_profile_name: Optional[str] = None,
        fast_mode: Optional[bool] = None,
        fast_mode_explicit: bool = False,
        daemon_effort: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.tasks = tasks
        self.claude_bin = claude_bin
        self.permission_mode = permission_mode
        # Home dir is the soft-sandbox cwd for every construct, daemon,
        # and pool warming subprocess. Logs and the session manifest
        # also default-locate under here. Everything the deck WRITES is
        # under home; everything it RUNS (its own .py source) is wherever
        # python was invoked from. Keep that boundary clean.
        self.home_dir = (
            home_dir if home_dir is not None
            else _resolve_home_dir(None)
        )
        # Bootstrap deck-protocol dispatcher script. Cyberdeck ships
        # with dispatcher.py; on every startup we (re)write that
        # source to <home>/tools/deck/cyberdeck.py so constructs can
        # invoke `python <home>/tools/deck/cyberdeck.py <subcmd>` via
        # their Bash tool to talk back to the deck UI. Idempotent —
        # overwrite-on-startup means dispatcher updates ship
        # automatically. The netrunner shouldn't edit the deck/
        # subdir; that's owned by cyberdeck. Other tool categories
        # under <home>/tools/ are netrunner/construct territory.
        try:
            self._bootstrap_deck_dispatcher()
        except Exception:
            # Never fail app startup over the dispatcher. If write
            # fails (read-only fs, permissions), constructs just
            # won't have deck-control protocol available — they'll
            # still spawn and run normally without it.
            pass
        # Bootstrap plugin-bridge dispatcher (P2 of the tools/plugins/
        # profiles retool, 2026-05-03). Same idempotent overwrite
        # pattern as the deck dispatcher above. The bridge lives at
        # <home>/tools/deck/plugin_bridge.py and forwards
        # `python <bridge> <plugin_name> [args...]` invocations to
        # the appropriate plugin's entry script in <deck-source>/
        # plugins/<name>/plugin.py. The bootstrap stamps the absolute
        # plugins-dir path into the script so the bridge can resolve
        # plugin folders without env vars or runtime walks.
        try:
            self._bootstrap_plugin_bridge()
        except Exception:
            # Same posture as the deck dispatcher — bootstrap failure
            # degrades to "no plugin invocations available" rather
            # than crashing app startup.
            pass
        # Profiles dir defaults to <home>/profiles. The registry will
        # create it if missing on start. Explicit --profiles-dir
        # overrides for testing or shared profile sets across multiple
        # home dirs.
        self.profiles_dir = (
            profiles_dir if profiles_dir is not None
            else self.home_dir / "profiles"
        )
        # Profile name to apply to all spawned constructs in this run.
        # Resolved against the registry at goal-start time (the registry
        # may not have finished its initial scan when __init__ runs).
        # If the name doesn't resolve, we fall back to "no profile"
        # behavior and surface a chatlog warning. Until C1e wires the
        # daemon up to pick profiles per-spawn, this is the only knob
        # the netrunner has for steering profile use.
        #
        # When the netrunner doesn't pass --default-profile, we use
        # "default" — the registry seeds default.toml on disk if it's
        # missing, so the file always exists and the netrunner can edit
        # it to tweak baseline behavior. Tracking explicit-vs-implicit
        # only matters for log noise: the resolved-profile line is
        # silent when default+implicit (the boring case), printed
        # otherwise (anything the netrunner cares about).
        self._default_profile_name = default_profile_name or "default"
        self._default_profile_explicit = default_profile_name is not None
        self.default_profile: Optional[Profile] = None
        # Caliber Phase 1 (2026-05-04): the deck's default per-spawn
        # caliber. Used as the fall-through when the daemon doesn't
        # specify model/effort in its spawn action JSON, and as the
        # explicit caliber for netrunner-direct spawns. Phase 3 will
        # add a separate daemon_caliber for the daemon process itself
        # (different bin invocation); Phase 2 will add pool_caliber
        # for warm-pool match gating.
        try:
            from caliber import Caliber
            self.default_caliber: Optional[Caliber] = Caliber.default()
            # Caliber Phase 3 (2026-05-04, scoped down): the daemon's
            # subprocess caliber. MODEL IS PINNED to opus by design —
            # the daemon is a manager doing decomposition + dispatch,
            # not a swappable agent; capability matters, model
            # variability doesn't. EFFORT is the netrunner's power-
            # level knob, surfaced in the Limits modal alongside
            # max_concurrent / pool_size / etc. Defaults to high.
            # Stored as a Caliber object so the existing
            # to_claude_args plumbing in Daemon._spawn_streaming
            # works unchanged; effort is the only mutable field.
            self.daemon_effort: str = "high"
            self.daemon_caliber: Optional[Caliber] = Caliber(
                model="opus", effort=self.daemon_effort,
            )
        except Exception:
            # caliber.py missing or broken — degrade gracefully.
            # Constructs spawn without --model/--effort and Claude
            # Code applies its own runtime default.
            self.default_caliber = None
            self.daemon_caliber = None
            self.daemon_effort = "high"
        # Caliber Phase 2 (2026-05-04): fast_mode is a deck-wide cost
        # governor — netrunner-controlled, defaults OFF. The daemon
        # never picks fast_mode autonomously; it's a 6x-cost-for-2.5x-
        # speed trade the netrunner opts into deliberately. Initial
        # value comes from CLI flag (if explicitly passed); otherwise
        # the persisted state.json value loaded later (in on_mount,
        # via load_limits) overrides this default. Explicit-CLI wins
        # over persisted to make `--fast-mode` / `--no-fast-mode`
        # one-shot overrides usable.
        self.fast_mode: bool = bool(fast_mode) if fast_mode is not None else False
        self._fast_mode_explicit = fast_mode_explicit
        # Caliber Phase 3 (scoped down): apply --daemon-effort CLI
        # override when passed. Model is always opus — not netrunner-
        # configurable. The explicit flag wins over the persisted
        # value loaded later (in on_mount via load_limits) and also
        # writes back to state.json so the new value persists.
        self._daemon_effort_explicit = daemon_effort is not None
        if (
            self.daemon_caliber is not None
            and daemon_effort is not None
        ):
            try:
                from caliber import Caliber as _C
                self.daemon_effort = daemon_effort
                self.daemon_caliber = _C(
                    model="opus",
                    effort=daemon_effort,
                )
            except Exception:
                pass
        # Phase 7 of the unified-event-stream slice: per-launch log
        # files in `<deck source>/logs/` (operational artifacts, not
        # deck-content). Defaults to a `logs/` directory next to the
        # .py source files. Explicit --log-dir / CYBERDECK_LOG_DIR
        # overrides for testing or external mount points. The level
        # is the minimum severity that gets persisted; CRITICAL is
        # always written regardless. DeckLogger instance is built
        # AFTER self.bus and attaches itself as a subscriber.
        if log_dir is None:
            # _deck_source_dir() — the directory tui.py lives in.
            # Same logic as brake_hook.deck_source_dir() but local
            # to keep tui.py from depending on brake_hook for path
            # resolution. The hook's resolution is the canonical
            # one; this matches it deliberately.
            self.log_dir = Path(__file__).resolve().parent / "logs"
        else:
            self.log_dir = Path(log_dir)
        self.log_level = log_level.lower()
        self.deck_logger: Optional[DeckLogger] = None  # built in __init__ below
        self.goal = goal
        # Daemon binary can differ from construct binary (for mocking)
        self.daemon_bin = daemon_bin or claude_bin
        self.max_concurrent = max_concurrent
        self.max_total_spawns = max_total_spawns
        self.streaming_mode = streaming_mode
        self.use_pool = use_pool
        self.pool_size = pool_size
        # Wedge-timeout ceiling for the post-stdout-close c.wait() in
        # fleet's _consume finally. Threaded into Fleet at construction
        # and editable live via the Limits modal — _handle_limits_
        # submitted writes straight to fleet.wedge_timeout_seconds so
        # changes apply on the next finalize, no fleet rebuild needed.
        self.wedge_timeout_seconds = wedge_timeout_seconds
        # Variable-outcome delay window (slice 3 of the safety
        # architecture pass). Read fresh per spawn from
        # fleet.delay_window_seconds, baked into each construct's
        # per-spawn settings JSON. Limits modal mutates this attr +
        # fleet's mirror; changes apply to the NEXT spawn (existing
        # in-flight spawns keep what they were spawned with — same
        # propagation model as brake state itself). 0 = no delay =
        # pre-slice-3 behavior.
        self.delay_window_seconds = delay_window_seconds
        self.fleet: Optional[Fleet] = None
        self.daemon: Optional[Daemon] = None
        self.session: Optional[DaemonSession] = None
        self.session_manager: Optional[SessionManager] = None
        self.session_pool: Optional[SessionPool] = None
        self._daemon_task: Optional[asyncio.Task] = None
        # The spine. One canonical event bus that every event source
        # on the deck eventually publishes to. Phase 1 added the
        # primitives (event_bus.py); Phase 2 wires Fleet through it
        # (alongside the existing add_listener path so behavior is
        # unchanged); subsequent phases migrate Daemon, Tripwires,
        # Blacklist, Brake, Connection, Profiles, Plugins, direct
        # chatlog writes, and the file logger. Subscribers register
        # with role-derived filters; the bus enforces visibility.
        # Constructs DO NOT receive a reference — they're work units,
        # not observers. See `Design Files/cyberdeck-event-stream-
        # design.md` for the full migration plan.
        self.bus = EventBus()
        # Subscription handles for the two _drive_fleet-scoped bus
        # subscribers (_handle_event, _scan_for_tripwires). Stored on
        # self so each new _drive_fleet invocation can unsubscribe the
        # prior ones before re-subscribing. Without this, every
        # _drive_fleet call (initial + post-EJECT respawn) accumulated
        # a NEW subscription, so each fleet event fired the handler
        # multiple times — visible as duplicate construct panes (one
        # stuck at [STARTING] forever per ghost subscription) and
        # double-fired tripwire scans. Bug latent since Phase 8;
        # caught real-deck 2026-04-30 late.
        self._fleet_event_sub: Optional[Any] = None
        self._fleet_tripwire_scan_sub: Optional[Any] = None
        # Phase 7 file logger gets instantiated at the end of __init__
        # (after brake_state_store has loaded from disk) so the header
        # captures the real brake state, not a placeholder. We
        # initialize the field here for type-clarity; the actual
        # DeckLogger construction lives down past brake_state_store.
        # Set of (construct_id, action) pairs we've already warned
        # about for unknown deck-protocol markers. A misbehaving
        # construct emitting the same unknown action in a tight loop
        # would otherwise paint the fleet log yellow until restart;
        # one warning per (source, action) is plenty — the netrunner
        # gets the signal once and isn't drowned in repeats.
        self._unknown_action_seen: set[tuple[str, str]] = set()
        # Profile registry is always live, regardless of goal mode.
        # Started in on_mount, shut down in on_unmount. Listens to
        # disk and pushes events into _handle_profile_event so the
        # Tools tab can stay in sync.
        self.profile_registry = ProfileRegistry(
            self.profiles_dir,
            bus=self.bus,
        )
        # Phase 8: subscribe via the bus instead of the legacy
        # `on_event=` callback. Filter `profile.*` matches every
        # ProfileEvent kind (added, changed, removed, scan_complete,
        # scan_error). Handler receives DeckEvent whose payload is
        # the ProfileEvent.
        self.bus.subscribe(
            self._handle_profile_event,
            filter=["profile.*"],
            name="tui.profile_event",
        )
        # Plugins directory: lives under DECK SOURCE, not workspace.
        # Plugins are capability bundles (manifest + README + entry
        # script) that extend what constructs can do beyond Bash +
        # builtins. Unlike profiles, plugins are NOT hot-reloaded —
        # adding a new plugin or editing an entry script requires a
        # deck restart. Plugin registry scans once at startup; that's
        # the entire lifecycle.
        #
        # P2 of the tools/plugins/profiles retool (2026-05-03) moved
        # plugins from <home>/plugins/ into <deck-source>/plugins/
        # for one specific safety guarantee: the brake hook protects
        # <deck-source>/ from constructs by `path_is_protected()`.
        # Putting plugin code there means constructs CANNOT write to
        # plugin files via Write/Edit/Bash, period — closes the
        # "construct writes a half-baked plugin file mid-run and the
        # deck self-destructs at restart" failure mode at the
        # filesystem layer. The construct-facing surface is the
        # bridge dispatcher at <home>/tools/deck/plugin_bridge.py,
        # which the deck regenerates on every startup (the bridge
        # itself can't be tampered with persistently).
        self.plugins_dir = (
            Path(__file__).resolve().parent / "plugins"
        )
        self.plugin_registry = PluginRegistry(
            self.plugins_dir,
            bus=self.bus,
        )
        # Phase 8: bus subscription (same pattern as profile_registry).
        # P3 of the retool added a separate hook-event subscription
        # below — narrow this filter to registry-emitted kinds only
        # so hook events (which carry a different payload shape)
        # don't fall into the registry handler.
        self.bus.subscribe(
            self._handle_plugin_event,
            filter=[
                "plugin.loaded",
                "plugin.scan_error",
                "plugin.scan_complete",
            ],
            name="tui.plugin_event",
        )
        # Plugin hook lifecycle events (P3 of the tools/plugins/
        # profiles retool, 2026-05-03). Separate subscriber because
        # the payload is a small dict, not a PluginEvent dataclass —
        # avoids `if isinstance(payload, ...)` switching inside the
        # registry handler. Bus filter scopes to the `plugin.hook_*`
        # namespace.
        self.bus.subscribe(
            self._handle_plugin_hook_event,
            filter=["plugin.hook_*"],
            name="tui.plugin_hook_event",
        )
        # Tools registry (P1 of the tools/plugins/profiles retool, 2026-
        # 05-03). Single-file watcher over <home>/tools/tools.toml. The
        # netrunner declares system-installed CLIs (binaries on PATH,
        # scripts on disk) here; the deck checks at load time whether
        # each is reachable and surfaces the result in the Tools panel.
        # Same lifecycle as profile_registry: started in on_mount, shut
        # down in on_unmount, hot-reloads on file edit.
        self.tool_registry = ToolRegistry(
            self.home_dir / "tools",
            bus=self.bus,
        )
        self.bus.subscribe(
            self._handle_tool_event,
            filter=["tool.*"],
            name="tui.tool_event",
        )
        # Watchdog Q&A oracle. Async question→answer pipe backed by
        # one-shot `claude -p` invocations. The simpler half of the
        # spec'd Watchdog (tripwires + blacklist still deferred).
        # Started in on_mount, shut down in on_unmount.
        #
        # Substrate note: spec calls for a local 7B model so the
        # watchdog runs "free." We're using cloud Claude until D1
        # lands; tokens cost real money on each ask. See watchdog.py
        # for the swap point.
        self.watchdog = Watchdog(
            claude_bin=self.claude_bin,
            on_blacklist_event=self._handle_blacklist_event,
            # Persistent Q&A log under the deck's internal state
            # directory. Replayed into WatchdogPane on mount so the
            # netrunner's prior Q&A history survives a deck restart.
            # First slice of the watchdog-log initiative filed in
            # cyberdeck-state.md; future expansions (tripwire fires,
            # blacklist change records) will share the same JSONL
            # via a `kind` field.
            history=WatchdogHistory(
                self.home_dir / ".cyberdeck" / "watchdog.jsonl",
            ),
            # Tripwire fire callback — engine lives on the watchdog
            # (per spec, "LLM authors, deterministic enforces"); fires
            # render to the chatlog via this handler. Default
            # tripwires (credentials keyword, destructive SQL) are
            # installed on engine construction; LLM-authored
            # tripwires land in slice 2.
            on_tripwire_fire=self._handle_tripwire_fire,
            # Phase 4 of the unified-event-stream slice: pass the bus
            # so the watchdog's owned components (Blacklist + Tripwire
            # engine) and authoring lifecycle publish to it alongside
            # the existing on_event / on_fire callbacks.
            bus=self.bus,
            # Slice 2 of the safety architecture pass: home_dir is
            # threaded to the TripwireEngine so it can write per-
            # construct deny_pending.json files. The brake hook reads
            # those files at every invocation and denies the next
            # tool call from a construct that fired a warning or
            # critical tripwire — that's how tripwires get teeth.
            home_dir=self.home_dir,
        )
        # Connection monitor — heartbeats api.anthropic.com:443 to
        # detect Online/Degraded/Offline transitions. Per spec line
        # 114: "sidebar status reflects current state at all times."
        # Today: indicator + chatlog announcement on transition.
        # Future M5+: spawn-blocking on Degraded/Offline, daemon
        # parking, recovery flow. This monitor exposes the state +
        # events that those consumers will subscribe to.
        self.connection_monitor = ConnectionMonitor(
            bus=self.bus,
        )
        # Phase 8: bus subscription replaces the legacy
        # `on_state_change=` callback. Filter `connection.transition`
        # is the single kind connection_monitor publishes; handler
        # receives DeckEvent whose payload is the StateChangeEvent.
        self.bus.subscribe(
            self._handle_connection_change,
            filter=["connection.transition"],
            name="tui.connection_change",
        )
        # Brake state — deck-global enum (paranoid/default/yolo) that
        # gates what constructs are permitted to do at runtime. The
        # store loads on app start, persists changes to disk, and
        # broadcasts transitions to listeners (sidebar indicator,
        # chatlog announcer, eventual hook-config refresh hook for
        # subsequent spawns). Mirrors ConnectionMonitor in shape.
        # See brake_state.py for the data model and brake_hook.py
        # (vendored at startup) for the actual enforcement layer.
        self.brake_state_store = BrakeStateStore(
            state_path=self.home_dir / ".cyberdeck" / "state.json",
            bus=self.bus,
        )
        # Loaded synchronously in __init__ rather than on_mount so
        # any spawn (including the initial pool warm-up) sees the
        # netrunner's last-set brake from disk, not the cold default.
        self.brake_state_store.load()
        # Phase 1.5 of the safety architecture pass: persist the
        # runtime tunables (delay_window_seconds, wedge_timeout_
        # seconds) so a deck restart doesn't lose the netrunner's
        # last-set values. Loaded from the same state.json file as
        # brake; missing keys leave the App-init values (= the
        # CyberdeckApp.__init__ defaults of 0.0 and 30.0
        # respectively) in place. No-op on a fresh install — the
        # file doesn't exist yet, so we use the defaults.
        # Build-plan item 0b: persisted preferences flow through the
        # Preferences accessor (single import surface, typed
        # properties, future-proofed schema). Internally still
        # delegates to brake_state.load_limits / save_limits so the
        # state.json shape is unchanged.
        self.prefs = Preferences(self.home_dir)
        try:
            # Tunables that aren't CLI-overridden load from prefs.
            # Each falls back to the default from preferences.py if
            # the key is missing or malformed in state.json.
            self.delay_window_seconds = self.prefs.delay_window_seconds
            self.wedge_timeout_seconds = self.prefs.wedge_timeout_seconds
            # Caliber Phase 2 (2026-05-04): fast_mode is a deck-wide
            # cost governor. Explicit CLI flag wins over persisted;
            # otherwise load from prefs.
            if not self._fast_mode_explicit:
                self.fast_mode = self.prefs.fast_mode
            # If the netrunner DID pass --fast-mode / --no-fast-mode
            # explicitly, persist their choice now so the next launch
            # without the flag picks it up.
            if self._fast_mode_explicit:
                self.prefs.save(fast_mode=self.fast_mode)
            # Caliber Phase 3 (scoped down, 2026-05-04): persisted
            # daemon_effort. CLI-explicit wins over persisted.
            if (
                self.daemon_caliber is not None
                and not self._daemon_effort_explicit
            ):
                de = self.prefs.daemon_effort
                if de != self.daemon_effort:
                    try:
                        from caliber import Caliber as _C
                        self.daemon_effort = de
                        self.daemon_caliber = _C(
                            model="opus", effort=de,
                        )
                    except Exception:
                        pass
            if self._daemon_effort_explicit and self.daemon_caliber:
                self.prefs.save(daemon_effort=self.daemon_caliber.effort)
        except Exception:
            # Preferences is already best-effort (load_limits returns
            # {} on read failure, save_limits swallows OSError); wrap
            # once more so a malformed entry can't break startup.
            pass
        # Phase 8 of the unified-event-stream slice: subscribe via the
        # bus instead of the legacy `on_change=` callback. The handler
        # receives a DeckEvent whose payload is the BrakeChangeEvent.
        self.bus.subscribe(
            self._handle_brake_change,
            filter=["brake.change"],
            name="tui.brake_change",
        )

        # Slice 3 of the safety architecture pass: variable-outcome
        # delay UX. DelayMonitor polls <home>/.cyberdeck/spawns/ for
        # *.delay_pending.json files written by brake_hook when an
        # interesting tool call enters the delay window. On
        # appearance / disappearance it publishes brake.delay_opened /
        # brake.delay_resolved bus events; the chatlog renderer and
        # the new Delays tab subscribe.
        #
        # Always constructed (delay_window_seconds == 0 just means no
        # files ever appear; the monitor's poll is essentially free).
        # Started later in _drive_fleet alongside ConnectionMonitor.
        self.delay_monitor = DelayMonitor(
            home_dir=self.home_dir,
            bus=self.bus,
        )
        self.bus.subscribe(
            self._handle_delay_opened,
            filter=["brake.delay_opened"],
            name="tui.delay_opened",
        )
        self.bus.subscribe(
            self._handle_delay_resolved,
            filter=["brake.delay_resolved"],
            name="tui.delay_resolved",
        )
        # Periodic refresh of the DelayPanel's countdown bars. 100ms
        # gives smooth visible motion without burning cycles. Started
        # after the UI mounts; see on_mount.
        self._delay_refresh_timer = None

        # Slice 3 phase 2: attention items (blacklist proposals today;
        # future kinds plug in via AttentionItem.kind). Open items live
        # here keyed by item_id. _attention_timers holds the asyncio
        # task that fires expiry; cancelled on approve. The 100ms
        # refresh tick that drives delay countdowns also re-renders
        # the AttentionPanel from this dict so the bar drains smoothly.
        self._attention_items: dict[str, AttentionItem] = {}
        self._attention_timers: dict[str, asyncio.Task] = {}
        # AttentionPanel widget — set in compose(), used by _repaint_
        # attention. Initialized to None here so any pre-compose call
        # (e.g. a stray bus event landing during startup) finds the
        # field and the helper no-ops cleanly rather than AttributeError.
        self.attention_panel: Optional[AttentionPanel] = None

        # Tripwire-authoring spawn gate (filed 2026-05-02 after real-
        # deck race observed: fast constructs were finishing in ~7-15s,
        # tripwire authoring took ~25s, so the entire spawn batch ran
        # without authored coverage). Initial state is SET ("ok to
        # spawn") because no authoring is in progress at startup.
        # _kick_off_tripwire_authoring clears the event when starting
        # an authoring pass; _author_tripwires_wrapper sets it again
        # in its finally block (always — success, failure, or crash).
        # DaemonSession awaits this event before dispatching each
        # spawn action so the first batch of spawns gets authored
        # tripwire coverage from event 1, not from event N where N
        # is whenever authoring happens to land.
        self._tripwire_authoring_complete = asyncio.Event()
        self._tripwire_authoring_complete.set()
        # Default window for blacklist proposals — long enough that
        # the netrunner has time to read the proposal and decide,
        # short enough that proposals don't pile up if they walk
        # away. Same scale as the EJECT confirmation gesture (3s)
        # but generous because blacklist decisions deserve more
        # consideration than "press space to halt." Mutable at
        # runtime if a future Limits-modal field exposes it.
        self.blacklist_proposal_window_seconds: float = 30.0

        # Phase 7 — instantiate the file logger now that brake_state has
        # loaded from disk so the header records the real value. Built
        # best-effort: a disk-write failure (read-only fs, no
        # permissions) shouldn't break startup; any exception here
        # degrades to "no file logger" without crashing the deck. The
        # logger subscribes to the bus with no kind filter — every
        # event meeting the severity threshold gets written. Header
        # carries argv + env + brake + home + python/OS so any single
        # shared file is self-describing.
        try:
            self.deck_logger = DeckLogger(
                log_dir=self.log_dir,
                level=self.log_level,
                argv=list(sys.argv),
                env_snapshot={
                    k: v for k, v in os.environ.items()
                    if k.startswith("CYBERDECK_") or k.startswith("CLAUDE_")
                },
                deck_version="cyberdeck-spine-phase-7",
                brake_label=self.brake_state_store.state.value,
                home_dir=self.home_dir,
                # Slice 2: dump blacklist contents on close so cross-
                # run analysis can spot recurring fingerprints. Lambda
                # rather than a direct list reference because the
                # blacklist mutates over the session's lifetime; we
                # want the snapshot AT close time, not at construction.
                blacklist_provider=lambda: (
                    self.watchdog.blacklist.entries
                    if self.watchdog is not None
                    and self.watchdog.blacklist is not None
                    else []
                ),
            )
            self.deck_logger.attach_to_bus(self.bus)
        except Exception as exc:
            print(
                f"DeckLogger init failed: {exc!r} — running without file log",
                file=sys.stderr,
            )
            self.deck_logger = None

        self.panes: dict[str, ConstructPane] = {}
        self.goal_pane: Optional[GoalPane] = None
        self.daemon_pane: Optional[DaemonPane] = None
        self.watchdog_pane: Optional[WatchdogPane] = None
        self.pool_meter: Optional[PoolMeter] = None
        # Pending injections: construct_id -> (mode, message, session_id, prior_task).
        # Populated when the user submits InjectScreen; consumed in
        # _handle_meta when the targeted construct finalizes (queue
        # mode) or just after the kill has flushed (interrupt mode).
        # Either way, we spawn a follow-up Construct via --resume so
        # the injected message lands as the next user turn in the
        # same session.
        self._pending_injections: dict[
            str, tuple[str, str, str, str]
        ] = {}

        # (Phase 6 of the unified-event-stream slice retired the
        # standalone `_chatlog_event_buffer` deque that used to live
        # here. The bus's ring buffer (default maxlen=10000 events,
        # ~10× the old chatlog buffer's capacity) is now the single
        # source of truth. _render_chatlog_buffer and
        # _build_watchdog_context iterate self.bus.snapshot() and
        # filter via _chatlog_format_bus_event.)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Horizontal(id="top_area"):
            # LEFT: sidebar — goal, counters, fleet log
            with Vertical(id="sidebar"):
                yield Label("[b]CYBERDECK[/b]")
                yield Label("[dim]m5.3: pool live[/dim]")
                # Connection status indicator. Single Label, color-
                # coded per state. Updates from ConnectionMonitor's
                # on_state_change callback. Sits high in the sidebar
                # because the spec calls for "sidebar status reflects
                # current state at all times" — it's a glance-check,
                # not buried.
                yield Label(
                    "[green]●[/green] online",
                    id="connection_status",
                )
                # Brake state indicator. Sits next to connection because
                # the two together answer "is the deck running normally
                # right now?" at a glance. Initial render uses the
                # store's already-loaded state so the indicator never
                # shows a stale default after a restart.
                yield Label(
                    self._brake_indicator_text(self.brake_state_store.state),
                    id="brake_status",
                )
                yield Label("", id="sidebar_info")
                # Pool meter sits between sidebar info and the goal pane
                # so it's the first state widget the netrunner sees. It
                # hides itself entirely when --no-pool is set.
                self.pool_meter = PoolMeter(id="pool_meter")
                yield self.pool_meter
                # Goal pane is always mounted. In idle state it shows
                # "(no goal set)"; once a goal lands it updates inline.
                self.goal_pane = GoalPane(self.goal or "", id="goal_pane")
                yield self.goal_pane
                yield Label("[b]fleet log[/b]", id="sidebar_log_label")
                fleet_log_widget = RichLog(
                    id="fleet_log",
                    markup=True,
                    wrap=True,
                    max_lines=500,
                    auto_scroll=True,
                    min_width=10,
                )
                # Focusable on purpose — the netrunner needs to be able
                # to focus and space-to-expand the fleet log just like
                # the right-panel logs. WASD navigation still works:
                # focus lands here as part of the left-section cycle.
                yield fleet_log_widget

            # CENTER: main — construct panes stack here. Single zone
            # because Textual's remove+remount re-runs compose() and
            # blows away the moved widget's children (i.e., the pane's
            # log content). We preserve order via move_child instead:
            # active panes at the top, terminal panes pushed to the
            # bottom by _move_pane_to_bottom on completion. Visual
            # separation comes from the .-compact CSS class, not from
            # a separate container.
            with VerticalScroll(id="main", can_focus=False):
                # AttentionPanel is the first child — pinned at the
                # top of the construct pool. Hidden when empty, pops
                # out with a magenta border + countdown bars when
                # blacklist proposals (or future attention kinds)
                # are pending. The promote-to-top + compact-to-
                # bottom move_child code paths skip non-ConstructPane
                # children, so this static panel doesn't interfere
                # with construct ordering.
                self.attention_panel = AttentionPanel(id="attention_panel")
                yield self.attention_panel
                # construct panes added dynamically on spawn

            # RIGHT: files/tools tabs. Always mounted; the active tab's
            # content list is the focus target. Tab/Shift+Tab within
            # this section swaps which tab is active and refocuses
            # the new content (handled in _cycle_in_section). The tab
            # buttons themselves aren't focus targets — Textual's
            # convention is that tabs are meta-controls; content is what
            # users interact with.
            with Vertical(id="right_panel"):
                # Tab order matters: Chatlog goes first because it's
                # the most useful at-a-glance view. Files and Tools
                # are reference-only; the chatlog is *what's
                # happening right now*. New tabs append to the right.
                with TabbedContent(initial="chatlog_tab", id="right_panel_tabs"):
                    with TabPane("Chatlog", id="chatlog_tab"):
                        yield RichLog(
                            id="chatlog_log",
                            max_lines=1000,
                            markup=True,
                            # No-wrap is deliberate. The right panel is
                            # narrow (~15-25 cols) and chatlog lines
                            # frequently include daemon thinking up to
                            # 120 chars. With wrap=True, anything
                            # multi-word produced one-word-per-line
                            # vomit that lost any sense of "this is one
                            # log entry." With wrap=False, lines clip
                            # at the visible right edge and the
                            # netrunner can scroll horizontally on the
                            # rare occasion they want the full text.
                            # Per-line readability beats per-line
                            # completeness for a streaming log view.
                            wrap=False,
                            min_width=10,
                            auto_scroll=True,
                        )
                    with TabPane("Files", id="files_tab"):
                        # Files tab is a ListView of FileListItems —
                        # each file touched by a construct becomes a
                        # focusable row. C1g.4 wires space here to
                        # launch a follow-up construct with the file
                        # pre-loaded. Same architecture as Tools tab.
                        # No header label needed — empty state is
                        # handled by an in-list placeholder.
                        yield ListView(id="files_list")
                    with TabPane("Profiles", id="profiles_tab"):
                        # P5 of the tools/plugins/profiles retool
                        # (2026-05-04): profiles graduate from a
                        # section inside the Tools tab into their own
                        # tab. The dedicated tab gives profiles room
                        # without competing for vertical space against
                        # tools + plugins, and matches the conceptual
                        # split — profiles are recipes (the daemon
                        # picks them per task), tools/plugins are
                        # capabilities (constructs invoke them).
                        # Header label kept for the count badge;
                        # ListView ID `tools_profile_list` preserved
                        # so existing query_one + handler wiring
                        # stays untouched (no need to chase ID
                        # renames for cosmetic structure shifts).
                        yield Label(
                            "[b]PROFILES[/b]  [dim](0)[/dim]",
                            id="tools_profiles_header",
                        )
                        yield ListView(id="tools_profile_list")
                    with TabPane("Tools", id="tools_tab"):
                        # P5: unified list combining registry-backed
                        # tools (binary + script kinds, from
                        # <home>/tools/tools.toml) and plugins
                        # (deck-extended capabilities from
                        # <deck-source>/plugins/). Three kind glyphs
                        # distinguish row types at a glance:
                        #   ⚙ binary  — system-installed CLI on PATH
                        #   ⌬ script  — registered scripts
                        #   ⊕ plugin  — deck-extended capability
                        # Available rows render in cyan; unavailable
                        # ones (missing binary, requires-failed
                        # plugin) get a red ✗ glyph + dimmed name —
                        # same convention as the pre-P5 split panels.
                        # The legacy SCRIPTS section (flat-file
                        # auto-scan of <home>/tools/<category>/) was
                        # retired; the design's "Don't auto-discover
                        # scripts" rule applies — the only files
                        # there were deck-bootstrapped infrastructure
                        # (cyberdeck.py, plugin_bridge.py), not
                        # netrunner-meaningful tools. Multi-part
                        # scripts now register through tools.toml as
                        # `kind = "script"` entries with `path`
                        # pointing at the entry file.
                        yield Label(
                            "[b]TOOLS[/b]  [dim](0)[/dim]",
                            id="tools_unified_header",
                        )
                        yield ListView(id="tools_unified_list")

        # BOTTOM: tabbed Daemon + Watchdog. Both surfaces are
        # output-only logs the netrunner can focus + magnify with z.
        # The id="daemon_bar" stays on the wrapper TabbedContent so
        # existing focus-section navigation (grid_neighbors etc.)
        # finds the bottom region by the same name. Inner tab
        # cycling reuses the right-panel pattern (W/S walks past
        # the section's last focusable → swap inner tab).
        self.daemon_pane = DaemonPane()
        self.watchdog_pane = WatchdogPane()
        with TabbedContent(initial="daemon_tab", id="daemon_bar"):
            with TabPane("Daemon", id="daemon_tab"):
                yield self.daemon_pane
            with TabPane("Watchdog", id="watchdog_tab"):
                yield self.watchdog_pane

        yield Footer()

    def _refresh_tools_panel(self) -> None:
        """Re-populate the Profiles tab's ListView and the unified
        Tools tab's ListView from the current registry states. Called
        on profile/plugin/tool scan_complete events and once at mount
        time. Idempotent — clears each list and rebuilds.

        P5 of the tools/plugins/profiles retool (2026-05-04) split
        the tab layout: Profiles graduated to its own tab, Tools
        absorbed plugins into a unified ListView with kind glyphs
        (⚙ binary, ⌬ script, ⊕ plugin). The legacy disk-scanned
        SCRIPTS section was retired — the design's "Don't auto-
        discover" rule applies, and the only files there were
        deck-bootstrapped infrastructure anyway."""
        try:
            profile_list = self.query_one(
                "#tools_profile_list", ListView
            )
            unified_list = self.query_one(
                "#tools_unified_list", ListView
            )
            profile_header = self.query_one(
                "#tools_profiles_header", Label
            )
            unified_header = self.query_one(
                "#tools_unified_header", Label
            )
        except Exception:
            # Pre-mount or test harness without right panel.
            return

        # ---- Profiles tab -----------------------------------------
        profiles_by_cat = self.profile_registry.by_category()
        profile_total = sum(len(ps) for ps in profiles_by_cat.values())
        profile_header.update(
            f"[b]PROFILES[/b]  [dim]({profile_total})[/dim]"
        )
        profile_list.clear()
        if profile_total == 0:
            empty_item = ListItem(Label(
                "[dim](no profiles — drop a .toml in "
                "~/profiles/)[/dim]",
                markup=True,
            ))
            empty_item.disabled = True
            profile_list.append(empty_item)
        else:
            for cat, profiles_in_cat in profiles_by_cat.items():
                for p in profiles_in_cat:
                    # Tool count badge. Post-P4 the field is `tools`
                    # (registry-backed CLI names). Legacy profiles
                    # that haven't been migrated still surface
                    # values via `recommended_tools`; we count
                    # whichever is present.
                    tool_count = len(p.tools) or len(p.recommended_tools)
                    count = (
                        f"[dim]{tool_count}t[/dim]"
                        if tool_count
                        else "[dim]·[/dim]"
                    )
                    label_markup = (
                        f"[dim]{cat}/[/dim]"
                        f"[cyan]{p.name}[/cyan]  {count}"
                    )
                    profile_list.append(
                        ProfileListItem(p, label_markup)
                    )

        # ---- Tools tab (unified tools + plugins) ------------------
        # Three kinds of rows. Render order: binary tools → script
        # tools → plugins. Within each section, alphabetical by name.
        # Single header showing availability ratio across all kinds —
        # unavailable count surfaces tools the netrunner registered
        # but the deck can't locate, plus plugins whose `requires`
        # checks failed.
        tools_by_kind = self.tool_registry.by_kind()
        binary_tools = sorted(
            tools_by_kind.get("binary", []),
            key=lambda t: t.name,
        )
        script_tools = sorted(
            tools_by_kind.get("script", []),
            key=lambda t: t.name,
        )
        all_plugins = sorted(
            self.plugin_registry.all(),
            key=lambda pl: (pl.category, pl.name),
        )

        total_count = (
            len(binary_tools) + len(script_tools) + len(all_plugins)
        )
        avail_count = (
            len([t for t in binary_tools if t.available])
            + len([t for t in script_tools if t.available])
            + len([pl for pl in all_plugins if pl.available])
        )
        if total_count == 0:
            unified_header.update(
                f"[b]TOOLS[/b]  [dim](0)[/dim]"
            )
        elif avail_count == total_count:
            unified_header.update(
                f"[b]TOOLS[/b]  [dim]({total_count})[/dim]"
            )
        else:
            unified_header.update(
                f"[b]TOOLS[/b]  "
                f"[dim]({avail_count}/{total_count} available)[/dim]"
            )

        unified_list.clear()
        if total_count == 0:
            empty_item = ListItem(Label(
                "[dim](empty — register CLIs in "
                "~/tools/tools.toml or drop plugins in "
                "<deck-source>/plugins/<name>/)[/dim]",
                markup=True,
            ))
            empty_item.disabled = True
            unified_list.append(empty_item)
        else:
            # Tools (binary + script) come first. Plugins after.
            # Within tools, sort by kind (binary first) then name.
            for tool in binary_tools:
                self._append_tool_row(
                    unified_list, tool, glyph="⚙",
                )
            for tool in script_tools:
                self._append_tool_row(
                    unified_list, tool, glyph="⌬",
                )
            for pl in all_plugins:
                self._append_plugin_row(unified_list, pl)

    def _append_tool_row(
        self, list_view: "ListView", tool, *, glyph: str,
    ) -> None:
        """Render one registry-backed tool row in the unified Tools
        list. Cyan name when available; red ✗ + dimmed name when
        the deck couldn't locate the binary/script. The Tool object
        is stashed on the ListItem so downstream handlers (z-magnify,
        future launch) can read it without a registry round-trip."""
        if tool.available:
            avail_marker = f"[dim]{glyph}[/dim]"
            name_color = "cyan"
        else:
            avail_marker = "[red]✗[/red]"
            name_color = "dim"
        label_markup = (
            f"{avail_marker} "
            f"[{name_color}]{tool.name}[/{name_color}]"
        )
        # Tools-UI Thought of Dave (build-plan 0c): use ToolListItem
        # so action_primary / action_expand can isinstance-dispatch
        # cleanly. Tool object stashed on .tool for downstream
        # readers (launch modal, info modal).
        list_view.append(ToolListItem(tool, label_markup))

    def _append_plugin_row(
        self, list_view: "ListView", pl,
    ) -> None:
        """Render one plugin row in the unified Tools list. The ⊕
        glyph distinguishes plugins from binary/script tools.
        Category prefix matches the pre-P5 plugin rendering so the
        netrunner sees `Capture/screenshot`, not just `screenshot`."""
        if pl.available:
            avail_marker = "[dim]⊕[/dim]"
            name_color = "cyan"
        else:
            avail_marker = "[red]✗[/red]"
            name_color = "dim"
        label_markup = (
            f"{avail_marker} [dim]{pl.category}/[/dim]"
            f"[{name_color}]{pl.name}[/{name_color}]"
        )
        list_view.append(PluginListItem(pl, label_markup))

    def _handle_profile_event(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: profile.<kind> event arrived.

        The DeckEvent's payload is the ProfileEvent dataclass.
        Coalesces display updates by only re-rendering on
        'scan_complete' (fires after a batch of changes settles);
        individual added/changed/removed events are no-ops here.
        scan_error gets surfaced in the chatlog so the netrunner
        sees their broken TOML quickly. Phase 8 of the unified-
        event-stream slice migrated this from the legacy `on_event=`
        callback to a bus subscriber."""
        event: ProfileEvent = deck_event.payload
        if event.kind == "scan_error":
            # Show the error inline in the chatlog. Keep it short —
            # the full path + reason is in stderr already (and in the
            # event itself for any future log surface).
            err = (event.error or "?").replace("\n", " ")
            if len(err) > 120:
                err = err[:117] + "..."
            self._chatlog_write(
                f"[red]profile error:[/red] {err}"
            )
            return
        if event.kind == "scan_complete":
            self._refresh_tools_panel()
            # Also surface a one-liner in the chatlog so changes are
            # visible even if the netrunner is on a different tab.
            count = len(self.profile_registry.all())
            self._chatlog_write(
                f"[yellow]profiles updated[/yellow] [dim]({count} loaded)[/dim]"
            )

    def _handle_plugin_event(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: plugin.<kind> event arrived.

        The DeckEvent's payload is the PluginEvent dataclass. Mirrors
        _handle_profile_event in shape: errors get surfaced in the
        chatlog, scan_complete triggers a Tools-panel re-render.
        Difference is that there's only one scan ever (no hot
        reload), so this fires at most once during deck startup
        plus any time the netrunner manually triggers a re-scan
        (deferred — no UI for that yet). Phase 8 of the unified-
        event-stream slice migrated this from the legacy `on_event=`
        callback to a bus subscriber."""
        event: PluginEvent = deck_event.payload
        if event.kind == "scan_error":
            err = (event.error or "?").replace("\n", " ")
            if len(err) > 120:
                err = err[:117] + "..."
            self._chatlog_write(
                f"[red]plugin error:[/red] {err}"
            )
            return
        if event.kind == "scan_complete":
            self._refresh_tools_panel()
            total = len(self.plugin_registry.all())
            avail = len(self.plugin_registry.available())
            # Suppress the chatlog line when there are no plugins
            # at all — empty plugins dir is a valid state on first
            # launch and shouldn't generate a notification.
            if total > 0:
                avail_note = (
                    f", {total - avail} unavailable"
                    if avail != total else ""
                )
                self._chatlog_write(
                    f"[yellow]plugins loaded[/yellow] "
                    f"[dim]({avail} available{avail_note})[/dim]"
                )

    def _handle_plugin_hook_event(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: plugin.hook_<status> event arrived.

        Status: loaded / skipped / error. Only `error` and `loaded`
        chatlog-render — `skipped` is the common case (a plugin
        without deck-side integration like screenshot) and would
        spam the chatlog if announced. P3 of the tools/plugins/
        profiles retool, 2026-05-03."""
        payload = deck_event.payload or {}
        if not isinstance(payload, dict):
            return
        plugin_name = payload.get("plugin_name", "?")
        status = payload.get("status", "?")
        reason = payload.get("reason", "")
        if status == "error":
            short = reason.replace("\n", " ")
            if len(short) > 160:
                short = short[:157] + "..."
            self._chatlog_write(
                f"[red]plugin hook error:[/red] {plugin_name} · {short}"
            )
        elif status == "loaded":
            self._chatlog_write(
                f"[yellow]plugin hook loaded:[/yellow] [dim]{plugin_name}[/dim]"
            )
        # status == "skipped" is silent — it's the no-op-plugin case
        # (most plugins won't have load_into_deck) and chatlog noise
        # would be worse than the visibility benefit.

    def _run_plugin_hooks(self) -> None:
        """Call each available plugin's `load_into_deck(app)` hook,
        if defined. P3 of the tools/plugins/profiles retool.

        Runs once during on_mount, after plugin_registry.scan() has
        populated the registry. Per-plugin try/except — a crashing
        hook skips that plugin's deck-side integration but the deck
        still boots.

        Plugins are imported via `importlib.util.spec_from_file_
        location` so the deck process loads `plugin.py` as a module
        without putting plugin folders on sys.path. Module names use
        a `cyberdeck_plugin_<name>` prefix to avoid sys.modules
        collisions; `load_into_deck` is only called if it exists as
        a module-level callable.

        Skipped:
          - Unavailable plugins (requires-failed): their deps may
            not even be importable.
          - Plugins with no `plugin.py` (`entry` field points
            elsewhere): the subprocess entry can be a different file,
            but deck-side integration must live in `plugin.py`.
          - Plugins where `load_into_deck` doesn't exist: legitimate
            no-op (screenshot is the canonical example — pure
            stateless capture, nothing to integrate deck-side).

        Errored:
          - Module import raised
          - `load_into_deck` exists but isn't callable
          - `load_into_deck(app)` itself raised

        Idempotency: this method runs ONCE during on_mount; the
        plugin's hook implementation is responsible for being
        idempotent against its own repeated bus subscribes etc. if
        the deck somehow re-runs hooks (it shouldn't, but defending
        the contract is cheap).
        """
        import importlib.util
        import sys as _sys

        for pl in self.plugin_registry.available():
            if pl.source_dir is None:
                # In-memory plugin (test fixture). No filesystem
                # source to import from; skip.
                self._publish_plugin_hook_event(
                    pl.name, "skipped",
                    reason="no source_dir",
                )
                continue
            plugin_py = pl.source_dir / "plugin.py"
            if not plugin_py.is_file():
                self._publish_plugin_hook_event(
                    pl.name, "skipped",
                    reason=f"no plugin.py at {plugin_py}",
                )
                continue

            module_name = f"cyberdeck_plugin_{pl.name}"
            try:
                spec = importlib.util.spec_from_file_location(
                    module_name, plugin_py,
                )
                if spec is None or spec.loader is None:
                    self._publish_plugin_hook_event(
                        pl.name, "error",
                        reason="spec_from_file_location returned None",
                    )
                    continue
                module = importlib.util.module_from_spec(spec)
                # Stash in sys.modules so relative imports inside
                # the plugin work and so the mechanic v2 integrity
                # scan (deferred) can find loaded modules.
                _sys.modules[module_name] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                self._publish_plugin_hook_event(
                    pl.name, "error",
                    reason=f"import failed: {exc!r}",
                )
                continue

            hook = getattr(module, "load_into_deck", None)
            if hook is None:
                # Legitimate no-op — plugin has no deck-side
                # integration (e.g. screenshot, pure capture).
                self._publish_plugin_hook_event(
                    pl.name, "skipped",
                    reason="no load_into_deck",
                )
                continue
            if not callable(hook):
                self._publish_plugin_hook_event(
                    pl.name, "error",
                    reason=(
                        f"load_into_deck is not callable "
                        f"(got {type(hook).__name__})"
                    ),
                )
                continue

            try:
                hook(self)
            except Exception as exc:
                self._publish_plugin_hook_event(
                    pl.name, "error",
                    reason=f"load_into_deck raised: {exc!r}",
                )
                continue

            self._publish_plugin_hook_event(pl.name, "loaded")

    def _publish_plugin_hook_event(
        self,
        plugin_name: str,
        status: str,
        *,
        reason: str = "",
    ) -> None:
        """Emit a `plugin.hook_<status>` bus event.

        Status: loaded / skipped / error. Severity: error events
        ride at WARNING (not ERROR — a single broken plugin
        shouldn't poison the broader log signal); loaded/skipped
        ride at INFO. Same convention as plugin.scan_error vs
        plugin.loaded."""
        try:
            from event_bus import DeckEvent
            severity = "warning" if status == "error" else "info"
            self.bus.publish(DeckEvent(
                kind=f"plugin.hook_{status}",
                source="tui.plugin_hooks",
                severity=severity,
                payload={
                    "plugin_name": plugin_name,
                    "status": status,
                    "reason": reason,
                },
            ))
        except Exception as exc:
            # Defense in depth — bus publish failure shouldn't
            # break the hook-loading loop.
            import sys as _sys
            print(
                f"plugin_hook: bus publish failed for "
                f"{plugin_name!r}: {exc!r}",
                file=_sys.stderr,
            )

    def _handle_tool_event(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: tool.<kind> event arrived.

        P1 of the tools/plugins/profiles retool. Mirrors the shape of
        _handle_profile_event / _handle_plugin_event: per-event
        kinds (added/changed/removed/unavailable) are quiet, the
        scan_complete tick rebuilds the Tools panel and posts a
        chatlog line. scan_error surfaces inline so the netrunner
        sees broken tools.toml fast.
        """
        event: ToolEvent = deck_event.payload
        if event.kind == "scan_error":
            err = (event.error or "?").replace("\n", " ")
            if len(err) > 120:
                err = err[:117] + "..."
            self._chatlog_write(
                f"[red]tools error:[/red] {err}"
            )
            return
        if event.kind == "unavailable":
            # Per-tool unavailability gets a soft chatlog note —
            # netrunner-actionable signal (the tool's declared but
            # missing). Short form, the panel renders the full reason
            # on hover.
            reason = (event.error or "?").replace("\n", " ")
            if len(reason) > 100:
                reason = reason[:97] + "..."
            self._chatlog_write(
                f"[yellow]tool unavailable:[/yellow] "
                f"[b]{event.name}[/b] [dim]· {reason}[/dim]"
            )
            return
        if event.kind == "scan_complete":
            self._refresh_tools_panel()
            total = len(self.tool_registry.all())
            avail = len(self.tool_registry.available())
            # Suppress chatlog noise on the empty-registry case (fresh
            # install, no [[tool]] entries declared yet).
            if total > 0:
                avail_note = (
                    f", {total - avail} unavailable"
                    if avail != total else ""
                )
                self._chatlog_write(
                    f"[yellow]tools updated[/yellow] "
                    f"[dim]({avail} available{avail_note})[/dim]"
                )

    def on_mount(self) -> None:
        self.title = "Cyberdeck"
        self._refresh_subtitle()
        # Populate the Tools tab from current registry state. Empty at
        # startup; the registry's first scan_complete event will refill
        # via _handle_profile_event → _refresh_tools_panel. Calling it
        # here ensures the construct-tools list is populated even in
        # the rare case of a delayed first scan.
        self._refresh_tools_panel()
        # Set initial daemon status
        if self.daemon_pane is not None:
            self.daemon_pane.set_status("idle")
        # Initialize the pool meter. If use_pool is False, hide it
        # entirely. If True, prime with target=pool_size, all empty —
        # cells fill from the left as warming completes.
        if self.pool_meter is not None:
            self.pool_meter.set_enabled(self.use_pool)
            if self.use_pool:
                self.pool_meter.update_state(
                    warm=0, warming=0, target=self.pool_size,
                )
        # Start the profile registry. Runs independently of goal mode,
        # so the Tools tab is live whether or not a goal is set. The
        # registry's first scan will fire 'scan_complete' which calls
        # _refresh_tools_panel, replacing the placeholder render above.
        # Direct create_task rather than run_worker because (a) start()
        # is a quick one-shot; the long-lived watcher it spawns is its
        # own task; and (b) Textual's run_worker has been finicky with
        # quick-returning coroutines in test harnesses.
        self._profile_registry_start_task = asyncio.create_task(
            self.profile_registry.start(),
            name="profile-registry-start",
        )
        # Tools registry (P1 retool 2026-05-03). Same lifecycle shape
        # as profile_registry — start() does an initial scan + spawns
        # a background mtime-watcher; on_unmount calls shutdown().
        self._tool_registry_start_task = asyncio.create_task(
            self.tool_registry.start(),
            name="tool-registry-start",
        )
        # Plugin registry is one-shot: scan synchronously here. No
        # background task to manage, no shutdown needed. Failures
        # during a single plugin's load become 'scan_error' events
        # routed through _handle_plugin_event; the scan as a whole
        # finishes regardless. Tools panel re-renders on
        # scan_complete to display whatever was found.
        try:
            self.plugin_registry.scan()
        except Exception as exc:
            # Defense in depth — the registry is supposed to swallow
            # per-plugin errors itself, but a top-level crash
            # shouldn't take down the deck. Log and proceed with an
            # empty registry.
            import sys as _sys
            print(
                f"plugin_registry: top-level scan crashed: {exc!r}",
                file=_sys.stderr,
            )
        # Plugin deck-side hooks (P3 of the tools/plugins/profiles
        # retool, 2026-05-03). Each available plugin gets its
        # plugin.py imported into the deck process and its optional
        # `load_into_deck(app)` function called once. Deliberately
        # AFTER scan() so we know which plugins are available + valid;
        # AFTER the rest of on_mount's setup completes is unnecessary
        # — plugins receive `self` and can inspect/subscribe to
        # whatever they need at the time the hook fires. Per-plugin
        # try/except inside _run_plugin_hooks; a crashing hook
        # surfaces a chatlog warning but doesn't break startup.
        try:
            self._run_plugin_hooks()
        except Exception as exc:
            # Top-level guard: should never trigger because
            # _run_plugin_hooks has its own per-plugin try/except,
            # but defensive in case the loop itself crashes.
            import sys as _sys
            print(
                f"plugin_hooks: loop crashed: {exc!r}",
                file=_sys.stderr,
            )
        # Watchdog worker loop. Idempotent start; runs until
        # on_unmount tears it down. Independent of fleet/daemon —
        # always available so the netrunner can ask questions even
        # when no goal is set.
        self._watchdog_start_task = asyncio.create_task(
            self.watchdog.start(),
            name="watchdog-start",
        )
        # Replay prior-session Q&A history into the WatchdogPane so
        # the netrunner's conversation persists across deck restarts.
        # First slice of the watchdog-log initiative; safe to fail
        # silently (history is best-effort observability).
        self._replay_watchdog_history()
        # ConnectionMonitor heartbeats start immediately. Independent
        # of fleet/daemon lifecycle — the indicator should be live
        # from the moment the deck opens, not just during sessions.
        self._connection_monitor_start_task = asyncio.create_task(
            self.connection_monitor.start(),
            name="connection-monitor-start",
        )
        # Slice 3: DelayMonitor polls the spawns dir for delay_pending
        # files. Cheap; runs unconditionally because delay_window_
        # seconds == 0 just means no files ever appear. Mirrors
        # ConnectionMonitor's start pattern.
        self._delay_monitor_start_task = asyncio.create_task(
            self.delay_monitor.start(),
            name="delay-monitor-start",
        )
        # 100ms refresh tick that drains the per-pane delay overlay's
        # countdown bar + the Delays tab's list items. Cheap when no
        # delays open; visible smooth countdown when one is. Mirrors
        # the EJECT modal's tick interval.
        self._delay_refresh_timer = self.set_interval(
            0.1, self._refresh_delay_countdowns
        )
        # Mechanic v0→v1 bridge (2026-05-04): liveness heartbeat.
        # Writes `<home>/.cyberdeck/heartbeat` every 5s with the
        # current timestamp. Mechanic v0 watches the deck PID
        # already, but a wedged event loop / hung Textual redraw
        # cycle keeps the PID alive while the netrunner sees a
        # frozen TUI — the heartbeat catches that case. Mechanic
        # v0+1 reads the heartbeat file's mtime and flags as stale
        # after ~20s; v1 LLM session (deferred) is what'll decide
        # what to do about it. For now the v0+1 path just logs
        # "stale heartbeat detected" — diagnostic only, no action.
        self._heartbeat_timer = self.set_interval(
            5.0, self._write_heartbeat
        )
        # Write once immediately so a fast-launching mechanic that
        # checks before the first 5s tick doesn't see a missing
        # heartbeat file.
        self._write_heartbeat()
        self.run_worker(self._drive_fleet(), exclusive=True, name="fleet")

    def _write_heartbeat(self) -> None:
        """Touch `<home>/.cyberdeck/heartbeat` with the current
        timestamp. Mechanic v0→v1 bridge (2026-05-04). Called every
        5s on a Textual interval timer.

        The Mechanic supervisor (mechanic.py, sibling process)
        watches this file's mtime. When the deck PID is alive but
        the heartbeat is older than ~20s, the supervisor knows the
        TUI is wedged (event loop stuck, Textual redraw cycle
        frozen, etc.) — a case PID-watching alone misses. Mechanic
        v0+1 just logs the detection; v1's LLM session (deferred)
        will decide whether to terminate the wedged deck and
        restart it.

        Best-effort. Failures (read-only filesystem, permission
        denied, transient mid-restart races) are swallowed — a
        missed heartbeat tick is worse-cased as a false-positive
        wedge detection from the supervisor side, which logs but
        doesn't act. Better to drop a tick silently than crash the
        timer task and stop heartbeating entirely.
        """
        try:
            beat_dir = self.home_dir / ".cyberdeck"
            beat_dir.mkdir(parents=True, exist_ok=True)
            beat_path = beat_dir / "heartbeat"
            # Single-line content: ISO timestamp + monotonic. Both
            # are useful — wall-clock for human inspection, monotonic
            # for the supervisor's staleness math (avoids clock-skew
            # surprises on systems where the clock jumps).
            import time as _time
            beat_path.write_text(
                f"{_time.time():.3f} {_time.monotonic():.3f}\n",
                encoding="utf-8",
            )
        except OSError:
            # See docstring — silently skip. Persistent failures
            # surface via the supervisor's stale-heartbeat warning,
            # which is the correct end of the system to notice and
            # act on this kind of problem.
            pass

    def _refresh_subtitle(self) -> None:
        """Update the window subtitle based on current state."""
        if self.goal:
            preview = self.goal[:40] + "..." if len(self.goal) > 40 else self.goal
            self.sub_title = f"goal: {preview}"
        else:
            self.sub_title = "idle — press 'e' to set a goal"

    async def _drive_fleet(self) -> None:
        # Single SessionManager for the lifetime of this app instance.
        # Loaded eagerly so cross-restart reuse can validate prior-run
        # warm sessions against staleness before the pool starts.
        # Manifest lives under the home dir so it migrates with the
        # rest of the deck's user data — survives source-tree moves,
        # stays out of git when source is checked in, etc.
        self.session_manager = SessionManager(
            manifest_path=self.home_dir / ".cyberdeck" / "sessions.json",
        )
        self.session_manager.load()

        # Boot recovery: clean up the manifest from the prior run.
        # Orphaned in_use sessions become expired (their subprocess is
        # gone). Stale warm sessions also expire (>5h old). Fresh
        # warm sessions survive — the pool will reuse them, saving us
        # from re-warming on every launch.
        recovery = self.session_manager.boot_recovery()
        reused_warm_count = len(self.session_manager.filter(
            kind="construct", state="warm"
        ))

        # Sync the meter with reality BEFORE the pool starts. If we
        # have warm sessions inherited from a prior run, the pool's
        # start() won't emit any "warmed" events for them — they're
        # already warm. So we update the meter directly to reflect the
        # inherited count; without this, the meter would stay at 0/N
        # until the next pool event (potentially never on a fully-
        # reused boot).
        if self.pool_meter is not None and self.use_pool:
            self.pool_meter.update_state(
                warm=reused_warm_count,
                warming=0,
                target=self.pool_size,
            )

        # Session pool: pre-warms default-profile constructs in the
        # background. M5.3b lands the pool itself; M5.3c will have the
        # daemon actually pull from it. With --no-pool, we skip pool
        # creation entirely (cheap mock testing, metered connections).
        # Pool warming subprocesses share the home dir with everyone
        # else — they're constructs, just very short-lived ones.
        if self.use_pool:
            self.session_pool = SessionPool(
                manager=self.session_manager,
                target_size=self.pool_size,
                claude_bin=self.claude_bin,
                on_event=self._handle_pool_event,
                cwd=str(self.home_dir),
                # Caliber Phase 2 (2026-05-04): pool warms at the
                # deck's default caliber. Spawns that match reuse
                # warm sessions; mismatches (daemon picked haiku+low
                # for cheap recon, opus+xhigh for synthesis) fall
                # through to fresh. Same shape as the existing
                # default-profile-only gating.
                warm_caliber=self.default_caliber,
            )

        self.fleet = Fleet(
            claude_bin=self.claude_bin,
            permission_mode=self.permission_mode,
            # Phase 7: don't ask Fleet to write its own NDJSON log —
            # DeckLogger subscribes to the bus and captures fleet
            # events along with everything else in one per-launch
            # file. Fleet's standalone `python fleet.py` console mode
            # still uses log_path for backward compat (see fleet.py
            # __main__), so the parameter stays on the Fleet API.
            log_path=None,
            install_signal_handlers=False,  # Textual owns SIGINT
            quiet=True,  # no console print; TUI renders instead
            max_concurrent=self.max_concurrent,
            session_manager=self.session_manager,
            session_pool=self.session_pool,
            cwd=str(self.home_dir),
            deck_addendum=self._build_deck_addendum(),
            # Brake-hook plumbing: fleet reads current brake at each
            # spawn (lambda closes over the store, not its value, so
            # changes to the store between spawns are reflected).
            # home_dir tells fleet where to drop the per-spawn
            # settings JSON (.cyberdeck/spawns/).
            brake_state_provider=lambda: self.brake_state_store.state,
            home_dir=self.home_dir,
            # Connection-aware spawn gate. ConnectionMonitor's state
            # attribute is read at every spawn; DEGRADED or OFFLINE
            # blocks the spawn cleanly with a fleet-log entry.
            connection_state_provider=lambda: self.connection_monitor.state,
            # Bus is the canonical event channel; Fleet publishes
            # every FleetEvent through it (Phase 2 of the unified-
            # event-stream slice; Phase 8 retired the legacy
            # `add_listener` shim and made bus the only fan-out).
            # See `Design Files/archive/shipped/cyberdeck-event-stream-design.md`.
            bus=self.bus,
            # Wedge-timeout ceiling for the post-stdout-close wait.
            # Limits modal mutates fleet.wedge_timeout_seconds in
            # place; the wait-call site reads it fresh per finalize.
            wedge_timeout_seconds=self.wedge_timeout_seconds,
            # Slice 3 delay window. Read fresh per spawn by Fleet
            # when it builds the per-spawn settings JSON. Limits
            # modal updates fleet.delay_window_seconds; new spawns
            # pick up the new value, in-flight spawns keep theirs.
            delay_window_seconds=self.delay_window_seconds,
            # Caliber Phase 2 (2026-05-04): deck-global fast_mode
            # cost governor. Read fresh per spawn (lambda closes
            # over self, so a runtime toggle takes effect on the
            # next spawn). Fleet gates on Opus 4.6 eligibility:
            # incompatible spawns silently skip fast and log a
            # `caliber.fast_skipped` event so the netrunner sees
            # when fast was wanted but ineligible.
            fast_mode_provider=lambda: self.fast_mode,
        )
        # Phase 8: subscribe via the bus instead of fleet.add_listener.
        # `fleet.*` matches every dotted-namespace fleet kind
        # (fleet.spawn, fleet.finalize, fleet.event, fleet.run_start,
        # fleet.run_end, fleet.spawn_blocked, fleet.spawn_failed,
        # fleet.kill_requested). Both handlers receive a DeckEvent
        # whose payload is the FleetEvent.
        #
        # Unsubscribe prior handles before re-subscribing — _drive_fleet
        # is called once at startup AND again after EJECT respawn.
        # Without this, accumulated subscriptions caused the spawn
        # handler to fire N times per event, mounting N panes per
        # construct (the others stuck at [STARTING] forever as
        # orphans because self.panes[cid] gets overwritten). Real-
        # deck-caught 2026-04-30 late.
        if self._fleet_event_sub is not None:
            try:
                self._fleet_event_sub.unsubscribe()
            except Exception:
                pass
        if self._fleet_tripwire_scan_sub is not None:
            try:
                self._fleet_tripwire_scan_sub.unsubscribe()
            except Exception:
                pass
        self._fleet_event_sub = self.bus.subscribe(
            self._handle_event,
            filter=["fleet.*"],
            name="tui.fleet_event",
        )
        # Tripwire scanner subscription — feeds construct events into
        # the watchdog's TripwireEngine. Registered separately from
        # the main event handler so the engine can be wrapped/replaced
        # without touching chatlog rendering. Per spec, the watchdog
        # owns the engine; Fleet stays ignorant of tripwires.
        self._fleet_tripwire_scan_sub = self.bus.subscribe(
            self._scan_for_tripwires,
            filter=["fleet.*"],
            name="tui.fleet_tripwire_scan",
        )
        fleet_log = self.query_one("#fleet_log", RichLog)
        async with self.fleet as fleet:
            self._refresh_sidebar_info()
            fleet_log.write(f"[b]start:[/b] {fleet.run_id}")
            # Surface the home dir on startup. One-time log line so the
            # netrunner sees where constructs will be reading/writing.
            # Skipped if the home dir is the cwd (i.e., legacy behavior
            # / no isolation) — that's not worth a log line.
            try:
                if self.home_dir.resolve() != Path.cwd().resolve():
                    fleet_log.write(
                        f"[dim]home: {self.home_dir}[/dim]"
                    )
            except Exception:
                pass

            # Surface boot recovery info. Three numbers a netrunner
            # might care about: how many warm sessions we inherit from
            # the prior run, how many entries we expired due to age
            # or orphaning. Quiet if nothing happened.
            if reused_warm_count > 0:
                fleet_log.write(
                    f"[dim]reused {reused_warm_count} warm session(s) "
                    f"from prior run[/dim]"
                )
            if recovery["orphaned"]:
                fleet_log.write(
                    f"[dim]expired {len(recovery['orphaned'])} orphaned "
                    f"session(s) from prior run[/dim]"
                )
            if recovery["stale"]:
                fleet_log.write(
                    f"[dim]expired {len(recovery['stale'])} stale "
                    f"session(s) (>5h old)[/dim]"
                )

            # Resolve the active profile via the registry. Wait for
            # the registry's initial scan to complete first so we look
            # up against a populated map (start() runs in parallel with
            # the fleet driver). If the name doesn't resolve — typo'd
            # flag, missing file, scan error — fall back to no profile
            # rather than crashing.
            #
            # Logging policy:
            #   default + implicit  → silent (boring; baseline behavior)
            #   default + explicit  → silent too (same effect)
            #   non-default         → log the profile + category
            #   unresolved          → log the error + available names
            try:
                await self._profile_registry_start_task
            except Exception:
                pass
            resolved = self.profile_registry.get(
                self._default_profile_name
            )
            if resolved is not None:
                self.default_profile = resolved
                if resolved.name != "default":
                    fleet_log.write(
                        f"[yellow]profile:[/yellow] {resolved.name} "
                        f"[dim]({resolved.category})[/dim]"
                    )
            else:
                available = ", ".join(
                    p.name for p in self.profile_registry.all()
                ) or "(none loaded)"
                fleet_log.write(
                    f"[red]profile not found:[/red] "
                    f"{self._default_profile_name!r} — "
                    f"available: {available}"
                )

            # Begin pool warming in the background. Returns immediately;
            # warming happens in fire-and-forget asyncio tasks. The
            # PoolMeter widget shows progress visually; no need to
            # narrate it in the fleet log. The pool's start() already
            # accounts for already-warm sessions in the manifest and
            # only warms the difference — so reused warm sessions
            # mean fewer subprocess spawns at launch.
            if self.session_pool is not None:
                await self.session_pool.start()

            # If launched with a goal, start the daemon immediately.
            # Otherwise we sit in idle until the netrunner sets a goal
            # via `e`, which calls _start_daemon_task.
            if self.goal is not None:
                self._start_daemon_task()

            try:
                await fleet.run(self.tasks, keep_alive=True)
            except Exception as e:
                fleet_log.write(f"[red]error: {e!r}[/red]")

            # Ensure daemon is stopped at shutdown
            if self.session is not None:
                await self.session.shutdown()
            if self._daemon_task is not None:
                try:
                    await self._daemon_task
                except Exception:
                    pass

            # Cancel any in-flight warming. Already-warm sessions stay
            # in the manifest as 'warm' for next launch (cross-restart
            # reuse lands in M5.3e).
            if self.session_pool is not None:
                await self.session_pool.shutdown()

            # Stop the profile registry's poll loop. Idempotent — safe
            # if start() never finished or hadn't been called.
            await self.profile_registry.shutdown()

            # Stop the tools registry's poll loop (P1 retool 2026-05-
            # 03). Same idempotent shape as profile_registry.shutdown.
            await self.tool_registry.shutdown()

            # Stop the watchdog worker. Cancels any in-flight question
            # subprocess; queued questions are dropped (matches EJECT
            # semantics — snapshot + halt, no graceful drain).
            await self.watchdog.shutdown()

            # Stop the connection monitor heartbeat loop.
            await self.connection_monitor.shutdown()

            # Slice 3: stop the delay monitor polling task. Idempotent;
            # safe if start() never finished or hadn't been called.
            try:
                await self.delay_monitor.stop()
            except Exception:
                pass
            # Stop the per-tick countdown refresh.
            if self._delay_refresh_timer is not None:
                try:
                    self._delay_refresh_timer.stop()
                except Exception:
                    pass
                self._delay_refresh_timer = None

            # Phase 7: close the file logger. Writes a footer event
            # with reason="shutdown" — heartbeat sensor + maintbot use
            # this marker to distinguish clean exit from unclean
            # crash. Idempotent; safe to call multiple times.
            if self.deck_logger is not None:
                self.deck_logger.close(reason="shutdown")

            fleet_log.write("[b]complete[/b]")

    def _handle_pool_event(self, event: PoolEvent) -> None:
        """Update the RAM meter from pool state changes. Most events
        update the meter visually and are silent in the log; failures
        still surface there for diagnostics."""
        # Update the meter on every event — counts may have shifted.
        if self.pool_meter is not None:
            self.pool_meter.update_state(
                warm=event.warm_count,
                warming=event.warming_count,
                target=event.target_size,
            )
        # Surface failures in the fleet log (rare; signal to investigate).
        if event.kind == "warm_failed":
            try:
                fleet_log = self.query_one("#fleet_log", RichLog)
                fleet_log.write(
                    f"[red]pool: warm failed:[/red] {event.error}"
                )
            except Exception:
                pass

    def _handle_connection_change(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: connection.transition event arrived.

        The DeckEvent's payload is the StateChangeEvent dataclass.
        Updates the sidebar indicator + announces in chatlog so the
        netrunner sees the moment the deck went degraded/offline (or
        recovered).

        Color coding:
          green ●  online    — normal operation
          yellow ◐ degraded  — connection unstable; remote work risky
          red ●    offline   — no connection at all

        Filled vs partial-filled glyphs (●/◐) reinforce the meaning
        for color-blind netrunners — degraded is visually distinct
        from both online and offline regardless of color rendering.
        Phase 8 of the unified-event-stream slice migrated this from
        the legacy `on_state_change=` callback to a bus subscriber.
        """
        event: StateChangeEvent = deck_event.payload
        glyph_color = {
            ConnectionState.ONLINE:   ("●", "green"),
            ConnectionState.DEGRADED: ("◐", "yellow"),
            ConnectionState.OFFLINE:  ("●", "red"),
        }.get(event.new_state, ("?", "dim"))
        glyph, color = glyph_color
        try:
            indicator = self.query_one("#connection_status", Label)
            indicator.update(
                f"[{color}]{glyph}[/{color}] {event.new_state.value}"
            )
        except Exception:
            # Pre-mount or test harness without sidebar — ignore.
            pass
        # Chatlog announcement so the transition is anchored in the
        # event timeline. Useful when a netrunner comes back to a
        # session and wonders "did I lose connection at some point?"
        # Suppress the announcement if the App is still composing
        # (chatlog widget not yet mounted) — the initial Online
        # state doesn't need a "you came online!" announcement.
        try:
            self._chatlog_write(
                f"[{color}]\\[connection][/{color}] "
                f"[dim]{event.old_state.value} →[/dim] "
                f"[b {color}]{event.new_state.value}[/b {color}]  "
                f"[dim]{event.reason}[/dim]"
            )
        except Exception:
            pass

    # ---- brake state ----------------------------------------------------

    def _brake_indicator_text(self, state: "BrakeState") -> str:
        """Render the brake state for the sidebar indicator.

        Glyphs deliberately echo the gravity:
          ▲ paranoid — restrictive, "ratchet up" arrow
          = default  — neutral, no-tilt
          ▼ yolo     — permissive, "open the floodgates" arrow
        Color-coded the same way: yellow / dim white / red.

        Same shape as _handle_connection_change's renderer — single
        Label, single line, color + glyph + label."""
        glyph_color = {
            BrakeState.PARANOID: ("▲", "yellow"),
            BrakeState.DEFAULT:  ("=", "white"),
            BrakeState.YOLO:     ("▼", "red"),
        }.get(state, ("?", "dim"))
        glyph, color = glyph_color
        return f"[{color}]{glyph}[/{color}] {state.value}"

    def _handle_brake_change(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: brake.change event arrived.

        The DeckEvent's payload is the BrakeChangeEvent dataclass with
        old_state / new_state / reason. Updates the sidebar indicator
        and announces in the chatlog so the netrunner has a timeline
        anchor. New spawns will pick up the new state at spawn time;
        in-flight constructs continue under the brake they spawned
        with — that's by design (Claude Code can't have its
        --permission-mode or --settings mutated post-spawn).

        Mirrors _handle_connection_change in shape — same pattern.
        Phase 8 of the unified-event-stream slice migrated this from
        the legacy `on_change=` callback to a bus subscriber."""
        event: BrakeChangeEvent = deck_event.payload
        try:
            indicator = self.query_one("#brake_status", Label)
            indicator.update(self._brake_indicator_text(event.new_state))
        except Exception:
            # Pre-mount or test harness — ignore.
            pass
        try:
            # Pull a color hint for the chatlog line — yellow for
            # tightening, red for loosening, dim for default. Same
            # color choice as the indicator itself.
            color = {
                BrakeState.PARANOID: "yellow",
                BrakeState.DEFAULT:  "white",
                BrakeState.YOLO:     "red",
            }.get(event.new_state, "dim")
            self._chatlog_write(
                f"[{color}]\\[brake][/{color}] "
                f"[dim]{event.old_state.value} →[/dim] "
                f"[b {color}]{event.new_state.value}[/b {color}]  "
                f"[dim]({event.reason}; applies to new spawns)[/dim]"
            )
        except Exception:
            pass

    # ---- delay (slice 3 of safety architecture pass) -------------------

    def _handle_delay_opened(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: brake.delay_opened event arrived.

        The DeckEvent's payload is a DelayEntry. Job: pop the
        construct's pane overlay (countdown bar + bold verb +
        X-press hint) AND promote the pane to the top of #main so
        the netrunner doesn't have to scroll past finalized panes
        to find what needs attention. Plus a chatlog marker for the
        timeline record. The 100ms refresh timer (started on mount)
        drains the countdown bar as the deadline approaches.

        Promote-to-top is the inverse of the compact-to-bottom move
        that finalized panes get — same `move_child` mechanism, just
        the opposite end. Delaying panes carry a magenta border (see
        ConstructPane CSS) that's distinct from every other pane
        state so the visual signal pops even at a glance.
        """
        entry: DelayEntry = deck_event.payload
        try:
            pane = self.panes.get(entry.construct_id)
            if pane is not None:
                pane.set_delay(entry)
                # Promote to the top of #main. The mirror image of
                # _compact_pane_after_delay's move-to-bottom: time-
                # sensitive panes float to where the netrunner is
                # already looking. Best-effort — if the move fails
                # (race against mount), the magenta border alone is
                # still the attention signal.
                #
                # Filter to ConstructPane children — #main now also
                # hosts the AttentionPanel (phase 2) at index 0.
                # We want "first construct pane," not literally
                # "first child of #main."
                try:
                    main = self.query_one("#main", VerticalScroll)
                    first_pane = next(
                        (c for c in main.children
                         if isinstance(c, ConstructPane)),
                        None,
                    )
                    if first_pane is not None and first_pane is not pane:
                        main.move_child(pane, before=first_pane)
                except Exception:
                    pass
        except Exception:
            pass
        # Chatlog marker — timeline record. Color matches the per-pane
        # overlay's bar color: yellow when default=deny (less urgent;
        # netrunner has time to approve), red when default=allow under
        # YOLO (more urgent; netrunner has time to interrupt).
        try:
            color = "yellow" if entry.default_action == "deny" else "red"
            self._chatlog_write(
                f"[{color}]⏳[/{color}] delay: "
                f"[cyan]{entry.construct_id}[/cyan] "
                f"[dim]{entry.tool_name}[/dim] "
                f"[{color}]{entry.default_action}[/{color}] "
                f"[dim]in {entry.delay_window_seconds:g}s "
                f"· press X to {entry.override_action}[/dim]"
            )
        except Exception:
            pass

    def _handle_delay_resolved(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: brake.delay_resolved event arrived.

        Closes the per-pane overlay (border returns to whatever it
        was before — usually $accent). Pane stays at the top of
        #main; we don't auto-reorder back. The netrunner just saw
        the resolution; leaving the pane at top keeps it findable
        for follow-up. Once finalized, the existing compact-to-
        bottom path takes over.

        Post-2026-05-07 redesign: this handler is also where critical
        tripwire consequences fire (kill + optional blacklist
        proposal). Both gated on `severity=critical AND applied_action
        =deny` — i.e., the tripwire fired AND the netrunner did NOT
        X-allow within the 5s window. X-allow on a critical tripwire
        skips both the kill and the blacklist proposal; the
        netrunner's override is final.
        """
        resolution = deck_event.payload
        cid = getattr(resolution, "construct_id", "")
        reason = getattr(resolution, "reason", "unknown")
        applied = getattr(resolution, "applied_action", "?")
        tripwire_name = getattr(resolution, "tripwire_name", "")
        tripwire_severity = getattr(resolution, "tripwire_severity", "")
        tripwire_bad_enough = getattr(resolution, "tripwire_bad_enough", False)
        try:
            pane = self.panes.get(cid)
            if pane is not None:
                pane.clear_delay()
        except Exception:
            pass
        try:
            # Color: green when override applied (netrunner X), dim
            # otherwise (default expired). Visible signal that "the
            # netrunner intervened" vs "timer ran out."
            color = "green" if reason == "override" else "dim"
            label = "X-pressed" if reason == "override" else "timer expired"
            self._chatlog_write(
                f"[{color}]⏳[/{color}] delay resolved: "
                f"[cyan]{cid}[/cyan] "
                f"applied=[b]{applied}[/b] "
                f"[dim]({label})[/dim]"
            )
        except Exception:
            pass

        # Critical tripwire consequences (kill + blacklist proposal)
        # gated here. We fire only on (severity=critical AND
        # applied_action=deny). X-allow short-circuits both.
        from tripwires import Severity
        if (
            tripwire_severity == Severity.CRITICAL
            and applied == "deny"
            and self.fleet is not None
        ):
            # Auto-term the construct. Source label keeps the
            # post-hoc diagnostic trail intact ("which tripwire
            # caused this construct's death?").
            try:
                self.run_worker(
                    self.fleet.kill_construct(
                        cid,
                        source=f"tripwire_critical:{tripwire_name}",
                    ),
                    name=f"tripwire-kill-{cid}",
                )
                self._chatlog_write(
                    f"[red b]× auto-term[/red b] [cyan]{cid}[/cyan] "
                    f"[dim](critical tripwire {tripwire_name})[/dim]"
                )
            except Exception:
                # run_worker can fail if the App isn't fully mounted
                # or if the fleet is in transitional state. The
                # tripwire's deny already blocked the dangerous call;
                # missing the kill leaves the construct running but
                # not actively destructive. Acceptable degradation.
                pass

            # bad_enough → propose adding the construct's task
            # fingerprint to the session blacklist. Independent of
            # the kill (the kill stops THIS construct; the blacklist
            # prevents future respawns matching the same task shape).
            # Threaded through the delay-resolution payload so the
            # netrunner's X-allow on the call ALSO suppresses this
            # proposal (otherwise the netrunner would face two
            # decisions per fire — confusing).
            if tripwire_bad_enough:
                # Reconstruct enough of a TripwireFire-shaped object
                # for _open_blacklist_proposal. We only have the
                # subset of fields propagated through the resolution;
                # the proposal renderer uses tripwire_name +
                # construct_id primarily.
                try:
                    from tripwires import TripwireFire
                    synthetic_fire = TripwireFire(
                        tripwire_name=tripwire_name,
                        severity=tripwire_severity,
                        construct_id=cid,
                        matched_text_excerpt="",
                        event_kind="",
                        description="",
                        suggestion="",
                        bad_enough=True,
                    )
                    self._open_blacklist_proposal(synthetic_fire)
                except Exception:
                    pass

    def _refresh_delay_countdowns(self) -> None:
        """100ms timer tick: walk every active pane, refresh the
        countdown rendering on any with an open delay. Cheap (no-op
        for panes without a delay; one label.update() for those with
        one). Started in on_mount, runs for the life of the app.

        Phase 2: also refreshes the AttentionPanel so blacklist-
        proposal countdowns drain smoothly. The panel is a single
        Static; one update() call repaints all rows."""
        for pane in self.panes.values():
            try:
                pane.refresh_delay_countdown()
            except Exception:
                pass
        try:
            if self.attention_panel is not None:
                self.attention_panel.refresh_countdown()
        except Exception:
            pass

    # ---- attention items (slice 3 phase 2) ----------------------------

    def _open_attention(self, item: AttentionItem) -> None:
        """Register a new attention item. Stores it, schedules an
        expiry timer, repaints the panel, publishes the bus event so
        chatlog + future subscribers see it.

        Idempotent on item_id: if an item with the same id is already
        open, it gets replaced (timer cancelled + reissued). In
        practice item_ids are uuid-fresh per call so collisions don't
        happen, but the safety net keeps state consistent if a caller
        re-uses an id."""
        old = self._attention_items.get(item.item_id)
        if old is not None:
            t = self._attention_timers.pop(item.item_id, None)
            if t is not None and not t.done():
                t.cancel()
        self._attention_items[item.item_id] = item
        # Schedule expiry. Using run_worker rather than create_task
        # so it shows up in Textual's worker registry alongside the
        # compact workers — consistent with how the rest of the
        # deck schedules sleeps.
        async def _expire_after(item_id: str, delay: float) -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return
            # Re-check; approve may have run + cleaned up while we slept.
            if item_id in self._attention_items:
                self._resolve_attention(item_id, AttentionResolution.EXPIRED)

        worker = self.run_worker(
            _expire_after(item.item_id, item.window_seconds),
            name=f"attention-{item.item_id}",
            exclusive=False,
        )
        # run_worker returns a Worker, not a Task; we can call
        # .cancel() on either. Stored so approve can cancel.
        self._attention_timers[item.item_id] = worker

        # Repaint the panel.
        self._repaint_attention()

        # Bus event for chatlog + future subscribers.
        try:
            self.bus.publish(DeckEvent(
                kind="attention.opened",
                source="tui.attention",
                timestamp=time.time(),
                construct_id=item.construct_id,
                severity="warning",
                text=(
                    f"attention: {item.title} "
                    f"[{item.window_seconds:g}s window]"
                ),
                payload=item,
            ))
        except Exception:
            pass

    def _approve_attention(self, item_id: str) -> bool:
        """Apply the item's payload + clean up. Returns True on
        success, False if the item is unknown (already resolved or
        never existed). Called from action_x_focused when an
        attention item is the X-press target.

        Per-kind dispatch lives here. Today: blacklist_proposal applies
        the BlacklistEntry to the watchdog's session blacklist."""
        item = self._attention_items.get(item_id)
        if item is None:
            return False
        if item.kind == AttentionKind.BLACKLIST_PROPOSAL:
            try:
                if self.watchdog is not None and self.watchdog.blacklist is not None:
                    self.watchdog.blacklist.add(item.payload)
            except Exception as exc:
                # Surface the failure but still clean up — the
                # netrunner pressed X, the proposal shouldn't sit
                # there forever just because the apply failed.
                try:
                    self.query_one("#fleet_log", RichLog).write(
                        f"[red]attention apply failed:[/red] {exc!r}"
                    )
                except Exception:
                    pass
        # Future kinds dispatch here.
        self._resolve_attention(item_id, AttentionResolution.APPROVED)
        return True

    def _resolve_attention(self, item_id: str, reason: str) -> None:
        """Internal: remove the item from state, cancel any timer,
        repaint, emit attention.resolved bus event. Called from both
        the approve path (reason=approved) and the expiry path
        (reason=expired). Safe to call with a stale item_id."""
        item = self._attention_items.pop(item_id, None)
        if item is None:
            return
        timer = self._attention_timers.pop(item_id, None)
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
        self._repaint_attention()
        try:
            resolved = AttentionResolved(
                item_id=item_id,
                kind=item.kind,
                reason=reason,
                construct_id=item.construct_id,
            )
            self.bus.publish(DeckEvent(
                kind="attention.resolved",
                source="tui.attention",
                timestamp=time.time(),
                construct_id=item.construct_id,
                severity="info",
                text=(
                    f"attention resolved: {item.title} ({reason})"
                ),
                payload=resolved,
            ))
        except Exception:
            pass

    def _repaint_attention(self) -> None:
        """Push the current items list to the panel. Cheap — one
        update() call. Sorted by opened_at descending so newest
        items appear at the top (matches the netrunner's eye flow:
        most recent thing demanding attention is first)."""
        try:
            if self.attention_panel is None:
                return
            items = sorted(
                self._attention_items.values(),
                key=lambda i: i.opened_at,
                reverse=True,
            )
            self.attention_panel.update_items(items)
        except Exception:
            pass

    def _open_blacklist_proposal(self, fire) -> None:
        """Build a BlacklistEntry from the firing construct's context
        and file it as an attention item. Mirrors the entry shape
        action_hard_kill_focused builds — same fingerprint scheme,
        same context fields — so a netrunner-approved auto-blacklist
        proposal is indistinguishable from a Shift+K-driven blacklist
        once applied.

        If the construct or watchdog is missing (race against fleet
        teardown, headless test path), no-op cleanly. The auto-term
        + chatlog marker still fired upstream."""
        if self.fleet is None or self.watchdog is None:
            return
        if self.watchdog.blacklist is None:
            return
        construct = self.fleet.get_construct(fire.construct_id)
        if construct is None:
            return
        try:
            entry = BlacklistEntry(
                fingerprint=_blacklist_fingerprint(construct.task),
                full_task=construct.task,
                source_construct_id=fire.construct_id,
                source_construct_state=construct.state.value,
                source_final_output=construct.final_output[:500],
                source_files_written=tuple(construct._files_written),
                reason=f"tripwire_critical+bad_enough:{fire.tripwire_name}",
            )
        except Exception:
            return
        # Title: short, scan-friendly. Detail: longer context the
        # netrunner can read while deciding. Truncations match the
        # delay overlay's pattern.
        task_preview = construct.task or "(no task)"
        if len(task_preview) > 60:
            task_preview = task_preview[:57] + "..."
        title = (
            f"blacklist proposal: {fire.construct_id} "
            f"(tripwire {fire.tripwire_name})"
        )
        detail = (
            f"task: {task_preview}  ·  "
            f"approve to refuse future spawns matching this fingerprint"
        )
        item = AttentionItem.new(
            kind=AttentionKind.BLACKLIST_PROPOSAL,
            title=title,
            detail=detail,
            window_seconds=self.blacklist_proposal_window_seconds,
            payload=entry,
            construct_id=fire.construct_id,
        )
        self._open_attention(item)
        # Chatlog marker — same shape as the delay-opened marker so
        # the timeline reads uniformly.
        try:
            self._chatlog_write(
                f"[magenta]⚠[/magenta] blacklist proposal: "
                f"[cyan]{fire.construct_id}[/cyan] "
                f"[dim]({fire.tripwire_name})[/dim] "
                f"[dim]· press X within "
                f"{self.blacklist_proposal_window_seconds:g}s "
                f"to approve[/dim]"
            )
        except Exception:
            pass

    def _start_daemon_task(self) -> None:
        """Kick off a daemon session worker for the current goal.
        Caller must have set self.goal before calling this."""
        if self.goal is None or self.fleet is None:
            return
        if self._daemon_task is not None and not self._daemon_task.done():
            return  # already running
        self._daemon_task = asyncio.create_task(self._drive_daemon())
        # Slice 2: kick off LLM-authored tripwire pass for this goal.
        # Fire-and-forget — the daemon spins up immediately; authoring
        # runs in parallel and surfaces in the chatlog when complete.
        # First few construct events may stream in before authored
        # rules land, which is fine — the two default deck-wide
        # tripwires (credentials, destructive SQL) cover the baseline.
        self._kick_off_tripwire_authoring()

    def _build_daemon_system_prompt(self) -> str:
        """Compose the daemon's system prompt with profile + brake awareness.

        Returns the baseline DAEMON_SYSTEM_PROMPT extended with:
          - The current deck-global brake state and what it implies.
          - A PROFILES catalog listing every loaded profile (name,
            category, description, recommended_tools).
          - Profile-selection rules (any profile is selectable; the
            brake handles runtime constraint, not the profile).
          - The active profile's default_daemon_addendum, if non-empty.

        Called once per session start. If the registry hasn't loaded
        anything yet (race), the catalog will be sparse but the prompt
        is still well-formed — the active profile's addendum is the
        important bit for steering, the catalog is for delegation hints.
        """
        sections: list[str] = [DAEMON_SYSTEM_PROMPT]

        # Brake state awareness. The daemon needs to know what's
        # currently allowed so it doesn't plan actions that'll get
        # denied at the hook layer (which costs construct startup
        # tokens for nothing). One-line summary per brake — terse
        # because the hook is the ground truth; this is just enough
        # for the daemon to plan compatibly.
        brake = self.brake_state_store.state
        brake_blurb = {
            BrakeState.PARANOID: (
                "PARANOID — investigate-only mode. Constructs CANNOT "
                "Write, Edit, run Bash, or use WebFetch. Read, Glob, "
                "Grep, WebSearch, and TodoWrite are still available. "
                "Plan tasks that produce findings rather than artifacts."
            ),
            BrakeState.DEFAULT: (
                "DEFAULT — most tools allowed. Constructs CANNOT write "
                "or edit files in OS roots (Windows/Program Files, /usr, "
                "/etc, /bin, /sbin, /var, /lib, /opt) or in the deck's "
                "own source directory; CANNOT run destructive bash "
                "patterns (rm -rf on system roots, format, dd of=/dev/, "
                "mkfs, fork bombs, shutdown). Everything else is fair."
            ),
            BrakeState.YOLO: (
                "YOLO — no brakes. Constructs run unrestricted. The "
                "netrunner has explicitly accepted this; plan freely."
            ),
        }.get(brake, "(unknown brake state)")
        sections.append(
            "\nDECK BRAKE STATE:\n"
            f"Current brake: {brake.value}\n"
            f"{brake_blurb}\n"
            "The brake is deck-global, set by the netrunner via the "
            "brake modal (`b` key). It applies to every spawn until "
            "the netrunner changes it. Constructs spawned under this "
            "brake will see denials surface as tool_result errors if "
            "they attempt forbidden actions."
        )

        # Profile catalog. P4 of the tools/plugins/profiles retool
        # (2026-05-03) shifted profiles' tools field from "soft hint
        # at Claude Code tool names" to "registry-backed CLI names."
        # Each profile's `tools` references entries in tools.toml;
        # constructs spawned with the profile see those tools'
        # name + short description in their prompt addendum.
        profiles_loaded = self.profile_registry.all()
        if profiles_loaded:
            catalog: list[str] = [
                "",
                "PROFILES — available steering templates for spawns:",
                "",
            ]
            for p in profiles_loaded:
                tool_str = (
                    ", ".join(p.tools)
                    if p.tools
                    else "(no specific tools)"
                )
                desc_short = " ".join(p.description.split())  # collapse newlines
                if len(desc_short) > 200:
                    desc_short = desc_short[:197] + "..."
                catalog.append(
                    f"- {p.name} ({p.category}): "
                    f"tools={tool_str}\n"
                    f"  {desc_short}"
                )
            sections.append("\n".join(catalog))

        # Profile-selection rules. With brake state moved out of
        # profiles, profiles are purely prescriptive — selection has
        # no privesc dimension. The daemon picks the right template
        # for the work; the deck-global brake handles enforcement
        # regardless of which profile gets used.
        active_name = (
            self.default_profile.name if self.default_profile else "default"
        )
        sections.append(
            "\nPROFILE SELECTION:\n"
            f"The active profile for this run is: {active_name}\n"
            "\n"
            "By default, every spawn action runs under the active profile.\n"
            "You MAY override per-spawn by adding a `profile` field to the\n"
            "spawn action, e.g.:\n"
            '  {"type": "spawn", "task": "...", "profile": "code_reviewer"}\n'
            "\n"
            "Profiles are prescriptive templates — they steer the construct\n"
            "with a system-prompt addendum and recommended tools. They do\n"
            "NOT enforce constraints; the deck-global brake (above) is the\n"
            "single source of runtime gating. Pick the profile that best\n"
            "matches the work shape, regardless of brake level.\n"
            "\n"
            "If unsure which profile fits, omit the field — the active\n"
            "profile applies."
        )

        # Active profile's daemon-side addendum
        if (self.default_profile is not None
                and self.default_profile.default_daemon_addendum.strip()):
            sections.append(
                "\nACTIVE PROFILE STEERING — additional guidance for this run:\n"
                + self.default_profile.default_daemon_addendum.strip()
            )

        # Plugins catalog. Only available plugins are listed — no
        # point telling the daemon about a plugin whose dependencies
        # aren't installed; that just produces failed spawns. One
        # line per plugin: name + category + description (collapsed).
        # The full README isn't injected here — too much per-turn
        # token cost. Constructs read individual plugin READMEs via
        # the bridge dispatcher's --help convention or the deck-source
        # path the construct addendum provides.
        avail_plugins = self.plugin_registry.available()
        if avail_plugins:
            catalog: list[str] = [
                "",
                "PLUGINS — capability bundles available for construct invocation:",
                "",
            ]
            for pl in avail_plugins:
                desc_short = " ".join(pl.description.split())
                if len(desc_short) > 200:
                    desc_short = desc_short[:197] + "..."
                catalog.append(
                    f"- {pl.name} ({pl.category}): {desc_short}"
                )
            catalog.append("")
            catalog.append(
                "When a construct needs a plugin's capability, instruct "
                "it to invoke the plugin via Bash through the bridge "
                "dispatcher: "
                "`python <home>/tools/deck/plugin_bridge.py <plugin_name> "
                "[args]`. The construct doesn't need to know where "
                "plugin code lives — the bridge resolves and forwards. "
                "The construct's spawn-time addendum carries the exact "
                "invocation shape and per-plugin docs path."
            )
            sections.append("\n".join(catalog))

        return "\n".join(sections)

    async def _drive_daemon(self) -> None:
        """Run a daemon session against the current goal. Returns to
        idle on completion (does not kill the app)."""
        assert self.goal is not None
        assert self.fleet is not None

        # Build the daemon's system prompt with profile context baked
        # in. Two additions on top of the baseline DAEMON_SYSTEM_PROMPT:
        #
        #   1. A PROFILES section listing every loaded profile with
        #      its category, description, and tool set. Tells the
        #      daemon what tools are available for delegation.
        #   2. The active profile's default_daemon_addendum, if any.
        #      This is the netrunner's run-level steering for the
        #      daemon itself (not for spawned constructs).
        #
        # Profile-switching rules are documented in the prompt so the
        # daemon knows it CAN ask for a non-default profile per spawn,
        # but also knows the privesc rule (de-escalate only).
        system_prompt = self._build_daemon_system_prompt()

        self.daemon = Daemon(
            claude_bin=self.daemon_bin,
            streaming_mode=self.streaming_mode,
            cwd=str(self.home_dir),
            system_prompt=system_prompt,
            # Caliber Phase 3 (2026-05-04): apply the deck's
            # daemon_caliber to the subprocess command line. This
            # bakes at subprocess-start time — T-chat directives
            # mutating self.daemon_caliber take effect on the NEXT
            # daemon run / goal restart, not mid-flight.
            caliber=self.daemon_caliber,
        )
        self.session = DaemonSession(
            daemon=self.daemon,
            fleet=self.fleet,
            on_daemon_event=self._handle_daemon_event,
            max_total_spawns=self.max_total_spawns,
            default_profile=self.default_profile,
            # Wire the registry as the profile resolver so the daemon's
            # per-spawn profile picks land against live profile data.
            profile_lookup=self.profile_registry.get,
            # Hand the daemon-session a reference to the watchdog's
            # session blacklist. _execute_action checks each daemon
            # spawn against it; matching spawns get refused with a
            # feedback line in the next outcome turn. Watchdog owns
            # the data structure (per spec); DaemonSession just reads.
            blacklist=self.watchdog.blacklist,
            # Phase 3 of the unified-event-stream slice: pass the
            # bus so DaemonEvents flow through it alongside the
            # existing on_daemon_event callback. The wrapper inside
            # DaemonSession means callsites stay unchanged — every
            # existing emission picks up bus publish for free.
            bus=self.bus,
            # 2026-05-02 race fix: gate spawn dispatch on tripwire
            # authoring completion. Event is SET ("ok to spawn") in
            # the App's __init__ and re-set by the authoring
            # wrapper's finally block; cleared by _kick_off_tripwire_
            # authoring on each authoring kickoff. DaemonSession
            # awaits this event in _execute_action before
            # dispatching each spawn — first batch waits for
            # authoring; subsequent batches within the same goal find
            # it set and proceed immediately.
            tripwire_authoring_complete=self._tripwire_authoring_complete,
            # P4 of the tools/plugins/profiles retool (2026-05-03):
            # callback the daemon-session calls before each spawn to
            # render the per-spawn addendum (profile.tools resolved
            # against tool_registry + plugins resolved against
            # plugin_registry). Centralizes registry access on the
            # TUI side; daemon-session stays a thin glue layer.
            per_spawn_addendum_renderer=self._build_per_spawn_addendum,
            # Caliber Phase 1 (2026-05-04): the deck's default
            # per-spawn caliber. DaemonSession uses this as the
            # fall-through when the daemon's spawn action doesn't
            # specify model/effort/fast_mode, so every spawn carries
            # an explicit caliber on its CLI args.
            default_caliber=self.default_caliber,
        )

        if self.daemon_pane is not None:
            self.daemon_pane.set_status("working")

        try:
            await self.session.run(self.goal)
        except Exception as e:
            if self.daemon_pane is not None:
                self.daemon_pane.write_error(repr(e))
                self.daemon_pane.set_status("failed")
        finally:
            # Return to idle: clear the daemon/session refs and reset
            # the goal so the netrunner can set a new one. The app
            # stays alive; only the daemon session ends.
            self._return_to_idle()

    def _return_to_idle(self) -> None:
        """Reset daemon state so the app is ready for a new goal."""
        self.daemon = None
        self.session = None
        self._daemon_task = None
        self.goal = None
        if self.goal_pane is not None:
            self.goal_pane.set_goal("")
        if self.daemon_pane is not None:
            self.daemon_pane.set_status("idle")
            self.daemon_pane.write_line(
                "[dim]— session complete; press 'e' to set a new goal —[/dim]"
            )
        self._refresh_subtitle()
        self._refresh_sidebar_info()

    def _handle_daemon_event(self, event: DaemonEvent) -> None:
        """Route a daemon event to the daemon pane and log it."""
        if self.daemon_pane is None:
            return
        if event.kind == "thinking":
            self.daemon_pane.write_thinking(event.payload.get("text", ""))
        elif event.kind == "chat":
            self.daemon_pane.write_chat(event.payload.get("text", ""))
        elif event.kind == "action":
            self.daemon_pane.write_action(event.payload.get("action", {}))
        elif event.kind == "status":
            self.daemon_pane.set_status(event.payload.get("status", "?"))
        elif event.kind == "error":
            self.daemon_pane.write_error(event.payload.get("text", ""))
        # 'started' and 'raw' aren't rendered in the daemon pane —
        # 'raw' is for eventual full logging, 'started' is implicit.

        # Mirror the salient daemon traffic into the chatlog. Daemon
        # actions are skipped here intentionally — the fleet emits a
        # spawn meta event a moment later that already announces them
        # from the receiving end, and we'd rather not double-log.
        line = chatlog_format_daemon(event)
        if line is not None:
            # Phase 6: skip publishing chatlog.direct because
            # daemon→bus (Phase 3) already publishes the raw
            # daemon.<kind> DeckEvent. The magnified view and Q&A
            # context-builder iterate bus.snapshot() and re-render
            # with untruncated=True from the original DaemonEvent.
            # If we ALSO published chatlog.direct here we'd produce
            # duplicate lines in the magnified view (one from the
            # re-render, one from the pre-rendered direct line).
            self._chatlog_write(line, publish_direct=False)

    def _handle_event(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber: fleet.* event arrived. Same loop as Fleet's
        own publish (Textual single-loop everywhere) so it's safe to
        touch widgets.

        The DeckEvent's payload is the FleetEvent dataclass. Phase 8
        of the unified-event-stream slice migrated this from the
        legacy `fleet.add_listener(...)` shim to a bus subscriber."""
        fevent: FleetEvent = deck_event.payload
        # Deck-protocol markers: scan tool_result text for cyberdeck
        # dispatcher emissions BEFORE formatters render anything, so
        # the markers get cleaned out of the displayed text. This
        # mutates fevent.payload in-place (sets a cleaned `text`
        # field) so downstream formatters and the chatlog event
        # buffer all see the clean version.
        self._scan_and_dispatch_deck_markers(fevent)

        if fevent.kind == "meta":
            self._handle_meta(fevent)
        else:
            self._handle_event_kind(fevent)
        # Mechanical chatlog: zero-token render of whatever just
        # happened. The formatter returns None for events we don't
        # want to surface (system_init, tool_result, allowed
        # rate_limits, etc.) — that's the whole filter. If the line
        # rendered, also buffer the raw event so the ExpandModal can
        # re-render in untruncated mode.
        line = chatlog_format_fleet(fevent)
        if line is not None:
            # Phase 6: skip publishing chatlog.direct because fleet→bus
            # (Phase 2) already publishes the raw `fleet.<kind>`
            # DeckEvent. The magnified view + Q&A context iterate
            # bus.snapshot() and re-render via chatlog_format_fleet
            # with untruncated=True. Same duplication-avoidance shape
            # as the daemon path above.
            self._chatlog_write(line, publish_direct=False)
        # Keep sidebar counters / cost live. Cheap enough to do per event;
        # Textual only repaints when the Label's content actually changes.
        self._refresh_sidebar_info()

    def _scan_and_dispatch_deck_markers(self, fevent: FleetEvent) -> None:
        """Pre-process a fleet event for deck-protocol markers.

        Markers arrive in tool_result text (constructs invoke the
        dispatcher via Bash; Bash captures stdout; stdout becomes the
        tool_result's content text). We scan that text, pull out any
        `__CYBERDECK::v1::ACTION::PAYLOAD__` substrings, dispatch the
        side effect for each, and replace the text with the cleaned
        version so the formatter doesn't render protocol bytes.

        Mutates fevent.payload in place. No-op if the event isn't a
        tool_result, has no text content, or contains no markers."""
        if fevent.kind != "event":
            return
        if fevent.payload.get("event_kind") != "tool_result":
            return
        # tool_result text lives at payload['raw']['message']['content'][i]['content']
        # for a content block; the construct might also emit a top-level
        # 'text' field depending on streaming variant. Try both, scan
        # whichever we find.
        raw = fevent.payload.get("raw") or {}
        message = raw.get("message") or {}
        blocks = message.get("content") or []
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            content = block.get("content")
            # tool_result content can be a string OR a list of
            # content-typed dicts. Handle both.
            if isinstance(content, str):
                events, cleaned = _extract_deck_markers(content)
                if events:
                    block["content"] = cleaned
                    for action, payload in events:
                        self._dispatch_deck_marker(
                            action, payload, fevent.construct_id,
                        )
            elif isinstance(content, list):
                for sub in content:
                    if not isinstance(sub, dict):
                        continue
                    text = sub.get("text")
                    if not isinstance(text, str):
                        continue
                    events, cleaned = _extract_deck_markers(text)
                    if events:
                        sub["text"] = cleaned
                        for action, payload in events:
                            self._dispatch_deck_marker(
                                action, payload, fevent.construct_id,
                            )

    def _dispatch_deck_marker(
        self,
        action: str,
        payload: str,
        construct_id: str,
    ) -> None:
        """Apply a single deck-protocol side effect.

        action and payload come straight from the dispatcher script's
        stdout. construct_id is the construct that emitted the
        marker — used as the FileListItem's owner column so the
        netrunner sees who surfaced what.

        Unknown actions are logged to fleet log and ignored — keeps
        forward-compat with future dispatcher versions that emit
        actions this deck build doesn't handle yet."""
        if action == "FILES_ADD":
            # payload is an absolute path. _append_files handles
            # display shortening to ~/... form.
            self._append_files(construct_id, [payload])
            return
        if action == "FILES_REMOVE":
            self._remove_file_from_panel(payload)
            return
        # Unknown / future action. Suppress repeats per
        # (construct_id, action) — the first warning conveys the
        # signal; a tight emit loop on the construct side shouldn't
        # be able to paint the fleet log yellow.
        key = (construct_id, action)
        if key in self._unknown_action_seen:
            return
        self._unknown_action_seen.add(key)
        self._notify_fleet_log(
            f"[dim]deck protocol: unknown action "
            f"'{action}' from {construct_id} (ignored; "
            f"further repeats from this construct suppressed)[/dim]"
        )

    def _remove_file_from_panel(self, abs_path: str) -> None:
        """Remove the first FileListItem with matching file_path
        from the Files tab. No-op if not present (idempotent — a
        construct removing a path that wasn't there isn't an error).

        Match is on normalized path (same comparison _append_files
        uses) so a `cyberdeck files remove` of a path that was
        added with a different separator style still finds and
        removes the entry."""
        try:
            files_lv = self.query_one("#files_list", ListView)
        except Exception:
            return
        try:
            target_key = os.path.normcase(os.path.normpath(abs_path))
        except Exception:
            target_key = abs_path
        for item in list(files_lv.children):
            if not isinstance(item, FileListItem):
                continue
            try:
                item_key = os.path.normcase(os.path.normpath(item.file_path))
            except Exception:
                item_key = item.file_path
            if item_key == target_key:
                item.remove()
                return

    def _refresh_sidebar_info(self) -> None:
        """Update the sidebar counters: spawned/finalized/cost/caps."""
        if self.fleet is None:
            return
        try:
            info = self.query_one("#sidebar_info", Label)
        except Exception:
            return
        active = self.fleet.total_spawned - self.fleet.total_finalized
        cost = self.fleet.total_cost_usd
        # Format tokens compactly: 1,234,567 → "1.2M", 12345 → "12k"
        tin = _humanize_tokens(self.fleet.total_tokens_in)
        tout = _humanize_tokens(self.fleet.total_tokens_out)
        # max_total_spawns == 0 means "no cap" per the new uncapped
        # semantics — render as ∞ rather than the literal 0 (which
        # would read as "you're already over cap"). max_concurrent
        # always >= 1 (LimitsScreen clamps), so it doesn't need the
        # same treatment.
        spawn_cap_str = (
            "∞" if self.max_total_spawns == 0
            else str(self.max_total_spawns)
        )
        # Caliber Phase 5 (2026-05-04): daemon caliber line + fast-
        # mode governor indicator. Daemon model is pinned to opus by
        # design; effort is the netrunner's power-level knob via the
        # Limits modal. Fast mode shows `🚀` when on, blank when off
        # (cost governor; defaults off). Symmetric with the existing
        # `bin:` / `spawn:` / `cost:` lines.
        daemon_str = (
            f"opus·[b]{self.daemon_effort}[/b]"
            if getattr(self, "daemon_effort", None) else "—"
        )
        fast_str = (
            " · [yellow]fast[/yellow]"
            if getattr(self, "fast_mode", False) else ""
        )
        lines = [
            f"run:    [b]{self.fleet.run_id}[/b]",
            f"bin:    {self.claude_bin}",
            f"daemon: {daemon_str}{fast_str}",
            f"spawn:  {self.fleet.total_spawned}/{spawn_cap_str}  "
            f"live: {active}/{self.max_concurrent}",
            f"cost:   [b]${cost:.2f}[/b]  tok: {tin}→{tout}",
        ]
        info.update("\n".join(lines))

    def _bootstrap_deck_dispatcher(self) -> None:
        """Write the deck-protocol dispatcher script into
        <home>/tools/deck/cyberdeck.py. Idempotent — overwrites every
        startup so the script always matches what cyberdeck expects.

        The dispatcher source is loaded from cyberdeck/dispatcher.py
        in the running cyberdeck package; we read its bytes and copy
        them to the target path. This way the dispatcher stays in
        sync with whatever the deck-side parser expects: bumping the
        protocol on one side automatically bumps both."""
        # Locate dispatcher.py inside the cyberdeck package directory
        # — same directory as this tui.py module.
        src = Path(__file__).resolve().parent / "dispatcher.py"
        if not src.is_file():
            # Source missing (perhaps an unusual install). Bail
            # quietly — the construct will run without the protocol;
            # we just won't have add-to-files-panel capability.
            return
        deck_dir = self.home_dir / "tools" / "deck"
        deck_dir.mkdir(parents=True, exist_ok=True)
        target = deck_dir / "cyberdeck.py"
        target.write_bytes(src.read_bytes())
        # Best-effort exec bit on Unix; no-op on Windows (the script
        # is invoked via `python <path>` anyway, so chmod isn't
        # strictly needed — the convention is just nice on Unix).
        try:
            target.chmod(0o755)
        except (NotImplementedError, OSError):
            pass

    def _bootstrap_plugin_bridge(self) -> None:
        """Write the plugin-bridge dispatcher script into
        <home>/tools/deck/plugin_bridge.py. Idempotent — overwrites
        every startup so the bridge always matches what the deck
        expects (same pattern as _bootstrap_deck_dispatcher).

        Difference from the deck dispatcher: the bridge needs to
        find plugin code at <deck-source>/plugins/<name>/plugin.py,
        but the bridge runs from <home>/tools/deck/, so it has no
        natural relative path to the plugin folders. Token
        replacement at bootstrap time stamps the absolute plugins-
        dir path into the source — the canonical source has a
        `__PLUGINS_DIR__` placeholder that the bridge resolves at
        runtime; we rewrite that placeholder during bootstrap so
        the installed copy points at the right location.

        Why not use env vars or argv? Each invocation comes through
        the construct's Bash; bolting an env var onto every Bash
        call would balloon the construct prompt, and an argv flag
        adds a moving part the construct could omit. Stamping the
        path at bootstrap time keeps the construct-side invocation
        as `python <bridge> <plugin_name> [args]` — minimal, stable,
        cache-friendly."""
        deck_root = Path(__file__).resolve().parent
        src = deck_root / "plugin_bridge.py"
        if not src.is_file():
            # Bridge source missing — bail quietly. Constructs will
            # see the bridge as unavailable; plugin invocations
            # would need to be rerouted (none exist today, but P3+
            # plugins would lose their capability path).
            return
        deck_dir = self.home_dir / "tools" / "deck"
        deck_dir.mkdir(parents=True, exist_ok=True)
        target = deck_dir / "plugin_bridge.py"
        # Token replacement: the canonical source carries
        # __PLUGINS_DIR__ as a placeholder; we replace it with the
        # resolved absolute path here. Use a string repr so backslash
        # path separators on Windows survive into the generated
        # source as a literal string, not as escape sequences.
        # Resolve plugins_dir from __file__ rather than
        # self.plugins_dir so we don't depend on instance attribute
        # ordering — bootstrap fires early in __init__ before some
        # other attrs are assigned.
        plugins_dir = str((deck_root / "plugins").resolve())
        text = src.read_text(encoding="utf-8")
        # Replace the assignment line that initializes PLUGINS_DIR
        # to the token. We swap the entire assignment to a string
        # literal so the bootstrapped copy carries a plain path
        # (using repr() to handle path separators safely).
        text = text.replace(
            'PLUGINS_DIR = _PLUGINS_DIR_TOKEN',
            f'PLUGINS_DIR = {plugins_dir!r}',
        )
        target.write_text(text, encoding="utf-8")
        try:
            target.chmod(0o755)
        except (NotImplementedError, OSError):
            pass

    def _scan_scripts(self) -> list[tuple[str, str, str]]:
        """Scan <home>/tools/ for scripts. Each script lives at
        <home>/tools/<category>/<filename> — flat one-level deep,
        with the parent directory name as category. Returns a list
        of (abs_path, category, name) tuples sorted by category then
        name. Empty list if tools/ doesn't exist yet (first run
        before bootstrap, or a netrunner who deleted it).

        Naming convention:
          - `name` is the filename WITHOUT extension. `cyberdeck.py`
            displays as `cyberdeck`.
          - Files DIRECTLY in tools/ (no category subdir) are
            ignored. Spec says scripts have a category; we honor
            that here to keep the panel structured.
          - Hidden files (starting with .) and `__pycache__` are
            skipped — Python build artifacts shouldn't show up as
            launchable scripts.

        Cheap to call repeatedly: small directory tree, no parsing
        per file, no caching needed at this scale."""
        scripts: list[tuple[str, str, str]] = []
        tools_dir = self.home_dir / "tools"
        if not tools_dir.is_dir():
            return scripts
        for category_dir in sorted(tools_dir.iterdir()):
            if not category_dir.is_dir():
                continue
            if category_dir.name.startswith("."):
                continue
            if category_dir.name == "__pycache__":
                continue
            category = category_dir.name
            for script_file in sorted(category_dir.iterdir()):
                if not script_file.is_file():
                    continue
                if script_file.name.startswith("."):
                    continue
                # Strip extension for display. `cyberdeck.py` →
                # `cyberdeck`. `scan_wifi.sh` → `scan_wifi`. Files
                # without an extension stay as-is.
                name = script_file.stem
                scripts.append(
                    (str(script_file.resolve()), category, name)
                )
        return scripts

    def _build_deck_addendum(self) -> str:
        """STATIC system-prompt addendum that describes the deck-
        control utility every construct can use. Set on Fleet at
        construction time; same string for every spawn.

        P4 of the tools/plugins/profiles retool (2026-05-03) split
        this from the per-spawn parts: profile-tools-with-descriptions
        and plugin-selection both depend on per-spawn context (which
        profile, which plugins the daemon picked) and now flow
        through `_build_per_spawn_addendum` instead. This static
        addendum stays small and cache-friendly; the per-spawn parts
        ride alongside as a separate string assembled by Construct.

        We pass the absolute dispatcher path here (rather than
        relying on PATH) because:
          (a) Constructs run with cwd=cyberdeck-home but their PATH
              isn't extended, so `cyberdeck` wouldn't resolve.
          (b) Telling the construct exact bytes to invoke is
              clearer than hoping it figures out invocation form."""
        dispatcher = self.home_dir / "tools" / "deck" / "cyberdeck.py"
        return (
            f"You have access to a deck-control utility at "
            f"{dispatcher}. Invoke it via the Bash tool to surface "
            f"files in the netrunner's UI panel:\n"
            f"  python {dispatcher} files add <path>\n"
            f"  python {dispatcher} files remove <path>\n"
            f"Use this when you produce a file the netrunner should "
            f"see in their Files panel — finished outputs, generated "
            f"reports, etc. Don't surface every intermediate scratch "
            f"file. Paths can be absolute or relative to your cwd."
        )

    def _build_per_spawn_addendum(
        self,
        profile: Optional["Profile"],
        plugins: Optional[list[str]],
    ) -> str:
        """Per-spawn system-prompt addendum: profile.tools resolved
        against the tool registry + plugins (daemon-selected or all
        available) resolved against the plugin registry.

        P4 of the tools/plugins/profiles retool (2026-05-03). Replaces
        the static all-plugins-always rendering that lived in
        _build_deck_addendum pre-P4. Now:

          - Profile.tools enumerates registry entries the construct
            should prefer for this profile's work. We surface name +
            short description; help_text is omitted (prompt-bloat-
            aware — the construct can `<tool> --help` if it needs
            argument shapes). Names that don't resolve against the
            registry render as bare names with a "(not in registry)"
            note; never silently dropped — visible drift is better
            than invisible drift.

          - Plugins selection: when `plugins` is a list, surface ONLY
            those (the daemon-per-spawn pick). When None, surface
            ALL available plugins (back-compat for netrunner-direct
            spawns and pre-P4 daemon spawns that don't carry a
            plugins field). Empty list = explicit no-plugins.

        Returns a possibly-empty string that Construct will append
        to its system prompt after the static deck_addendum."""
        sections: list[str] = []

        # Profile tools. Resolved against tool_registry — pulls each
        # tool's description for inclusion. Skipped silently for
        # profiles with no tools (the common case).
        tools_list: list[str] = []
        if profile is not None and profile.tools:
            tools_list = list(profile.tools)
        if tools_list:
            tool_lines: list[str] = [
                "Profile-recommended tools for this spawn — registry "
                "entries (system-installed CLIs the netrunner has "
                "declared). Prefer these over re-implementing the "
                "same capability in Bash composition:",
                "",
            ]
            registry = getattr(self, "tool_registry", None)
            for name in tools_list:
                tool = registry.get(name) if registry is not None else None
                if tool is None:
                    tool_lines.append(
                        f"  - {name}  (not in registry — netrunner "
                        f"may have removed this tool)"
                    )
                    continue
                desc_short = " ".join(tool.description.split())
                if len(desc_short) > 140:
                    desc_short = desc_short[:137] + "..."
                avail_marker = (
                    "" if tool.available else "  [unavailable: "
                    f"{tool.unavailable_reason or '?'}]"
                )
                tool_lines.append(
                    f"  - {name}: {desc_short}{avail_marker}"
                )
            sections.append("\n".join(tool_lines))

        # Plugin selection. Filter the available registry by `plugins`
        # if supplied; otherwise surface all available.
        all_avail = self.plugin_registry.available()
        if plugins is None:
            chosen = all_avail
        else:
            wanted = set(plugins)
            chosen = [pl for pl in all_avail if pl.name in wanted]
            # Surface a note for daemon-named plugins that don't
            # resolve (typo, unavailable plugin, plugin not registered).
            unresolved = wanted - {pl.name for pl in chosen}
            if unresolved:
                sections.append(
                    "Daemon requested plugins that didn't resolve "
                    f"(typo or unavailable): {sorted(unresolved)}. "
                    f"Continuing without them."
                )

        if chosen:
            bridge_path = (
                self.home_dir / "tools" / "deck" / "plugin_bridge.py"
            )
            heading = (
                "Plugins selected for this spawn"
                if plugins is not None
                else "Plugins available for this session"
            )
            plugin_lines: list[str] = [
                f"{heading} — invoke through the bridge dispatcher "
                f"at {bridge_path}. You don't reach plugin code "
                f"directly; call the bridge with the plugin name "
                f"and any arguments, and the bridge forwards to the "
                f"plugin's entry script.",
                "",
            ]
            for pl in chosen:
                desc_short = " ".join(pl.description.split())
                if len(desc_short) > 140:
                    desc_short = desc_short[:137] + "..."
                if pl.source_dir is not None:
                    plugin_lines.append(
                        f"  - {pl.name}: {desc_short}"
                    )
                    plugin_lines.append(
                        f"    invoke: python {bridge_path} {pl.name} [args]"
                    )
                    plugin_lines.append(
                        f"    docs:   Read {pl.source_dir / 'README.md'}"
                    )
                else:
                    plugin_lines.append(
                        f"  - {pl.name}: {desc_short}"
                    )
            plugin_lines.append("")
            plugin_lines.append(
                "Read a plugin's README before first use so you know "
                "the exact argument shape and output format. The "
                "deck's brake hook still gates plugin invocations "
                "the same way it gates any Bash — destructive "
                "patterns and protected paths apply."
            )
            sections.append("\n".join(plugin_lines))

        return "\n\n".join(sections)

    def _shorten_path(self, p: str) -> str:
        """Render `p` with `~/` substituted for the cyberdeck home
        prefix when applicable. Used by Files tab display so the
        narrow column doesn't burn two-thirds of its width on the
        home path on every row.

        '~/' here means cyberdeck-home (the soft-sandbox dir,
        defaults to cyberdeck-home/), NOT the OS user home. The
        construct's perspective is that ~ is the deck's working
        root — that's the convention we mirror.

        Falls back to the original path string if it isn't under
        home (e.g., constructs touching /tmp scratch files), or if
        Path() can't parse it for any reason. Best-effort
        cosmetics — never crashes file display over a weird path."""
        try:
            path_obj = Path(p)
            rel = path_obj.relative_to(self.home_dir)
            return f"~/{rel.as_posix()}"
        except (ValueError, OSError):
            return p

    def _append_files(self, construct_id: str, files: list[str]) -> None:
        """Append files to the right-panel Files tab. Each entry
        becomes a focusable FileListItem so C1g.4 can wire space →
        launch-with-file-context. Best-effort — no-op if the widget
        isn't mounted (m3 mode / pre-mount race / test harness
        without the right panel).

        De-dupes by NORMALIZED absolute path: if a path is already
        in the panel under any equivalent form, skip it. This lets
        BOTH the auto-surface path (finalize meta event with
        files-touched list) AND the dispatcher path (explicit
        cyberdeck files add invocation) coexist without producing
        duplicate entries when they refer to the same file.
        Provenance (which construct surfaced it) goes to whichever
        path got there first.

        Why normalized rather than literal: on Windows in particular,
        `C:\\Users\\...\\foo.md` and `C:/Users/.../foo.md` are the
        same file but compare unequal as strings. The dispatcher
        does `Path(p).resolve()` which can normalize one way; the
        construct's Write capture preserves whatever the model
        passed. Without normalization, identical files double up
        (this was the "Files panel double-listing" report). Same
        applies to redundant separators (`a//b`), trailing slashes,
        and `.` segments. We match on
        `os.path.normcase(os.path.normpath(p))` which collapses all
        of those without touching displayed paths.
        """
        try:
            files_lv = self.query_one("#files_list", ListView)
        except Exception:
            return

        def _norm(p: str) -> str:
            # normpath collapses `.`, `..`, redundant separators.
            # normcase folds case + slash style on Windows (no-op on
            # POSIX). Combined, two paths to the same file collide.
            try:
                return os.path.normcase(os.path.normpath(p))
            except Exception:
                return p  # never crash the panel over a path quirk

        existing_keys = {
            _norm(item.file_path)
            for item in files_lv.children
            if isinstance(item, FileListItem)
        }
        for fp in files:
            key = _norm(fp)
            if key in existing_keys:
                continue
            display = self._shorten_path(fp)
            files_lv.append(FileListItem(construct_id, fp, display_path=display))
            existing_keys.add(key)

    def _chatlog_write(self, line: str, *, publish_direct: bool = True) -> None:
        """Append a single chatlog line with an HH:MM:SS dim-prefix.

        The line is expected to already include color/markup from the
        formatter; this helper just stamps a timestamp on the front and
        writes to the right-panel chatlog RichLog.

        No-op if the chatlog widget isn't mounted (m3/keyboard-only mode,
        or pre-mount race). Mechanical extraction is best-effort by
        design — dropping a line never breaks anything else.

        Bus publish (Phase 6 of the unified-event-stream slice,
        controlled by `publish_direct`): when True (default), the
        line ALSO publishes a `chatlog.direct` DeckEvent on the bus
        so the magnified view (`z` ExpandModal) and the watchdog Q&A
        context-builder can both see it. Both readers iterate
        `bus.snapshot()`, dispatch on payload type for fleet/daemon
        events (re-rendering with untruncated=True for richer content)
        and read pre-rendered text for `chatlog.direct` events.

        Why the opt-out parameter: the fleet/daemon dispatch paths
        (`_handle_event`, `_handle_daemon_event`) already cause a
        `fleet.*` or `daemon.*` event to land on the bus via Phase
        2/3. If `_chatlog_write` also publishes `chatlog.direct` for
        the same logical event, the chatlog reader iterating the bus
        sees BOTH and emits duplicate lines (one from the fleet/daemon
        re-render, one from the pre-rendered direct line). Those
        dispatch paths pass `publish_direct=False` to skip the
        chatlog.direct publish; everything else (tripwire fires,
        watchdog markers, brake transitions, blacklist additions,
        goal-update markers, etc.) stays on the default True so it
        gets buffered for re-render in the magnified view.

        History note: until Phase 6 this method maintained a separate
        `_chatlog_event_buffer` deque on CyberdeckApp. That structure
        decayed silently as new event sources landed; the tactical
        fix added a `buffer_direct` opt-out param. Phase 6 retires
        the buffer entirely; bus.snapshot() is the single source of
        truth for "what's been on the chatlog." This `publish_direct`
        parameter is the bus-shaped equivalent of the old
        `buffer_direct` — same dispatch-path duplication concern,
        same fix shape.
        """
        try:
            chatlog = self.query_one("#chatlog_log", RichLog)
        except Exception:
            return
        # Defensive: collapse any embedded newlines to spaces so the
        # write is guaranteed to be one logical entry. Tool inputs
        # (especially WebSearch queries and unknown-tool fallbacks)
        # can occasionally contain \n that would otherwise produce
        # multi-strip writes that look like data corruption when
        # the panel is narrow.
        if "\n" in line or "\r" in line:
            line = line.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        now = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(now))
        chatlog.write(f"[dim]{ts}[/dim]  {line}")
        # Phase 6 bus publish — `chatlog.direct` carries the rendered
        # line in event.text. Subscribers wanting the raw structured
        # event should subscribe to the source kinds (tripwire.fire,
        # brake.change, blacklist.added, etc.); those producers
        # publish independently on the bus (Phases 4-5). The
        # chatlog.direct event is the "this is what the netrunner
        # saw" rendering for non-fleet/daemon markers; fleet/daemon
        # dispatch sites set publish_direct=False to avoid duplicate
        # bus entries (their raw events on the bus carry the
        # re-renderable payload).
        if publish_direct and getattr(self, "bus", None) is not None:
            try:
                from event_bus import DeckEvent
                self.bus.publish(DeckEvent(
                    kind="chatlog.direct",
                    source="tui.chatlog",
                    timestamp=now,
                    text=line,
                ))
            except Exception:
                pass

    # Chatlog reader — Phase 6 of the unified-event-stream slice
    # replaced the standalone `_chatlog_event_buffer` deque with
    # bus.snapshot() iteration. Two readers share the same dispatch
    # logic: _render_chatlog_buffer (magnified view) and
    # _build_watchdog_context (Q&A snapshot). Both call
    # _chatlog_format_bus_event below; they differ only in what
    # they do with the formatted lines (chrome them with timestamps
    # vs. strip markup for LLM input).

    def _chatlog_format_bus_event(
        self, event: "DeckEvent", *, untruncated: bool,
    ) -> Optional[str]:
        """Map a DeckEvent to a chatlog-ready line, or None when the
        event isn't chatlog-relevant.

        Dispatch:
          * `chatlog.direct` → emit event.text directly (pre-rendered
            by `_chatlog_write`)
          * fleet.* with FleetEvent payload → chatlog_format_fleet
          * daemon.* with DaemonEvent payload → chatlog_format_daemon
          * everything else → None (filtered out)

        Skipping non-chatlog kinds rather than rendering everything
        keeps the magnified view focused on what the netrunner
        already saw — tripwire.fire (raw), brake.change (raw),
        blacklist.added (raw), etc., are all also published on the
        bus, but they carry the structured payload, not the
        chatlog-line rendering. The chatlog-line rendering for those
        same events comes from `_chatlog_write` calls inside the
        respective handlers, which publish chatlog.direct."""
        kind = event.kind
        if kind == "chatlog.direct":
            return event.text
        # Fleet / daemon path: re-render from the original payload so
        # the magnified view can use untruncated=True for richer
        # content. The payload is the original FleetEvent / DaemonEvent
        # object courtesy of Phase 2 / 3's translator.
        if kind.startswith("fleet."):
            payload = event.payload
            if isinstance(payload, FleetEvent):
                return chatlog_format_fleet(payload, untruncated=untruncated)
            return None
        if kind.startswith("daemon."):
            payload = event.payload
            if isinstance(payload, DaemonEvent):
                return chatlog_format_daemon(payload, untruncated=untruncated)
            return None
        return None

    def _render_chatlog_buffer(self, *, untruncated: bool = False) -> list[str]:
        """Re-render the chatlog from the bus snapshot. Used by the
        ExpandModal to show un-truncated content (`untruncated=True`
        passes through to the formatters' looser sanity caps).

        Each entry includes the HH:MM:SS prefix that _chatlog_write
        normally stamps on, derived from the bus event's own timestamp
        so the modal's lines line up with what the live chatlog
        showed (give or take async ordering).

        Returns lines suitable for direct write to a RichLog with
        markup=True; the caller doesn't need to do further formatting.
        """
        out: list[str] = []
        if getattr(self, "bus", None) is None:
            return out
        for event in self.bus.snapshot():
            try:
                line = self._chatlog_format_bus_event(
                    event, untruncated=untruncated,
                )
            except Exception:
                # A buggy formatter shouldn't take down the modal —
                # skip the line and continue.
                continue
            if line is None:
                continue
            # Same newline-collapse as _chatlog_write so the modal's
            # lines stay one-strip-each.
            if "\n" in line or "\r" in line:
                line = line.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
            ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
            out.append(f"[dim]{ts}[/dim]  {line}")
        return out

    def _handle_meta(self, fevent: FleetEvent) -> None:
        ptype = fevent.payload.get("type")
        fleet_log = self.query_one("#fleet_log", RichLog)
        if ptype == "spawned":
            task = fevent.payload.get("task", "")
            parent_id = fevent.payload.get("parent_id")
            # When this is an inject follow-up, the actual task sent to
            # claude has a "[Netrunner halted/added...]" framing prefix.
            # That's right for the model; wrong for the pane preview
            # (verbose, repetitive across every inject). Strip it so the
            # pane shows the user's actual message. The framed prompt
            # still goes to claude unchanged — this is display-only.
            display_task = task
            if parent_id is not None and task.startswith("[Netrunner"):
                marker = "]\n\n"
                idx = task.find(marker)
                if idx != -1:
                    display_task = task[idx + len(marker):]
            self._spawn_pane(
                fevent.construct_id, display_task,
                injected_from=parent_id,
                profile_name=fevent.payload.get("profile_name"),
                # Caliber Phase 5 (2026-05-04): caliber display
                # string from the spawned meta event (rendered by
                # fleet.spawn from effective_caliber.display()).
                # None when no caliber metadata was attached
                # (legacy spawn path / pool warming).
                caliber=fevent.payload.get("caliber"),
            )
            # If this spawn has a parent (i.e., it's an inject follow-up),
            # update the parent pane to show the outgoing chevron link.
            # The parent's REDIRECTED state was set at finalize time;
            # this completes the visual story by pointing at the
            # destination.
            if parent_id is not None:
                parent_pane = self.panes.get(parent_id)
                if parent_pane is not None:
                    parent_pane.set_injected_to(fevent.construct_id)
                fleet_log.write(
                    f"[cyan]+[/cyan] {fevent.construct_id}  "
                    f"[dim](continuing {parent_id})[/dim]"
                )
            else:
                fleet_log.write(f"[cyan]+[/cyan] {fevent.construct_id}")
            # Slice 3 diagnostic: when delay > 0 is configured, every
            # spawn should be running the brake hook with that delay
            # baked into its argv. Surface the value at spawn time so
            # the netrunner can confirm the plumbing is live without
            # poking at .cyberdeck/spawns/ files. Quiet (dim, brief)
            # when delay is 0 so it doesn't spam the log in normal use.
            try:
                dw = getattr(self.fleet, "delay_window_seconds", 0.0)
                if dw and dw > 0:
                    fleet_log.write(
                        f"[dim]  └─ delay armed: {dw:g}s "
                        f"(brake_hook will hold qualifying calls)[/dim]"
                    )
            except Exception:
                pass
        elif ptype == "finalized":
            pane = self.panes.get(fevent.construct_id)
            files = fevent.payload.get("files_written") or []
            # Peek (don't pop) at the pending injection so we can decide
            # the visual state. Pop happens just below, after we've used
            # the metadata to drive the followup spawn.
            pending = self._pending_injections.get(fevent.construct_id)
            finalized_state = fevent.payload.get("state", "?")
            final_output = fevent.payload.get("final_output") or ""
            if pane is not None:
                if pending is not None and pending[0] == "interrupt":
                    # Interrupt-inject: we killed the construct, but the
                    # session continues in a new construct. KILLED would
                    # be technically true but visually misleading — show
                    # REDIRECTED instead. Queue-inject leaves the natural
                    # finalized state (DONE/FAILED/etc.) since the work
                    # genuinely completed before the followup.
                    pane.state = "redirected"
                else:
                    pane.state = finalized_state
                if files:
                    pane.set_files_written(files)
                # Brake-hook denials. List is empty (or absent on pre-
                # brake-refactor finalize events) for clean runs;
                # set_denials handles the empty case by clearing the
                # badge and the .-blocked class. When non-empty, the
                # pane's border turns yellow and a `[⚠ blocked: ...]`
                # badge appears in the header.
                pane.set_denials(fevent.payload.get("permission_denials") or [])
                # Anomaly check: a construct that ran cleanly but
                # produced zero stream-json events AND no output is
                # almost always a sign that something went wrong silently
                # — most notoriously, Windows argv mangling on multiline
                # tasks (claude receives garbage, exits 0, emits nothing).
                # Surface it loudly in the pane log so the netrunner
                # doesn't have to guess why their inject "worked but
                # didn't do anything." Skip for killed/redirected — those
                # were terminated before they had time to produce events,
                # which is expected, not anomalous.
                if (pane._event_count == 0
                        and finalized_state == "done"
                        and not final_output.strip()
                        and not files):
                    pane.add_event(
                        "anomaly",
                        "(no stream-json events received — subprocess "
                        "exited 0 with no output)",
                        "no events received",
                    )
            runtime = fevent.payload.get("runtime", 0)
            file_suffix = f" [cyan]→ {len(files)} file(s)[/cyan]" if files else ""
            display_state = (
                "redirected"
                if (pending is not None and pending[0] == "interrupt")
                else finalized_state
            )
            # State-aware glyph + color so the netrunner can scan the
            # log and tell at a glance which constructs ended cleanly,
            # which were terminated, and which got redirected. Falls
            # back to a neutral marker for unknown states.
            glyph_style = {
                "done":       ("✓", "cyan"),
                "failed":     ("✗", "red"),
                "killed":     ("×", "orange1"),
                "redirected": ("↪", "bright_blue"),
            }.get(display_state, ("·", "dim"))
            glyph, color = glyph_style
            fleet_log.write(
                f"[{color}]{glyph}[/{color}] {fevent.construct_id}: "
                f"{display_state} ({runtime:.1f}s){file_suffix}"
            )
            # Files panel auto-surface: every file the construct
            # touched gets pushed to the Files tab (per spec — sourced
            # from the files_written capture in the construct event
            # stream). _append_files de-dupes by absolute path, so
            # constructs that ALSO call the cyberdeck dispatcher to
            # explicitly surface a file don't double-up here.
            #
            # The dispatcher path remains useful for the case where a
            # construct discovers a pre-existing file via Glob/LS
            # (didn't touch it via Read/Write/Edit so it wouldn't be
            # in `files`) and wants to put it on the netrunner's
            # radar.
            if files:
                self._append_files(fevent.construct_id, files)
            # Pending injections still need to fire on finalize:
            pending = self._pending_injections.pop(
                fevent.construct_id, None,
            )
            if pending is not None:
                self.run_worker(
                    self._spawn_injected_followup(
                        fevent.construct_id, *pending,
                    ),
                    name=f"inject-{fevent.construct_id}",
                )
            # Schedule this pane to compact and pin to the bottom after
            # the grace period. Done/failed/killed/redirected all qualify
            # — they're all "this construct is finished doing things."
            # The netrunner can still expand them; they just stop hogging
            # screen real estate by default.
            self._schedule_compact_pane(fevent.construct_id)
        # run_start / run_end are noise in the TUI; logged to file regardless

    def _handle_event_kind(self, fevent: FleetEvent) -> None:
        pane = self.panes.get(fevent.construct_id)
        if pane is None:
            return  # event arrived before pane mounted; shouldn't happen but safe
        if pane.state == "starting":
            pane.state = "running"
        event_kind = fevent.payload.get("event_kind", "?")
        raw = fevent.payload.get("raw", {})
        # Two summaries with different jobs: log_summary is the verbose
        # archival line that fills the expanded log; activity_summary
        # is the "what's it doing right now" line on the always-visible
        # row. summarize_for_activity returns None for noise events
        # (system_init, tool_result), letting the prior activity line
        # persist instead of being overwritten with plumbing details.
        log_summary = summarize(raw)
        activity_summary = summarize_for_activity(raw)
        pane.add_event(event_kind, log_summary, activity_summary, raw=raw)

    def _spawn_pane(
        self,
        construct_id: str,
        task: str,
        injected_from: Optional[str] = None,
        profile_name: Optional[str] = None,
        caliber: Optional[str] = None,
    ) -> None:
        # Defensive idempotency: if a pane already exists for this
        # construct_id, don't create another. Belt-and-suspenders
        # against accumulated bus subscriptions firing _handle_event
        # multiple times for the same spawn (real-deck-caught
        # 2026-04-30 late — Phase 8 subscriptions were leaking on
        # _drive_fleet re-runs, fixed at the subscribe site too).
        # Surface the duplicate via the chatlog so any future
        # accidental double-fire is immediately visible rather than
        # silently mounting orphan panes.
        if construct_id in self.panes:
            try:
                self._chatlog_write(
                    f"[red]× double-spawn detected[/red] for "
                    f"[cyan]{construct_id}[/cyan] — pane reused; "
                    f"investigate bus subscription accumulation"
                )
            except Exception:
                pass
            return
        pane = ConstructPane(
            construct_id, task,
            injected_from=injected_from,
            profile_name=profile_name,
            caliber=caliber,
        )
        self.panes[construct_id] = pane
        main = self.query_one("#main", VerticalScroll)
        main.mount(pane)
        # If there are already terminal (compact) panes pinned at the
        # bottom, hop the new pane above them. mount() puts the new
        # widget at the end of the children list by default; we push
        # it before the first compact pane so the order stays:
        #   [active panes]  ← new one lands here
        #   [compact / terminal panes pinned at bottom]
        first_compact = next(
            (p for p in self.panes.values() if p.compact and p is not pane),
            None,
        )
        if first_compact is not None:
            try:
                main.move_child(pane, before=first_compact)
            except Exception:
                # move_child can race against the mount; if it fails
                # the pane just stays at the end of main, mixed in
                # with the compact panes. Cosmetic, not fatal.
                pass

    def _schedule_compact_pane(self, construct_id: str) -> None:
        """Plan to compact this pane after a short grace period.

        The delay (COMPACT_DELAY_SECS) lets the netrunner glance at
        the result before it shrinks. If the pane is already gone by
        the time the timer fires (e.g., EJECT cleared it), the worker
        just returns. No-op if the pane is already compact."""
        existing = self.panes.get(construct_id)
        if existing is None or existing.compact:
            return
        try:
            self.query_one("#fleet_log", RichLog).write(
                f"[dim]compact: scheduled {construct_id} "
                f"(in {self.COMPACT_DELAY_SECS:g}s)[/dim]"
            )
        except Exception:
            pass
        self.run_worker(
            self._compact_pane_after_delay(construct_id, self.COMPACT_DELAY_SECS),
            name=f"compact-{construct_id}",
        )

    async def _compact_pane_after_delay(
        self, construct_id: str, delay: float,
    ) -> None:
        """Wait `delay` seconds, then mark the pane compact, force-collapse
        it, and move it to the bottom of #main. Idempotent: if called
        twice for the same pane the second invocation is a no-op.

        Diagnostic surface (added 2026-05-01 after real-deck-observed
        compact-to-bottom regression): both the schedule and the fire
        log to fleet_log so the netrunner can see when each step
        happens. If "compact: schedule" appears but "compact: fired"
        doesn't, it's an asyncio scheduling issue. If both appear but
        the pane stays where it is, it's a Textual move_child issue.
        Exceptions inside the move are surfaced (not silently
        swallowed) so the failure mode is visible.
        """
        await asyncio.sleep(delay)
        pane = self.panes.get(construct_id)
        if pane is None or pane.compact:
            return
        try:
            self.query_one("#fleet_log", RichLog).write(
                f"[dim]compact: firing {construct_id}[/dim]"
            )
        except Exception:
            pass
        pane.compact = True
        # Force-collapse on transition. The user can re-expand later;
        # we just don't want a fully-expanded log eating screen real
        # estate at the bottom by default.
        pane.expanded = False
        # Move to the bottom of #main. move_child is sync and preserves
        # child widget state — important because the pane's log content
        # (everything the construct produced) lives in a child RichLog
        # that we want to keep around for autopsy.
        #
        # Filter to ConstructPane children — #main now also hosts the
        # AttentionPanel at index 0 (phase 2). "Last construct pane"
        # is what we want to move past, not literally last child.
        try:
            main = self.query_one("#main", VerticalScroll)
            last_pane = next(
                (c for c in reversed(list(main.children))
                 if isinstance(c, ConstructPane)),
                None,
            )
            if last_pane is not None and last_pane is not pane:
                main.move_child(pane, after=last_pane)
        except Exception as exc:
            # SURFACE the failure (not silent) — real-deck-observed
            # 2026-05-01 that compact-to-bottom stopped working with
            # no diagnostic signal. Whatever's failing here, the
            # netrunner needs to see it. Compact styling still
            # applied (the class flip is above this block), so the
            # pane still gets de-emphasized; only the spatial reorder
            # is at risk.
            try:
                self.query_one("#fleet_log", RichLog).write(
                    f"[red]compact: move failed for {construct_id}: "
                    f"{exc!r}[/red]"
                )
            except Exception:
                pass

    async def action_quit(self) -> None:
        """Smart quit (Phase 7b of the unified-event-stream slice).

        Idle path: clean exit, fleet teardown, normal Textual exit.

        Running path: blocks the quit and surfaces a toast pointing
        at EJECT (Ctrl+F). Reasoning: Ctrl+Q used to drop work mid-
        flight without warning — easy to lose a goal-in-progress by
        muscle memory. Now Ctrl+Q has a "do you actually mean it?"
        gate when work is in flight; the netrunner can either let
        it finish OR EJECT to halt deliberately. The escape hatch is
        always one keypress (Ctrl+F held) — see
        cyberdeck-philosophy.md, design principle 6.

        "Running" means: a daemon session is alive OR the fleet has
        non-terminal constructs in flight. Either signals "real work
        the netrunner cares about losing."
        """
        # Detect running state. Defensive — fleet/session might be
        # partially constructed during early startup.
        live_constructs: list[str] = []
        if self.fleet is not None:
            try:
                live_constructs = [
                    c.id for c in self.fleet._constructs
                    if c.state.value not in ("done", "failed", "killed")
                ]
            except Exception:
                pass
        daemon_running = (
            self.session is not None
            and self._daemon_task is not None
            and not self._daemon_task.done()
        )

        if live_constructs or daemon_running:
            # Block + toast. The netrunner gets concrete feedback about
            # what's holding the deck so they can decide: wait for the
            # work to settle, or EJECT to halt deliberately.
            parts: list[str] = []
            if daemon_running:
                parts.append("daemon session active")
            if live_constructs:
                if len(live_constructs) == 1:
                    parts.append(f"1 construct in flight ({live_constructs[0]})")
                else:
                    parts.append(
                        f"{len(live_constructs)} constructs in flight: "
                        + ", ".join(live_constructs[:3])
                        + ("…" if len(live_constructs) > 3 else "")
                    )
            self._toast(
                f"[yellow]quit blocked:[/yellow] {' + '.join(parts)}. "
                "Hold Ctrl+F to EJECT if you need to halt now."
            )
            return

        # Idle path — clean exit. Two-step teardown:
        #
        # 1. Close the deck logger FIRST with reason="shutdown" so
        #    the log_footer gets flushed before any further async
        #    cleanup races with process exit. The Mechanic's
        #    supervisor reads this footer to distinguish clean
        #    Ctrl+Q from unclean crash; without it, Mechanic v1
        #    fires expensive triage on every clean shutdown.
        #    Bug shipped 2026-05-06 ("close fires from _drive_fleet
        #    teardown") — wrong because _drive_fleet may never have
        #    started (idle deck) AND because Textual's exit() can
        #    cancel _drive_fleet before its finally block runs.
        #    Mirrors what _do_eject already does explicitly.
        #    DeckLogger.close is idempotent — _drive_fleet's
        #    belt-and-suspenders close call below stays as a no-op
        #    in this path.
        # 2. Tell fleet to tear down (cheap if already idle), then
        #    let Textual unwind.
        if self.deck_logger is not None:
            self.deck_logger.close(reason="shutdown")
        if self.fleet is not None:
            self.fleet.shutdown()
        self.exit()

    # ---- EJECT (emergency halt) ----------------------------------------

    def action_open_eject(self) -> None:
        """Open the EJECT confirmation modal. The modal handles the
        deliberate-consent step; if confirmed, _do_eject does the
        actual halting."""
        # Don't stack EJECT modals if one's already open
        if isinstance(self.screen, (EjectScreen, EjectedScreen)):
            return
        self.push_screen(EjectScreen(), self._handle_eject_confirmed)

    def _handle_eject_confirmed(self, confirmed: bool) -> None:
        """Callback from EjectScreen. Confirmed=True means commit;
        False means user cancelled or timed out."""
        if not confirmed:
            return
        # Run the eject as a worker so we don't block the UI on cleanup.
        self.run_worker(self._do_eject(), exclusive=False, name="eject")

    async def _do_eject(self) -> None:
        """The actual destruction. SIGKILL all constructs, halt the
        daemon, drain queues, write a postmortem snapshot, then show
        the post-eject screen.

        Critical: every kill is AWAITED before we proceed. Earlier
        versions only signaled shutdown and let the loop catch up
        eventually, which let constructs run to completion before the
        kill propagated. EJECT is the "I want this to STOP NOW" path —
        we wait for processes to actually die before showing 'EJECTED'."""
        # Snapshot FIRST, while state is still readable. If we kill
        # everything before snapshotting, we lose the postmortem.
        snapshot_path: Optional[Path] = None
        try:
            snapshot_path = self._write_ejection_snapshot()
        except Exception as e:
            try:
                self.query_one("#fleet_log", RichLog).write(
                    f"[red]eject: snapshot failed:[/red] {e!r}"
                )
            except Exception:
                pass

        # Kill all live constructs DIRECTLY and WAIT. Bypassing
        # fleet.shutdown() (which only sets a flag) means we don't
        # rely on the run loop to notice — we kill them ourselves.
        # Construct.kill() does SIGTERM-then-SIGKILL with a 2s timeout
        # per construct; we run them in parallel.
        if self.fleet is not None:
            try:
                live = [
                    c for c in self.fleet._constructs
                    if c.state.value not in ("done", "failed", "killed")
                ]
                if live:
                    # Slice-2-followup: each c.kill() carries reason
                    # "eject" so the finalize event's kill_source
                    # field shows the correct attribution. We don't
                    # route through fleet.kill_construct here because
                    # gather-of-direct-kills is meaningfully faster
                    # for many-construct EJECTs and the finalize
                    # field is sufficient observability for the
                    # post-mortem (no real-time-during-eject use case
                    # for the bus event since the deck is being torn
                    # down).
                    await asyncio.gather(
                        *(c.kill(reason="eject") for c in live),
                        return_exceptions=True,
                    )
                # Now signal the fleet's run loop to exit too. With all
                # constructs killed, this is mostly a no-op but keeps
                # state consistent.
                self.fleet.shutdown()
            except Exception:
                pass

        # Cancel the daemon task and WAIT for it to actually unwind.
        # Without the await, _do_eject returns while the daemon's
        # _drive_daemon coroutine is still mid-step.
        if self._daemon_task is not None and not self._daemon_task.done():
            self._daemon_task.cancel()
            try:
                await self._daemon_task
            except (asyncio.CancelledError, Exception):
                pass

        if self.session is not None:
            try:
                await self.session.shutdown()
            except Exception:
                pass

        # Cancel pool warming (already-warm sessions remain in manifest
        # and stay valid for next launch — eject doesn't invalidate them).
        if self.session_pool is not None:
            try:
                await self.session_pool.shutdown()
            except Exception:
                pass

        # Surface in fleet log too — the user might miss the modal at
        # first if the screen flashed.
        try:
            self.query_one("#fleet_log", RichLog).write(
                "[red b]EJECTED[/red b]"
            )
        except Exception:
            pass

        # Phase 7: write a footer with reason="eject" so the heartbeat
        # sensor + future maintbot can distinguish a deliberate halt
        # (snapshot is the autopsy artifact, no triage needed) from an
        # unclean crash. The logger keeps subscribed up to this point
        # so any in-flight events from the kill cascade still get
        # written to the file.
        if self.deck_logger is not None:
            self.deck_logger.close(reason="eject")

        # Show post-eject modal with snapshot path + return-or-quit.
        # By now: constructs killed, daemon cancelled, pool stopped.
        self.push_screen(
            EjectedScreen(snapshot_path=snapshot_path),
            self._handle_post_eject_choice,
        )

    def _handle_post_eject_choice(self, choice: Optional[str]) -> None:
        """User picked 'idle' (return to goal-select) or 'quit'."""
        if choice == "quit":
            self.exit()
            return
        # 'idle' or None (Esc): clean up and return to idle state.
        # Most of the cleanup already happened in _do_eject; we just
        # need to reset the UI state and let the user start over.

        # Clear the construct panes from the prior run. After eject,
        # the panes show killed constructs that are no longer
        # meaningful — wipe the slate so the next run starts clean.
        try:
            main_container = self.query_one("#main", VerticalScroll)
            for pane in list(self.panes.values()):
                try:
                    pane.remove()
                except Exception:
                    pass
            self.panes.clear()
        except Exception:
            pass

        self._return_to_idle()

        # Reset pool meter to "warming from scratch" — the prior
        # session pool was shutdown'd, the new fleet will warm fresh.
        if self.pool_meter is not None and self.use_pool:
            self.pool_meter.update_state(
                warm=0, warming=0, target=self.pool_size,
            )

        # Restart the fleet worker so the user can spawn fresh
        # constructs after EJECT. exclusive=True cancels any
        # leftover fleet worker.
        self.run_worker(self._drive_fleet(), exclusive=True, name="fleet")

    def _write_ejection_snapshot(self) -> Optional[Path]:
        """Write a JSON postmortem of the current run. Returns the
        path on success, raises on failure (caller handles the log)."""
        if self.fleet is None:
            return None
        # File alongside the per-launch log file. Phase 7's DeckLogger
        # owns the canonical log directory; eject snapshots live next
        # to it so the morgue / maintbot can find both via one path.
        # Falls back to cwd if the logger never came up (rare:
        # disk-write failure during init).
        log_dir = (
            self.deck_logger.log_dir
            if self.deck_logger is not None
            else self.log_dir
        )
        snapshot_path = log_dir / f"ejected-{self.fleet.run_id}.json"

        # Build the snapshot. Be defensive about every field — eject
        # is a "things may already be on fire" path; we want to write
        # what we can rather than fail because one attribute is None.
        snapshot: dict = {
            "ejected_at": time.time(),
            "run_id": self.fleet.run_id,
            "reason": "user_eject",
            "fleet": {
                "total_spawned": getattr(self.fleet, "total_spawned", 0),
                "total_finalized": getattr(self.fleet, "total_finalized", 0),
                "total_cost_usd": getattr(self.fleet, "total_cost_usd", 0.0),
                "total_tokens_in": getattr(self.fleet, "total_tokens_in", 0),
                "total_tokens_out": getattr(self.fleet, "total_tokens_out", 0),
            },
            "constructs": [],
            "daemon": None,
            "goal": self.goal,
        }

        # Per-construct dump
        try:
            for c in getattr(self.fleet, "_constructs", []):
                snapshot["constructs"].append({
                    "id": c.id,
                    "task": getattr(c, "task", ""),
                    "state": c.state.value if hasattr(c, "state") else "?",
                    "session_id": getattr(c, "session_id", None),
                    "exit_code": getattr(c, "exit_code", None),
                    "runtime": getattr(c, "runtime", None),
                    "files_written": list(getattr(c, "files_written", [])),
                    "final_output": (getattr(c, "final_output", "") or "")[:2048],
                })
        except Exception:
            pass  # partial snapshot is better than no snapshot

        # Daemon state
        if self.daemon is not None:
            try:
                snapshot["daemon"] = {
                    "id": getattr(self.daemon, "id", None),
                    "session_id": getattr(self.daemon, "_session_id", None),
                    "claude_bin": getattr(self.daemon, "claude_bin", None),
                }
            except Exception:
                pass

        # Atomic write: serialize to temp, then rename. Same pattern as
        # session manifest, for the same reason — a crash mid-write
        # shouldn't leave a half-written postmortem.
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = snapshot_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)
        os.replace(tmp_path, snapshot_path)
        return snapshot_path

    # ---- M3 navigation + actions ---------------------------------------

    def _pane_list(self) -> list[ConstructPane]:
        """Ordered list of mounted construct panes in *visual* order.

        Uses the actual children of #main so move_child reordering
        (active panes above, compact ones below) is reflected in
        keyboard navigation. Falls back to dict-insertion order if
        the container isn't mounted yet."""
        try:
            main = self.query_one("#main", VerticalScroll)
            return [c for c in main.children if isinstance(c, ConstructPane)]
        except Exception:
            return list(self.panes.values())

    def _focused_pane(self) -> Optional[ConstructPane]:
        f = self.focused
        return f if isinstance(f, ConstructPane) else None

    # ---- focus / section navigation ------------------------------------

    # Section adjacency map. A section is a structural region of the
    # screen; widgets focused within a section determine which one is
    # "active." Adjacency is spatial (no wrap) per the keymap spec.
    #
    # Layout:
    #     ┌────────┬──────┬────────────┐
    #     │sidebar │ main │right_panel │
    #     ├────────┴──────┴────────────┤
    #     │       daemon_bar           │
    #     └────────────────────────────┘
    _SECTION_NEIGHBORS = {
        # section_id -> {direction: neighbor_section_id_or_None}
        "sidebar":     {"up": None,  "down": "daemon_bar", "left": None,        "right": "main"},
        "main":        {"up": None,  "down": "daemon_bar", "left": "sidebar",   "right": "right_panel"},
        "right_panel": {"up": None,  "down": "daemon_bar", "left": "main",      "right": None},
        "daemon_bar":  {"up": "main", "down": None,        "left": None,        "right": None},
    }

    # All four sections are always mounted in M5.2+. Daemon idle state
    # means "no goal active," not "no daemon panel exists."
    def _available_sections(self) -> set[str]:
        return {"sidebar", "main", "right_panel", "daemon_bar"}

    def _section_of(self, widget) -> Optional[str]:
        """Walk up the widget tree to find which section a widget lives in."""
        node = widget
        while node is not None:
            wid = getattr(node, "id", None)
            if wid in self._SECTION_NEIGHBORS:
                return wid
            node = getattr(node, "parent", None)
        return None

    def _current_section(self) -> str:
        """Best guess at the active section based on focus. Defaults to
        'main' if nothing is focused (the most useful place to land)."""
        focused = self.focused
        if focused is not None:
            section = self._section_of(focused)
            if section is not None:
                return section
        return "main"

    def _focus_section(self, section: str) -> None:
        """Move focus to the canonical first widget of a section.
        No-op if the section isn't mounted (e.g., right_panel in m3)."""
        if section not in self._available_sections():
            return

        if section == "main":
            panes = self._pane_list()
            if panes:
                panes[0].focus()
            return

        if section == "sidebar":
            # Goal pane is the only focusable in the sidebar. In m3
            # mode (no goal) there's nothing focusable; this is a no-op.
            if self.goal_pane is not None:
                self.goal_pane.focus()
            return

        if section == "right_panel":
            # Focus the content list of the active tab (Files or Tools).
            # Tab cycling within this section swaps the active tab and
            # refocuses the new content — see _cycle_in_section. Tab
            # buttons themselves aren't focus targets (Textual
            # convention; tabs are meta-controls, content is focused).
            target = self._right_panel_active_content()
            if target is not None:
                target.focus()
            return

        if section == "daemon_bar":
            # Bottom panel went tabbed (Daemon + Watchdog). Focus the
            # active tab's RichLog so keyboard nav (uppercase S into
            # daemon_bar) lands on the actual focusable target.
            # Mirrors right_panel handling above.
            target = self._bottom_panel_active_content()
            if target is not None:
                target.focus()
            return

    def action_list_up(self) -> None:
        """w: move focus to the previous focusable in the current
        section. Pure focus traversal — no scroll handling. Use W
        (capital) to scroll the focused log up.
        """
        self._list_walk(direction="up")

    def action_list_down(self) -> None:
        """s: mirror of action_list_up. Pure focus traversal."""
        self._list_walk(direction="down")

    def action_section_left(self) -> None:
        self._jump_section("left")

    def action_section_right(self) -> None:
        self._jump_section("right")

    def action_scroll_up(self) -> None:
        """W: scroll the focused log up by one line. Also flips the
        log out of auto-scroll/follow mode so new events don't yank
        the viewport back to the bottom while the netrunner is reading.
        Auto-scroll re-engages when they scroll all the way back to
        the bottom (standard tail -f / chat-app convention).

        No-op if focused widget isn't scrollable or already at top."""
        self._scroll_focused(direction="up")

    def action_scroll_down(self) -> None:
        """S: scroll the focused log down by one line. If this lands
        the viewport at the bottom edge, auto-scroll re-engages so
        new events resume tailing."""
        self._scroll_focused(direction="down")

    def action_scroll_left(self) -> None:
        """A: scroll the focused log left by one column. Useful when
        we have horizontal scroll (chatlog with wrap=False, long log
        lines)."""
        self._scroll_focused(direction="left")

    def action_scroll_right(self) -> None:
        """D: scroll the focused log right by one column."""
        self._scroll_focused(direction="right")

    def _scroll_focused(self, direction: str) -> None:
        """Implementation of lowercase w/a/s/d — "act on what you have"
        within the focused widget. Behavior depends on widget type:

          - RichLog / ScrollView / generic scrollable: scroll by line
            (vertical) or column (horizontal). Manages auto_scroll
            state per the follow-tail convention:
            * Scrolling up disables auto_scroll (netrunner is reading
              history; don't yank them back).
            * Scrolling down to the bottom re-enables auto_scroll
              (netrunner is back at the live edge; resume tailing).
            Horizontal scrolls don't touch auto_scroll — that's
            only a vertical-axis concept.

          - ListView: w/s steps the cursor between items (the natural
            unit for a discrete list); a/d are no-ops because list
            items are full-width and have no horizontal axis. We
            don't try to fake it with viewport-scroll — stepping is
            what the netrunner means.

          - ConstructPane: focus target is the pane (so q/k/etc.
            land), but the scrollable content is the inner pane_log.
            Resolve to the inner widget before scrolling.
        """
        focused = self.focused
        if focused is None:
            return

        # ListView: w/s = step item, a/d = no-op. Done before the
        # generic scroll path because ListView ALSO has scroll_<dir>
        # methods that would scroll the viewport — but viewport-
        # scrolling a list of discrete items doesn't match the
        # netrunner's intent.
        if isinstance(focused, ListView):
            if direction == "up":
                focused.action_cursor_up()
            elif direction == "down":
                focused.action_cursor_down()
            # a/d intentionally do nothing — lists are vertical.
            return

        # ConstructPane → its inner pane_log. The pane is the focus
        # target (so it can receive inject/kill/etc. actions), but the
        # scrollable surface is the log inside it.
        target = focused
        if isinstance(focused, ConstructPane):
            try:
                target = focused.query_one("#pane_log")
            except Exception:
                return
        # Duck-type: any scrollable widget exposes scroll_<dir> methods.
        scroll_method = getattr(target, f"scroll_{direction}", None)
        if scroll_method is None:
            return
        scroll_method(animate=False)
        # Auto-scroll management for vertical scrolls only.
        if direction == "up" and hasattr(target, "auto_scroll"):
            target.auto_scroll = False
        elif direction == "down" and hasattr(target, "auto_scroll"):
            if getattr(target, "is_vertical_scroll_end", False):
                target.auto_scroll = True

    def _list_walk(self, direction: str) -> None:
        """Implementation of W/S — pure focus traversal. Walk to the
        prev/next focusable in the current section. At the edge of
        the section, fall through to the first focusable of the
        up/down neighbor section.

        Cross-section fall-through matters because some sections only
        have one focusable (right_panel's active tab content,
        daemon_bar's active tab log). Without fall-through, W/S in
        those sections is dead — you can't get OUT of right_panel
        downward, and you can't get INTO daemon_bar at all (a/d only
        navigates left/right). Fall-through lets a netrunner press S
        from right_panel and land in daemon_bar's active log, which
        is the natural "go to the next thing below" gesture.

        Empty neighbor sections are skipped transitively (same as
        _jump_section). No wrap at the layout edges.
        """
        focused = self.focused
        section = self._current_section()
        focusables = self._focusables_in_section(section)

        if not focusables:
            # Nothing focusable here — try to fall through immediately.
            self._fall_through_to_neighbor(section, direction)
            return

        try:
            idx = focusables.index(focused)
        except (ValueError, AttributeError):
            # Nothing focused in this section yet — pick a sensible
            # starting point based on direction.
            idx = 0 if direction == "down" else len(focusables) - 1
            focusables[idx].focus()
            return

        if direction == "up":
            new_idx = idx - 1
            if new_idx < 0:
                # At top of section; fall through upward.
                self._fall_through_to_neighbor(section, "up")
                return
        else:
            new_idx = idx + 1
            if new_idx >= len(focusables):
                # At bottom of section; fall through downward.
                self._fall_through_to_neighbor(section, "down")
                return
        focusables[new_idx].focus()

    def _fall_through_to_neighbor(self, section: str, direction: str) -> None:
        """Walk to the up/down neighbor section's first focusable,
        skipping empty sections transitively. If the direct chain
        dead-ends because we walked through empty sections to a None
        terminator, fall back to any non-source populated section so
        the netrunner doesn't get trapped.

        The trap was: daemon_bar.up = main; main is empty (no
        constructs spawned); main.up = None. Without the fallback,
        pressing W from daemon_bar with an empty main is a no-op,
        and the netrunner has no keyboard route back upward.

        The fallback is conditional on having WALKED into at least
        one section. At a true layout edge (e.g., sidebar pressing W
        with sidebar.up = None) we stay put — the user is at the
        boundary, not stuck.

        direction is 'up' or 'down'.
        """
        current = section
        walked = False  # did we step into at least one neighbor?
        for _ in range(len(self._SECTION_NEIGHBORS)):
            neighbor = self._SECTION_NEIGHBORS[current][direction]
            if neighbor is None:
                break
            walked = True
            if self._focusables_in_section(neighbor):
                self._focus_section(neighbor)
                return
            current = neighbor
        # Direct chain ended. Stay put if we never moved (layout edge);
        # otherwise jump to any populated non-source section.
        if not walked:
            return
        # Lexical preference: sidebar > main > right_panel > daemon_bar.
        # In practice this fires for "W from daemon_bar with empty main"
        # and lands the netrunner in sidebar (goal/fleet_log) — the
        # closest populated section above.
        for fallback in ("sidebar", "main", "right_panel", "daemon_bar"):
            if fallback == section:
                continue
            if self._focusables_in_section(fallback):
                self._focus_section(fallback)
                return

    def _jump_section(self, direction: str) -> None:
        """Move focus to the neighbor section in the given direction.
        Skips empty sections transitively — if `main` has no constructs
        yet, pressing `d` from `sidebar` lands in `right_panel`. This
        keeps a/d navigation responsive in idle state where the layout
        is mostly empty.

        If no non-empty section exists in that direction, stays put.

        As of C1g.1, only "left" / "right" are reachable from
        keybindings; "up" / "down" are kept for completeness but no
        action ties to them — w/s now walk within-section instead.
        """
        current = self._current_section()
        # Walk in the given direction, skipping empty sections, up to a
        # safety bound. Four sections total; we never need >4 hops.
        section = current
        for _ in range(len(self._SECTION_NEIGHBORS)):
            neighbor = self._SECTION_NEIGHBORS[section][direction]
            if neighbor is None:
                return  # edge of layout, no wrap
            if self._focusables_in_section(neighbor):
                self._focus_section(neighbor)
                return
            section = neighbor  # empty; keep walking same direction
        # No non-empty section found in this direction; stay put.

    def action_focus_next_in_section(self) -> None:
        """Tab cycle: move to the next focusable widget within the
        current section. Uses Textual's built-in focus_next but
        constrained to within-section bounds.

        When a modal is active, defer to the modal's standard focus
        traversal — section-bounded cycling is a cyberdeck-deck
        concept that doesn't apply inside a two-input dialog. Without
        this delegation, Tab in a modal would do nothing (the App's
        priority Tab binding fires, but `_cycle_in_section` doesn't
        find any focusables in the deck section the modal is
        obscuring).
        """
        from textual.screen import ModalScreen
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_next()
            return
        self._cycle_in_section(forward=True)

    def action_focus_prev_in_section(self) -> None:
        from textual.screen import ModalScreen
        if isinstance(self.screen, ModalScreen):
            self.screen.focus_previous()
            return
        self._cycle_in_section(forward=False)

    def _focusables_in_section(self, section: str) -> list:
        """Ordered list of focusable widgets within a section. The
        order defines tab traversal."""
        if section == "main":
            return list(self._pane_list())
        if section == "sidebar":
            # goal_pane up top, fleet_log below it. Both focusable
            # since C1g.1: fleet_log gained focus targeting so w/s
            # can scroll it. Order matters — w/s walks them in this
            # order top-to-bottom.
            items: list = []
            if self.goal_pane is not None:
                items.append(self.goal_pane)
            try:
                items.append(self.query_one("#fleet_log", RichLog))
            except Exception:
                pass
            return items
        if section == "right_panel":
            # Right panel returns whatever focusables the active tab
            # exposes. Tools tab has TWO ListViews (profiles +
            # construct tools), each independently focusable; W/S
            # walks between them. The other tabs each have one
            # RichLog.
            return self._right_panel_focusables()
        if section == "daemon_bar":
            # Bottom panel went tabbed (Daemon + Watchdog). Active tab's
            # log is the focusable target. Same shape as right_panel:
            # only one focusable at a time, since the inactive tab's
            # content isn't mounted.
            return self._bottom_panel_focusables()
        return []

    def _bottom_panel_focusables(self) -> list:
        """Active bottom-panel tab's focusable content. Mirrors
        _right_panel_focusables. The TabbedContent that wraps both
        bottom panes has id='daemon_bar'; we query by that id and
        ask for its `active` attribute."""
        try:
            tabs = self.query_one("#daemon_bar", TabbedContent)
            if tabs.active == "daemon_tab":
                return [self.query_one("#daemon_log", RichLog)]
            if tabs.active == "watchdog_tab":
                return [self.query_one("#watchdog_log", RichLog)]
        except Exception:
            return []
        return []

    def _bottom_panel_active_content(self):
        focusables = self._bottom_panel_focusables()
        return focusables[0] if focusables else None

    def _right_panel_focusables(self) -> list:
        """Ordered list of focusable widgets in the active right-panel
        tab. Used by _focusables_in_section and (first element only)
        by _right_panel_active_content for the tab-cycle code path
        that just wants 'wherever focus should land when this tab
        becomes active'."""
        try:
            tabs = self.query_one("#right_panel_tabs", TabbedContent)
            active_tab = tabs.active
            if active_tab == "chatlog_tab":
                return [self.query_one("#chatlog_log", RichLog)]
            if active_tab == "files_tab":
                return [self.query_one("#files_list", ListView)]
            if active_tab == "profiles_tab":
                # P5 retool (2026-05-04): profiles graduated to its
                # own tab. Single ListView per tab now — focusables
                # collapse to one entry.
                return [self.query_one("#tools_profile_list", ListView)]
            if active_tab == "tools_tab":
                # P5 retool: tools + plugins unified into one ListView
                # with kind glyphs. The pre-P5 four-list ordering
                # (profiles → plugins → tools → scripts) collapses
                # to a single focusable per tab.
                return [self.query_one("#tools_unified_list", ListView)]
        except Exception:
            return []
        return []

    def _right_panel_active_content(self):
        """Default focus target when the right panel becomes the
        active section — first focusable in the active tab. Used by
        the tab-cycle code (Tab on the right panel switches tabs and
        focuses the new tab's first item)."""
        focusables = self._right_panel_focusables()
        return focusables[0] if focusables else None

    def _cycle_in_section(self, forward: bool) -> None:
        section = self._current_section()
        # Right panel and bottom panel are both tabbed: cycling = swap
        # the active tab. Single focusable per tab since inactive
        # content isn't mounted.
        if section == "right_panel":
            self._cycle_right_panel_tabs(forward)
            return
        if section == "daemon_bar":
            self._cycle_bottom_panel_tabs(forward)
            return
        focusables = self._focusables_in_section(section)
        if not focusables:
            return
        focused = self.focused
        try:
            idx = focusables.index(focused)
        except (ValueError, AttributeError):
            idx = -1 if forward else 0
        new_idx = (idx + 1) % len(focusables) if forward else (idx - 1) % len(focusables)
        focusables[new_idx].focus()

    def _cycle_bottom_panel_tabs(self, forward: bool) -> None:
        """Swap the active tab in the bottom panel (Daemon ↔ Watchdog)
        and focus the new tab's content. Mirror of
        _cycle_right_panel_tabs."""
        try:
            tabs = self.query_one("#daemon_bar", TabbedContent)
        except Exception:
            return
        order = ["daemon_tab", "watchdog_tab"]
        try:
            cur_idx = order.index(tabs.active)
        except ValueError:
            cur_idx = 0
        new_idx = (cur_idx + 1) % len(order) if forward else (cur_idx - 1) % len(order)
        tabs.active = order[new_idx]
        target = self._bottom_panel_active_content()
        if target is not None:
            target.focus()

    def _cycle_right_panel_tabs(self, forward: bool) -> None:
        """Swap the active tab in right_panel
        (Chatlog -> Files -> Profiles -> Tools) and focus the new
        tab's content. P5 retool (2026-05-04) added the Profiles
        tab between Files and Tools."""
        try:
            tabs = self.query_one("#right_panel_tabs", TabbedContent)
        except Exception:
            return
        # Chatlog is the default landing tab; cycle proceeds rightward
        # through the visual tab order. Adding another tab? Just append.
        order = ["chatlog_tab", "files_tab", "profiles_tab", "tools_tab"]
        try:
            cur_idx = order.index(tabs.active)
        except ValueError:
            cur_idx = 0
        new_idx = (cur_idx + 1) % len(order) if forward else (cur_idx - 1) % len(order)
        tabs.active = order[new_idx]
        # Refocus the new content
        target = self._right_panel_active_content()
        if target is not None:
            target.focus()

    def action_unfocus(self) -> None:
        self.set_focus(None)

    def action_jump(self, n: int) -> None:
        """Jump to element N within the current section."""
        section = self._current_section()
        focusables = self._focusables_in_section(section)
        if 1 <= n <= len(focusables):
            focusables[n - 1].focus()

    def action_jump_construct(self, n: int) -> None:
        """Global jump to construct N regardless of current section."""
        panes = self._pane_list()
        if 1 <= n <= len(panes):
            panes[n - 1].focus()

    # ---- primary action (Space / Enter on focused element) -------------

    def action_primary(self) -> None:
        """Space/Enter: do the contextually-obvious INTERACT on the
        focused element.

        - Construct pane (header) → toggle expand/collapse in layout
        - Goal pane → start editing
        - ListView focused with a ProfileListItem highlighted → C1g.4
          will launch a construct using that profile. Today: surface
          a 'not yet implemented' message in the fleet log so the
          netrunner sees the wire-up working without misleading them.
        - ListView focused with a ScriptListItem highlighted → eventual
          shortcut for spinning up a one-off construct that exercises
          just that tool. Today: same fleet-log message.
        - Otherwise → no-op

        The ExpandModal "magnify this widget" route lives on `z`
        (action_expand) so it doesn't collide with these list-item
        interactions.
        """
        focused = self.focused
        if isinstance(focused, ConstructPane):
            focused.expanded = not focused.expanded
            return
        if focused is self.goal_pane and self.goal_pane is not None:
            self.action_edit_goal()
            return

        # Bottom-panel logs: space = open the contextually-obvious
        # modal. Daemon log → action_talk_daemon (currently a stub
        # that toasts "not yet implemented"; this binding wires the
        # primary-action affordance now so when the modal lands the
        # netrunner muscle-memory just works). Watchdog log →
        # action_talk_watchdog. Same shape as Tools and Files panels:
        # focus the surface, hit space, primary action fires.
        # Try blocks scoped to query_one only — if action_* is
        # missing or raises, that's a real bug we want to surface.
        try:
            daemon_log = self.query_one("#daemon_log", RichLog)
        except Exception:
            daemon_log = None
        if daemon_log is not None and focused is daemon_log:
            self.action_talk_daemon()
            return
        try:
            watchdog_log = self.query_one("#watchdog_log", RichLog)
        except Exception:
            watchdog_log = None
        if watchdog_log is not None and focused is watchdog_log:
            self.action_talk_watchdog()
            return

        # ListView path. The ListView itself is the focus target;
        # `.highlighted_child` is the item currently under the
        # cursor. We dispatch on its TYPE so future list flavors
        # (FileListItem in C1g.3, etc.) can each get their own
        # branch without leaking knowledge into the generic handler.
        if isinstance(focused, ListView):
            highlighted = focused.highlighted_child
            if isinstance(highlighted, ProfileListItem):
                # Push LaunchScreen with profile context. The callback
                # composes the spawn with profile= set; user input
                # becomes the task verbatim.
                p = highlighted.profile
                # Show the recommended-tools count if any, else a dot.
                # Profiles no longer carry brake state (the deck-global
                # brake replaces it), so the old P/D/Y sigil is gone.
                rec_count = (
                    f"{len(p.recommended_tools)} recommended"
                    if p.recommended_tools else "no specific recs"
                )
                self.push_screen(
                    LaunchScreen(
                        header_markup="Launch with profile",
                        context_markup=(
                            f"[cyan]{p.name}[/cyan]  "
                            f"[dim]({p.category} · {rec_count})[/dim]"
                        ),
                    ),
                    self._make_launch_handler(profile=p),
                )
                return
            if isinstance(highlighted, ScriptListItem):
                # C1g.4-future: launch a construct configured to use
                # this script as its primary tool. For now just a
                # status line so the wiring is testable. The
                # ProfileListItem launch path is the existing analog.
                self._notify_fleet_log(
                    f"[dim yellow]script[/dim yellow] "
                    f"[dim]{highlighted.category}/[/dim]"
                    f"[cyan]{highlighted.script_name}[/cyan]: "
                    f"[dim]launch shortcut not yet "
                    f"implemented[/dim]"
                )
                return
            if isinstance(highlighted, FileListItem):
                # Display uses the shortened path so the modal context
                # line matches what the netrunner saw in the list.
                # The FILE: envelope (composed in _make_launch_handler)
                # uses the absolute file_path so the spawned construct
                # can resolve it unambiguously.
                self.push_screen(
                    LaunchScreen(
                        header_markup="Launch with file",
                        context_markup=(
                            f"[cyan]{highlighted.display_path}[/cyan]  "
                            f"[dim](from {highlighted.construct_id})[/dim]"
                        ),
                    ),
                    self._make_launch_handler(
                        file_path=highlighted.file_path,
                    ),
                )
                return
            if isinstance(highlighted, ToolListItem):
                # Tools-UI Thought of Dave (build-plan 0c): space on a
                # tool row → spawn-targeting NewConstructScreen with
                # the tool name pre-set in the task body via the
                # TOOL: envelope. The construct gets a "you should use
                # <tool>" steering hint by default; netrunner edits
                # the task before submitting.
                tool = highlighted.tool
                avail_note = (
                    "" if tool.available
                    else " [red](unavailable)[/red]"
                )
                self.push_screen(
                    LaunchScreen(
                        header_markup="Launch with tool",
                        context_markup=(
                            f"[cyan]{tool.name}[/cyan]  "
                            f"[dim]({tool.kind}: {tool.command})[/dim]"
                            f"{avail_note}"
                        ),
                    ),
                    self._make_launch_handler(tool=tool),
                )
                return
            if isinstance(highlighted, PluginListItem):
                # Tools-UI Thought of Dave (build-plan 0c): space on a
                # plugin row → spawn-targeting NewConstructScreen with
                # the plugin pre-selected (passed as `plugins=[name]`
                # to fleet.spawn so the per-spawn addendum renders
                # ONLY this plugin, not the full registry).
                pl = highlighted.plugin
                avail_note = (
                    "" if pl.available
                    else f" [red](unavailable: "
                    f"{pl.unavailable_reason})[/red]"
                )
                self.push_screen(
                    LaunchScreen(
                        header_markup="Launch with plugin",
                        context_markup=(
                            f"[cyan]{pl.name}[/cyan]  "
                            f"[dim]({pl.category})[/dim]"
                            f"{avail_note}"
                        ),
                    ),
                    self._make_launch_handler(plugin=pl),
                )
                return
            # Other ListView (none today) — no-op.
            return
        # No primary action defined for whatever is focused.

    def _notify_fleet_log(self, line: str) -> None:
        """Write a one-shot status line to the fleet log. Used for
        'not yet implemented' notices and similar netrunner-facing
        feedback that doesn't rise to the level of a chatlog event.
        Best-effort; failures are absorbed (pre-mount race or m3
        keyboard-only mode)."""
        try:
            fleet_log = self.query_one("#fleet_log", RichLog)
            fleet_log.write(line)
        except Exception:
            pass

    def _make_launch_handler(
        self,
        *,
        profile=None,
        file_path: Optional[str] = None,
        tool=None,
        plugin=None,
    ):
        """Build a callback for LaunchScreen.dismiss() that knows how
        to compose the spawn for the given context.

        - profile=<Profile>: spawn with that profile, task verbatim.
        - file_path=<str>: spawn with the active default profile,
          task wrapped in the FILE: envelope so the construct's
          initial prompt makes the file context explicit.
        - tool=<Tool>: spawn with a TOOL: envelope (build-plan 0c).
          The construct's initial prompt names the tool +
          description so the model knows what to reach for first.
          Tool-launched spawns surface all available plugins via
          plugins=None (same as other netrunner-direct spawns).
        - plugin=<Plugin>: spawn with a PLUGIN: envelope. The
          per-spawn addendum scopes to ONLY this plugin (passed
          as plugins=[plugin.name] to fleet.spawn) so the construct
          sees just the relevant capability, not the full registry.
        - Multiple contexts: not currently combined from any list
          item, but the helper handles each independently.

        Returning a closure keeps the spawn logic out of the modal
        itself (the modal only collects a string). The closure
        captures `self` and the context, so when the modal dismisses,
        the closure has everything it needs to fire."""
        def handler(task: Optional[str]) -> None:
            if not task:
                return  # cancel or empty
            if self.fleet is None:
                self._notify_fleet_log(
                    "[red]launch failed:[/red] "
                    "[dim]no active fleet (set a goal first?)[/dim]"
                )
                return
            # Compose the final task. File / tool / plugin contexts
            # go in front of the user's input as one-shot framing
            # lines — the construct sees them as part of the initial
            # prompt and treats the named context as primary input.
            final_task = task
            if file_path:
                final_task = f"FILE: {file_path}\n\n{task}"
            elif tool is not None:
                # Construct sees: "TOOL: <name> — <description>".
                # The name is the registry key (what the construct
                # invokes); the description is the tool's one-line
                # blurb so the model has the context at the top of
                # its prompt. Construct can run `<command> --help`
                # for the full interface.
                final_task = (
                    f"TOOL: {tool.name} — {tool.description}\n"
                    f"command: {tool.command}\n\n{task}"
                )
            elif plugin is not None:
                # Construct sees plugin name + the bridge invocation
                # pattern. The per-spawn addendum (rendered below
                # via plugins=[plugin.name]) carries the full
                # plugin metadata + bridge usage example.
                final_task = (
                    f"PLUGIN: {plugin.name} — {plugin.description}"
                    f"\n\n{task}"
                )
            # Profile picks: explicit (from list item) > default.
            spawn_profile = profile if profile is not None else self.default_profile
            # Plugins selection: when launching with a plugin, scope
            # the per-spawn addendum to ONLY that plugin (the netrunner
            # picked it deliberately; surfacing the full registry
            # would dilute focus). Otherwise None = all available.
            spawn_plugins = (
                [plugin.name] if plugin is not None else None
            )
            # Fleet log status line so the netrunner sees the spawn
            # ack — same shape as _handle_new_task (the n-key path).
            preview = task[:24] + ("..." if len(task) > 24 else "")
            if profile is not None:
                origin_label = f"profile={profile.name}"
            elif file_path:
                origin_label = f"file={file_path}"
            elif tool is not None:
                origin_label = f"tool={tool.name}"
            elif plugin is not None:
                origin_label = f"plugin={plugin.name}"
            else:
                origin_label = "n-key"
            self._notify_fleet_log(
                f"[bright_blue]⟳[/bright_blue] launch ({origin_label}): {preview}"
            )
            # origin="netrunner" — Tools/Files launch is human-driven.
            # (Local var renamed to origin_label to avoid shadowing the
            # spawn kwarg; both are about provenance but at different
            # granularities — origin_label is descriptive for the log
            # line, origin is the categorical attribution flag.)
            self.run_worker(
                self.fleet.spawn(
                    final_task,
                    profile=spawn_profile,
                    origin="netrunner",
                    plugins=spawn_plugins,
                    # P4 retool: render per-spawn addendum (profile.tools
                    # resolved + plugins). plugins=spawn_plugins means
                    # "surface only the daemon-selected (or netrunner-
                    # picked) plugin". None falls through to all
                    # available — same as the n-key / file-launch path.
                    per_spawn_addendum=self._build_per_spawn_addendum(
                        spawn_profile, spawn_plugins,
                    ),
                    # Caliber Phase 1: netrunner-direct spawn — use
                    # deck default. Future: a launch-modal field for
                    # one-off caliber overrides (the new EffortPicker-
                    # Screen is designed to be reused here).
                    caliber=self.default_caliber,
                ),
                name="spawn",
            )
        return handler

    def action_expand(self) -> None:
        """z: open ExpandModal on the focused widget.

        Universal magnifier — works on any RichLog (chatlog, fleet
        log, files, tools, future per-pane logs) and on ConstructPane
        (routes to the pane's inner log). Snapshot at open time;
        `r` inside refreshes from the source.

        For chatlog and pane logs, the modal uses an "untruncated"
        re-render of the source events so long content (multi-paragraph
        thinking, large tool_results) shows in full rather than being
        chopped at the same 500-char cap the live view uses.
        """
        focused = self.focused
        if focused is None:
            return

        # ConstructPane → re-render its event buffer untruncated for
        # the modal. Same provider-closure pattern as the chatlog: the
        # modal pulls from the buffer (raw events kept around) and
        # re-formats with summarize(untruncated=True), so long thinking
        # blocks and big tool_results show in full rather than the
        # 500-char chop the live pane uses. `r` inside the modal calls
        # the provider again — useful when the construct produces new
        # events while the modal is open.
        if isinstance(focused, ConstructPane):
            title = f"Pane log — {focused.construct_id}"
            pane = focused
            self.push_screen(ExpandModal(
                title=title,
                snapshot_lines=pane.render_buffer(untruncated=True),
                source_widget_id=None,
                provider=lambda p=pane: p.render_buffer(untruncated=True),
                # Chronological pane log — open on the most recent
                # event, not the oldest.
                start_at_end=True,
            ))
            return

        # RichLog with un-truncated provider (chatlog, future pane
        # logs). The provider closure re-renders raw events with
        # untruncated=True so the modal shows the full content, not
        # the live-view-trimmed snapshot.
        if isinstance(focused, RichLog) and focused.id == "chatlog_log":
            title = "Chatlog (full content)"
            self.push_screen(ExpandModal(
                title=title,
                snapshot_lines=self._render_chatlog_buffer(untruncated=True),
                source_widget_id="chatlog_log",
                provider=lambda: self._render_chatlog_buffer(untruncated=True),
                # Chronological chatlog — open at most recent event.
                start_at_end=True,
            ))
            return

        # Any other RichLog (fleet_log, files_list, tools_list) —
        # snapshot the live widget. These are formatted in-place
        # without per-event truncation, so the snapshot IS the content.
        if isinstance(focused, RichLog):
            title = _expandable_title(focused)
            source_id = (
                focused.id
                if focused.id in _EXPANDABLE_RICHLOGS
                else None
            )
            snapshot = _snapshot_richlog(focused)
            # Every entry in _EXPANDABLE_RICHLOGS today is chronological
            # (fleet_log, daemon_log, watchdog_log) — open the modal at
            # the most recent line. ListView paths and ad-hoc RichLogs
            # outside the registry still default to top-of-document.
            chat_shaped = focused.id in _EXPANDABLE_RICHLOGS
            self.push_screen(ExpandModal(
                title=title,
                snapshot_lines=snapshot,
                source_widget_id=source_id,
                start_at_end=chat_shaped,
            ))
            return

        # ListView path: focus is on the ListView itself; the
        # highlighted child is what we want to view. Each list-item
        # type loads its content from a different source on disk.
        # `r` (refresh in modal) re-reads from disk so edits made
        # while the modal is open show up.
        if isinstance(focused, ListView):
            highlighted = focused.highlighted_child
            if isinstance(highlighted, FileListItem):
                self._open_file_view(
                    highlighted.file_path,
                    display_path=highlighted.display_path,
                )
                return
            if isinstance(highlighted, ProfileListItem):
                # The profile's source path lives on the registry
                # entry. Read fresh from disk rather than using
                # already-loaded TOML values — the disk version
                # includes comments and the netrunner's exact
                # whitespace, which is what they typically want to
                # see when "viewing the profile."
                src_path = getattr(highlighted.profile, "source_path", None)
                title = f"Profile: {highlighted.profile.name}"
                if src_path:
                    self._open_file_view(str(src_path), title=title)
                else:
                    # Built-in or registry-internal profile with no
                    # source file — render the dataclass repr as a
                    # fallback. Rare path; default profile usually
                    # gets seeded to disk on first run.
                    self._open_text_view(
                        title=title,
                        text=repr(highlighted.profile),
                    )
                return
            if isinstance(highlighted, ScriptListItem):
                title = (
                    f"Script: {highlighted.category}/"
                    f"{highlighted.script_name}"
                )
                self._open_file_view(
                    highlighted.script_path, title=title,
                )
                return
            if isinstance(highlighted, ToolListItem):
                # Tools-UI Thought of Dave (build-plan 0c): z on a
                # tool row → info modal showing manifest fields +
                # availability. Tools aren't file-backed (they're
                # entries in tools.toml), so we synthesize text
                # rather than opening a file. The full registry
                # entry is in <home>/tools/tools.toml; netrunner
                # can navigate there if they want the raw source.
                #
                # sub-feature 3 (2026-05-05): the info modal also
                # carries an Advisor handle — pressing `h` from
                # inside the modal opens AdvisorScreen scoped to
                # this tool. The "press H for interactive help"
                # tooltip renders alongside the manifest text.
                target = target_from_tool(highlighted.tool)
                siblings = self._build_advisor_siblings(target.name)
                self._open_text_view(
                    title=f"Tool: {highlighted.tool.name}",
                    text=self._render_tool_info(highlighted.tool),
                    advisor_target=target,
                    advisor_siblings=siblings,
                )
                return
            if isinstance(highlighted, PluginListItem):
                # Tools-UI Thought of Dave (build-plan 0c): z on a
                # plugin row → info modal with manifest + README.
                # README is the LLM-facing interface doc; netrunner
                # reads it for invocation details. When no README,
                # falls back to manifest-only synthesis.
                #
                # sub-feature 3 (2026-05-05): the README content
                # ALSO threads into the AdvisorTarget's
                # extended_text so the Advisor's system prompt has
                # full interface depth (flag tables, examples).
                # We read the file once here regardless of which
                # display path the modal uses — the cost is small
                # and the symmetry's worth it.
                pl = highlighted.plugin
                readme_path = (
                    pl.source_dir / "README.md"
                    if pl.source_dir is not None else None
                )
                readme_text = ""
                if readme_path is not None and readme_path.is_file():
                    try:
                        # 200KB cap matches what _load_file_lines
                        # would tolerate on its own; over that, we
                        # fall back to manifest-only Advisor
                        # context. Best-effort.
                        if readme_path.stat().st_size <= 200_000:
                            readme_text = readme_path.read_text(
                                encoding="utf-8", errors="replace",
                            )
                    except Exception:
                        readme_text = ""
                target = target_from_plugin(pl, readme_text=readme_text)
                siblings = self._build_advisor_siblings(target.name)
                if readme_path and readme_path.is_file():
                    self._open_file_view(
                        str(readme_path),
                        title=f"Plugin: {pl.category}/{pl.name}",
                        advisor_target=target,
                        advisor_siblings=siblings,
                    )
                else:
                    self._open_text_view(
                        title=f"Plugin: {pl.category}/{pl.name}",
                        text=self._render_plugin_info(pl),
                        advisor_target=target,
                        advisor_siblings=siblings,
                    )
                return
            return  # ListView with non-viewable item → no-op

    def _build_advisor_siblings(self, target_name: str) -> tuple[str, ...]:
        """Build the sibling-tools name list for an Advisor session.

        Names only — the Advisor is allowed to mention other tools
        in cross-references but must not pretend to know their
        internals. We also filter out the target's own name so the
        prompt doesn't read "you can also reference ripgrep" inside
        the ripgrep Advisor's prompt.

        Used by action_expand's tool + plugin branches; centralised
        here so the filter rule + ordering live in one place. Tools
        before plugins reflects the visual ordering of the unified
        Tools panel; helps the Advisor prefer mentioning siblings
        the netrunner has been recently looking at.
        """
        names = tuple(
            t.name for t in self.tool_registry.all()
        ) + tuple(
            p.name for p in self.plugin_registry.all()
        )
        return tuple(n for n in names if n != target_name)

    def action_copy_focused(self) -> None:
        """y: yank focused widget's content to the OS clipboard.

        Sidesteps the Ctrl+C-as-copy issue on Windows where the
        SIGINT propagated to child claude subprocesses (filed in
        cyberdeck-state.md → Filed gotchas, 2026-04-30). The set of
        copyable surfaces matches `action_expand` — anything you can
        zoom, you can yank.
        """
        focused = self.focused
        if focused is None:
            self._toast("copy: nothing focused")
            return
        text = _extract_text_for_copy(focused)
        if text is None:
            self._toast("copy: nothing to yank from this widget")
            return
        if not text:
            self._toast("copy: empty content")
            return
        ok, err = clipboard.copy(text)
        if ok:
            char_count = len(text)
            self._toast(f"copy: yanked {char_count} chars to clipboard")
        else:
            # Stash the reason so the netrunner can grep their fleet
            # log later if the message scrolls off. err is a short
            # human-readable phrase from clipboard.py.
            self._toast(f"copy: failed — {err}")

    def action_copy_focused_json(self) -> None:
        """Y: yank focused widget's structured data as JSON.

        Same surface map as `action_copy_focused`, different shape:
        the underlying record (raw events, bus snapshot, list-item
        fields) instead of the rendered text. The netrunner's escape
        hatch when rendered output has already lost the structure
        they need downstream.
        """
        focused = self.focused
        if focused is None:
            self._toast("copy-json: nothing focused")
            return
        text = _extract_json_for_copy(focused)
        if text is None:
            self._toast("copy-json: no structured data on this widget")
            return
        ok, err = clipboard.copy(text)
        if ok:
            self._toast(f"copy-json: yanked {len(text)} chars to clipboard")
        else:
            self._toast(f"copy-json: failed — {err}")

    def _open_file_view(
        self,
        path: str,
        *,
        title: Optional[str] = None,
        display_path: Optional[str] = None,
        advisor_target: Optional[AdvisorTarget] = None,
        advisor_siblings: tuple[str, ...] = (),
    ) -> None:
        """Open ExpandModal with the contents of `path`. Title defaults
        to a "File: <shortened path>" header. Errors (missing file,
        permission denied, binary content) surface as a single-line
        error in the modal rather than crashing — viewing should be
        forgiving.

        advisor_target/advisor_siblings: optional Advisor handle for
        the modal's `h` keybind (Tools-UI Thought of Dave 0c). Set
        only when the file being viewed represents a tool or plugin
        — the plugin README path passes the loaded Plugin in. All
        other file-view callers leave it None; their modals don't
        render the "press H" hint.
        """
        if title is None:
            short = display_path or self._shorten_path(path)
            title = f"File: {short}"

        provider = lambda p=path: self._load_file_lines(p)
        snapshot = provider()
        self.push_screen(ExpandModal(
            title=title,
            snapshot_lines=snapshot,
            source_widget_id=None,
            provider=provider,  # `r` re-reads from disk
            advisor_target=advisor_target,
            advisor_siblings=advisor_siblings,
        ))

    def _open_text_view(
        self,
        *,
        title: str,
        text: str,
        advisor_target: Optional[AdvisorTarget] = None,
        advisor_siblings: tuple[str, ...] = (),
    ) -> None:
        """Open ExpandModal with arbitrary text content. Used as the
        fallback for list-items that don't map cleanly to a file
        (e.g. registry-internal profiles, synthesized tool info,
        synthesized plugin info when no README).

        advisor_target/advisor_siblings: see _open_file_view."""
        lines = text.splitlines() or [text]
        self.push_screen(ExpandModal(
            title=title,
            snapshot_lines=lines,
            source_widget_id=None,
            advisor_target=advisor_target,
            advisor_siblings=advisor_siblings,
        ))

    def _render_tool_info(self, tool) -> str:
        """Synthesize an info-view text for a registry-backed tool.
        Tools-UI Thought of Dave (build-plan 0c). Tools live in
        tools.toml so they're not file-backed individually; this
        renders the dataclass fields as a readable info page.

        Format mirrors what a netrunner would see if they read
        tools.toml directly, plus an availability summary the
        registry computed at scan time."""
        avail = (
            "[green]available[/green]" if tool.available
            else f"[red]unavailable[/red] · {tool.unavailable_reason}"
        )
        lines: list[str] = [
            f"[b]{tool.name}[/b]  [dim]({tool.kind})[/dim]",
            f"status:  {avail}",
            f"command: {tool.command}",
        ]
        if tool.path:
            lines.append(f"path:    {tool.path}")
        lines.append("")
        lines.append("[b]description[/b]")
        # description is a single line in the registry; render verbatim.
        lines.append(tool.description or "(no description)")
        if tool.help_text:
            lines.append("")
            lines.append("[b]help[/b]")
            lines.extend(tool.help_text.splitlines())
        lines.append("")
        lines.append("[dim]Edit ~/tools/tools.toml to modify this "
                     "tool. Press Esc to close.[/dim]")
        return "\n".join(lines)

    def _render_plugin_info(self, plugin) -> str:
        """Synthesize an info-view text for a plugin without a
        README.md. Tools-UI Thought of Dave (build-plan 0c).
        Mirrors _render_tool_info's shape so the modal layouts
        match across kinds. When the plugin DOES have a README,
        the file path opens directly (better view fidelity for
        markdown — the modal's syntax highlighting handles it)."""
        avail = (
            "[green]available[/green]" if plugin.available
            else f"[red]unavailable[/red] · {plugin.unavailable_reason}"
        )
        lines: list[str] = [
            f"[b]{plugin.name}[/b]  [dim]({plugin.category})[/dim]",
            f"status:  {avail}",
        ]
        if plugin.source_dir is not None:
            lines.append(f"path:    {plugin.source_dir}")
        lines.append(f"entry:   {plugin.entry}")
        if plugin.requires_platforms:
            lines.append(
                f"platforms: {', '.join(plugin.requires_platforms)}"
            )
        if plugin.requires_python_imports:
            lines.append(
                f"requires: {', '.join(plugin.requires_python_imports)}"
            )
        lines.append("")
        lines.append("[b]description[/b]")
        lines.append(plugin.description or "(no description)")
        lines.append("")
        lines.append("[dim](no README.md found at the plugin's source "
                     "dir; shown synthesized info instead.)[/dim]")
        return "\n".join(lines)

    @staticmethod
    def _load_file_lines(path: str) -> list:
        """Read `path` and return its lines for the modal. Failure
        modes folded into a single error line so the modal can render
        them like any other content. UTF-8 with replacement so a
        weird byte doesn't kill the read; size cap so we don't load
        a 5GB log into RAM.

        For files with a recognized language extension, the entire
        content goes back as a single Rich Syntax renderable — RichLog
        renders it line-by-line with pygments-driven syntax highlighting.
        Falls back to plain-text-with-bracket-escape for unknown
        extensions, which keeps `[default]` TOML headers from being
        eaten by the markup parser.
        """
        SIZE_CAP = 2_000_000  # 2MB — generous for source/profile/log
        try:
            import os as _os
            size = _os.path.getsize(path)
            if size > SIZE_CAP:
                return [
                    f"[red]file is {size:,} bytes "
                    f"(over {SIZE_CAP:,} cap); "
                    f"open it directly to read[/red]",
                    "",
                    f"[dim]path: {path}[/dim]",
                ]
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if not content:
                return [f"[dim](empty file: {path})[/dim]"]

            # Try syntax highlighting. If we recognize the file as a
            # language pygments understands, return a Syntax renderable
            # in a single-element list. RichLog.write accepts any
            # Renderable and renders it across multiple visible lines
            # — w/s scrolling still works line-by-line because each
            # highlighted line is its own Strip in the viewport.
            lang = CyberdeckApp._detect_file_language(path, content)
            if lang is not None:
                try:
                    syntax = Syntax(
                        content,
                        lang,
                        # github-dark: balanced, prose-friendly, no
                        # token backgrounds, muted compared to
                        # ansi_dark (which used the full ANSI palette
                        # and felt aggressive). Looks like what
                        # GitHub / VSCode show by default.
                        theme="github-dark",
                        # Line numbers off. The gutter Rich draws for
                        # them has a slightly tinted background that
                        # reads as "highlight" — visual weight without
                        # navigation value for short files. If a netrunner
                        # needs line numbers for a long source file,
                        # they're more likely to open it in their
                        # editor anyway. The modal is for "skim what's
                        # in here", not "find line 247".
                        line_numbers=False,
                        # Source code shouldn't wrap mid-line; reading
                        # is easier when the visual structure (indent,
                        # alignment) is preserved. w/s + Home/End nav
                        # works regardless. Long lines scroll
                        # horizontally via the body's natural overflow.
                        word_wrap=False,
                        # Inherit the terminal/panel background so
                        # the code area sits flush with the modal
                        # surface, no second background showing
                        # through.
                        background_color="default",
                    )
                    return [syntax]
                except Exception:
                    # Pygments lexer error or some other rendering
                    # issue — fall through to plain text rather than
                    # showing nothing. Whatever broke, the netrunner
                    # still wants to read the file.
                    pass

            # Plain-text fallback. Escape opening brackets so the
            # markup parser doesn't reinterpret literal brackets in
            # config files / source as color tags.
            return [line.replace("[", "\\[") for line in content.splitlines()]
        except FileNotFoundError:
            return [f"[red]file not found:[/red] {path}"]
        except PermissionError:
            return [f"[red]permission denied:[/red] {path}"]
        except OSError as e:
            return [f"[red]read failed:[/red] {e}"]
        except Exception as e:
            return [f"[red]unexpected error reading file:[/red] {e!r}"]

    # Extension → pygments language name. Conservative coverage —
    # the most common file types a netrunner would view from the
    # deck. Pygments knows hundreds more; if you want one added,
    # check `pygments.lexers.find_lexer_class_by_name(...)` works
    # and add the extension here.
    _EXT_TO_LANG = {
        ".py": "python",
        ".pyw": "python",
        ".sh": "bash", ".bash": "bash", ".zsh": "bash",
        ".toml": "toml",
        ".md": "markdown", ".markdown": "markdown",
        ".json": "json", ".jsonl": "json",
        ".yaml": "yaml", ".yml": "yaml",
        ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "tsx", ".jsx": "jsx",
        ".html": "html", ".htm": "html",
        ".css": "css", ".scss": "scss",
        ".rs": "rust",
        ".go": "go",
        ".c": "c", ".h": "c",
        ".cpp": "cpp", ".hpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
        ".java": "java",
        ".rb": "ruby",
        ".sql": "sql",
        ".xml": "xml",
        ".lua": "lua",
        ".vim": "vim",
        ".dockerfile": "dockerfile",
        ".tf": "hcl",  # terraform
        ".tfvars": "hcl",
        ".ini": "ini",
        ".cfg": "ini",
        ".diff": "diff", ".patch": "diff",
    }

    @staticmethod
    def _detect_file_language(path: str, content: str) -> Optional[str]:
        """Return a pygments language name for `path`, or None if we
        don't recognize it. Three signals, in order of reliability:

          1. File extension (most reliable; ~30 supported below).
          2. Bare-name special cases (Dockerfile, Makefile — these
             conventionally have no extension).
          3. Shebang on the first line — catches scripts saved
             without extensions (`<home>/tools/recon/scan` with
             `#!/bin/bash` first line).

        Returning None drops the modal back to plain-text rendering.
        """
        from pathlib import Path as _Path
        ext = _Path(path).suffix.lower()
        if ext in CyberdeckApp._EXT_TO_LANG:
            return CyberdeckApp._EXT_TO_LANG[ext]

        name_lower = _Path(path).name.lower()
        # Common no-extension special-case names.
        if name_lower in ("dockerfile",):
            return "dockerfile"
        if name_lower in ("makefile", "gnumakefile"):
            return "makefile"

        # Shebang-driven detection for executables without extension.
        first_line = content.split("\n", 1)[0] if content else ""
        if first_line.startswith("#!"):
            shebang = first_line.lower()
            if "python" in shebang:
                return "python"
            if "node" in shebang:
                return "javascript"
            if "ruby" in shebang:
                return "ruby"
            # Order matters: 'bash' check before 'sh' since /bin/bash
            # contains both, but bash is the more accurate label.
            if "bash" in shebang:
                return "bash"
            if "sh" in shebang:
                return "bash"  # sh-flavored close enough for highlighting
            if "perl" in shebang:
                return "perl"
        return None

    # ---- construct interaction -----------------------------------------
    # (_focused_pane is defined once in the navigation section; this
    # section consumes it. An identical paste-duplicate used to live
    # here too, silently overriding the original — caught by slice 2's
    # in-deck code review against the codebase. Removed 2026-04-29.)

    def action_kill_focused(self) -> None:
        """Soft kill: terminate the focused construct. No-op if the
        construct is already in a terminal state."""
        pane = self._focused_pane()
        if pane is None or self.fleet is None:
            return
        if pane.state in TERMINAL_PANE_STATES:
            self._toast(f"{pane.construct_id} already {pane.state}; not killing")
            return
        cid = pane.construct_id
        fleet_log = self.query_one("#fleet_log", RichLog)
        fleet_log.write(f"[orange1]×[/orange1] {cid}")
        # Source label travels through fleet.kill_construct → bus
        # event → finalize.kill_source. Filed 2026-04-30 late after
        # real-deck mystery-kill investigation: prior to this, the
        # only signal of a netrunner-k kill was a TUI-only fleet_log
        # write — bus + log file showed nothing.
        self.run_worker(
            self.fleet.kill_construct(cid, source="netrunner_k"),
            name=f"kill-{cid}",
        )

    def action_hard_kill_focused(self) -> None:
        """Hard kill: terminate the focused construct AND register its
        task fingerprint with the Watchdog's session blacklist.

        After registration:
          - DaemonSession refuses any future spawn whose first 80
            chars (lowercased) match the registered fingerprint.
          - In-flight constructs whose task ALSO matches get visually
            flagged (red border) but NOT auto-killed. The netrunner
            decides whether to k them individually; the spec is
            explicit that automatic mass-kill is what EJECT is for.
          - The daemon sees the blacklist on every outcome turn and
            knows to halt branches of its plan that depended on the
            forbidden pattern.

        "Hard" here refers to the blacklist propagation, not a more
        violent termination — the actual process kill is the same
        SIGTERM/SIGKILL escalation as soft-kill.
        """
        pane = self._focused_pane()
        if pane is None:
            return
        if pane.state in TERMINAL_PANE_STATES:
            self._toast(
                f"{pane.construct_id} already {pane.state}; not killing"
            )
            return
        if self.fleet is None:
            return

        cid = pane.construct_id

        # Capture rich context BEFORE the kill — final_output and
        # files_written are populated mid-stream and will still be
        # readable post-kill, but state changes during the kill window
        # so we snapshot it now. The construct may have produced no
        # output yet (just spawned); that's fine — entry just carries
        # less context for the future tripwire-authoring pass to
        # reason over.
        construct = self.fleet.get_construct(cid)
        if construct is not None and self.watchdog is not None:
            entry = BlacklistEntry(
                fingerprint=_blacklist_fingerprint(construct.task),
                full_task=construct.task,
                source_construct_id=cid,
                source_construct_state=construct.state.value,
                source_final_output=construct.final_output[:500],
                source_files_written=tuple(construct._files_written),
                reason="hard-kill",
            )
            self.watchdog.blacklist.add(entry)
            # Note: scan-and-flag of in-flight matches happens in
            # _handle_blacklist_event, which fires via Blacklist's
            # on_event callback. Single source of truth for the
            # flagging logic, regardless of who added the entry.

        fleet_log = self.query_one("#fleet_log", RichLog)
        fleet_log.write(
            f"[red]×[/red] {cid} [dim](hard-kill: blacklisted)[/dim]"
        )
        # Source label travels through fleet.kill_construct → bus
        # event → finalize.kill_source. blacklist.added is already
        # emitted to the bus separately (above), but the kill itself
        # gets its own event so the kill source is observable
        # uniformly with other kill paths.
        self.run_worker(
            self.fleet.kill_construct(cid, source="netrunner_shift_k"),
            name=f"hard-kill-{cid}",
        )

    def action_x_focused(self) -> None:
        """Universal deck-wide approval / execute keypress.

        X is unidirectional (post-2026-05-07 redesign): X always means
        "allow this particular action to ignore the rules." The
        netrunner's judgment is final — no surface where X means
        "stop / interrupt." YOLO no longer installs the hook, so the
        prior bidirectional matrix collapsed cleanly.

        Resolution order (first match wins):
          1. A focused ConstructPane with an open delay → allow that
             pane's blocked tool call. (Primary path: delaying panes
             get promoted to the top of #main with a magenta border,
             so "find the construct that needs attention, focus it,
             X" is the everyday flow.)
          2. Single open brake/tripwire delay anywhere → allow it.
             (If only one delay is pending, X always lands on it
             regardless of focus.)
          3. Most-recent open AttentionItem → approve it. (Blacklist
             proposals from critical+bad_enough tripwires land here.
             Items render in the AttentionPanel above the construct
             pool with their own countdown bars.)
          4. Nothing → toast "no pending action".

        X-press semantic per surface (all unidirectional approve):
          - Brake delay (deny default):       X = allow the call
          - Tripwire delay (deny default):    X = allow + skip kill
                                              (and skip blacklist
                                              proposal if bad_enough)
          - Attention item (blacklist proposal): X = approve the
                                              blacklist add
        The per-pane overlay / panel always shows what X will do
        BEFORE the netrunner presses, so it's never ambiguous.
        """
        if self.fleet is None or self.delay_monitor is None:
            return

        # 1. Focused pane's open delay.
        focused_pane = self._focused_pane()
        if (focused_pane is not None
                and focused_pane.delay_entry is not None):
            self._x_resolve_delay(focused_pane.delay_entry)
            return

        # 2. Single-pending-brake-delay convenience.
        try:
            active_delays = list(self.delay_monitor._active.values())
        except Exception:
            active_delays = []
        if len(active_delays) == 1:
            self._x_resolve_delay(active_delays[0])
            return

        # 3. Most-recent attention item. Sorted-by-opened-at-desc means
        # the first iter value is newest. Single item case is also
        # handled here by virtue of "most recent of one is the one."
        if self._attention_items:
            newest = max(
                self._attention_items.values(),
                key=lambda i: i.opened_at,
            )
            ok = self._approve_attention(newest.item_id)
            if ok:
                try:
                    fleet_log = self.query_one("#fleet_log", RichLog)
                    fleet_log.write(
                        f"[green]X[/green] approved: "
                        f"[magenta]{newest.kind}[/magenta] "
                        f"[dim]{newest.title}[/dim]"
                    )
                except Exception:
                    pass
                return

        # 4. Nothing pending — bail with toast. Multiple delays open +
        # no focused-pane match also lands here (netrunner needs to
        # focus the specific construct). Same UX as the prior
        # "no delay to override" message; widened wording.
        if len(active_delays) > 1:
            self._toast(
                "multiple delays open — focus a construct's pane to choose"
            )
        else:
            self._toast("no pending action for X")

    def _x_resolve_delay(self, target_entry) -> None:
        """Common path for X-press on a brake-hook delay. Extracted
        from action_x_focused so the dispatch above stays readable."""
        cid = target_entry.construct_id
        action = target_entry.override_action
        # Note before write so the resolution event is attributed
        # correctly even if the hook polls before the monitor's next
        # tick (note_override populates the dict synchronously; the
        # disappearance lookup happens later).
        self.delay_monitor.note_override(cid, action)
        ok = write_delay_override(self.home_dir, cid, action)
        if ok:
            try:
                fleet_log = self.query_one("#fleet_log", RichLog)
                fleet_log.write(
                    f"[green]X[/green] override: "
                    f"[cyan]{cid}[/cyan] → [b]{action}[/b]"
                )
            except Exception:
                pass
        else:
            self._toast(f"X-press for {cid} failed (couldn't write override)")

    def _scan_for_tripwires(self, deck_event: "DeckEvent") -> None:
        """Bus subscriber that feeds construct events into the
        watchdog's TripwireEngine.

        The DeckEvent's payload is the FleetEvent dataclass. Only
        construct events (FleetEvent.kind == "event") are scanned;
        meta events (spawn, finalize, etc.) carry control-plane
        metadata that tripwires don't need to match against today.
        The engine's scope/event-kind gating decides what actually
        gets compared.

        Best-effort: a scan failure must not break the chatlog
        subscriber path. The engine itself is defensive (catches per-
        listener exceptions in on_fire dispatch); we wrap one more
        layer here in case the event payload is malformed. Phase 8
        of the unified-event-stream slice migrated this from the
        legacy `fleet.add_listener(...)` shim to a bus subscriber."""
        fevent: FleetEvent = deck_event.payload
        if fevent.kind != "event":
            return
        if self.watchdog is None or self.watchdog.tripwires is None:
            return
        try:
            event_kind = fevent.payload.get("event_kind", "other")
            raw = fevent.payload.get("raw", {}) or {}
            if not isinstance(raw, dict):
                return
            self.watchdog.tripwires.scan(
                fevent.construct_id, event_kind, raw,
            )
        except Exception:
            pass

    def _handle_tripwire_fire(self, fire: "TripwireFire") -> None:
        """Render a tripwire fire to the chatlog.

        Post-2026-05-07 redesign: the kill-on-critical and the
        bad_enough → blacklist-proposal effects MOVED to
        `_handle_delay_resolved`. Both are now gated on the netrunner's
        X-window outcome — if the netrunner X-allows the offending
        tool call within the 5s delay, kill is skipped AND the
        blacklist proposal is suppressed. The netrunner's override
        is final across all of the tripwire's consequences.

        This handler renders the immediate fire to chatlog so the
        netrunner sees the event the moment the engine fires; the
        consequence rendering ("auto-term" / blacklist proposal)
        happens in the delay-resolved handler when the X-window
        actually closes.
        """
        # Severity → markup style. Yellow for warning matches the
        # brake-blocked treatment elsewhere in the chatlog.
        from tripwires import Severity
        if fire.severity == Severity.CRITICAL:
            tag_style = "red b"
        elif fire.severity == Severity.WARNING:
            tag_style = "yellow"
        else:  # low
            tag_style = "dim yellow"
        excerpt = fire.matched_text_excerpt or "(empty match)"
        # Excerpt is already truncated to ~120 chars by the engine;
        # bracket-escape so any [foo] inside the excerpt doesn't
        # confuse Rich's markup parser.
        excerpt_safe = excerpt.replace("[", r"\[")
        try:
            self._chatlog_write(
                f"[{tag_style}]⚠ tripwire[/{tag_style}] "
                f"[bold]{fire.tripwire_name}[/bold] on "
                f"[cyan]{fire.construct_id}[/cyan]: "
                f"[dim]{excerpt_safe}[/dim]"
            )
        except Exception:
            pass

    # ---- slice 2: LLM-authored tripwires --------------------------------
    #
    # Authoring trigger points are goal-start (handled inside
    # _start_daemon_task so both --goal launch and idle→running submit
    # share one path) and explicit non-clarification goal-update via
    # `e` (handled in _handle_goal_submitted's mid-flight branch).
    # Clarifications skip authoring — the model already authored for
    # this goal direction and the netrunner is just adding detail.
    # Pivots and scope-changes re-author from scratch (clear
    # LLM_AUTHORED, register fresh).

    def _kick_off_tripwire_authoring(
        self,
        *,
        classification: Optional[str] = None,
        old_goal: Optional[str] = None,
    ) -> None:
        """Fire-and-forget the watchdog's tripwire authoring pass for
        the current goal. Goal-set / goal-update flow doesn't block on
        the LLM call directly; the daemon's spawn dispatch DOES block
        on it via _tripwire_authoring_complete (see filed-2026-05-02
        race fix below).

        Skips if there's no goal or no watchdog. Defensive — neither
        condition should hit in practice (callers gate before calling)
        but the no-ops keep this safe to invoke from anywhere.

        Race-fix wiring (2026-05-02): real-deck-observed that fast
        constructs (echo-style test spawns, ~7s end-to-end) were
        finishing before authoring landed (~25s), so authored
        tripwires never had a chance to fire on them. Fix: clear the
        _tripwire_authoring_complete event when authoring starts;
        DaemonSession awaits this event before dispatching each spawn
        action; wrapper sets it again on completion. This delays the
        first batch of spawns by however long authoring takes (~25s
        typical), but ensures every daemon-driven spawn has authored
        coverage from the moment its first event streams in. Re-
        authoring on goal-update follows the same pattern but only
        skips the gate when LLM_AUTHORED tripwires are already
        registered (existing rules still cover during re-authoring;
        no need to block).
        """
        if not self.goal:
            return
        if self.watchdog is None:
            return
        # Clear the gate before launching the worker. The worker's
        # finally block sets it again whether authoring succeeds,
        # fails, or crashes — never leaves the gate stuck closed.
        try:
            self._tripwire_authoring_complete.clear()
        except Exception:
            pass
        self.run_worker(
            self._author_tripwires_wrapper(
                goal=self.goal,
                classification=classification,
                old_goal=old_goal,
            ),
            name="tripwire-authoring",
            exclusive=False,
        )

    async def _author_tripwires_wrapper(
        self,
        *,
        goal: str,
        classification: Optional[str],
        old_goal: Optional[str],
    ) -> None:
        """Snapshot deck context, run the watchdog authoring pass, and
        render the result. Spawned as a worker by
        _kick_off_tripwire_authoring; never called directly from the
        netrunner-facing flow.

        Context snapshot includes brake state and active blacklist
        entries — the prompt builder uses both to avoid duplicating
        brake-hook coverage and to author sharper rules around the
        netrunner's already-rejected task shapes."""
        if self.watchdog is None:
            return

        # Snapshot context. Both reads are cheap and synchronous; we
        # do them at task-spawn time rather than inside the watchdog
        # so the authoring substrate stays ignorant of TUI globals.
        brake_label = self.brake_state_store.state.value
        blacklist_summary = [
            entry.short_summary()
            for entry in self.watchdog.blacklist.entries
        ]

        # Start announcement. Dim because authoring is a background
        # event the netrunner doesn't need to attend to — they'll see
        # the registered count when it finishes.
        try:
            self._chatlog_write(
                "[dim]\\[watchdog] authoring tripwires for current goal…[/dim]"
            )
        except Exception:
            pass

        try:
            try:
                result = await self.watchdog.author_tripwires(
                    goal,
                    classification=classification,
                    old_goal=old_goal,
                    brake_label=brake_label,
                    blacklist_summary=blacklist_summary,
                )
            except Exception as e:
                try:
                    self._chatlog_write(
                        f"[red]\\[watchdog] tripwire authoring "
                        f"crashed:[/red] [dim]{e}[/dim]"
                    )
                except Exception:
                    pass
                return

            self._render_tripwire_authoring_result(result)
        finally:
            # ALWAYS release the spawn gate, regardless of outcome.
            # If authoring succeeded → spawns proceed with the new
            # rules. If authoring failed → spawns proceed under
            # whatever rules WERE registered (defaults at minimum).
            # If authoring crashed → same. Filed 2026-05-02 race fix:
            # under no condition should the gate stay closed forever
            # and starve the daemon of the ability to dispatch spawns.
            try:
                self._tripwire_authoring_complete.set()
            except Exception:
                pass

    def _render_tripwire_authoring_result(
        self, result: "TripwireAuthoringResult",
    ) -> None:
        """Render the chatlog summary for one authoring pass.

        Three shapes:
          - failure: red line with reason; raw response preview if
            parse-failure (so the netrunner can see what the model
            actually said when it didn't output JSON).
          - success, empty: dim line noting the model decided no rules
            applied. Legitimate outcome — better than padding with
            weak rules.
          - success, non-empty: yellow line with registered count +
            names. Rejected entries (validation failures, regex
            compile failures) get one dim line each so the netrunner
            can see what didn't make it and why.
        """
        rung = "fork" if result.used_resume else "fresh"
        elapsed = f"{result.elapsed_s:.1f}s"

        if not result.success:
            try:
                self._chatlog_write(
                    f"[red]\\[watchdog] tripwire authoring failed[/red] "
                    f"[dim]({rung}, {elapsed}):[/dim] {result.error}"
                )
                if result.raw_response:
                    # Bracket-escape so any [foo] in the raw response
                    # doesn't confuse Rich markup. Truncate to keep
                    # the chatlog readable.
                    preview = (
                        result.raw_response[:200]
                        .replace("[", r"\[")
                        .replace("\n", " ")
                    )
                    self._chatlog_write(
                        f"[dim]   raw preview: {preview}…[/dim]"
                    )
            except Exception:
                pass
            return

        n_reg = len(result.registered)
        n_rej = len(result.rejected)

        if n_reg == 0 and n_rej == 0:
            try:
                self._chatlog_write(
                    f"[dim]\\[watchdog] authored 0 tripwires "
                    f"({rung}, {elapsed}) — no rules applied[/dim]"
                )
            except Exception:
                pass
            return

        try:
            names = ", ".join(tw.name for tw in result.registered) or "(none)"
            line = (
                f"[yellow]\\[watchdog] +{n_reg} tripwires authored[/yellow] "
                f"[dim]({rung}, {elapsed}):[/dim] {names}"
            )
            if n_rej:
                line += f" [dim](rejected {n_rej})[/dim]"
            self._chatlog_write(line)
            for label, reason in result.rejected:
                # Names from the model could plausibly contain brackets
                # in pathological cases; escape defensively.
                label_safe = str(label).replace("[", r"\[")
                reason_safe = str(reason).replace("[", r"\[")
                self._chatlog_write(
                    f"[dim]   rejected {label_safe}: {reason_safe}[/dim]"
                )
        except Exception:
            pass

    def _handle_blacklist_event(self, event: dict) -> None:
        """Receive blacklist events from the Watchdog's Blacklist
        instance. Today only fires on `blacklist_added`; future
        tripwire-authored entries will use the same channel.

        Two responsibilities:
          1. Render a chatlog line so the netrunner sees the addition
             in the same surface they read everything else through.
          2. Scan in-flight constructs for fingerprint matches and
             visually flag any matches (red border + chatlog notice).
             Per netrunner direction: flag, do not auto-kill — at
             that point we should be ejecting.

        Called from the watchdog's Blacklist when an entry is added.
        Synchronous, fast, no async; safe to call from any context."""
        if event.get("type") != "blacklist_added":
            return
        entry = event.get("entry")
        if not isinstance(entry, BlacklistEntry):
            return

        # Chatlog announcement. Goes to the fleet log since blacklist
        # is a session-scoped concept that affects future spawns
        # across the whole fleet.
        try:
            fleet_log = self.query_one("#fleet_log", RichLog)
            fleet_log.write(
                f"[red]⛔ blacklist[/red] [dim]+[/dim] "
                f'"{entry.full_task[:50]}'
                f'{"..." if len(entry.full_task) > 50 else ""}" '
                f"[dim](source: {entry.source_construct_id})[/dim]"
            )
        except Exception:
            pass

        # In-flight match scan. Iterate the fleet snapshot for
        # constructs whose task matches the new entry's fingerprint.
        # Skip the source construct itself (it's about to die from
        # the kill we just kicked off) and skip terminal-state
        # constructs (they're not "in flight" — flagging a done
        # pane is just visual noise).
        if self.fleet is None:
            return
        matched: list[str] = []
        for c in self.fleet.constructs:
            if c.id == entry.source_construct_id:
                continue
            if c.state.value in ("done", "failed", "killed"):
                continue
            if _blacklist_fingerprint(c.task) != entry.fingerprint:
                continue
            matched.append(c.id)
            # Find the pane and apply the .-blacklisted class. Pane
            # may not exist if the construct hasn't been mounted yet
            # (rare but possible in the spawn race window) — skip
            # silently; future scans (none today, but slice 2's
            # tripwire-authored entries will run at richer cadence)
            # will catch it on a later add.
            try:
                for pane in self.query(ConstructPane):
                    if pane.construct_id == c.id:
                        pane.add_class("-blacklisted")
                        break
            except Exception:
                pass

        if matched:
            try:
                fleet_log = self.query_one("#fleet_log", RichLog)
                fleet_log.write(
                    f"[yellow]⚠[/yellow] [dim]in-flight matches "
                    f"flagged (not auto-killed): "
                    f"{', '.join(matched)}[/dim]"
                )
            except Exception:
                pass

    def action_queue_inject(self) -> None:
        """Open inject modal pre-set to queue mode (deliver at next break)."""
        self._open_inject_modal(mode="queue")

    def action_interrupt_inject(self) -> None:
        """Open inject modal pre-set to interrupt mode (kill + redirect)."""
        self._open_inject_modal(mode="interrupt")

    def _open_inject_modal(self, mode: str) -> None:
        """Open the inject modal targeting the focused construct, or
        toast if no construct is focused / focused construct is in a
        terminal state."""
        pane = self._focused_pane()
        if pane is None:
            self._toast("inject: no construct focused")
            return
        if pane.state in TERMINAL_PANE_STATES:
            # Special-case REDIRECTED: the session continues in a
            # follow-up construct, so the netrunner probably wanted
            # that one. Point them at it rather than just refusing.
            # Without this catch we'd happily spawn a *second* follow-up
            # against the same session_id that the existing successor
            # is already using — two parallel turns on one session,
            # nothing good downstream.
            if pane.state == "redirected" and pane.injected_to:
                self._toast(
                    f"inject: {pane.construct_id} was redirected to "
                    f"{pane.injected_to} — focus that pane and inject there"
                )
            else:
                self._toast(
                    f"inject: {pane.construct_id} is {pane.state}; "
                    f"can't inject into a terminal construct"
                )
            return
        # Verify the construct has a session_id we can resume. Without
        # one, --resume won't work. Should be present unless the
        # construct died before its system_init landed.
        construct = self._construct_by_id(pane.construct_id)
        if construct is None or construct.session_id is None:
            self._toast(
                f"inject: {pane.construct_id} has no session_id yet; "
                f"can't inject (try again in a moment)"
            )
            return
        self.push_screen(
            InjectScreen(
                construct_id=pane.construct_id,
                construct_task=construct.task,
                mode=mode,
            ),
            self._handle_inject_submitted,
        )

    def _handle_inject_submitted(
        self,
        result: Optional[tuple[str, str, str]],
    ) -> None:
        """Callback from InjectScreen. result is (mode, message,
        target_construct_id) or None.

        target_construct_id comes from the modal, not from current
        focus — focus may have moved while the modal was up (a
        construct finalized and focus auto-shifted, the user Tabbed
        while typing, etc.). We must inject into the construct the
        netrunner *meant* to inject into, which is the one that was
        focused at q/Q press time."""
        if result is None:
            return
        mode, message, target_id = result
        # Look up the original target by id. This is the fix for the
        # focus-after-modal issue: the construct we want is the one
        # baked into the modal, not whatever's focused now.
        construct = self._construct_by_id(target_id)
        if construct is None or construct.session_id is None:
            self._toast(
                f"inject: target {target_id} unavailable "
                "(may have finalized before injection landed)"
            )
            return
        # Confirm the target is still inject-able. If it terminated
        # while the modal was up, queue-inject can still spawn a
        # follow-up via --resume (the session lives on); interrupt
        # just becomes a no-op kill on a dead process.
        target_pane = self.panes.get(target_id)

        if mode == "queue":
            # Polite: wait for the construct to finalize, then spawn a
            # follow-up with --resume + the new message. Stored on the
            # app; consumed in _handle_meta when the finalize event fires.
            # If the construct has ALREADY finalized, the pending entry
            # would never be consumed via the meta event path — we'd
            # need to spawn the follow-up directly. Detect that case.
            already_terminal = (
                target_pane is not None
                and target_pane.state in TERMINAL_PANE_STATES
            )
            if already_terminal:
                # Construct finalized while the modal was up. Spawn the
                # follow-up immediately rather than waiting for a
                # finalize event that already fired.
                self.run_worker(
                    self._spawn_injected_followup(
                        target_id, "queue", message,
                        construct.session_id, construct.task,
                    ),
                    name=f"inject-{target_id}",
                )
                try:
                    self.query_one("#fleet_log", RichLog).write(
                        f"[dim]inject (post-finalize) for {target_id}: "
                        f"{message[:60]}{'...' if len(message) > 60 else ''}[/dim]"
                    )
                except Exception:
                    pass
                return
            self._pending_injections[target_id] = (
                "queue", message, construct.session_id, construct.task,
            )
            try:
                self.query_one("#fleet_log", RichLog).write(
                    f"[dim]inject queued for {target_id}: "
                    f"{message[:60]}{'...' if len(message) > 60 else ''}[/dim]"
                )
            except Exception:
                pass
            return

        # Interrupt: kill current work, then spawn the follow-up with
        # an explicit redirect framing on the resumed session. The
        # construct will see "Previous turn was interrupted by the
        # netrunner. Their new direction supersedes prior intent: ..."
        # as the next user message.
        session_id = construct.session_id
        prior_task = construct.task
        try:
            self.query_one("#fleet_log", RichLog).write(
                f"[yellow]inject interrupting[/yellow] {target_id}"
            )
        except Exception:
            pass
        # Store as a pending injection so _handle_meta does the spawn
        # AFTER the kill finalizes (avoids racing the slot semaphore).
        self._pending_injections[target_id] = (
            "interrupt", message, session_id, prior_task,
        )
        # Now actually kill — finalize event will fire, _handle_meta
        # will see the pending injection and spawn the follow-up.
        # Source "inject_interrupt" surfaces in finalize.kill_source +
        # fleet.kill_requested bus event so observers can tell this
        # kill apart from a netrunner-k or wedge-timeout (real-deck
        # filed 2026-04-30 late: prior to this, every kill looked
        # the same in the log).
        if self.fleet is not None:
            self.run_worker(
                self.fleet.kill_construct(target_id, source="inject_interrupt"),
                name=f"inject-kill-{target_id}",
            )

    def _construct_by_id(self, construct_id: str) -> Optional[Construct]:
        """Look up a construct by ID via the Fleet's internal list.
        Returns None if not found or fleet not yet up."""
        if self.fleet is None:
            return None
        for c in self.fleet._constructs:
            if c.id == construct_id:
                return c
        return None

    async def _spawn_injected_followup(
        self,
        original_id: str,
        mode: str,
        message: str,
        session_id: str,
        prior_task: str,
    ) -> None:
        """Spawn a follow-up Construct that resumes the prior session
        with the injected message as the next user turn.

        The two modes carry different *intent* about how the model
        should integrate the message:

        - queue: "and also this" — the prior work was acceptable; the
          new message extends or refines it. Additive.
        - interrupt: "stop, do this instead" — the prior approach is
          being corrected. The new message may replace or redirect
          the prior intent rather than supplement it. The model
          should treat the new message as authoritative course
          correction, not a parallel addition.

        The framings below are deliberately terse — the model already
        has its own session history to reason about *what* it was
        doing; framing only needs to convey the human's *attitude*
        toward that history.

        original_id is the construct we're following up on. It's threaded
        into fleet.spawn so the new construct's 'spawned' event carries
        the link back to its origin — that's how the parent pane learns
        about its outgoing chevron, and the new pane learns about its
        incoming chevron."""
        if self.fleet is None:
            return
        if mode == "interrupt":
            # Stop-and-redirect framing. The bracketed prefix tells
            # the model the human halted them and is pivoting; the
            # message body is the new direction. "Reconsider" rather
            # than "continue" — the model should not assume the new
            # message complements prior work.
            framed_task = (
                "[Netrunner halted you mid-work and is redirecting. "
                "Reconsider your approach in light of this new "
                "instruction; do not assume it supplements your "
                "prior plan.]\n\n"
                f"{message}"
            )
        else:
            # Queue-inject: additive framing. The prior work finished
            # naturally; the human waited and is adding to the load.
            # "Also" signals supplement rather than replacement.
            framed_task = (
                "[Netrunner is adding to your work. Continue with your "
                "prior plan and also address this:]\n\n"
                f"{message}"
            )
        new_construct: Optional[Construct] = None
        try:
            # Inject continues an existing session; the system prompt
            # is already cached server-side, so per-spawn addendum
            # rendering would just bloat the resume turn for no gain.
            # Pass None — fleet falls back to the static deck_addendum
            # which the original session already has anyway.
            new_construct = await self.fleet.spawn(
                task=framed_task,
                resume_session_id=session_id,
                parent_id=original_id,
                origin="inject",
                # Caliber Phase 1: pass deck default. Whether Claude
                # Code honors a per-turn effort change on `--resume`
                # is an open question (filed in the caliber design
                # doc); we pass the args explicitly either way so the
                # behavior is at least consistent across spawns.
                caliber=self.default_caliber,
            )
        except Exception as e:
            try:
                self.query_one("#fleet_log", RichLog).write(
                    f"[red]inject spawn failed:[/red] {e!r}"
                )
            except Exception:
                pass

        # If the followup didn't actually spawn, the parent's
        # REDIRECTED state is a lie (no destination exists). Roll back
        # to KILLED so the visual matches reality. Queue-inject parents
        # don't need rollback — their state was already the natural
        # finalized one (DONE/FAILED/etc.), and a missing followup
        # just means the chevron link never appears.
        if new_construct is None and mode == "interrupt":
            parent_pane = self.panes.get(original_id)
            if parent_pane is not None:
                parent_pane.state = "killed"

    # ---- daemon / goal -------------------------------------------------

    def action_talk_watchdog(self) -> None:
        """t — open AskWatchdogScreen and on submit enqueue the
        question. Async: this returns immediately, the answer arrives
        later in the chatlog. The Watchdog has read-only access to
        the recent event stream so it can answer "what's happening?"
        questions without affecting any plans.

        Distinct from action_talk_daemon (T) — that's plan-affecting
        and synchronous. The visual differentiation (yellow border /
        prefix vs daemon's green) reinforces the gravity difference.
        """
        depth = self.watchdog.queue_depth()
        busy = self.watchdog.is_busy()
        self.push_screen(
            AskWatchdogScreen(queue_depth=depth, busy=busy),
            self._on_watchdog_question_submitted,
        )

    def _on_watchdog_question_submitted(
        self, question: Optional[str]
    ) -> None:
        """Modal callback. None = cancelled (no-op). Non-empty string
        = enqueue. We snapshot the recent chatlog into a context blob
        at submit time so the watchdog reasons about what was
        happening when the question was asked, not when it eventually
        gets processed (which could be seconds later if a queue is
        ahead of it).
        """
        if not question:
            return
        # QOL: switch the bottom tab to Watchdog so the netrunner sees
        # their question land + the eventual answer arrive without
        # having to manually click over. The TabbedContent fires no
        # extra side effects on .active set; safe to do regardless of
        # what was previously visible. Best-effort guard for pre-mount
        # races.
        try:
            self.query_one("#daemon_bar", TabbedContent).active = "watchdog_tab"
        except Exception:
            pass
        # Show the question in chatlog immediately. The netrunner's
        # focus may have moved on by the time the answer comes back;
        # this anchors the eventual answer to a visible asked-line so
        # the conversation reads chronologically.
        self._chatlog_write(
            rf"[yellow]\[watchdog][/yellow] [dim]asked:[/dim] {question}"
        )
        # Also write to the dedicated Watchdog tab — full-fidelity
        # Q&A history with paragraph breaks preserved. Chatlog gets
        # the breadcrumb (chronological context); the tab is where
        # you go to read the actual conversation.
        if self.watchdog_pane is not None:
            self.watchdog_pane.write_question(question)
        # Snapshot the chatlog. Not via _render_chatlog_buffer (which
        # produces markup-decorated lines) — we want plain text for
        # the model. Same buffer; same formatters; just no markup
        # decoration on the result.
        context_text = self._build_watchdog_context(max_events=30)
        self.watchdog.ask(
            question,
            context_text,
            self._on_watchdog_answer,
        )
        # Update the busy/queue indicator. queue_depth() reflects what's
        # *waiting* (excludes the in-flight one), so after enqueueing
        # we recompute. The submitted question may already have been
        # picked up by the worker — in which case queue_depth=0 and
        # busy=True. Or it may be queued behind another — busy=True,
        # queue_depth>=1.
        if self.watchdog_pane is not None:
            self.watchdog_pane.set_status(
                busy=True,
                queue_depth=self.watchdog.queue_depth(),
            )

    def _build_watchdog_context(
        self, *, max_events: int = 30
    ) -> str:
        """Snapshot the most recent N chatlog-relevant bus events as
        plain text for the watchdog to reason over.

        We re-use the chatlog formatters with untruncated=True so the
        watchdog sees full content (long thinking blocks, full
        tool_results) — the netrunner asked because they want
        understanding, and a chopped context blocks that. Markup
        tags get stripped because the model doesn't need them and
        they waste tokens.

        Headers prepended (cheap, bounded, deterministic):
          - `DECK BRAKE: <state>` — current brake level so the
            watchdog can interpret `brake blocked: ...` markers it
            sees on finalized events.
          - `CURRENT BLACKLIST:` — entries currently registered with
            the watchdog's session blacklist. Authoritative current-
            state source; the chatlog markers (`⛔ blacklist + ...`)
            tell the watchdog WHEN entries were added, but those can
            scroll off the buffer. The header is the source of truth
            for "what's blacklisted right now."

        Phase 6 source: iterates `self.bus.snapshot()` and uses the
        same `_chatlog_format_bus_event` dispatcher as the magnified
        view. Pre-Phase-6 source was a separate `_chatlog_event_buffer`
        deque whose readers had a fleet/daemon-only filter that
        silently dropped tripwire/brake/blacklist/etc. markers despite
        the watchdog system prompt instructing the model to read them.
        Bus snapshot fixes that bug class structurally — every event
        the netrunner sees in the chatlog is also visible to the
        watchdog's Q&A context.
        """
        # Tail of the bus snapshot — chronological order, capped at
        # max_events so we don't waste tokens on a long history when
        # the netrunner cares about recent activity. Pre-filter to
        # chatlog-relevant kinds so the cap is measured in things-
        # the-netrunner-saw rather than total bus volume (otherwise
        # a busy fleet with system_init / rate_limit churn would
        # eat the whole budget on events the chatlog formatter
        # returns None for).
        relevant: list[tuple[DeckEvent, str]] = []
        if getattr(self, "bus", None) is not None:
            for event in self.bus.snapshot():
                try:
                    line = self._chatlog_format_bus_event(
                        event, untruncated=True,
                    )
                except Exception:
                    continue
                if line is not None:
                    relevant.append((event, line))
        tail = relevant[-max_events:]
        lines: list[str] = []
        for event, line in tail:
            if line is None:
                continue
            # Strip Rich markup tags. The model doesn't need them and
            # they're token waste. Broad regex to catch any
            # `[anything]` or `[/anything]` shape — colors with
            # # hex, bold/dim modifiers, complex compound styles.
            import re
            stripped = re.sub(r"\[/?[^\[\]]+\]", "", line)
            # Newline collapse same as the chatlog write path so
            # one event = one line in the context.
            stripped = (
                stripped.replace("\r\n", " ")
                .replace("\n", " ")
                .replace("\r", " ")
            )
            ts = time.strftime("%H:%M:%S", time.localtime(event.timestamp))
            lines.append(f"{ts}  {stripped}")
        # Prepend the brake-state header. Single line, bounded cost.
        # Watchdog uses this to interpret `brake blocked: ...` markers
        # the chatlog renders on finalized events.
        brake_header = f"DECK BRAKE: {self.brake_state_store.state.value}"

        # Prepend the current-blacklist block. Authoritative current-
        # state — without this, the watchdog can only see blacklist
        # additions that happen to still be in the chatlog buffer
        # window. Empty case suppresses the header entirely so a
        # session with no blacklist entries doesn't waste tokens on
        # an "empty list" line.
        blacklist_block = ""
        if (self.watchdog is not None
                and self.watchdog.blacklist is not None
                and len(self.watchdog.blacklist) > 0):
            bl_lines = ["CURRENT BLACKLIST:"]
            for entry in self.watchdog.blacklist.entries:
                bl_lines.append(f"  - {entry.short_summary()}")
            blacklist_block = "\n".join(bl_lines) + "\n\n"

        header = brake_header + "\n\n" + blacklist_block
        if not lines:
            return header + "(no recent activity)"
        return header + "\n".join(lines)

    def _on_watchdog_answer(self, wq: WatchdogQuestion) -> None:
        """Callback fired by the Watchdog worker when an answer is
        ready (or failed). Writes to the chatlog. Multi-paragraph
        answers get newlines collapsed to ` ¶ ` so they stay one
        chatlog entry — long answers are still readable via the
        ExpandModal (z on chatlog) which shows the full text.

        IMPORTANT: this callback fires from the watchdog's worker
        task, which is on the same event loop as the App but not
        synchronously on the App's render path. Calls into the UI
        widget tree are safe because Textual serializes them.
        """
        if wq.failed:
            err = wq.error or "(unknown failure)"
            self._chatlog_write(
                rf"[yellow]\[watchdog][/yellow] [red]failed:[/red] "
                f"{err[:200]}"
            )
            if self.watchdog_pane is not None:
                self.watchdog_pane.write_failure(err)
                # Recompute busy state: there may be more queued.
                qd = self.watchdog.queue_depth()
                self.watchdog_pane.set_status(busy=qd > 0, queue_depth=qd)
            return
        answer = wq.answer or "(empty)"
        # Compact answer for the chatlog breadcrumb (paragraphs → ¶,
        # whitespace collapsed). Full-fidelity answer with paragraph
        # breaks preserved goes into the watchdog tab.
        compact = answer.replace("\r\n", "\n")
        # Normalize multiple newlines into the glyph.
        import re
        compact = re.sub(r"\n\s*\n+", " ¶ ", compact)
        # Remaining single newlines (within a paragraph) become spaces.
        compact = compact.replace("\n", " ")
        # Collapse runs of whitespace.
        compact = re.sub(r"\s+", " ", compact).strip()
        self._chatlog_write(
            rf"[yellow]\[watchdog][/yellow] [dim]→[/dim] {compact}"
        )
        # Full answer to the dedicated tab.
        if self.watchdog_pane is not None:
            self.watchdog_pane.write_answer(answer)
            qd = self.watchdog.queue_depth()
            self.watchdog_pane.set_status(busy=qd > 0, queue_depth=qd)

    def _replay_watchdog_history(self) -> None:
        """Read the persistent watchdog Q&A log and render the last
        N entries into WatchdogPane before live activity begins.

        Counterpart to the live `_on_watchdog_answer` writer. Best-
        effort throughout — if the history file is missing, empty,
        or unparseable, we render nothing. The pane is initialized
        empty in compose() so the no-history case looks like a fresh
        deck, which is the correct UX.

        Replayed entries get a 'prior session' visual marker so the
        netrunner can tell them apart from live current-session Q&A.
        """
        if self.watchdog is None or self.watchdog.history is None:
            return
        if self.watchdog_pane is None:
            return
        try:
            entries = self.watchdog.history.replay(n=50)
        except Exception:
            entries = []
        if not entries:
            return
        try:
            self.watchdog_pane.write_history_separator(len(entries))
            for e in entries:
                self.watchdog_pane.write_question(e.question)
                if e.failed:
                    self.watchdog_pane.write_failure(
                        e.error or "(unknown failure)"
                    )
                else:
                    self.watchdog_pane.write_answer(e.answer)
            self.watchdog_pane.write_live_session_marker()
        except Exception:
            # If the pane render fails (mount race / Textual quirk),
            # we already have the entries on disk; the netrunner can
            # still find them via cat/grep against the JSONL.
            pass

    def action_talk_daemon(self) -> None:
        """T — open the talk-to-daemon modal.

        The loud counterpart to `t` (watchdog Q&A). What you type here
        is plan-affecting input that the daemon weighs on its next
        outcome turn. Goes through the same deferred-propagation
        machinery as goal updates: stashed on DaemonSession, picked
        up at next natural break, wake-event keeps idle delivery
        prompt.

        Without a running session, the modal still opens but warns
        clearly that there's no daemon to talk to. Submit-with-no-
        session drops the message and toasts a follow-up reminder.
        We don't auto-start a session because the message-as-goal
        semantics are murky — better to have the netrunner press
        `e` deliberately.
        """
        # Compute pending message count so the modal can hint about
        # queue depth. Defensive: if no session, count is zero.
        if self.session is not None:
            pending = len(self.session._pending_netrunner_messages)
            session_running = self.daemon is not None
        else:
            pending = 0
            session_running = False

        self.push_screen(
            TalkDaemonScreen(
                pending_count=pending,
                session_running=session_running,
            ),
            self._handle_daemon_message_submitted,
        )

    def _handle_daemon_message_submitted(
        self, message: Optional[str]
    ) -> None:
        """Callback from TalkDaemonScreen on submit/cancel.

        Two branches:
          - None or empty → cancel, no-op (Esc-to-cancel path)
          - Non-empty → if session running, stash + surface in chatlog
                        + write to daemon pane; otherwise toast that
                        the message was dropped (cooperate with the
                        modal's no-session warning).
        """
        if not message:
            return

        if self.session is None or self.daemon is None:
            self._toast(
                "no daemon session — message dropped. "
                "Press 'e' to set a goal and start one."
            )
            return

        # QOL: switch the bottom tab to Daemon so the netrunner sees
        # the message register in the daemon pane + watches the
        # response arrive without a manual tab click. Symmetric with
        # the watchdog path. Best-effort guard for pre-mount races.
        try:
            self.query_one("#daemon_bar", TabbedContent).active = "daemon_tab"
        except Exception:
            pass

        # Stash for next outcome turn delivery
        self.session.set_pending_netrunner_message(message)

        # Chatlog breadcrumb so the netrunner sees their message
        # register and can scroll back to it later. Truncated for
        # the inline summary; full text reaches the daemon intact.
        preview = message if len(message) <= 80 else message[:77] + "..."
        self._chatlog_write(
            f"[primary]\\[netrunner → daemon][/primary] {preview}"
        )

        # Also surface in the daemon pane so the daemon-tab reader
        # gets a clean record of the conversation flowing in. The
        # daemon's response will appear here as normal daemon output
        # on the next turn, pairing visually.
        if self.daemon_pane is not None:
            self.daemon_pane.write_line(
                f"[primary]≫ netrunner:[/primary] {message}"
            )

    def action_edit_goal(self) -> None:
        """e — open the goal-set/edit modal.

        Three modes depending on session state:
          - No goal set yet → set goal, start daemon
          - Goal set but no session running → replace goal, start daemon
          - Session running → edit goal mid-flight; classify the diff
            (clarification / scope-change / pivot) and propagate to
            the daemon at next outcome turn (per spec)

        Force-push (apply-now, interrupt the in-flight turn) is M5+.
        Today's deferred path is fine — the daemon picks up the
        update at the next natural break, which the
        set_pending_goal_update wake-event makes prompt even when no
        constructs are running.
        """
        # All paths use the same modal, pre-filled with current goal.
        # The submit handler decides whether this is "set", "replace",
        # or "mid-flight edit" based on session state at submit time.
        self.push_screen(
            GoalSetScreen(current_goal=self.goal or ""),
            self._handle_goal_submitted,
        )

    @staticmethod
    def _classify_goal_diff(old: str, new: str) -> str:
        """Classify a goal edit per spec — clarification / scope-change
        / pivot. Cheap heuristic: tokenize, compute Jaccard, check
        subset relation. Spec mentions a model-driven classifier as
        the eventual approach; this lives here as the cheap
        deferrable-to-later version.

        Rules in priority order:
          1. If old is a strict subset of new (every old token
             appears in new), it's a clarification — netrunner
             added detail without changing direction.
          2. If Jaccard similarity ≥ 0.5, it's a scope-change —
             same general territory but materially different
             coverage.
          3. Otherwise, it's a pivot — the new goal shares little
             vocabulary with the old.

        Stopwords stripped. Punctuation stripped. Case-folded. This
        is "good enough to label" — a model would do better but at
        the cost of latency and tokens, which we don't pay yet.
        """
        import re
        STOPWORDS = {
            "a", "an", "the", "is", "are", "was", "were", "be", "been",
            "being", "to", "of", "in", "on", "at", "for", "with", "by",
            "from", "as", "and", "or", "but", "if", "then", "i", "me",
            "my", "we", "us", "our", "you", "your", "it", "its", "this",
            "that", "these", "those", "do", "does", "did", "have", "has",
            "had", "will", "would", "could", "should", "may", "might",
            "can", "shall", "must", "not", "no",
        }

        def tokenize(s: str) -> set[str]:
            # Crude singularize: trailing 's' on tokens of length 4+
            # so "website" / "websites" match without a real stemmer.
            # 4+ length guard avoids killing short tokens like "is",
            # "as" (which are stopwords anyway) and "us" / "ms" /
            # "os" that ARE legitimate as-is. Imperfect — "process"
            # becomes "proces", "address" becomes "addres" — but
            # symmetric, so both old AND new get the same butchering
            # and Jaccard stays meaningful.
            tokens = set()
            for w in re.findall(r"[a-z0-9]+", s.lower()):
                if not w or w in STOPWORDS:
                    continue
                if len(w) >= 4 and w.endswith("s"):
                    w = w[:-1]
                tokens.add(w)
            return tokens

        old_tokens = tokenize(old)
        new_tokens = tokenize(new)

        if not old_tokens and not new_tokens:
            return "clarification"
        if not old_tokens:
            return "scope-change"  # going from empty to something
        if old_tokens.issubset(new_tokens):
            return "clarification"

        intersection = old_tokens & new_tokens
        union = old_tokens | new_tokens
        jaccard = len(intersection) / len(union) if union else 0.0
        # Threshold tuned against test cases — 0.25 catches the
        # "same general territory, different coverage" cases (a goal
        # diff with ~25-50% token overlap usually reads as
        # scope-change to a human) while still routing real pivots
        # (zero or near-zero overlap) to the pivot bucket. Real-
        # world goals are short (5-10 content tokens) so each token
        # shift swings the ratio more than in long-prose comparisons.
        #
        # Known limitation: crude singularize doesn't handle -es
        # plurals (e.g. "process" vs "processes" stem differently),
        # so some clarifications-with-plural-shift get mis-classed
        # as pivots. Real Porter stemming would fix this. M5+ when
        # we go model-driven, this becomes moot.
        if jaccard >= 0.25:
            return "scope-change"
        return "pivot"

    def _handle_goal_submitted(self, goal: Optional[str]) -> None:
        """Callback from GoalSetScreen on submit/cancel.

        Three branches:
          - None or empty → cancel, no-op
          - Same as current goal → no-op, brief toast
          - Different → either set/replace (idle) or update (running)
        """
        if not goal:
            return
        old_goal = self.goal or ""
        # Identical text — nothing to do. Surface as a toast so the
        # netrunner sees their action register without any state
        # change.
        if goal == old_goal:
            self._toast("goal unchanged")
            return

        # Mid-flight branch: session is running, daemon exists.
        # Classify the diff and stash it for the session loop to
        # propagate.
        if (self.session is not None
                and self.daemon is not None
                and old_goal):
            classification = self._classify_goal_diff(old_goal, goal)
            self.goal = goal
            if self.goal_pane is not None:
                self.goal_pane.set_goal(goal)
            self._refresh_subtitle()
            self._refresh_sidebar_info()
            # Surface in chatlog so the netrunner can trace what they
            # changed. The daemon's response to this update will
            # arrive on the next turn and will appear in the chatlog
            # as a normal daemon line — they pair visually.
            self._chatlog_write(
                f"[yellow]\\[goal-update][/yellow] [dim]{classification}:[/dim] "
                f"{goal[:100]}{'...' if len(goal) > 100 else ''}"
            )
            self.session.set_pending_goal_update(goal, classification, old_goal)
            # Slice 2: re-author tripwires on scope-change / pivot —
            # the watch patterns for the new direction may differ from
            # the old. Skip on clarification: the netrunner is adding
            # detail to the existing goal, the model already authored
            # for this direction, re-running burns tokens for no
            # signal change. The clear-LLM_AUTHORED-then-register
            # lifecycle inside author_tripwires drops yesterday's
            # rules cleanly when we do re-author.
            if classification != "clarification":
                self._kick_off_tripwire_authoring(
                    classification=classification,
                    old_goal=old_goal,
                )
            return

        # Idle branch: set or replace goal, start daemon. Same as
        # before — this is the original action_edit_goal behavior
        # for first-time goal entry.
        self.goal = goal
        if self.goal_pane is not None:
            self.goal_pane.set_goal(goal)
        self._refresh_subtitle()
        self._refresh_sidebar_info()
        if self.daemon_pane is not None:
            self.daemon_pane.set_status("starting")
            self.daemon_pane.write_line(
                f"[dim]— new goal: {goal[:60]}{'...' if len(goal) > 60 else ''} —[/dim]"
            )
        self._start_daemon_task()

    def action_toggle_daemon_pause(self) -> None:
        self._toast("daemon pause/unpause: not yet implemented")

    # ---- spawn ---------------------------------------------------------

    def action_new_construct(self) -> None:
        self.push_screen(NewConstructScreen(), self._handle_new_task)

    def action_new_construct_invisible(self) -> None:
        # Same modal for now; visible/invisible distinction lands in M5+
        self._toast("invisible spawn: same as visible until M5+")
        self.action_new_construct()

    # ---- routing / wiring ----------------------------------------------

    def action_wire_route(self) -> None:
        self._toast("wiring: not yet implemented")

    # ---- plugins -------------------------------------------------------

    def action_plugin_quickfire(self) -> None:
        self._toast("plugin quickfire: no plugins registered")

    def action_plugin_picker(self) -> None:
        self._toast("plugin picker: no plugins registered")

    def action_toggle_airgap(self) -> None:
        self._toast("airgap toggle: no plugins to gate")

    def action_open_limits(self) -> None:
        """Open the Limits modal showing current caps + usage; apply
        any submitted changes back to fleet/session state."""
        # Cap-hit auto-open is deferred (M5.x). For now, l just opens
        # the viewer/adjuster on demand.
        live = 0
        spawned = 0
        cost = 0.0
        if self.fleet is not None:
            live = sum(
                1 for c in self.fleet._constructs
                if c.state.value not in ("done", "failed", "killed")
            )
            spawned = self.fleet.total_spawned
            cost = getattr(self.fleet, "total_cost_usd", 0.0)
        self.push_screen(
            LimitsScreen(
                max_concurrent=self.max_concurrent,
                max_total_spawns=self.max_total_spawns,
                pool_size=self.pool_size,
                wedge_timeout_seconds=self.wedge_timeout_seconds,
                delay_window_seconds=self.delay_window_seconds,
                # Caliber Phase 3 (scoped down): daemon effort.
                # Daemon model is always opus, not configurable.
                daemon_effort=self.daemon_effort,
                fast_mode=self.fast_mode,
                current_live=live,
                current_spawned=spawned,
                cost_so_far=cost,
            ),
            self._handle_limits_submitted,
        )

    def _handle_limits_submitted(self, result: Optional[dict]) -> None:
        """Callback from LimitsScreen. Applies new caps to live state.

        Caveats:
        - max_concurrent change applies to NEW spawns only. The fleet's
          existing semaphore is set at construction; resizing it
          mid-flight would race with in-flight acquires. Pragmatic v1:
          new value takes effect on next fleet rebuild (i.e., next
          session). Surfaced as a fleet-log note so the user knows.
        - max_total_spawns applies live to the active daemon session
          since daemon_session checks it on every spawn action.
        - If raising max_total_spawns above the prior cap-hit value
          and the daemon has already halted, this does NOT auto-resume
          the session — that's deferred until the daemon grows a
          paused state. User would need to set a new goal.
        - pool_size raised mid-flight tops up the pool to the new
          target on the spot (start() is idempotent — needed = target
          - already_warm). Lowering doesn't actively shrink — existing
          warm sessions stay until consumed; topping up just stops.
          Acceptable: shrink-on-demand isn't worth the complexity."""
        if result is None:
            return

        new_mc = result.get("max_concurrent", self.max_concurrent)
        new_mts = result.get("max_total_spawns", self.max_total_spawns)
        new_ps = result.get("pool_size", self.pool_size)
        new_wt = result.get(
            "wedge_timeout_seconds", self.wedge_timeout_seconds
        )
        new_dw = result.get(
            "delay_window_seconds", self.delay_window_seconds
        )

        changes: list[str] = []
        if new_mc != self.max_concurrent:
            changes.append(
                f"max_concurrent: {self.max_concurrent} → {new_mc} "
                f"(applies to next session)"
            )
            self.max_concurrent = new_mc
        if new_mts != self.max_total_spawns:
            changes.append(
                f"max_total_spawns: {self.max_total_spawns} → {new_mts}"
            )
            self.max_total_spawns = new_mts
            # Apply live to the current daemon session if one is
            # running. daemon_session reads this on every spawn action.
            if self.session is not None:
                try:
                    self.session.max_total_spawns = new_mts
                    # Clear the cap_hit latch if we just raised the
                    # cap above current spawn count — without this,
                    # daemon_session stays in halted state even though
                    # there's now headroom.
                    if (
                        getattr(self.session, "_cap_hit", False)
                        and (new_mts == 0 or new_mts > self.session._total_spawns)
                    ):
                        self.session._cap_hit = False
                        changes.append(
                            "[yellow]cap_hit cleared; daemon may need "
                            "a new goal to resume work[/yellow]"
                        )
                except Exception:
                    pass
        if new_ps != self.pool_size:
            changes.append(
                f"pool_size: {self.pool_size} → {new_ps}"
            )
            self.pool_size = new_ps
            # Live pool exists only when use_pool is enabled and the
            # session has been started. Update target_size + nudge
            # start() to top up to the new target. start() is
            # idempotent — it computes needed = target - already_warm
            # and only spawns the difference. Lowering target leaves
            # existing warm sessions intact (they'll be pulled
            # naturally; the pool just stops topping up).
            if self.session_pool is not None:
                try:
                    self.session_pool.target_size = new_ps
                    # Schedule a top-up. Fire-and-forget — the pool
                    # warms in the background and emits PoolEvents
                    # the sidebar already listens for.
                    if new_ps > 0:
                        self.run_worker(
                            self.session_pool.start(),
                            exclusive=False,
                        )
                except Exception:
                    pass
        if new_wt != self.wedge_timeout_seconds:
            changes.append(
                f"wedge_timeout: {self.wedge_timeout_seconds:g}s → "
                f"{new_wt:g}s"
            )
            self.wedge_timeout_seconds = new_wt
            # Apply live to the running fleet — fleet's _consume reads
            # the attribute fresh on every finalize, so the new ceiling
            # takes effect for the next construct that exits. No fleet
            # rebuild needed (max_concurrent's "next session only"
            # caveat doesn't apply here because no semaphore is bound
            # to this value).
            if self.fleet is not None:
                try:
                    self.fleet.wedge_timeout_seconds = new_wt
                except Exception:
                    pass
        if new_dw != self.delay_window_seconds:
            changes.append(
                f"delay_window: {self.delay_window_seconds:g}s → "
                f"{new_dw:g}s [dim](next spawn)[/dim]"
            )
            self.delay_window_seconds = new_dw
            # Mirror to fleet so the NEXT spawn picks up the new value
            # via make_spawn_settings. Existing in-flight spawns keep
            # what they were spawned with — same propagation model as
            # brake state itself (Claude Code can't have its --settings
            # mutated post-spawn, so the value is baked in).
            if self.fleet is not None:
                try:
                    self.fleet.delay_window_seconds = new_dw
                except Exception:
                    pass

        # Caliber Phase 3 (scoped down, 2026-05-04): daemon effort.
        # Applies on next goal start — the streaming daemon
        # subprocess bakes its caliber at spawn time. Mid-flight
        # daemon restart was tried + reverted as overengineered;
        # the next-goal apply is consistent with how max_concurrent
        # / pool_size also work at this layer.
        new_de = result.get("daemon_effort", self.daemon_effort)
        if new_de != self.daemon_effort:
            changes.append(
                f"daemon effort: {self.daemon_effort} → {new_de} "
                f"(applies on next goal start)"
            )
            self.daemon_effort = new_de
            if self.daemon_caliber is not None:
                try:
                    from caliber import Caliber as _C
                    self.daemon_caliber = _C(
                        model="opus", effort=new_de,
                    )
                except Exception:
                    pass

        # Fast-mode toggle (post-2026-05-07: F-key in LimitsScreen).
        # Pre-fix this was --fast-mode CLI only. Applies on next
        # spawn for constructs (settings file is per-spawn under
        # fast_mode=True for cache reasons); daemon model/effort
        # bake at next goal start same as daemon_effort.
        new_fm = result.get("fast_mode", self.fast_mode)
        if new_fm != self.fast_mode:
            changes.append(
                f"fast_mode: {'on' if self.fast_mode else 'off'} → "
                f"{'on' if new_fm else 'off'} (applies to next spawn)"
            )
            self.fast_mode = new_fm

        if not changes:
            return

        # Phase 1.5: persist the runtime tunables so a deck restart
        # doesn't lose the netrunner's just-submitted values.
        # delay_window_seconds + wedge_timeout_seconds + daemon_effort
        # + fast_mode are persisted; max_concurrent / max_total_spawns /
        # pool_size are session-scoped (the netrunner sets caps for
        # THIS goal; the next session might want different caps).
        # save_limits is best-effort; failures don't block the
        # in-session apply.
        try:
            # Build-plan item 0b: route through Preferences. Same
            # underlying state.json file; same read-merge-write
            # semantics; cleaner import surface.
            self.prefs.save(
                delay_window_seconds=self.delay_window_seconds,
                wedge_timeout_seconds=self.wedge_timeout_seconds,
                daemon_effort=self.daemon_effort,
                fast_mode=self.fast_mode,
            )
        except Exception:
            pass

        # Surface changes in the fleet log so the netrunner has a
        # record of when caps moved and what they moved to.
        try:
            log = self.query_one("#fleet_log", RichLog)
            for c in changes:
                log.write(f"[dim]limits: {c}[/dim]")
        except Exception:
            pass

        # Sidebar reflects max_concurrent + max_total_spawns; refresh.
        self._refresh_sidebar_info()

    # ---- brake ---------------------------------------------------------

    def action_open_brake(self) -> None:
        """Open the brake-state modal. Submitted state (if non-None
        and different from current) triggers _handle_brake_submitted
        which mutates the store + broadcasts to listeners.

        Modal short-circuits the YOLO selection through a
        deliberate-consent confirmation (YoloConfirmScreen) — caller
        only sees BrakeState.YOLO if the netrunner held through that
        gesture."""
        self.push_screen(
            BrakeScreen(current=self.brake_state_store.state),
            self._handle_brake_submitted,
        )

    def _handle_brake_submitted(self, result: Optional[BrakeState]) -> None:
        """Callback from BrakeScreen.

        result is None when the netrunner cancelled (Esc, or backed out
        of the YOLO confirmation). Anything else is a confirmed state
        choice — pass it through to the store, which fires the change
        event and runs the listeners (sidebar update, chatlog line)."""
        if result is None:
            return
        # Store handles the no-op-same-state case internally — calling
        # set() with the current value just returns without firing.
        self.brake_state_store.set(result, reason="netrunner")

    # ---- help overlay --------------------------------------------------

    def action_show_keybinds(self) -> None:
        self.push_screen(KeybindsScreen())

    # ---- toast helper --------------------------------------------------

    def _toast(self, msg: str) -> None:
        """Show a transient message in the fleet log so stub bindings
        give feedback rather than failing silently."""
        try:
            self.query_one("#fleet_log", RichLog).write(f"[dim]{msg}[/dim]")
        except Exception:
            pass

    def _handle_new_task(self, task: Optional[str]) -> None:
        if not task or self.fleet is None:
            return
        fleet_log = self.query_one("#fleet_log", RichLog)
        fleet_log.write(f"[bright_blue]⟳[/bright_blue] new: {task[:24]}{'...' if len(task) > 24 else ''}")
        # Apply the active default profile to this manually-issued
        # spawn. Until the daemon picks profiles per-spawn (C1e),
        # all spawns share whichever profile the netrunner set on
        # launch via --default-profile.
        # origin="netrunner" so the chatlog spawn line gets a [you]
        # badge — distinguishes human-initiated from daemon-initiated
        # at a glance, both for the netrunner and for the watchdog.
        self.run_worker(
            self.fleet.spawn(
                task,
                profile=self.default_profile,
                origin="netrunner",
                # P4 retool: per-spawn addendum (profile.tools +
                # all available plugins). Netrunner-direct spawn,
                # so plugins=None (surface all).
                per_spawn_addendum=self._build_per_spawn_addendum(
                    self.default_profile, None,
                ),
                # Caliber Phase 1: netrunner-direct spawn (basic n-key
                # path) — use deck default. Same rationale as the
                # file-launcher path.
                caliber=self.default_caliber,
            ),
            name="spawn",
        )


if __name__ == "__main__":
    # Phase 7b of the unified-event-stream slice — silent SIGINT
    # swallow so Ctrl+C-as-copy in Windows Terminal stops killing
    # the deck mid-session. The netrunner uses Ctrl+C as the copy
    # shortcut; without this handler, when there's no selection, WT
    # forwards the signal to the Python process and Textual unwinds
    # mid-work. With the handler installed before any deck code runs,
    # the deck never sees Ctrl+C at all — WT either copies (if a
    # selection exists) or the signal is silently dropped. Ctrl+F
    # held remains the only way to halt the deck immediately; Ctrl+Q
    # is the polite quit (with the running-state guard from
    # action_quit). See cyberdeck-philosophy.md design principle 6
    # — "the escape hatch is always one key away" — that key is
    # Ctrl+F, not Ctrl+C.
    import signal as _signal
    try:
        _signal.signal(_signal.SIGINT, lambda *_a: None)
    except (ValueError, OSError):
        # signal.signal() can fail in non-main-thread contexts and
        # under some embedded runtimes. The deck is always launched
        # as the main process so this should never fail in practice;
        # the guard is belt-and-suspenders so a bad Python
        # configuration doesn't take down the deck.
        pass

    # Suppress the asyncio ProactorEventLoop noise on shutdown.
    # On Windows, when subprocess transports get garbage-collected
    # after the loop has closed, BaseSubprocessTransport.__del__
    # tries to format `f'fd={self._sock.fileno()}'` for its repr —
    # but the socket is already closed, raising ValueError. Python
    # prints these as "Exception ignored while calling deallocator"
    # tracebacks. Each leaked transport produces ~12 lines of noise
    # that scrolls the terminal after the user presses Ctrl+C, hiding
    # any real output above.
    #
    # Filter ONLY the specific known-harmless pattern: ValueError on
    # closed pipe inside an asyncio __del__ deallocator. Anything
    # else falls through to the default handler so real bugs still
    # surface.
    _orig_unraisable = sys.unraisablehook

    def _filter_unraisable(unraisable):
        exc = unraisable.exc_value
        if isinstance(exc, ValueError):
            msg = str(exc)
            if "closed pipe" in msg or "closed file" in msg:
                # Asyncio proactor cleanup on Windows. Drop silently.
                return
        _orig_unraisable(unraisable)

    sys.unraisablehook = _filter_unraisable

    parser = argparse.ArgumentParser(
        description="Cyberdeck TUI — orchestrate Claude Code constructs",
    )
    parser.add_argument(
        "tasks",
        nargs="*",
        help="Initial construct tasks (optional). Runs without a daemon unless --goal is given.",
    )
    parser.add_argument(
        "--goal",
        type=str,
        default=None,
        help="High-level goal for the daemon. When set, starts daemon-driven mode.",
    )
    parser.add_argument(
        "--daemon-bin",
        type=str,
        default=None,
        help="Binary to use for the daemon session. Defaults to --claude-bin.",
    )
    parser.add_argument(
        "--claude-bin",
        type=str,
        default=os.environ.get("CLAUDE_BIN", "claude"),
        help="Binary to use for constructs (default: $CLAUDE_BIN or 'claude').",
    )
    parser.add_argument(
        "--mode",
        type=str,
        default=os.environ.get("CLAUDE_MODE", "bypassPermissions"),
        help="Permission mode passed to constructs (default: "
             "bypassPermissions). Headless `-p` mode can't show "
             "interactive permission prompts, so stricter modes like "
             "'acceptEdits' or 'default' silently fail tool calls "
             "that would normally prompt (WebFetch, certain Bash, etc). "
             "Brake profiles (Phase C2) will eventually replace this "
             "with deck-side consent UI.",
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=os.environ.get("CYBERDECK_LOG_DIR"),
        help="Directory for per-launch NDJSON log files. Each launch "
             "creates a new file `cyberdeck-YYYY-MM-DD-HHMMSS.log` "
             "with a `latest.log` pointer alongside (Phase 7 of the "
             "unified-event-stream slice — replaces the prior "
             "single-file CYBERDECK_LOG). Default: "
             "`<deck source>/logs/`. Logs live next to the .py "
             "files (operational artifacts, not deck-content) so the "
             "brake hook protects them from constructs by default.",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default=os.environ.get("CYBERDECK_LOG_LEVEL", "info"),
        help="Minimum severity for log file writes: debug / info / "
             "warning / error / critical. CRITICAL events are always "
             "written regardless of threshold. Default: info.",
    )
    parser.add_argument(
        "--home",
        type=str,
        default=None,
        help="Working directory for constructs, daemon, and the session "
             "manifest. The deck soft-sandboxes its tool calls into this "
             "dir so they don't operate on the deck's own source. "
             "Default: $CYBERDECK_HOME or ./cyberdeck-home (created if "
             "missing).",
    )
    parser.add_argument(
        "--profiles-dir",
        type=str,
        default=None,
        help="Directory of profile .toml files. Hot-reloaded — drop, "
             "edit, or delete files at runtime and the deck picks it up. "
             "Default: <home>/profiles (created if missing).",
    )
    parser.add_argument(
        "--default-profile",
        type=str,
        default=None,
        help="Name of the profile to apply to every spawned construct "
             "this run. Stand-in for the daemon picking profiles per-"
             "spawn (lands in C1e). Pool reuse only happens for the "
             "'default' profile — other profiles spawn fresh subprocesses "
             "so their system-prompt addendum lands cleanly. Unknown "
             "names log a warning and fall back to no-profile behavior.",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=10,
        help="Maximum simultaneously-running constructs (default: 10). "
             "Spawns beyond this wait in a queue until slots free up. "
             "Adjustable mid-session via the `l` Limits modal; the "
             "previous 9-construct ceiling (legacy of the number-key "
             "construct-jump bindings) was retired 2026-04-30.",
    )
    parser.add_argument(
        "--max-spawns",
        type=int,
        default=30,
        help="Cap on total constructs per daemon session (default: 30). "
             "Hitting this halts the session to prevent runaway token use. "
             "Pass 0 for no cap. Only meaningful with --goal.",
    )
    parser.add_argument(
        "--streaming",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use streaming JSON input mode for the daemon (default: ON). "
             "Keeps one claude subprocess alive across all turns instead of "
             "spawning fresh per turn — nuclear-grade speedup observed in "
             "real use. Pass --no-streaming for the legacy one-shot-per-turn "
             "path if streaming misbehaves on a particular claude version.",
    )
    parser.add_argument(
        "--fast-mode",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Cost governor for fast mode (default: OFF, persists across "
             "restarts). Fast mode runs Opus 4.6 at up to 2.5x output speed "
             "for 6x cost; netrunner-only switch — daemon never picks fast "
             "autonomously. When ON, spawns whose model resolves to Opus "
             "4.6 use fast inference; other models silently run standard. "
             "Beta / requires Anthropic waitlist access. Default value is "
             "loaded from state.json's limits namespace; this flag overrides "
             "for the current session only (passing --fast-mode also "
             "persists the new value to state.json).",
    )
    parser.add_argument(
        "--daemon-effort",
        type=str,
        default=None,
        help="Power level for the daemon's subprocess (default: high). "
             "The daemon is always Opus — model is pinned because the "
             "daemon is a manager doing decomposition + dispatch, not a "
             "swappable agent. Effort is the netrunner's power-level "
             "knob: low / medium / high / xhigh / max. Also editable "
             "live via the Limits modal (`l`); persisted to state.json. "
             "Applies on next goal start.",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="Run the prerequisite check and exit. Reports Python "
             "version, textual + mss library presence, claude CLI on "
             "PATH, and `claude --version` health. Use to diagnose a "
             "fresh install or after an upgrade. The check also runs "
             "automatically on first launch + on detected failure; "
             "this flag re-runs on demand.",
    )
    parser.add_argument(
        "--no-doctor",
        action="store_true",
        help="Skip the prerequisite check entirely. Escape hatch for "
             "environments where the probe itself is broken (e.g., "
             "claude binary present but rejects --version on stdin "
             "in a way the doctor misreads). Most netrunners should "
             "leave this off.",
    )
    parser.add_argument(
        "--no-pool",
        action="store_true",
        help="Disable the session pool. Without the pool, every construct "
             "spawns fresh (no --resume warmth). Useful for token-cheap "
             "mock testing and metered connections where pre-warming is "
             "wasteful.",
    )
    parser.add_argument(
        "--pool-size",
        type=int,
        default=5,
        help="Target number of pre-warmed sessions in the pool (default: 5). "
             "Higher = snappier spawns, more startup token use. Adjustable "
             "mid-session via the `l` Limits modal. Ignored when "
             "--no-pool is set.",
    )
    args = parser.parse_args()

    # M5.2: no startup requirement. App launches into idle if neither
    # --goal nor positional tasks are provided; netrunner sets a goal
    # via 'e' once running.

    # Resolve the home dir first — _resolve_home_dir creates the dir
    # if missing so subsequent writes can assume it's there.
    home_dir = _resolve_home_dir(args.home)

    # Build-plan item 0a: first-run prerequisite check. Runs BEFORE
    # the TUI mounts so a missing Python/textual/claude binary
    # produces a clean error message + exit instead of an opaque
    # crash deep in async setup.
    #
    # Display rules:
    #   --doctor flag: always show, then exit (don't launch TUI)
    #   --no-doctor flag: skip entirely
    #   first run (no sentinel): always show
    #   subsequent runs: show ONLY on FAIL (silent on PASS/WARN)
    #
    # On FAIL: print report, exit(1) before TUI construction.
    # On PASS: write the sentinel so subsequent runs stay quiet.
    # WARN doesn't block — the netrunner sees it once, then the
    # next launch is silent (mss missing → screenshot plugin
    # unavailable, but the deck still runs).
    if not args.no_doctor:
        # Orchestration lives in doctor.run_doctor_or_exit — see
        # there for the full flow (run checks, print report on
        # first-run / FAIL / --doctor, exit on FAIL, prompt-to-
        # install on plugin-dep WARN, mark first-run done on PASS).
        # Either returns cleanly (deck launches) or calls sys.exit.
        from doctor import run_doctor_or_exit
        run_doctor_or_exit(
            claude_bin=args.claude_bin,
            home_dir=home_dir,
            doctor_flag=args.doctor,
        )

    # Phase 7: log directory defaults to <deck source>/logs/ inside
    # CyberdeckApp.__init__ when log_dir is None. Explicit
    # --log-dir / CYBERDECK_LOG_DIR overrides for testing or
    # external mount points.
    log_dir = Path(args.log_dir).expanduser() if args.log_dir else None

    app = CyberdeckApp(
        tasks=args.tasks,
        claude_bin=args.claude_bin,
        permission_mode=args.mode,
        log_dir=log_dir,
        log_level=args.log_level,
        goal=args.goal,
        daemon_bin=args.daemon_bin,
        max_concurrent=args.max_concurrent,
        max_total_spawns=args.max_spawns,
        streaming_mode=args.streaming,
        use_pool=not args.no_pool,
        pool_size=args.pool_size,
        fast_mode=args.fast_mode,
        fast_mode_explicit=(args.fast_mode is not None),
        daemon_effort=args.daemon_effort,
        home_dir=home_dir,
        profiles_dir=(
            Path(args.profiles_dir).expanduser()
            if args.profiles_dir is not None
            else None
        ),
        default_profile_name=args.default_profile,
    )
    app.run()
