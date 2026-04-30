"""
event_bus.py — the deck's spine. One canonical event stream that every
event source publishes to; every observer subscribes via a role-derived
filter.

Phase 1 of the unified-event-stream slice (full design at
`Design Files/cyberdeck-event-stream-design.md`). Ships the primitives
only — `DeckEvent`, `EventBus`, `Subscription`, `Severity`. No event
source publishes yet, no observer subscribes yet, no integration with
the rest of the deck. Phase 2 wires Fleet through the bus; subsequent
phases migrate Daemon, Tripwires, Blacklist, Brake, Connection,
Profiles, Plugins, direct chatlog writes, and the file logger one at
a time.

The architectural payoff this enables:
  - Single source of truth for "what's happening on the deck right now"
  - Role-derived filters: observers declare what they care about; the
    bus enforces it programmatically. Adding a new event kind doesn't
    silently fall through 11 callback chains — either an existing
    filter matches it or it doesn't, and the answer is inspectable.
  - "What does the watchdog see?" becomes literally "check its
    filter against the stream tail." No more tracing through TUI
    middleware to reverse-engineer visibility.
  - Constructs stay hermetic — they're work units, not observers, and
    the bus is simply never passed to them. Spec compliance enforced
    at the visibility layer, not just in prompting.
  - The "15 LLMs shouting over each other in a room while they all go
    insane" failure mode is structurally prevented: everyone yells in
    the same room, the bus decides programmatically who hears what.

Design notes for the curious:
  - Synchronous publish on a single event loop. Matches the deck's
    threading model (Textual single-loop everywhere).
  - Per-callback exception isolation — a misbehaving subscriber
    cannot poison the bus or block other subscribers.
  - Bounded ring-buffer history so retroactive subscribers (file
    logger that opens late, ExpandModal pulling tail) can catch up.
  - Filters accept either a predicate function or an iterable of kind
    patterns (exact strings or fnmatch globs like "fleet.*"). The
    iterable form is the easy path for declaring role-derived
    visibility; the callable form handles composite logic
    (kind + severity + construct_id) when needed.
"""
from __future__ import annotations

import fnmatch
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Union


# Severity tiers. Same shape as tripwires' Severity but covers a wider
# scale because the bus carries non-tripwire events too. CRITICAL gets
# special treatment downstream — file logger duplicates to stderr,
# display surfaces may flash / pull focus regardless of role-filter.
class Severity:
    DEBUG = "debug"
    INFO = "info"          # default — most things
    WARNING = "warning"    # heuristic concern (tripwire fire, brake denial)
    ERROR = "error"        # something failed but the deck keeps running
    CRITICAL = "critical"  # something requires netrunner attention NOW


# Filter shape: either a predicate function or an iterable of kind
# patterns. The iterable form is the easy path for role-derived
# subscriptions ("the watchdog cares about these kinds"); the callable
# form is for composite logic (severity gates, construct_id pinning,
# etc.).
FilterSpec = Union[
    Callable[["DeckEvent"], bool],
    Iterable[str],
    None,
]


@dataclass
class DeckEvent:
    """One event on the spine.

    `kind` is dotted-namespace `<source>.<event_type>` (e.g.
    `fleet.spawn`, `tripwire.fire`, `brake.change`). Filters match
    against this; using a stable namespace lets glob-style filters
    like `"fleet.*"` work.

    `source` identifies the producing subsystem (`"fleet"`,
    `"watchdog"`, `"tui.chatlog"`, etc.). Redundant with `kind`'s
    prefix in practice, but kept separate so the filter never needs
    to parse strings — match on whichever shape the consumer prefers.

    `timestamp` defaults to publish-time (`time.time()`). Producers
    don't need to set it; the bus does it on receipt.

    `construct_id` is the relevant cx-XXXX when applicable. Filters
    can pin to a specific construct (e.g. tripwire engine's
    per-construct scoping) by checking this field.

    `severity` is one of `Severity.*`. Default INFO covers most
    events; producers escalate when warranted.

    `text` is an optional pre-rendered display string for events
    where the producer already composed the markup (today's
    `_chatlog_write` direct-write case). Subscribers that render to
    a UI surface may use this directly; subscribers doing structured
    work read `payload` instead.

    `payload` is the source-specific data — a FleetEvent, a
    TripwireFire, a dict, etc. Producers stash whatever fits;
    subscribers know the shape of the kinds they subscribe to.
    """
    kind: str
    source: str
    timestamp: float = field(default_factory=time.time)
    construct_id: Optional[str] = None
    severity: str = Severity.INFO
    text: Optional[str] = None
    payload: Any = None


