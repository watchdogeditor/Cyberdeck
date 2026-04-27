"""
Session manager: single source of truth for session_ids the deck knows about.

A session, in our world, is a long-lived Claude Code conversation identified
by its server-side session_id. We capture session_ids from the `system_init`
event of every subprocess we spawn, and keep them around because:

1. They let us `--resume` cheap. No need to re-process the system prompt;
   just feed a new user turn.
2. They let us recover after a restart. The session lives server-side; we
   just need to remember it exists.
3. They feed the eventual session pool (M5.3b+) and recovery flow (later).

This module is the *registry* of those session_ids — a small JSON file on
disk plus an in-memory mirror. It does NOT spawn subprocesses, NOT manage
the pool (that's M5.3b), and NOT make recovery decisions (that's later).
It records facts.

M5.3a scope: track session_ids of every spawned subprocess (constructs and
daemon turns). Persist across restarts. No pool, no recovery, no UI.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Iterable, Callable


# Default location: alongside the run log, under .cyberdeck/ in the cwd.
# Configurable per-deck, but the default keeps everything project-local
# and inspection-friendly.
DEFAULT_MANIFEST_PATH = Path(".cyberdeck") / "sessions.json"


@dataclass
class SessionEntry:
    """One session the deck knows about.

    Lifecycle:
        warm     — pre-warmed, ready to be pulled (M5.3b+; unused in M5.3a)
        in_use   — actively serving a task right now
        done     — the construct finished cleanly; session can be discarded
                   or kept as autopsy reference
        failed   — subprocess errored out
        killed   — soft-killed or hard-killed by the netrunner
        expired  — too old to safely resume; manifest will GC these
    """
    session_id: str
    created_at: float
    last_seen_at: float
    state: str  # warm | in_use | done | failed | killed | expired
    kind: str  # 'construct' | 'daemon'
    profile: Optional[str] = None  # name of profile used; None until M5.5
    task_preview: str = ""  # first ~80 chars of the task, for inspection


class SessionManager:
    """Owns the on-disk session manifest. Thread-safe-ish: all writes go
    through this object; concurrent calls within a single asyncio loop are
    fine since they don't interleave."""

    # Sessions older than this are considered too stale to safely resume.
    # 5h is a conservative bound based on the rolling window Claude Code
    # appears to use for session_id validity (also matches the Pro/Max
    # rate-limit reset cycle, which observable behavior suggests gates
    # session liveness too). If --resume fails on a session that's
    # actually been evicted server-side, we fall back to fresh spawn —
    # so being slightly aggressive here costs only a few extra warming
    # tokens, while being too lax can leave constructs stuck waiting
    # on dead sessions.
    DEFAULT_STALE_AFTER_SECS = 5 * 60 * 60

    def __init__(
        self,
        manifest_path: Optional[Path] = None,
        stale_after_secs: float = DEFAULT_STALE_AFTER_SECS,
    ) -> None:
        self.manifest_path = manifest_path or DEFAULT_MANIFEST_PATH
        self.stale_after_secs = stale_after_secs
        self._entries: dict[str, SessionEntry] = {}
        self._loaded = False

    # ---- lifecycle ------------------------------------------------------

    def load(self) -> None:
        """Read the manifest file from disk. Safe to call multiple times;
        a missing file is treated as "no prior sessions," not an error."""
        self._entries.clear()
        if not self.manifest_path.exists():
            self._loaded = True
            return
        try:
            raw = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupted manifest — start fresh, but don't blow up. The
            # corrupted file stays on disk for postmortem; the deck just
            # behaves as if there were no prior sessions.
            self._loaded = True
            return
        for item in raw.get("sessions", []):
            try:
                entry = SessionEntry(**item)
                self._entries[entry.session_id] = entry
            except (TypeError, KeyError):
                continue  # skip malformed entries silently
        self._loaded = True

    def save(self) -> None:
        """Atomically write the manifest to disk. Writes to a sibling
        .tmp file then renames, so a crash mid-write can't leave a
        half-written manifest."""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "saved_at": time.time(),
            "sessions": [asdict(e) for e in self._entries.values()],
        }
        # NamedTemporaryFile in the same dir so rename is atomic on every
        # platform we care about (POSIX guarantees, Windows mostly).
        fd, tmp_path = tempfile.mkstemp(
            prefix=".sessions-", suffix=".json.tmp",
            dir=str(self.manifest_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
            os.replace(tmp_path, self.manifest_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---- record / update ------------------------------------------------

    def record(
        self,
        session_id: str,
        kind: str,
        state: str = "in_use",
        profile: Optional[str] = None,
        task_preview: str = "",
    ) -> SessionEntry:
        """Add a new session entry. If the session_id already exists, this
        is treated as an update of state/last_seen_at — the original
        creation time and kind are preserved."""
        if not self._loaded:
            self.load()
        now = time.time()
        existing = self._entries.get(session_id)
        if existing is not None:
            existing.state = state
            existing.last_seen_at = now
            if profile is not None:
                existing.profile = profile
            if task_preview and not existing.task_preview:
                existing.task_preview = task_preview[:80]
            self.save()
            return existing
        entry = SessionEntry(
            session_id=session_id,
            created_at=now,
            last_seen_at=now,
            state=state,
            kind=kind,
            profile=profile,
            task_preview=task_preview[:80],
        )
        self._entries[session_id] = entry
        self.save()
        return entry

    def update(
        self,
        session_id: str,
        state: Optional[str] = None,
        profile: Optional[str] = None,
        task_preview: Optional[str] = None,
    ) -> Optional[SessionEntry]:
        """Update fields on an existing entry. Returns None if the
        session_id isn't tracked (silently — record-then-update is fine
        but update-without-record is suspicious enough to surface)."""
        if not self._loaded:
            self.load()
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        if state is not None:
            entry.state = state
        if profile is not None:
            entry.profile = profile
        if task_preview is not None:
            entry.task_preview = task_preview[:80]
        entry.last_seen_at = time.time()
        self.save()
        return entry

    def mark_done(self, session_id: str) -> Optional[SessionEntry]:
        return self.update(session_id, state="done")

    def mark_failed(self, session_id: str) -> Optional[SessionEntry]:
        return self.update(session_id, state="failed")

    def mark_killed(self, session_id: str) -> Optional[SessionEntry]:
        return self.update(session_id, state="killed")

    # ---- query ----------------------------------------------------------

    def get(self, session_id: str) -> Optional[SessionEntry]:
        if not self._loaded:
            self.load()
        return self._entries.get(session_id)

    def all(self) -> list[SessionEntry]:
        if not self._loaded:
            self.load()
        return list(self._entries.values())

    def filter(
        self,
        kind: Optional[str] = None,
        state: Optional[str] = None,
        predicate: Optional[Callable[[SessionEntry], bool]] = None,
    ) -> list[SessionEntry]:
        """Return entries matching the given filters. All conditions are
        ANDed together."""
        if not self._loaded:
            self.load()
        out: list[SessionEntry] = []
        for entry in self._entries.values():
            if kind is not None and entry.kind != kind:
                continue
            if state is not None and entry.state != state:
                continue
            if predicate is not None and not predicate(entry):
                continue
            out.append(entry)
        return out

    # ---- maintenance ----------------------------------------------------

    def is_stale(self, entry: SessionEntry, now: Optional[float] = None) -> bool:
        """A session is stale if it hasn't been touched recently enough
        to be worth resuming. Sessions in terminal states (done/failed/
        killed/expired) are never 'stale' — they're done and stay done."""
        if entry.state in ("done", "failed", "killed", "expired"):
            return False
        ref = now if now is not None else time.time()
        return (ref - entry.last_seen_at) > self.stale_after_secs

    def mark_stale_as_expired(self) -> list[SessionEntry]:
        """Walk all live entries; mark any that have aged out as
        'expired'. Returns the list of entries that were transitioned."""
        if not self._loaded:
            self.load()
        now = time.time()
        transitioned: list[SessionEntry] = []
        for entry in self._entries.values():
            if self.is_stale(entry, now):
                entry.state = "expired"
                entry.last_seen_at = now
                transitioned.append(entry)
        if transitioned:
            self.save()
        return transitioned

    def boot_recovery(self) -> dict[str, list[SessionEntry]]:
        """At app launch, clean up the manifest from the prior run.

        Two transitions happen here:

        1. `in_use` entries from the prior run are *orphaned* — their
           owning construct subprocess is long gone, but a clean
           shutdown didn't get to mark them done/failed. We can't
           safely reuse them (the conversation may be partial) and we
           don't want the pool to count them as warm. Transition: expired.

        2. `warm` entries from the prior run get the normal stale check
           — fresh ones survive and the pool can reuse them, old ones
           expire. This is where cross-restart pool reuse pays off.

        Returns a dict {"orphaned": [...], "stale": [...]} with the
        entries that were transitioned, for logging/diagnostics."""
        if not self._loaded:
            self.load()
        now = time.time()
        orphaned: list[SessionEntry] = []
        stale: list[SessionEntry] = []
        for entry in self._entries.values():
            if entry.state == "in_use":
                # No owning process can possibly be alive at boot; this
                # is by definition orphaned.
                entry.state = "expired"
                entry.last_seen_at = now
                orphaned.append(entry)
            elif entry.state == "warm" and self.is_stale(entry, now):
                entry.state = "expired"
                entry.last_seen_at = now
                stale.append(entry)
        if orphaned or stale:
            self.save()
        return {"orphaned": orphaned, "stale": stale}

    def gc_terminal(self, keep_recent: int = 50) -> int:
        """Garbage-collect old terminal-state entries. Keeps the N most
        recently-touched terminal entries (for autopsy / inspection) and
        drops the rest. Live entries (warm/in_use) are never GC'd here.
        Returns the number of entries removed."""
        if not self._loaded:
            self.load()
        terminal_states = {"done", "failed", "killed", "expired"}
        terminal = [
            e for e in self._entries.values() if e.state in terminal_states
        ]
        if len(terminal) <= keep_recent:
            return 0
        # Sort newest-first; keep the first `keep_recent`, drop the rest
        terminal.sort(key=lambda e: e.last_seen_at, reverse=True)
        to_drop = terminal[keep_recent:]
        for entry in to_drop:
            del self._entries[entry.session_id]
        self.save()
        return len(to_drop)


# ---- Session Pool ----------------------------------------------------
#
# The pool is the *active* counterpart to the manifest. Where SessionManager
# is a passive record, SessionPool actually spawns warming subprocesses,
# tracks in-flight warming, and serves pulls to whoever wants a fresh
# construct (the daemon, in M5.3c).
#
# Architecture: each warm = one short-lived Construct subprocess that runs
# a bare noop ("Acknowledge ready."), captures its session_id from
# system_init, and exits. The session_id is recorded as state=warm in the
# manifest. Later, when a real task arrives, we pull a warm session_id and
# `--resume` it with the actual task — saving subprocess startup time and
# priming server-side conversation state.
#
# M5.3b scope: pool warms at startup and refills on pull. Daemon doesn't
# pull yet (M5.3c). Cross-restart reuse is M5.3e.

import asyncio
from typing import Callable as _Callable, Optional as _Optional


@dataclass
class PoolEvent:
    """Status update from the pool. Used by the TUI to drive the RAM
    meter and any other pool-aware UI."""
    kind: str  # 'warming_started' | 'warmed' | 'warm_failed' | 'pulled' | 'shutdown'
    session_id: _Optional[str] = None
    error: _Optional[str] = None
    # Snapshot fields useful for renderers
    warm_count: int = 0
    warming_count: int = 0
    target_size: int = 0


class SessionPool:
    """Pre-warms a fixed-size pool of default-profile construct sessions.

    On `start()`, spawns N background tasks, each warming one session.
    Each warm is one short-lived `claude -p '<noop>'` subprocess; we
    capture its session_id and let it exit. Warm sessions sit in the
    manifest with state='warm' until pulled.

    Each `pull()` triggers a background refill so the pool stays at
    target size. Refills are fire-and-forget; pull returns immediately.

    The pool intentionally does NOT block on warming. If a pull happens
    while warm count is zero, pull returns None and the caller spawns
    fresh the old way. The pool is an optimization, not a requirement.

    Lifecycle:
        pool = SessionPool(manager, target_size=3, claude_bin="claude")
        await pool.start()
        ...
        entry = await pool.pull()  # marks warm session in_use
        ...
        await pool.shutdown()  # cancels in-flight warming
    """

    DEFAULT_WARM_MESSAGE = "Acknowledge ready."
    DEFAULT_TARGET_SIZE = 3

    def __init__(
        self,
        manager: SessionManager,
        target_size: int = DEFAULT_TARGET_SIZE,
        claude_bin: str = "claude",
        warm_message: str = DEFAULT_WARM_MESSAGE,
        on_event: _Optional[_Callable[[PoolEvent], None]] = None,
        cwd: _Optional[str] = None,
    ) -> None:
        self.manager = manager
        self.target_size = target_size
        self.claude_bin = claude_bin
        self.warm_message = warm_message
        self._on_event = on_event
        # Working directory for warming subprocesses. None = inherit
        # from python's cwd. Pass an explicit home dir to keep warming
        # processes confined alongside the rest of the deck.
        self.cwd = cwd
        # In-flight warming tasks. We track them so shutdown can cancel
        # cleanly and warming_count reflects real state.
        self._warming_tasks: set = set()
        self._stopped = False

    # ---- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        """Begin warming to reach target size. Returns immediately;
        warming happens asynchronously."""
        # Account for any sessions already-warm in the manifest from a
        # previous run. We don't re-warm those — they're already valid.
        # (Cross-restart reuse details land in M5.3e; for M5.3b we just
        # avoid double-warming.)
        already_warm = self.warm_count
        needed = max(0, self.target_size - already_warm)
        for _ in range(needed):
            self._spawn_warming_task()

    async def shutdown(self) -> None:
        """Cancel any in-flight warming tasks. Already-warm sessions
        stay in the manifest as 'warm' — they remain valid for next
        app launch (cross-restart reuse, M5.3e)."""
        self._stopped = True
        if not self._warming_tasks:
            self._emit(PoolEvent(kind="shutdown"))
            return
        for task in list(self._warming_tasks):
            task.cancel()
        # Wait briefly for cancellation to settle. We use return_exceptions
        # so a CancelledError doesn't propagate up and abort shutdown.
        await asyncio.gather(*self._warming_tasks, return_exceptions=True)
        self._warming_tasks.clear()
        self._emit(PoolEvent(kind="shutdown"))

    # ---- pull / refill --------------------------------------------------

    async def pull(self) -> _Optional[SessionEntry]:
        """Take the oldest warm session, mark it in_use, and trigger
        a background refill. Returns None if no warm sessions exist —
        the caller should fall back to a fresh spawn in that case."""
        warm_entries = self.manager.filter(kind="construct", state="warm")
        if not warm_entries:
            return None
        # FIFO: oldest warm session first. Older sessions are likely
        # closer to going stale, so it's better to use them up.
        warm_entries.sort(key=lambda e: e.created_at)
        entry = warm_entries[0]
        # Mark in_use immediately so concurrent pulls don't grab the
        # same entry. Manifest writes are sync, so this is atomic
        # within the asyncio loop.
        self.manager.update(entry.session_id, state="in_use")
        # Refill in the background; pull returns immediately.
        self._spawn_warming_task()
        self._emit(PoolEvent(
            kind="pulled",
            session_id=entry.session_id,
        ))
        return entry

    def _spawn_warming_task(self) -> None:
        """Schedule a new warming task. No-op if pool is shutting down."""
        if self._stopped:
            return
        task = asyncio.create_task(self._warm_one())
        self._warming_tasks.add(task)
        # Auto-clean from the set when done so warming_count is accurate.
        task.add_done_callback(self._warming_tasks.discard)
        self._emit(PoolEvent(kind="warming_started"))

    async def _warm_one(self) -> None:
        """Spawn one warming subprocess: Construct with a noop task,
        capture session_id from system_init, let it run to completion,
        record as 'warm' in manifest."""
        # Import lazily to avoid circular import — Construct doesn't
        # know about SessionPool, but SessionPool uses Construct.
        from construct import Construct

        try:
            c = Construct(
                task=self.warm_message,
                claude_bin=self.claude_bin,
                # No tools needed for warming — this is just session
                # establishment, not real work. Empty list also keeps
                # the warming subprocess fast and minimal.
                tools=[],
                cwd=self.cwd,
            )
            await c.spawn()
            # Drain events to keep stdout flowing; the subprocess will
            # block on stdout buffer otherwise. session_id is captured
            # by Construct itself when system_init flies past.
            async for _ in c.events():
                pass
            await c.wait(timeout=30.0)

            if c.session_id and c.state.value == "done":
                self.manager.record(
                    session_id=c.session_id,
                    kind="construct",
                    state="warm",
                    task_preview="(warm)",
                )
                self._emit(PoolEvent(
                    kind="warmed",
                    session_id=c.session_id,
                ))
            else:
                self._emit(PoolEvent(
                    kind="warm_failed",
                    error=f"warm subprocess did not produce session_id "
                          f"(state={c.state.value}, sid={c.session_id})",
                ))
        except asyncio.CancelledError:
            # Shutdown path. Don't emit on cancellation; shutdown event
            # was already emitted by shutdown().
            raise
        except Exception as e:
            self._emit(PoolEvent(
                kind="warm_failed",
                error=str(e),
            ))

    # ---- introspection --------------------------------------------------

    @property
    def warm_count(self) -> int:
        """Number of sessions currently sitting warm in the pool."""
        return len(self.manager.filter(kind="construct", state="warm"))

    @property
    def warming_count(self) -> int:
        """Number of warming subprocesses currently in flight."""
        return len(self._warming_tasks)

    def _emit(self, event: PoolEvent) -> None:
        """Fire a PoolEvent to the listener, if any. Snapshots warm
        count and warming count so listeners get a consistent view."""
        if self._on_event is None:
            return
        # Populate snapshot fields
        event.warm_count = self.warm_count
        event.warming_count = self.warming_count
        event.target_size = self.target_size
        try:
            self._on_event(event)
        except Exception:
            pass  # listener errors don't break the pool
