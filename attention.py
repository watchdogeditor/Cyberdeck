"""attention.py — pending approval items for the netrunner.

Phase 2 of safety architecture pass slice 3 (filed 2026-05-01,
landing post-phase-1). The "attention needed" UI surface
consolidates proposal-shaped events that need a netrunner X-press
to approve before they auto-resolve.

Today there's exactly one kind: `blacklist_proposal`, fired when a
critical+bad_enough tripwire matches a construct's stream. The
construct gets auto-termed (slice 2 behavior), AND a blacklist
proposal is filed to remember the task pattern for the rest of the
session. Netrunner presses X within `window_seconds` to approve;
expiry drops the proposal silently.

Future kinds plug in here without re-architecture:
  - `daemon_capture_request` — daemon asks "show me what error message
    is on the screen" (per spec)
  - `per_spawn_allowlist_override` — netrunner explicitly opts a
    construct into a normally-denied verb (slice 1 deferred follow-up)
  - `slow_resume_warning` — connection-recovery path asks before
    resuming a mid-stream session (M5+)

Architectural shape mirrors the brake-hook delay UX (slice 3 phase 1)
so X-press resolution feels uniform across the deck:

  - Open: emit a bus event, schedule a timer for expiry, render
    in the AttentionPanel + chatlog.
  - Approve: X-press → apply the kind-specific payload, cancel
    the timer, emit a resolved event with reason="approved".
  - Expire: timer fires → drop the item silently (no payload
    application), emit a resolved event with reason="expired".

Distinct from brake-hook delays in two ways:

  1. Deck-owned timer. Brake-hook delays poll a file the hook
     subprocess wrote; the deck just observes. Attention items
     don't have a hook — they're TUI-side reactions to bus events
     (tripwire fires) that never leave the deck process. The
     timer is just an asyncio.Task on the App.

  2. No file protocol. Brake-hook delays use spawns/<cid>.delay_
     pending.json so a separate process (the hook) can read +
     respect the netrunner's choice. Attention items are entirely
     in-process; the only persistence is the bus event log.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


# Kind constants — class-as-namespace pattern (same as
# tripwires.Severity / construct.EventKind). Use these instead of
# bare strings so a typo on the consumer side becomes
# AttributeError at import rather than a silent dead branch.
class AttentionKind:
    """Categories of attention item. Each kind has its own
    payload shape + approve-side behavior, dispatched by name in
    the App's attention handlers."""
    BLACKLIST_PROPOSAL = "blacklist_proposal"


# Resolution reasons for attention.resolved bus events. Symmetric
# with brake.delay_resolved's reason field so consumers can switch
# uniformly.
class AttentionResolution:
    APPROVED = "approved"   # netrunner X-pressed in time
    EXPIRED = "expired"     # window elapsed without action
    DROPPED = "dropped"     # programmatically cancelled (e.g. EJECT)


@dataclass(frozen=True)
class AttentionItem:
    """One pending approval prompt for the netrunner.

    `item_id` is unique per item (uuid). The App's attention dict
    keys on this so X-press can target the focused item without
    relying on construct_id (which may be absent for non-construct-
    scoped attention kinds).

    `kind` selects the approve-side handler. `payload` is the data
    needed to apply the approval — for blacklist_proposal, it's the
    BlacklistEntry to add to the watchdog's session blacklist.

    `construct_id` is optional because not every attention kind is
    tied to a construct. Today's kind (blacklist_proposal) is, so
    the field is populated; future kinds may set it to None.

    Window timing mirrors DelayEntry's shape (opened_at +
    deadline_ts + window_seconds) so the AttentionPanel can render
    its countdown bar with the same primitives as the per-pane
    delay overlay.
    """
    item_id: str
    kind: str
    title: str          # one-line summary for the panel
    detail: str         # longer description (truncated when rendered)
    construct_id: Optional[str]
    opened_at: float
    deadline_ts: float
    window_seconds: float
    payload: Any        # kind-specific; dispatched by AttentionKind

    @classmethod
    def new(
        cls,
        kind: str,
        title: str,
        detail: str,
        window_seconds: float,
        payload: Any,
        construct_id: Optional[str] = None,
        item_id: Optional[str] = None,
    ) -> "AttentionItem":
        """Construct a fresh item with a generated id and timestamps.
        Caller passes window_seconds + payload + the kind-specific
        labels; we mint the rest."""
        now = time.time()
        return cls(
            item_id=item_id or f"att-{uuid.uuid4().hex[:8]}",
            kind=kind,
            title=title,
            detail=detail,
            construct_id=construct_id,
            opened_at=now,
            deadline_ts=now + window_seconds,
            window_seconds=window_seconds,
            payload=payload,
        )

    @property
    def remaining_seconds(self) -> float:
        """How long the netrunner has left to press X. Clamps at 0
        so UI countdown bars don't display negative time when the
        timer is already cleaning up."""
        return max(0.0, self.deadline_ts - time.time())

    @property
    def progress(self) -> float:
        """0.0 = just opened, 1.0 = expired. Drives the panel's
        countdown bar fill (mirrors DelayEntry.progress)."""
        if self.window_seconds <= 0:
            return 1.0
        elapsed = time.time() - self.opened_at
        return max(0.0, min(1.0, elapsed / self.window_seconds))


@dataclass(frozen=True)
class AttentionResolved:
    """Emitted on attention.resolved bus events. Consumers (chatlog
    renderer, future Q&A context) read this to render the resolution
    line. `reason` is one of AttentionResolution constants."""
    item_id: str
    kind: str
    reason: str
    construct_id: Optional[str] = None
