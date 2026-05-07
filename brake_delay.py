"""brake_delay.py — deck-side counterpart to brake_hook.py's delay window.

Slice 3 of the safety architecture pass: variable-outcome delay UX. The
brake hook (separate subprocess, runs per tool call) holds interesting
calls for up to `delay_window_seconds`, watching for an override file.
The deck side has three jobs:

  1. **Notice** when a delay window opens. The hook writes
     `<home>/.cyberdeck/spawns/<cid>.delay_pending.json`; we poll the
     directory at low cadence and emit `brake.delay_opened` bus events
     so the DelayPanel + chatlog can render.

  2. **Notice** when a delay window resolves. The hook deletes the
     pending file before exiting (override applied or default expired);
     we see the disappearance and emit `brake.delay_resolved`.

  3. **Override**. When the netrunner presses X on a focused delay
     entry, the deck calls `write_delay_override(...)` which writes
     `<home>/.cyberdeck/spawns/<cid>.delay_override.json`. The hook's
     polling loop picks it up, applies the flipped action, deletes
     both files.

The polling cadence (50ms) is well below human reaction time and well
above asyncio's scheduling granularity. Filesystem activity is small —
one short JSON file per pending delay, gone within seconds. No fsnotify
/ watchdog dependency: stdlib + asyncio.

Events flow:
  - hook writes <cid>.delay_pending.json
  - DelayMonitor sees it on next poll → publishes brake.delay_opened
  - DelayPanel renders entry; netrunner sees countdown
  - netrunner presses X → action_x_focused calls write_delay_override
  - hook polls (every 100ms), sees override file, applies action, deletes both files
  - DelayMonitor sees the pending file gone → publishes brake.delay_resolved
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------- shape


@dataclass
class DelayEntry:
    """One pending delay window. Mirrors the JSON shape brake_hook.py
    writes; see `run_delay_window` in that file for the producer side.

    Tripwire fields (post-2026-05-07 redesign): set when the delay was
    opened in response to a tripwire fire (engine wrote deny_pending
    .json, hook routed through delay window). Empty for brake-only
    delays. The deck-side overlay reads these to label "tripwire
    blocked" vs "brake blocked"; the kill-on-critical subscriber to
    brake.delay_resolved reads severity to decide whether to terminate
    the construct on a deny outcome.
    """
    construct_id: str
    tool_name: str
    tool_input_summary: str
    brake: str
    default_action: str       # "allow" | "deny"
    default_reason: str        # human-readable; empty when default_action=allow
    override_action: str       # "allow" | "deny" — always "allow" post-2026-05-07
    opened_at: float
    deadline_ts: float
    delay_window_seconds: float
    # Optional tripwire context — empty strings (bool False) when the
    # delay is purely brake-driven (no tripwire fired).
    tripwire_name: str = ""
    tripwire_severity: str = ""        # "warning" | "critical" | ""
    tripwire_description: str = ""
    tripwire_suggestion: str = ""
    tripwire_excerpt: str = ""
    tripwire_bad_enough: bool = False  # critical+bad_enough → blacklist proposal on deny

    @classmethod
    def from_dict(cls, d: dict) -> Optional["DelayEntry"]:
        """Parse a delay_pending.json blob. Returns None on missing
        required fields rather than raising — a malformed file from a
        future hook version shouldn't crash the monitor loop."""
        try:
            return cls(
                construct_id=str(d["construct_id"]),
                tool_name=str(d.get("tool_name", "")),
                tool_input_summary=str(d.get("tool_input_summary", "")),
                brake=str(d.get("brake", "default")),
                default_action=str(d.get("default_action", "deny")),
                default_reason=str(d.get("default_reason", "")),
                override_action=str(d.get("override_action", "allow")),
                opened_at=float(d.get("opened_at", time.time())),
                deadline_ts=float(d.get("deadline_ts", time.time())),
                delay_window_seconds=float(
                    d.get("delay_window_seconds", 0.0)
                ),
                tripwire_name=str(d.get("tripwire_name", "")),
                tripwire_severity=str(d.get("tripwire_severity", "")),
                tripwire_description=str(d.get("tripwire_description", "")),
                tripwire_suggestion=str(d.get("tripwire_suggestion", "")),
                tripwire_excerpt=str(d.get("tripwire_excerpt", "")),
                tripwire_bad_enough=bool(d.get("tripwire_bad_enough", False)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @property
    def is_tripwire_driven(self) -> bool:
        """True if a tripwire fire is what opened this delay window
        (vs. a brake-level deny). Convenience for the chatlog/overlay
        renderers."""
        return bool(self.tripwire_name) or bool(self.tripwire_severity)

    @property
    def remaining_seconds(self) -> float:
        """How long the netrunner has left to press X. Clamps at 0 so
        UI countdown bars don't display negative time when the hook is
        still cleaning up."""
        return max(0.0, self.deadline_ts - time.time())

    @property
    def progress(self) -> float:
        """0.0 = just opened, 1.0 = expired. Drives the panel's
        countdown bar fill."""
        if self.delay_window_seconds <= 0:
            return 1.0
        elapsed = time.time() - self.opened_at
        return max(0.0, min(1.0, elapsed / self.delay_window_seconds))


@dataclass
class DelayResolution:
    """Why a delay window closed. Carried in brake.delay_resolved
    events so chatlog / Q&A consumers can show 'overridden by X' vs
    'default applied.' DelayMonitor's bookkeeping is best-effort —
    races between the X-press file write and the hook's polling can
    occasionally produce reason="unknown" — but human-observation
    accuracy is fine without filesystem-level precision.

    Tripwire context (post-2026-05-07 redesign): when the delay was
    tripwire-driven, severity is set so the deck's critical-kill
    subscriber can fire a kill iff severity == "critical" AND
    applied_action == "deny" (i.e., the tripwire fired AND the
    netrunner did NOT X-allow). For brake-only delays, severity is
    "" and tripwire_name is "" — the kill subscriber ignores them.
    """
    construct_id: str
    reason: str  # "override" | "expired" | "unknown"
    applied_action: str  # "allow" | "deny"
    tripwire_name: str = ""
    tripwire_severity: str = ""  # "warning" | "critical" | ""
    tripwire_bad_enough: bool = False  # propagated for blacklist-proposal gating


# ---------------------------------------------------------------------- paths


def _spawns_dir(home_dir: Path) -> Path:
    return Path(home_dir) / ".cyberdeck" / "spawns"


def delay_pending_path(home_dir: Path, construct_id: str) -> Path:
    return _spawns_dir(home_dir) / f"{construct_id}.delay_pending.json"


def delay_override_path(home_dir: Path, construct_id: str) -> Path:
    return _spawns_dir(home_dir) / f"{construct_id}.delay_override.json"


# ---------------------------------------------------------------------- writer


def write_delay_override(
    home_dir: Path,
    construct_id: str,
    action: str,
) -> bool:
    """Write the X-press override file for the hook to pick up.

    `action` must be "allow" or "deny" — the flipped-default for this
    specific delay. Caller (action_x_focused) reads the DelayEntry's
    override_action field and passes it through unchanged.

    Returns True on success. False if the spawns dir doesn't exist or
    write failed — the caller surfaces a chatlog warning in that case
    (very rare; means the spawn never installed brake hook settings,
    which means there's nothing to override anyway).
    """
    if action not in ("allow", "deny"):
        return False
    spawns = _spawns_dir(home_dir)
    if not spawns.exists():
        return False
    path = delay_override_path(home_dir, construct_id)
    try:
        path.write_text(
            json.dumps({"action": action}), encoding="utf-8",
        )
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------- snapshot


def read_active_delays(home_dir: Path) -> list[DelayEntry]:
    """Synchronous snapshot of all currently-pending delays. Used by
    the DelayPanel on mount (before the bus catches it up via replay)
    and by tests. Skips unparseable files silently."""
    spawns = _spawns_dir(home_dir)
    if not spawns.exists():
        return []
    entries: list[DelayEntry] = []
    for path in spawns.glob("*.delay_pending.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entry = DelayEntry.from_dict(data) if isinstance(data, dict) else None
        if entry is not None:
            entries.append(entry)
    return entries


# ---------------------------------------------------------------------- monitor


class DelayMonitor:
    """Polls the spawns dir for delay_pending.json files and publishes
    bus events on appearance / disappearance.

    Single-loop, single-task — same shape as ConnectionMonitor. Bus may
    be None for standalone tests; events still get computed but nothing
    surfaces.

    The dedupe state is the set of construct_ids whose pending file we
    last observed. New file = appearance (publish opened). Missing file
    = disappearance (publish resolved). The hook is the source of
    truth; we just translate filesystem events into bus events.
    """

    DEFAULT_POLL_INTERVAL = 0.05  # 50ms

    def __init__(
        self,
        home_dir: Path,
        *,
        bus: Optional[object] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> None:
        self.home_dir = Path(home_dir)
        self.bus = bus
        self.poll_interval = poll_interval

        # construct_id -> DelayEntry currently visible. When a cid
        # leaves this set we publish brake.delay_resolved.
        self._active: dict[str, DelayEntry] = {}
        # construct_id -> action ("allow"/"deny") recorded by the X
        # handler just before writing the override file. Lets us
        # report `reason="override"` on the corresponding resolution
        # event. Cleared on resolution so a future delay for the same
        # cid doesn't inherit a stale override.
        self._pending_overrides: dict[str, str] = {}

        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._stop_event: Optional[asyncio.Event] = None

    # ---- bus wiring ----------------------------------------------------

    def _publish(self, kind: str, payload, *, severity: str = "info",
                 text: Optional[str] = None) -> None:
        """Publish a DeckEvent through the bus, swallowing any
        bus-side errors so a misbehaving subscriber can't break the
        monitor loop. Per-callback exception isolation lives on the
        bus, but the bus itself raising on publish is theoretically
        possible (full ring buffer? OOM?), so we wrap defensively."""
        if self.bus is None:
            return
        try:
            from event_bus import DeckEvent
            self.bus.publish(DeckEvent(
                kind=kind,
                source="brake_delay",
                timestamp=time.time(),
                severity=severity,
                text=text,
                payload=payload,
                construct_id=getattr(payload, "construct_id", None),
            ))
        except Exception:
            pass

    # ---- public API ----------------------------------------------------

    def note_override(self, construct_id: str, action: str) -> None:
        """Called by action_x_focused right before write_delay_override.
        Records the action so the next resolution event can attribute
        the close to a netrunner override rather than a default-expiry."""
        self._pending_overrides[construct_id] = action

    async def start(self) -> None:
        """Start the polling loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self._run(), name="brake-delay-monitor",
        )

    async def stop(self) -> None:
        """Signal the loop to exit and await its termination. Safe to
        call multiple times; safe to call before start (no-op)."""
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
        self._task = None
        self._stop_event = None

    # ---- the loop ------------------------------------------------------

    async def _run(self) -> None:
        """Poll the spawns dir. Diff against last seen set. Publish
        opened/resolved events. Sleep poll_interval. Repeat."""
        spawns = _spawns_dir(self.home_dir)
        while not self._stopped:
            current: dict[str, DelayEntry] = {}
            try:
                if spawns.exists():
                    for path in spawns.glob("*.delay_pending.json"):
                        try:
                            data = json.loads(
                                path.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            # Mid-write or transient — try again next tick.
                            continue
                        if not isinstance(data, dict):
                            continue
                        entry = DelayEntry.from_dict(data)
                        if entry is not None:
                            current[entry.construct_id] = entry
            except OSError:
                # Spawns dir vanished or perms hiccup — try again.
                pass

            # Newly-appeared delays.
            for cid, entry in current.items():
                if cid not in self._active:
                    self._publish(
                        kind="brake.delay_opened",
                        payload=entry,
                        severity="info",
                        text=(
                            f"delay opened: {cid} "
                            f"[{entry.tool_name}] "
                            f"default={entry.default_action} "
                            f"({entry.delay_window_seconds:g}s)"
                        ),
                    )

            # Disappeared delays — resolve.
            for cid in list(self._active.keys()):
                if cid not in current:
                    prior = self._active[cid]
                    override_action = self._pending_overrides.pop(cid, None)
                    if override_action is not None:
                        reason = "override"
                        applied = override_action
                    else:
                        reason = "expired"
                        applied = prior.default_action
                    resolution = DelayResolution(
                        construct_id=cid,
                        reason=reason,
                        applied_action=applied,
                        tripwire_name=prior.tripwire_name,
                        tripwire_severity=prior.tripwire_severity,
                        tripwire_bad_enough=prior.tripwire_bad_enough,
                    )
                    self._publish(
                        kind="brake.delay_resolved",
                        payload=resolution,
                        severity="info",
                        text=(
                            f"delay resolved: {cid} "
                            f"applied={applied} ({reason})"
                        ),
                    )

            self._active = current

            # Stop early if signalled.
            if self._stop_event is not None:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval,
                    )
                    # If we got here without timeout, stop was requested.
                    break
                except asyncio.TimeoutError:
                    continue
            else:
                await asyncio.sleep(self.poll_interval)