def _make_predicate(spec: FilterSpec) -> Callable[[DeckEvent], bool]:
    """Normalize a FilterSpec into a predicate function.

    Three input shapes:
      - None / no filter → accept all
      - callable → use as-is
      - iterable of strings → match if event.kind matches any
        pattern (fnmatch-style: `"fleet.spawn"`, `"fleet.*"`,
        `"tripwire.*"`, `"*"`).

    Iterable form is the convenient one for role-derived
    declarations: a class declares its KINDS_OF_INTEREST as a frozenset
    and passes it to subscribe(). Adding a new wildcard or kind to the
    set automatically opens visibility without touching the bus.
    """
    if spec is None:
        return lambda _e: True
    if callable(spec):
        return spec
    # Iterable of strings — materialize once so we don't iterate the
    # producer every check, and so generators don't get exhausted.
    patterns = tuple(spec)
    if not patterns:
        # Empty iterable: nobody told us we wanted anything; deliver
        # nothing. Symmetric with "no filter = everything."
        return lambda _e: False

    def _match(event: DeckEvent) -> bool:
        kind = event.kind
        for pat in patterns:
            if pat == kind or fnmatch.fnmatchcase(kind, pat):
                return True
        return False

    return _match


@dataclass
class Subscription:
    """A single subscriber's registration on the bus.

    Returned by `EventBus.subscribe(...)`. Callers hold the object
    so they can `unsubscribe()` later (typical: TUI shutdown
    cleanup, or temporary subscriptions like a one-off
    debug-listener).

    Equality / hashing on the auto-generated `sub_id` so subscriptions
    are safe to put in a dict / set keyed by id without surprising
    behavior.
    """
    sub_id: str
    callback: Callable[[DeckEvent], None]
    predicate: Callable[[DeckEvent], bool]
    name: str = ""  # human-readable label for debugging / introspection
    _bus: Optional["EventBus"] = field(default=None, repr=False)

    def unsubscribe(self) -> None:
        """Detach from the bus. Idempotent; safe to call twice or after
        the bus has already torn down."""
        if self._bus is not None:
            self._bus._unsubscribe(self)
            self._bus = None

    def __hash__(self) -> int:  # type: ignore[override]
        return hash(self.sub_id)

    def __eq__(self, other) -> bool:  # type: ignore[override]
        return isinstance(other, Subscription) and self.sub_id == other.sub_id


