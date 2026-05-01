"""
Fleet: run multiple constructs concurrently and multiplex their event
streams into a unified log.

Milestone One. Still no TUI, no daemon, no watchdog — just proving that
N constructs can run side by side cleanly and that we can observe them
in a single stream without the plumbing getting garbled.

Architecture notes:

- Single-process for now. Each construct is still its own OS subprocess
  (inherited from M0), but the orchestration lives in one Python process
  using asyncio primitives. ZeroMQ gets introduced later when we split
  the daemon and watchdog into their own processes.

- Per-construct consumer tasks push events into a shared asyncio.Queue.
  A single drain loop reads from the queue, writes to console, and
  appends to the NDJSON log. This is the same shape the Textual UI will
  use in M2 (the TUI becomes the queue consumer).

- Log format: NDJSON, one line per entry. Entries are fleet-level
  envelopes: {run_id, ts, construct_id, kind, payload}. `kind` is
  'event' for Construct events or 'meta' for fleet-level annotations
  (spawn, finalize). This is the foundation for the spec's "annotated
  append-only run logs."
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Profile is referenced by signature only on Fleet.spawn. Avoid a
    # runtime cycle since profiles.py is upstream of the rest of the
    # deck and imports cleanly without us.
    from profiles import Profile
    from event_bus import EventBus

from construct import Construct
from display import summarize
from session_manager import SessionManager, SessionPool
from brake_state import (
    BrakeState,
    make_spawn_settings,
    cleanup_spawn_settings,
)
from connection_monitor import ConnectionState


@dataclass
class FleetEvent:
    """One item in the multiplexed stream.

    kind='event'  → payload is {'event_kind': str, 'raw': dict}
    kind='meta'   → payload is {'type': 'spawned'|'finalized'|..., ...}
    """
    timestamp: float
    kind: str
    construct_id: str
    payload: dict


# Mapping from FleetEvent meta payload "type" → DeckEvent kind. Used
# by the Fleet → bus translator (Phase 2 of the unified-event-stream
# slice). Centralized here so the kind taxonomy stays consistent —
# anything new emitted via FleetEvent meta should also land in this
# table so subscribers can filter on dotted-namespace strings without
# having to inspect payloads.
_META_TYPE_TO_KIND = {
    "run_start":      "fleet.run_start",
    "run_end":        "fleet.run_end",
    "spawned":        "fleet.spawn",
    "finalized":      "fleet.finalize",
    "spawn_blocked":  "fleet.spawn_blocked",
    "spawn_failed":   "fleet.spawn_failed",
    # Slice-2-followup: emitted BEFORE c.kill() at every kill site
    # so observers see WHO requested the kill regardless of how the
    # subprocess actually terminates. Closes the observability gap
    # surfaced 2026-04-30 late where ~36s wedge-timeout kills had
    # zero upstream cause in the log file.
    "kill_requested": "fleet.kill_requested",
}


def _fleet_event_to_deck_event(fevent: FleetEvent):
    """Translate a FleetEvent into a DeckEvent for bus publish.

    Returns None when the FleetEvent shouldn't surface on the bus
    (today: nothing is filtered out, but the contract leaves room
    for fleet-internal events that shouldn't propagate).

    Imported lazily so fleet.py stays importable when event_bus.py
    isn't on the path (mock-test scenarios, partial deployments).
    Practically there's always a bus when there's a deck, but
    fleet.py runs standalone too (the `python fleet.py "task A"`
    console entry).
    """
    from event_bus import DeckEvent
    if fevent.kind == "meta":
        ptype = fevent.payload.get("type", "unknown")
        kind = _META_TYPE_TO_KIND.get(ptype, f"fleet.meta.{ptype}")
    elif fevent.kind == "event":
        # Per-construct streaming event. Phase 2 collapses these all
        # into one bus kind; subscribers drill into the payload's
        # `event_kind` field for sub-classification (system_init,
        # tool_use, tool_result, thinking, assistant, result, etc.).
        # Finer kind breakdown (e.g., fleet.event.tool_use) can land
        # later if a subscriber actually needs to filter at that
        # granularity. Until then this keeps the taxonomy small.
        kind = "fleet.event"
    else:
        # Unknown FleetEvent.kind — pass through as-is. The bus is
        # content-agnostic; subscribers either match it or don't.
        kind = f"fleet.{fevent.kind}"

    return DeckEvent(
        kind=kind,
        source="fleet",
        timestamp=fevent.timestamp,
        construct_id=(
            fevent.construct_id
            if fevent.construct_id != "fleet"  # the run_start/run_end synthetic id
            else None
        ),
        # Severity stays INFO across all fleet events for now. Future
        # phases may escalate spawn_blocked / spawn_failed to WARNING,
        # finalize-with-failed-state to WARNING, etc. — leave at INFO
        # until a subscriber actually needs the gradient.
        payload=fevent,
    )


def _terminal_manifest_state(construct_state: str) -> str:
    """Map a Construct's terminal state name to the SessionManager
    state vocabulary. Construct states are: STARTING, RUNNING, DONE,
    FAILED, KILLED. We only call this on terminal states; non-terminal
    fall-through to 'failed' as a safe default."""
    mapping = {
        "done":   "done",
        "failed": "failed",
        "killed": "killed",
    }
    return mapping.get(construct_state.lower(), "failed")


class Fleet:
    def __init__(
        self,
        claude_bin: str = "claude",
        permission_mode: str = "bypassPermissions",
        log_path: Optional[Path] = None,
        quiet: bool = False,
        install_signal_handlers: bool = True,
        max_concurrent: int = 5,
        session_manager: Optional[SessionManager] = None,
        session_pool: Optional["SessionPool"] = None,
        cwd: Optional[str] = None,
        deck_addendum: Optional[str] = None,
        brake_state_provider: Optional[Callable[[], "BrakeState"]] = None,
        home_dir: Optional[Path] = None,
        connection_state_provider: Optional[
            Callable[[], "ConnectionState"]
        ] = None,
        bus: Optional["EventBus"] = None,
        wedge_timeout_seconds: float = 30.0,
    ):
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.claude_bin = claude_bin
        self.permission_mode = permission_mode
        self.log_path = log_path
        self.quiet = quiet
        self.install_signal_handlers = install_signal_handlers
        self.max_concurrent = max_concurrent
        # Working directory for every construct this fleet spawns. None
        # means "inherit from python's cwd" (legacy behavior). Set to an
        # explicit path to soft-sandbox constructs into a home dir —
        # they'll default-search/write there instead of the deck source.
        self.cwd = cwd
        # Deck-wide system-prompt addendum applied to every construct
        # spawned through this fleet, regardless of profile. Used by
        # the TUI to advertise the cyberdeck dispatcher script
        # (<home>/tools/deck/cyberdeck.py) so constructs know they
        # can call back to the deck UI.
        self.deck_addendum = deck_addendum
        # SessionManager is optional. When provided, every Construct
        # spawned through this Fleet will be recorded in the manifest
        # — session_id captured on system_init, state transitioned on
        # finalization. Pool-aware behavior lands in M5.3b+.
        self.session_manager = session_manager
        # SessionPool is optional. When provided, spawn() pulls a warm
        # session from the pool and uses --resume. When unavailable
        # or empty, falls back to a fresh spawn (current behavior).
        self.session_pool = session_pool

        # Brake-state callable + home dir for per-spawn hook settings.
        # When `brake_state_provider` is wired, every spawn calls it
        # to read the current deck-global brake at spawn time, then
        # generates a transient claude --settings file in
        # <home>/.cyberdeck/spawns/. The construct's lifecycle owns
        # that file: written on spawn, deleted on finalize.
        # When the provider is None (e.g., headless console runs of
        # fleet.py without a TUI), no hook is installed — equivalent
        # to YOLO. home_dir defaults to cwd because pre-brake-refactor
        # callers passed cwd as their soft-sandbox dir; that's the
        # natural place for the .cyberdeck/spawns/ directory too.
        self.brake_state_provider = brake_state_provider
        self.home_dir = (
            Path(home_dir) if home_dir is not None
            else (Path(cwd) if cwd is not None else None)
        )

        # Connection-aware spawn gating. When the deck has wired up a
        # connection-state provider, every spawn checks it first and
        # refuses to start if the connection isn't ONLINE — DEGRADED
        # and OFFLINE both block. In-flight constructs are NOT killed;
        # they continue with whatever connection they have. Only NEW
        # spawns get blocked. None means no gating (e.g., fleet.py
        # console runs without a TUI to host the monitor).
        self.connection_state_provider = connection_state_provider

        # Bus is the only fan-out path. Phase 8 of the unified-event-
        # stream slice retired the legacy `on_event=` kwarg + `_listeners`
        # list + add_listener/remove_listener shims; every consumer now
        # subscribes through bus.subscribe (bus filter `fleet.*`). bus
        # may be None when fleet.py runs standalone (the `python fleet.py`
        # console entry) — _render then publishes nothing and the
        # console-print path (when not quiet) is the only output. See
        # `Design Files/cyberdeck-event-stream-design.md`.
        self.bus = bus

        # Upper bound for the post-stdout-close `c.wait()` in _consume's
        # finally. Normal exits return in <1s; this ceiling exists so a
        # wedged subprocess (Windows orphan, Node child holding stdout
        # open after parent exit, claude wedged on a network call)
        # escalates to force-kill instead of blocking shutdown. Mutable
        # at runtime — the Limits modal writes straight to this attr,
        # so changes take effect for the next finalize without a fleet
        # rebuild. 0 disables the timeout (debug only — a real wedge
        # will hold up shutdown indefinitely). Filed 2026-05-01.
        self.wedge_timeout_seconds = wedge_timeout_seconds

        self._constructs: list[Construct] = []
        self._consumers: list[asyncio.Task] = []
        self._queue: asyncio.Queue[FleetEvent] = asyncio.Queue()
        self._log_fp = None
        self._started_at = time.time()
        self._stop = asyncio.Event()
        self._pending_count = 0
        self._keep_alive = False

        # Concurrency gate. Acquired in spawn() before the subprocess is
        # actually started; released in _consume()'s finally after the
        # subprocess exits. This caps peak in-flight constructs without
        # making fleet.spawn() itself fail — callers just wait their turn.
        self._slot_sema = asyncio.Semaphore(max_concurrent)

        # Observability: running totals for the whole fleet run.
        # - total_cost_usd is summed from the `total_cost_usd` field
        #   on result events. For Max subscribers this is an estimate,
        #   not a bill, but it's still the right signal for "am I
        #   burning through my quota?"
        # - total_tokens_in/out are a more reliable proxy than cost
        #   because they're always reported and never zero for real
        #   work. Cost shows $0.00 in some configurations; tokens
        #   always tell you if the fleet actually did anything.
        self.total_spawned = 0
        self.total_finalized = 0
        self.total_cost_usd = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0

    # ---- external control ----------------------------------------------

    def shutdown(self) -> None:
        """Trigger graceful shutdown from outside run() (e.g. TUI quit)."""
        if not self._stop.is_set():
            if not self.quiet:
                print("\n[fleet] shutdown requested, killing all constructs...")
            self._stop.set()

    # ---- context management ---------------------------------------------

    async def __aenter__(self) -> "Fleet":
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(self.log_path, "a", encoding="utf-8")
            self._write_log({
                "run_id": self.run_id,
                "ts": time.time(),
                "construct_id": "fleet",
                "kind": "meta",
                "payload": {"type": "run_start"},
            })
        return self

    async def __aexit__(self, *exc) -> None:
        if self._log_fp is not None:
            self._write_log({
                "run_id": self.run_id,
                "ts": time.time(),
                "construct_id": "fleet",
                "kind": "meta",
                "payload": {"type": "run_end"},
            })
            self._log_fp.close()
            self._log_fp = None

    # ---- output ---------------------------------------------------------

    def _write_log(self, entry: dict) -> None:
        if self._log_fp is not None:
            self._log_fp.write(json.dumps(entry) + "\n")
            self._log_fp.flush()

    def _render(self, fevent: FleetEvent) -> None:
        """Emit a fleet event to console (unless quiet), NDJSON log, and
        callback (if registered)."""
        rel = fevent.timestamp - self._started_at
        cid = fevent.construct_id

        if not self.quiet:
            if fevent.kind == "meta":
                mtype = fevent.payload.get("type", "?")
                if mtype == "spawned":
                    task = fevent.payload.get("task", "")
                    print(f"[{cid} +{rel:5.2f}s] META   spawned: {task!r}")
                elif mtype == "finalized":
                    state = fevent.payload.get("state")
                    exit_code = fevent.payload.get("exit_code")
                    runtime = fevent.payload.get("runtime")
                    print(f"[{cid} +{rel:5.2f}s] META   finalized: state={state} "
                          f"exit={exit_code} runtime={runtime:.2f}s")
                else:
                    print(f"[{cid} +{rel:5.2f}s] META   {fevent.payload}")
            else:
                event_kind = fevent.payload.get("event_kind", "?")
                raw = fevent.payload.get("raw", {})
                print(f"[{cid} +{rel:5.2f}s] {event_kind:14s} {summarize(raw)}")

        self._write_log({
            "run_id": self.run_id,
            "ts": fevent.timestamp,
            "construct_id": cid,
            "kind": fevent.kind,
            "payload": fevent.payload,
        })

        # Bus publish — the only fan-out path. Translation: build a
        # DeckEvent from the FleetEvent + payload type. See
        # `_fleet_event_to_deck_event` for the kind mapping. The bus
        # itself isolates per-callback exceptions; the producer
        # doesn't need its own try/except loop anymore.
        if self.bus is not None:
            deck_event = _fleet_event_to_deck_event(fevent)
            if deck_event is not None:
                self.bus.publish(deck_event)

    # ---- per-construct consumer ----------------------------------------

    async def _consume(self, c: Construct) -> None:
        """Pump one construct's events into the shared queue, then finalize."""
        # Track whether we've already recorded this session in the
        # manifest. session_id arrives on the first system_init event,
        # which can land at any time during the stream; we want to
        # record exactly once.
        recorded_session = False
        # Per-construct accumulator for brake-hook denials. Populated
        # from the `permission_denials` field on result events; rolled
        # into the finalize meta event payload so listeners (chatlog,
        # watchdog) can see what got blocked. Empty list means "no
        # denials this run" (which is the common case).
        permission_denials: list[dict] = []
        try:
            async for event in c.events():
                # If we've now seen the session_id and haven't recorded
                # it yet, do so. This is idempotent in SessionManager
                # but the local guard avoids redundant disk writes.
                if (not recorded_session
                        and self.session_manager is not None
                        and c.session_id is not None):
                    self.session_manager.record(
                        session_id=c.session_id,
                        kind="construct",
                        state="in_use",
                        task_preview=c.task,
                    )
                    recorded_session = True

                # Opportunistically scrape cost, tokens, and brake-hook
                # permission_denials from result events. All three live
                # on the top-level result event in Claude Code's
                # stream-json. permission_denials is the catalog of
                # what the brake hook blocked this turn — surfaced in
                # the finalize meta event so the chatlog and watchdog
                # see what got caught.
                if (event.kind == "result"
                        or event.raw.get("type") == "result"):
                    cost = event.raw.get("total_cost_usd")
                    if isinstance(cost, (int, float)):
                        self.total_cost_usd += float(cost)
                    pd = event.raw.get("permission_denials")
                    if isinstance(pd, list):
                        permission_denials.extend(
                            d for d in pd if isinstance(d, dict)
                        )
                    usage = event.raw.get("usage") or {}
                    # Sum all input flavors: fresh input + cache reads
                    # + cache creation. For a user watching the sidebar,
                    # this is the "how much did the model see" number.
                    tin = (
                        int(usage.get("input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                    )
                    tout = int(usage.get("output_tokens") or 0)
                    self.total_tokens_in += tin
                    self.total_tokens_out += tout

                await self._queue.put(FleetEvent(
                    timestamp=event.timestamp,
                    kind="event",
                    construct_id=c.id,
                    payload={"event_kind": event.kind, "raw": event.raw},
                ))
        finally:
            try:
                # Post-stdout-close upper bound. Normal exits take
                # <1s. If the subprocess is wedged (Windows orphan,
                # etc.), this escalates to force-kill inside wait()
                # rather than hanging in _consume's finally and
                # blocking Ctrl-C. Sourced from `self.wedge_timeout_
                # seconds` (configurable via the Limits modal); 0 means
                # "no timeout" (debug only — a real wedge will block
                # shutdown). Read fresh each finalize so live edits
                # apply on the next construct that exits.
                _wt = self.wedge_timeout_seconds
                await c.wait(timeout=(_wt if _wt > 0 else None))
                self._pending_count = max(0, self._pending_count - 1)
                self.total_finalized += 1

                # Update SessionManager with terminal state. If
                # session_id never landed (e.g., subprocess died before
                # system_init), there's nothing to record — that's fine.
                # If we never recorded the session because system_init
                # came late, do a last-chance record here so terminal
                # state is at least captured.
                if self.session_manager is not None and c.session_id is not None:
                    if not recorded_session:
                        self.session_manager.record(
                            session_id=c.session_id,
                            kind="construct",
                            state=_terminal_manifest_state(c.state.value),
                            task_preview=c.task,
                        )
                    else:
                        self.session_manager.update(
                            c.session_id,
                            state=_terminal_manifest_state(c.state.value),
                        )

                # Truncate final_output — if a construct produces a
                # massive report, we don't want the full thing wedged
                # into daemon context every outcome turn. ~2KB handles
                # summaries, file lists, short reports. Constructs that
                # produce bigger artifacts should write to disk anyway.
                final = c.final_output or ""
                if len(final) > 2048:
                    final = final[:2045] + "..."
                # files_written sanity pass: Construct populates this
                # from the model's `tool_use` events for the Write
                # tool — i.e. it tracks ATTEMPTED writes, not
                # confirmed ones. When the brake hook denies the
                # call, the file isn't actually created, but the
                # path stayed in the list. Subtract anything in
                # permission_denials (Write/Edit) here so listeners
                # see real artifacts, not hopeful ones.
                #
                # Path comparison is normalized (normcase + normpath)
                # so backslash-vs-forward-slash and case differences
                # between the model's spelling and our captured form
                # don't cause us to miss the match.
                _denied_write_paths: set[str] = set()
                for d in permission_denials:
                    if not isinstance(d, dict):
                        continue
                    if d.get("tool_name") not in ("Write", "Edit"):
                        continue
                    p = (d.get("tool_input") or {}).get("file_path", "")
                    if not p:
                        continue
                    try:
                        _denied_write_paths.add(
                            os.path.normcase(os.path.normpath(p))
                        )
                    except Exception:
                        _denied_write_paths.add(p)

                def _kept(path: str) -> bool:
                    try:
                        norm = os.path.normcase(os.path.normpath(path))
                    except Exception:
                        norm = path
                    return norm not in _denied_write_paths

                actual_files_written = [
                    f for f in c.files_written if _kept(f)
                ]

                await self._queue.put(FleetEvent(
                    timestamp=time.time(),
                    kind="meta",
                    construct_id=c.id,
                    payload={
                        "type": "finalized",
                        "state": c.state.value,
                        "exit_code": c.exit_code,
                        "runtime": c.runtime,
                        "final_output": final,
                        # Filtered list — only files the brake hook
                        # didn't deny. False positives (file actually
                        # was written but somehow appears in denials)
                        # are vanishingly unlikely; false negatives
                        # (denied file appears in files_written) were
                        # the bug we just fixed.
                        "files_written": actual_files_written,
                        # Brake-hook denials. Empty list when nothing
                        # was blocked. Each entry is a dict with
                        # tool_name, tool_use_id, tool_input — the
                        # shape Claude Code's result event provides.
                        # Listeners use this to render chatlog notes
                        # ("blocked: Write -> C:/Windows/...") and to
                        # feed the watchdog's brake-awareness layer.
                        "permission_denials": list(permission_denials),
                        # Slice-2-followup: who/what initiated the
                        # kill, populated from Construct._kill_reason
                        # in Construct.kill(). None for non-killed
                        # finalizes (state=done/failed). For state=
                        # killed without an explicit reason set, this
                        # would be "unspecified" — visible signal that
                        # some kill site needs to be wired up. Real-
                        # deck-confirmed sources: "netrunner_k",
                        # "netrunner_shift_k", "tripwire_critical:
                        # <name>", "inject_interrupt", "eject",
                        # "fleet_shutdown", "fleet_wedge_timeout".
                        "kill_source": (
                            c._kill_reason if c._kill_reason else None
                        ),
                        # Wedge-timeout post-mortem (filed 2026-05-01).
                        # When the kill came from the c.wait() timeout
                        # branch, Construct.wait() drained stderr (with
                        # a 2s ceiling) before signalling. Surface the
                        # tail (~500 chars) here so DeckLogger persists
                        # it on the bus event and the chatlog/Q&A path
                        # can reach it without re-opening the construct.
                        # Only populated when the wedge-timeout path
                        # ran — every other kill source skipped the
                        # drain, and clean exits already have their
                        # stderr captured but it's rarely interesting.
                        # None elsewhere to keep payloads tight.
                        "stderr_excerpt": (
                            (c.stderr or "")[-500:]
                            if c._kill_reason == "fleet_wedge_timeout"
                            else None
                        ),
                    },
                ))
            finally:
                # Always release the slot, even on crash. If we didn't,
                # one buggy construct could starve the fleet permanently.
                self._slot_sema.release()
                # Clean up the per-spawn brake-settings file. Best-
                # effort; cleanup_spawn_settings is None-safe and
                # idempotent. Done here rather than in spawn's
                # finally because we want it to run on the *consumer*
                # side, after the subprocess actually exits — keeping
                # the settings file alive for the construct's full
                # lifetime is the simple semantic.
                cleanup_spawn_settings(
                    Path(c.settings_path) if c.settings_path else None,
                )

    # ---- construct management ------------------------------------------

    async def spawn(
        self,
        task: str,
        resume_session_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        profile: Optional["Profile"] = None,
        origin: str = "daemon",
    ) -> Optional[Construct]:
        """Spawn a single construct and wire its consumer into the fleet.

        If max_concurrent in-flight constructs are already running, this
        call waits (via semaphore) until a slot frees. Release happens
        automatically when the construct finalizes in _consume(), so
        callers don't need to worry about it.

        When a SessionPool is wired up and has warm sessions available,
        the new construct is spawned via --resume to reuse a pre-warmed
        session. Falls back to fresh spawn when pool is empty/disabled.

        `origin` records who initiated the spawn. Values:
          - "daemon"    — daemon's plan dispatched it (default; safest
                          fallback for any caller that hasn't been
                          updated yet).
          - "netrunner" — direct human action (n key, action_new_construct).
          - "inject"    — netrunner injection on a running construct
                          (q/Q queue/interrupt). The new construct
                          continues an existing session, so semantically
                          it's still netrunner-initiated, but the
                          parent_id linkage carries the additional
                          context. Distinguishing inject from a fresh
                          netrunner spawn matters because inject
                          follow-ups have a different mental model
                          (continuation vs. new task).
        Threaded into the 'spawned' meta event so listeners (chatlog,
        watchdog, future telemetry) can attribute spawns at source
        rather than reverse-engineering it from log timing. Solves the
        case where the watchdog had to infer "this was netrunner-
        initiated because no daemon thinking line preceded it" — now
        it's right there in the event.

        Pool reuse is GATED on profile: only spawns under the default
        profile (or no profile) consult the pool. Other profiles
        always spawn fresh — the pool's warm sessions don't carry the
        profile's system-prompt addendum, and a profile-bound spawn
        resuming a default-warmed session would silently get the wrong
        steering. (We considered per-profile pools; concluded the
        warming cost wasn't worth the complexity.)

        When resume_session_id is explicitly provided, the pool is
        bypassed and the spawn resumes that specific session — used by
        injection to continue an existing construct's conversation.
        Profile is also ignored in that case: the resumed session has
        whatever system prompt it was started with.

        When parent_id is provided, it's threaded into the 'spawned' meta
        event so listeners (the TUI in particular) can link the new pane
        to its origin. Used by inject — the new construct is a turn-N+1
        continuation of the parent's session, not an independent task.
        """
        # Connection-aware gate. If the deck's connection isn't ONLINE,
        # remote-model spawns can't reach claude.ai/anthropic — they'd
        # either timeout, fail with a network error, or burn pool
        # warmth on a doomed turn. Refuse cleanly instead. In-flight
        # constructs aren't touched; this only blocks NEW spawns.
        # When no provider is wired (console runs of fleet.py without
        # a TUI hosting ConnectionMonitor), this gate is a no-op.
        if self.connection_state_provider is not None:
            conn_state = self.connection_state_provider()
            if conn_state != ConnectionState.ONLINE:
                # Mint a placeholder id for the meta event; no
                # Construct is created, no semaphore is acquired.
                # The chatlog renders this as `✗ spawn blocked: ...`
                # so the netrunner sees why the spawn didn't happen.
                blocked_id = f"cx-blocked-{uuid.uuid4().hex[:6]}"
                await self._queue.put(FleetEvent(
                    timestamp=time.time(),
                    kind="meta",
                    construct_id=blocked_id,
                    payload={
                        "type": "spawn_blocked",
                        "task": task,
                        "reason": (
                            f"connection state is {conn_state.value}; "
                            f"remote-model spawns are blocked until "
                            f"the connection recovers"
                        ),
                        "connection_state": conn_state.value,
                        "origin": origin,
                    },
                ))
                return None

        # Gate on the concurrency semaphore BEFORE creating the Construct
        # object, so queued spawns don't pile up as half-initialized
        # objects in memory. If spawn_exec fails, we release in the
        # error path below.
        await self._slot_sema.acquire()

        # Resolve the session_id we'll resume (if any). Explicit caller
        # request wins; otherwise try the pool (only when profile is
        # default-or-absent); otherwise fresh spawn.
        resume_id: Optional[str] = resume_session_id
        pool_eligible = (
            profile is None
            or profile.name == "default"
        )
        if (resume_id is None
                and self.session_pool is not None
                and pool_eligible):
            warm_entry = await self.session_pool.pull()
            if warm_entry is not None:
                resume_id = warm_entry.session_id

        # Build the construct first (we need its id for the settings
        # filename), then generate the per-spawn brake settings file
        # and attach. Doing it in this order keeps the construct id
        # canonical (Construct mints one in __init__ if not given).
        c = Construct(
            task=task,
            claude_bin=self.claude_bin,
            permission_mode=self.permission_mode,
            resume_session_id=resume_id,
            cwd=self.cwd,
            profile=profile,
            deck_addendum=self.deck_addendum,
        )

        # Generate the brake-hook --settings file for this spawn, if
        # the deck has wired up a brake-state provider and a home dir
        # to write into. YOLO brake (and missing config) yields None,
        # which means no --settings on the claude command line and
        # therefore no hook installed for this construct.
        if (self.brake_state_provider is not None
                and self.home_dir is not None):
            try:
                settings_path = make_spawn_settings(
                    brake=self.brake_state_provider(),
                    home_dir=self.home_dir,
                    construct_id=c.id,
                )
                if settings_path is not None:
                    c.settings_path = str(settings_path)
            except Exception as exc:
                # Spawn-settings generation must never break a spawn.
                # Surface the issue and run the construct without a
                # hook (effectively YOLO for this one). Caller will
                # see the warning in stderr; the deck stays usable.
                if not self.quiet:
                    print(
                        f"[fleet] brake settings generation failed: "
                        f"{exc!r}",
                        file=sys.stderr,
                    )

        self._constructs.append(c)
        self._pending_count += 1
        self.total_spawned += 1
        try:
            await c.spawn()
        except FileNotFoundError as e:
            self._pending_count -= 1
            self.total_spawned -= 1
            self._constructs.remove(c)
            self._slot_sema.release()  # never got to _consume, release here
            # Settings file will go un-cleaned-up by _consume since
            # we never reached it. Best-effort cleanup here.
            cleanup_spawn_settings(
                Path(c.settings_path) if c.settings_path else None,
            )
            # Surface the error via the event stream so TUIs and logs
            # see it; don't silently drop.
            await self._queue.put(FleetEvent(
                timestamp=time.time(),
                kind="meta",
                construct_id=c.id,
                payload={"type": "spawn_failed", "task": task, "error": str(e)},
            ))
            return None

        await self._queue.put(FleetEvent(
            timestamp=time.time(),
            kind="meta",
            construct_id=c.id,
            payload={
                "type": "spawned",
                "task": task,
                "resumed_from": resume_id,  # None if fresh spawn
                "parent_id": parent_id,  # None unless this is an inject followup
                # Profile name (or None). Lets the TUI show a profile
                # badge on the pane header without reaching back into
                # construct internals. None means no profile applied.
                "profile_name": c.profile_name,
                # Spawn provenance: "daemon" / "netrunner" / "inject".
                # Surfaced in the chatlog so a glance tells the
                # netrunner who dispatched this work, and so the
                # watchdog stops having to reverse-engineer it from
                # log timing.
                "origin": origin,
                # OS pid of the construct's claude subprocess. Read
                # by the Mechanic supervisor (sibling process) from
                # the per-launch NDJSON log file — it builds a live
                # set of "spawned but not finalized" pids and kills
                # them on detected deck death so claude subprocesses
                # don't orphan when the deck dies (Task Manager kill,
                # OOM, blue screen, etc.). Other subprocess sources
                # (daemon, watchdog Q&A, pool warming) aren't tracked
                # by the v0 supervisor — they orphan the same way as
                # before until a follow-up slice publishes their pids
                # too. Falls back to None if the subprocess somehow
                # never came up; the supervisor skips entries with
                # pid=None.
                "pid": c.pid,
            },
        ))
        consumer = asyncio.create_task(self._consume(c))
        self._consumers.append(consumer)
        return c

    async def kill_construct(
        self, construct_id: str, source: str = "unspecified",
    ) -> bool:
        """Kill one construct by ID. Returns True if found and killed.

        `source` identifies who requested the kill — surfaced as a
        `fleet.kill_requested` bus event BEFORE c.kill() is called,
        and stamped on the finalize event's `kill_source` field via
        Construct._kill_reason. Canonical sources are documented on
        Construct.kill(). The bus event lets observers (chatlog,
        watchdog, file logger) see kills in real-time; the finalize
        field gives durable post-hoc attribution. Real-deck filed
        2026-04-30 late: prior to this addition, mystery kills (and
        even legitimate netrunner-k kills) had no observable cause
        in the log file beyond `state=killed` on finalize.
        """
        for c in self._constructs:
            if c.id == construct_id:
                # Emit kill_requested BEFORE c.kill so the bus event
                # is visible regardless of whether the kill itself
                # finishes quickly.
                await self._queue.put(FleetEvent(
                    timestamp=time.time(),
                    kind="meta",
                    construct_id=construct_id,
                    payload={"type": "kill_requested", "source": source},
                ))
                await c.kill(reason=source)
                return True
        return False

    def get_construct(self, construct_id: str) -> Optional[Construct]:
        """Lookup a tracked construct by id. Returns None if not found.

        Used by callers (the TUI hard-kill / blacklist path) that need
        to read the actual Construct object — task text, files_written,
        final_output, state — to capture context at kill time. Cleaner
        than reaching into fleet._constructs directly."""
        for c in self._constructs:
            if c.id == construct_id:
                return c
        return None

    @property
    def constructs(self) -> list[Construct]:
        """Snapshot of currently-tracked constructs. Returns a fresh
        list so callers can iterate without worrying about concurrent
        spawn/finalize churning the underlying list. Used by the TUI's
        in-flight blacklist-match scan."""
        return list(self._constructs)

    # ---- orchestration --------------------------------------------------

    async def run(self, tasks: list[str], keep_alive: bool = False) -> None:
        """Spawn initial tasks and drain events.

        keep_alive=False (default): drain exits when all pending constructs
            finalize. Suits one-shot console runs.
        keep_alive=True: drain runs until shutdown() is called. Suits TUIs
            where the user may spawn more constructs interactively.
        """
        self._keep_alive = keep_alive

        for task in tasks:
            await self.spawn(task)

        # Signal handling: SIGINT → kill all constructs, let consumers finalize.
        # Skipped when install_signal_handlers=False (e.g. under a TUI that
        # owns signals and drives shutdown via fleet.shutdown()).
        if self.install_signal_handlers:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.shutdown)
                except NotImplementedError:
                    pass  # Windows

        async def drain() -> None:
            while True:
                if self._stop.is_set():
                    break
                try:
                    fevent = await asyncio.wait_for(
                        self._queue.get(), timeout=0.1,
                    )
                    self._render(fevent)
                except asyncio.TimeoutError:
                    # Queue idle. If we're in one-shot mode and nothing
                    # is pending, we're done.
                    if not self._keep_alive and self._pending_count == 0:
                        break

        drain_task = asyncio.create_task(drain())
        stop_task = asyncio.create_task(self._stop.wait())

        done, pending = await asyncio.wait(
            {drain_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done and not drain_task.done():
            # Kill everything in parallel. Consumers will emit finalized
            # meta events, drain_task will catch them and exit on the
            # pending-zero check (in non-keep-alive mode) or stop check
            # (in keep-alive mode, which set the stop event to get here).
            #
            # Slice-2-followup: each c.kill() carries a reason that's
            # stamped on the finalize event's kill_source field. We
            # don't emit a separate fleet.kill_requested event here
            # because (a) drain_task is already winding down and may
            # not consume queue puts reliably during shutdown, and (b)
            # the finalize event with kill_source="fleet_shutdown"
            # carries enough signal — the netrunner / observer can
            # see WHY each construct went down post-hoc.
            await asyncio.gather(
                *(c.kill(reason="fleet_shutdown") for c in self._constructs),
                return_exceptions=True,
            )
            await drain_task

        # Clean up any leftover tasks
        for p in pending:
            p.cancel()
        for t in self._consumers:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._consumers, return_exceptions=True)


# ---- entry point ---------------------------------------------------------

async def _main(tasks: list[str], claude_bin: str, permission_mode: str,
                log_path: Optional[Path]) -> int:
    async with Fleet(
        claude_bin=claude_bin,
        permission_mode=permission_mode,
        log_path=log_path,
    ) as fleet:
        print(f"[fleet] run_id={fleet.run_id}  bin={claude_bin}  mode={permission_mode}  "
              f"tasks={len(tasks)}"
              + (f"  log={log_path}" if log_path else ""))
        await fleet.run(tasks)
    return 0


if __name__ == "__main__":
    # Usage:
    #   python fleet.py "task 1" "task 2" [...]
    # Env overrides:
    #   CLAUDE_BIN=./mock_claude.py  python fleet.py "task A" "task B"
    #   CLAUDE_MODE=bypassPermissions python fleet.py "..."
    #   CYBERDECK_LOG=/path/to/log.ndjson python fleet.py "..."
    import os

    tasks = sys.argv[1:]
    if not tasks:
        print('usage: python fleet.py "task 1" "task 2" [...]')
        print("env:   CLAUDE_BIN=./mock_claude.py  (default: claude)")
        print("       CLAUDE_MODE=acceptEdits      (default: acceptEdits)")
        print("       CYBERDECK_LOG=cyberdeck.log  (default: cyberdeck.log)")
        sys.exit(2)

    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    permission_mode = os.environ.get("CLAUDE_MODE", "bypassPermissions")
    log_env = os.environ.get("CYBERDECK_LOG", "cyberdeck.log")
    log_path = Path(log_env) if log_env else None

    sys.exit(asyncio.run(_main(tasks, claude_bin, permission_mode, log_path)))
