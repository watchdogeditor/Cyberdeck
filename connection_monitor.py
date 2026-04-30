"""
ConnectionMonitor: periodic heartbeats to detect Online/Degraded/Offline.

Per spec (cyberdeck-spec.md §Connection state):
  - Online    — full capability
  - Degraded  — unstable/transitioning; remote-model spawns blocked
  - Offline   — no connection at all; daemon parks

This module implements DETECTION only. The blocking semantics (spawn
gating, daemon parking) and recovery flow are downstream consumers
of the state events — they read the monitor's current state and
react. Today only the UI consumes state for an indicator. The
spawn/park hooks land later when M5+ wiring catches up to the spec.

Detection strategy: cheap TCP heartbeat to a configurable target
(default `api.anthropic.com:443`). DNS failure → Offline (no network
at all). Connect timeout / TCP error → Degraded (network exists,
target unreachable). Three consecutive successes → Online. Threshold-
based to avoid flapping on a single dropped packet.

Per spec line 114, the spec also calls for stderr-pattern scan
(EAI_AGAIN/ECONNRESET in subprocess output) to catch in-band
failures without waiting for the next heartbeat. That's a downstream
hook — call `record_subprocess_error(text)` from wherever the deck
sees stderr — and is wired in tui.py once construct/daemon stderr
plumbing is decided. For now the monitor exposes the entry point.
"""
from __future__ import annotations

import asyncio
import socket
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional


class ConnectionState(Enum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


@dataclass
class StateChangeEvent:
    """Emitted when the monitor's state transitions. The UI subscribes
    to repaint the indicator; future consumers (spawn gate, daemon
    park controller) will subscribe to gate or pause work."""
    timestamp: float
    old_state: ConnectionState
    new_state: ConnectionState
    reason: str


class ConnectionMonitor:
    """Periodic heartbeat + state machine.

    Lifecycle:
        cm = ConnectionMonitor(on_state_change=cb)
        await cm.start()
        ...
        await cm.shutdown()

    Polling:
        Default 30s interval — fast enough to catch wifi handoffs
        within a useful window, slow enough to be invisible cost on
        the deck. Increase for power-conscious wearable mode (the
        Pi-class deck on the move) when battery matters more than
        latency-to-detect.

    State transitions:
        ONLINE → DEGRADED  on `degraded_threshold` consecutive failures
        ONLINE → OFFLINE   on a DNS-resolution failure (clean signal:
                           no network at all, vs reachability dip)
        DEGRADED → ONLINE  on `recovery_threshold` consecutive successes
        DEGRADED → OFFLINE on a DNS failure (lost network entirely
                           after losing target)
        OFFLINE → ONLINE   on `recovery_threshold` consecutive successes
                           (skip Degraded; if it works it works)

    Defaults pick a 2-failure degrade threshold and 1-success recovery
    threshold — degrade conservatively (don't flap on a single packet
    drop), recover aggressively (one success means we're actually
    back, no need to wait for triple-confirmation).
    """

    DEFAULT_INTERVAL = 30.0       # seconds between heartbeats
    DEFAULT_TIMEOUT = 3.0         # seconds per heartbeat attempt
    DEFAULT_DEGRADE_THRESHOLD = 2 # consecutive failures → DEGRADED
    DEFAULT_RECOVER_THRESHOLD = 1 # consecutive successes → ONLINE

    def __init__(
        self,
        target_host: str = "api.anthropic.com",
        target_port: int = 443,
        *,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        degrade_threshold: int = DEFAULT_DEGRADE_THRESHOLD,
        recover_threshold: int = DEFAULT_RECOVER_THRESHOLD,
        on_state_change: Optional[Callable[[StateChangeEvent], None]] = None,
        # Optional injection for tests — replaces the live socket
        # heartbeat with a stub that returns ("ok"|"timeout"|"refused"
        # |"dns_failure"). Real deck always uses the live socket.
        heartbeat_fn: Optional[Callable[[], "asyncio.Future[str]"]] = None,
        bus: Optional[object] = None,
    ) -> None:
        self.target_host = target_host
        self.target_port = target_port
        self.interval = interval
        self.timeout = timeout
        self.degrade_threshold = degrade_threshold
        self.recover_threshold = recover_threshold
        self.on_state_change = on_state_change
        self.heartbeat_fn = heartbeat_fn
        # Phase 5 of the unified-event-stream slice. When wired,
        # state transitions ALSO publish a `connection.transition`
        # DeckEvent on the bus alongside on_state_change.
        self.bus = bus

        # State. Start ONLINE on the optimistic assumption that the
        # deck launches with a working connection — we'll downgrade
        # on heartbeat evidence rather than show "checking..." for
        # the first interval. If the deck launches genuinely offline,
        # the first failed heartbeat catches it within `interval`
        # seconds, which is fine.
        self.state = ConnectionState.ONLINE
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        # Recent heartbeat outcomes for diagnostics. Bounded so
        # long-running decks don't accumulate a ledger.
        self._recent: deque = deque(maxlen=20)

        self._task: Optional[asyncio.Task] = None
        self._stopped = False
        self._stop_event: Optional[asyncio.Event] = None

    async def start(self) -> None:
        """Start the heartbeat loop. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        # First heartbeat fires near-immediately so the state lands
        # accurately within a few seconds of startup, not after a
        # full interval. Subsequent ones space out per `interval`.
        self._task = asyncio.create_task(
            self._run(),
            name="connection-monitor",
        )

    async def shutdown(self) -> None:
        """Stop the heartbeat loop. Idempotent."""
        self._stopped = True
        if self._stop_event is not None:
            self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None

    def record_subprocess_error(self, stderr_text: str) -> None:
        """Hook for in-band detection — call this with subprocess
        stderr that contains network-failure markers
        (EAI_AGAIN, ECONNRESET, etc.) to skip the heartbeat wait
        and immediately count a failure. Currently a stub at the
        usage layer (no plumbing in tui.py yet); the monitor exposes
        the API so wiring is one-line when we wire it.

        Conservative: only the unmistakable network-failure markers
        count. Not every non-zero exit; not every traceback. Leave
        the noisy detection to the heartbeat path."""
        markers = ("EAI_AGAIN", "ECONNRESET", "ENETUNREACH", "EHOSTUNREACH")
        if not any(m in stderr_text for m in markers):
            return
        self._handle_result("subprocess_error")

    async def _run(self) -> None:
        """Heartbeat loop. Sleeps `interval` between checks, exits on
        stop_event."""
        try:
            while not self._stopped:
                await self._heartbeat_once()
                if self._stopped:
                    return
                # Sleep with early-exit on stop signal so shutdown is
                # snappy even if the interval is long. wait_for raises
                # TimeoutError on the normal-tick path; that's the
                # signal to loop, not an error.
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.interval,
                    )
                    # If wait returned without timeout, the stop_event
                    # fired — exit the loop.
                    return
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    async def _heartbeat_once(self) -> None:
        """Run one heartbeat attempt and update state. Failures are
        bucketed by kind so DNS issues distinguish from reachability
        dips — that's how OFFLINE is differentiated from DEGRADED."""
        if self.heartbeat_fn is not None:
            # Test injection path
            try:
                result = await self.heartbeat_fn()
            except Exception as e:
                result = f"error: {e}"
        else:
            result = await self._tcp_heartbeat()
        self._handle_result(result)

    async def _tcp_heartbeat(self) -> str:
        """Attempt TCP connect to (target_host, target_port). Returns
        a result string for state classification."""
        loop = asyncio.get_event_loop()
        try:
            # getaddrinfo first so we can distinguish DNS-failure
            # ("offline-grade") from connection-failure ("degraded-
            # grade"). asyncio's open_connection collapses both into
            # OSError otherwise.
            await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(
                    self.target_host, self.target_port,
                    proto=socket.IPPROTO_TCP,
                ),
            )
        except socket.gaierror:
            return "dns_failure"
        except Exception as e:
            return f"resolve_error: {e}"

        try:
            fut = asyncio.open_connection(self.target_host, self.target_port)
            reader, writer = await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            return "timeout"
        except (ConnectionRefusedError, OSError) as e:
            return f"refused: {e}"
        # Close cleanly — we just wanted to know the connect worked.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass  # close errors don't invalidate the success
        return "ok"

    def _handle_result(self, result: str) -> None:
        """Update counters + transition state based on heartbeat
        outcome. Single point of state-machine logic; both heartbeat
        path and subprocess-error path funnel through here."""
        self._recent.append((time.time(), result))

        if result == "ok":
            self._consecutive_successes += 1
            self._consecutive_failures = 0
            if (self.state != ConnectionState.ONLINE
                    and self._consecutive_successes >= self.recover_threshold):
                self._transition(ConnectionState.ONLINE, "heartbeat success")
        elif result == "dns_failure":
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            # DNS failure = no network at all. Skip Degraded; go
            # straight to Offline. Single failure is enough — DNS
            # rarely flaps the way packet loss does.
            if self.state != ConnectionState.OFFLINE:
                self._transition(
                    ConnectionState.OFFLINE,
                    "dns resolution failed",
                )
        else:
            # All other failures (timeout, refused, subprocess_error,
            # etc.) → Degraded after threshold.
            self._consecutive_failures += 1
            self._consecutive_successes = 0
            if (self.state == ConnectionState.ONLINE
                    and self._consecutive_failures >= self.degrade_threshold):
                self._transition(
                    ConnectionState.DEGRADED,
                    f"{self._consecutive_failures} failures: {result}",
                )

    def _transition(self, new_state: ConnectionState, reason: str) -> None:
        """Apply a state change and fire the callback. Counters reset
        on transition so the next threshold count starts fresh."""
        if new_state == self.state:
            return
        old = self.state
        self.state = new_state
        # Reset counters so we measure new-state stability cleanly.
        # E.g. after going Online, we want to see degrade_threshold
        # NEW failures, not the historical count.
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        evt = StateChangeEvent(
            timestamp=time.time(),
            old_state=old,
            new_state=new_state,
            reason=reason,
        )
        if self.on_state_change is not None:
            try:
                self.on_state_change(evt)
            except Exception:
                # Listener errors don't kill the monitor.
                pass
        # Phase 5: also publish on the bus when wired. Severity
        # reflects the destination state — anything other than
        # ONLINE escalates to warning so subscribers can react to
        # "we lost network" without inspecting payload state names.
        if self.bus is not None:
            try:
                from event_bus import DeckEvent
                severity = (
                    "info" if new_state == ConnectionState.ONLINE
                    else "warning"
                )
                self.bus.publish(DeckEvent(
                    kind="connection.transition",
                    source="connection_monitor",
                    timestamp=evt.timestamp,
                    severity=severity,
                    text=(
                        f"connection: {old.value} → "
                        f"{new_state.value} ({reason})"
                    ),
                    payload=evt,
                ))
            except Exception:
                pass