class EventBus:
    """The deck's spine.

    Single-threaded synchronous publish/subscribe with a bounded
    ring-buffer history. Producers call `publish(event)`; subscribers
    register with a callback and an optional filter via `subscribe()`.

    Per-callback exception isolation: a subscriber raising never
    poisons the bus or affects other subscribers. The exception is
    stashed on the bus's `errors` list (bounded, for introspection)
    and otherwise swallowed.

    The ring buffer holds the last `history_size` events for late
    subscribers and snapshot consumers (file logger opening at deck
    startup, ExpandModal pulling the tail, watchdog Q&A context-
    builder reading recent activity, future maintbot triage). Default
    10000 events — sized to cover several minutes of fleet-burst
    activity without unbounded memory growth.

    Lifecycle: instantiate one per app. The deck instantiates exactly
    one in `CyberdeckApp.__init__` (lands in Phase 2). Constructs
    DO NOT receive a reference — they are work units, not observers,
    and the bus is the visibility surface that enforces the spec's
    role separation.
    """

    DEFAULT_HISTORY_SIZE = 10_000

    def __init__(self, *, history_size: int = DEFAULT_HISTORY_SIZE) -> None:
        self._subscribers: list[Subscription] = []
        # Ring buffer of recent events. `deque(maxlen=N)` evicts the
        # oldest on overflow, no manual bookkeeping needed.
        self._history: deque[DeckEvent] = deque(maxlen=history_size)
        # Per-callback exceptions get stashed here for inspection.
        # Bounded so a misbehaving subscriber that raises every time
        # doesn't grow this list unbounded.
        self.errors: deque[tuple[str, BaseException]] = deque(maxlen=100)

    # ---- producer API -----------------------------------------------

    def publish(self, event: DeckEvent) -> None:
        """Publish one event to the bus.

        Synchronous: every subscriber whose filter matches gets called
        before this returns. Per-subscriber exceptions are caught and
        recorded in `errors` so a single misbehaving listener can't
        block the others or poison the bus.

        Producers don't need to set `event.timestamp`; if it was
        defaulted at construction, it'll already carry the time the
        DeckEvent was created (which is essentially publish-time
        for the synchronous case). If a producer constructs the
        event well before publishing (unusual), the timestamp
        reflects construction, not publish — that's intentional, the
        timestamp is "when did this happen?" not "when did the bus
        see it?"
        """
        # Append to history first so it's there regardless of what
        # subscribers do. (A subscriber that calls bus.snapshot()
        # during its callback should see the event-being-delivered.)
        self._history.append(event)
        # Iterate a snapshot of subscribers so a listener that
        # subscribes / unsubscribes during dispatch can't invalidate
        # iteration. Cheap — list is small (~10s in the fully migrated
        # deck; ~1 today).
        for sub in list(self._subscribers):
            try:
                if sub.predicate(event):
                    sub.callback(event)
            except BaseException as exc:  # noqa: BLE001 — defensive
                # Record + continue. We catch BaseException because a
                # subscriber raising KeyboardInterrupt or SystemExit
                # mid-publish would otherwise propagate out of an
                # event-loop callback in confusing ways. The deck's
                # actual quit path is EJECT, not exception
                # propagation.
                self.errors.append((sub.name or sub.sub_id, exc))

    # ---- consumer API -----------------------------------------------

    def subscribe(
        self,
        callback: Callable[[DeckEvent], None],
        *,
        filter: FilterSpec = None,
        name: str = "",
        replay: bool = False,
    ) -> Subscription:
        """Register a subscriber.

        `callback`: called with each matching DeckEvent.

        `filter`: a predicate function, an iterable of kind patterns
        (e.g. `["fleet.*", "tripwire.fire"]`), or None to accept all.
        See FilterSpec / `_make_predicate`. Iterable form is the
        right choice for role-derived subscriptions (declare the
        kinds your role cares about); callable form handles composite
        logic.

        `name`: human-readable label for introspection / debugging.
        Shows up in the errors list when a subscriber raises.
        Strongly recommended.

        `replay`: when True, the bus immediately delivers the
        currently-buffered events (filtered) to the new subscriber
        before returning. Useful for late subscribers (file logger
        opening at deck startup) that need the history-since-launch.
        Default False — most subscribers want forward-only delivery.

        Returns a Subscription object. Hold onto it; call
        `subscription.unsubscribe()` when done. The TUI lifecycle
        owns subscription cleanup (on_unmount).
        """
        predicate = _make_predicate(filter)
        sub = Subscription(
            sub_id=f"sub-{uuid.uuid4().hex[:8]}",
            callback=callback,
            predicate=predicate,
            name=name,
            _bus=self,
        )
        self._subscribers.append(sub)
        if replay:
            # Replay the buffered history through the same isolated
            # callback path as live publish. Order: chronological
            # (oldest first) — the deque iterates left-to-right.
            for event in list(self._history):
                try:
                    if predicate(event):
                        callback(event)
                except BaseException as exc:  # noqa: BLE001
                    self.errors.append((name or sub.sub_id, exc))
        return sub

    def _unsubscribe(self, sub: Subscription) -> None:
        """Internal: remove a subscription. Idempotent."""
        try:
            self._subscribers.remove(sub)
        except ValueError:
            # Already removed (maybe double-unsubscribe). Idempotent
            # by design — callers shouldn't have to track state.
            pass

    # ---- introspection ----------------------------------------------

    def snapshot(self, n: Optional[int] = None) -> list[DeckEvent]:
        """Return the last N events from the ring buffer.

        `n=None` (default) returns the entire buffered history.
        Useful for ExpandModal-style consumers, the watchdog Q&A
        context-builder, the future maintbot's post-mortem reader.

        Returns a fresh list so callers can iterate without worrying
        about concurrent publish.
        """
        if n is None:
            return list(self._history)
        if n <= 0:
            return []
        return list(self._history)[-n:]

    def subscribers(self) -> list[Subscription]:
        """Snapshot of the current subscriber list. For
        introspection — "what's listening to the bus right now?"
        Returns a fresh list."""
        return list(self._subscribers)

    def __len__(self) -> int:
        """Number of events in the ring buffer right now."""
        return len(self._history)


# -- kind-namespace constants -----------------------------------------
#
# Producers should import these rather than spelling kind strings as
# literals, so renames stay one-touch. Subsequent phases populate this
# as each event source migrates onto the bus. Phase 1 ships an empty
# namespace; Phase 2 adds Fleet kinds; etc.
#
# Pattern: dotted-namespace, lowercase, period-separated. The exact
# kind taxonomy is not part of the bus contract — the bus is content-
# agnostic. Producers and subscribers agree on kind strings via
# this module so wildcard filters like "fleet.*" stay coherent across
# the codebase.

class Kind:
    """Kind-namespace constants. Populated incrementally as event
    sources migrate onto the bus.

    Producers should import these constants rather than spelling kind
    strings as literals, so renames stay one-touch and typos surface
    at import time. Subscribers can mix constant references with
    glob patterns (e.g., `[Kind.FLEET_SPAWN, "fleet.event"]`) — both
    are just strings to the bus.

    Phase 2 adds the FLEET_* kinds. Phase 3 adds DAEMON_*. Subsequent
    phases extend further (tripwire, brake, blacklist, connection,
    profile, plugin, chatlog, lifecycle).
    """
    # Fleet (Phase 2). The translator in fleet.py maps each FleetEvent
    # kind+payload-type onto one of these. Per-construct streaming
    # events all collapse to FLEET_EVENT today; finer breakdowns
    # (tool_use, thinking, tool_result, etc.) can land later if a
    # subscriber actually needs to filter at that granularity. Until
    # then, subscribers drill into the payload for sub-classification.
    FLEET_RUN_START = "fleet.run_start"
    FLEET_RUN_END = "fleet.run_end"
    FLEET_SPAWN = "fleet.spawn"
    FLEET_FINALIZE = "fleet.finalize"
    FLEET_SPAWN_BLOCKED = "fleet.spawn_blocked"
    FLEET_SPAWN_FAILED = "fleet.spawn_failed"
    FLEET_EVENT = "fleet.event"  # per-construct streaming events

    # Daemon (Phase 3). DaemonEvent.kind enumerates: thinking, chat,
    # action, status, error, raw. Each maps to a daemon.<kind> bus
    # event. DaemonSession synthesizes additional `error` events
    # for blacklist-refused spawns, spawn-cap halts, and respawn-loop
    # warnings — those flow through the same channel as daemon-
    # subprocess events; subscribers can't tell synthetic from
    # subprocess-emitted apart from the payload's `text` field.
    DAEMON_THINKING = "daemon.thinking"
    DAEMON_CHAT = "daemon.chat"
    DAEMON_ACTION = "daemon.action"
    DAEMON_STATUS = "daemon.status"
    DAEMON_ERROR = "daemon.error"
    DAEMON_RAW = "daemon.raw"

    # Tripwires (Phase 4). The deterministic-matcher half: every fire
    # the engine dispatches becomes a TRIPWIRE_FIRE event on the bus.
    # Severity on the DeckEvent reflects the tripwire's own severity
    # so subscribers can filter on `(kind=tripwire.fire, severity=critical)`
    # for slice-3-style focus-pulling without inspecting the payload.
    TRIPWIRE_FIRE = "tripwire.fire"
    # The LLM-authoring half (slice 2): authoring lifecycle so
    # subscribers can observe the watchdog's session-shaping.
    TRIPWIRE_AUTHOR_STARTED = "tripwire.author_started"
    TRIPWIRE_AUTHOR_COMPLETED = "tripwire.author_completed"
    TRIPWIRE_AUTHOR_FAILED = "tripwire.author_failed"

    # Blacklist (Phase 4). Today the only event is "added" — entries
    # never get explicitly removed (session-scoped, cleared on
    # watchdog shutdown). If a `blacklist.removed` ever lands, it
    # joins this namespace.
    BLACKLIST_ADDED = "blacklist.added"

    # Brake state (Phase 5). The deck-global brake. Single event today
    # — `brake.change` covers paranoid/default/yolo transitions in
    # either direction. Severity escalates with the destination tier
    # (paranoid=info, default=info, yolo=warning) so subscribers can
    # gate on severity for "alert me when constructs go unrestricted"
    # workflows.
    BRAKE_CHANGE = "brake.change"

    # Connection monitor (Phase 5). Single event for online/degraded/
    # offline transitions; payload carries the StateChangeEvent.
    # Severity reflects the destination state (online=info, others
    # =warning) so subscribers can react to "we lost network" without
    # looking at the payload.
    CONNECTION_TRANSITION = "connection.transition"

    # Profile registry (Phase 5). Each ProfileEvent kind maps to a
    # corresponding bus kind. Subscribers that just want "anything
    # changed in the profile world" use `profile.*` glob; ones that
    # care specifically about a single shape use the constant.
    PROFILE_ADDED = "profile.added"
    PROFILE_CHANGED = "profile.changed"
    PROFILE_REMOVED = "profile.removed"
    PROFILE_SCAN_ERROR = "profile.scan_error"
    PROFILE_SCAN_COMPLETE = "profile.scan_complete"

    # Plugin registry (Phase 5). Same shape as profile kinds —
    # PluginEvent.kind ∈ {loaded, scan_error, scan_complete} maps to
    # plugin.<kind>.
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_SCAN_ERROR = "plugin.scan_error"
    PLUGIN_SCAN_COMPLETE = "plugin.scan_complete"

    # Direct chatlog writes (Phase 6). Pre-rendered lines that the TUI
    # composes itself (tripwire fire chrome, brake transition lines,
    # blacklist additions, goal-update markers, watchdog Q&A
    # markers, authoring announcements). The line is in event.text;
    # subscribers wanting the raw events should subscribe to the
    # source kinds (tripwire.fire, brake.change, blacklist.added,
    # etc.) instead. Phase 6 also retires the standalone
    # `_chatlog_event_buffer` deque on CyberdeckApp — bus is now the
    # single source of truth for "what's been on the chatlog."
    CHATLOG_DIRECT = "chatlog.direct"
