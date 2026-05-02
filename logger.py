"""
logger.py — DeckLogger: a bus subscriber that writes per-launch
NDJSON files to <deck source>/logs/.

Phase 7 of the unified-event-stream slice. Replaces the prior
single-file `cyberdeck.log` (which appended every session ever and
was a PITA to share) with per-launch files in a logs/ directory.
Each launch gets its own timestamped file; a `latest.log` pointer
makes "tail the current run" / "send Claude my log" trivial.

Why a separate file per launch:
  - "Send me your log" stops dragging in every prior session.
  - Past runs are inspectable artifacts in their own right (the
    morgue and maintbot read them). Nothing has to grep for the
    boundary between two sessions in a concatenated file.
  - On the eventual Pi/wall-mount port, log rotation becomes a
    file-deletion concern instead of a truncation concern.

Why next to the .py files instead of cyberdeck-home/:
  - Operational artifacts, not deck-content. The deck shouldn't read
    its own log files via construct as a default permission — they
    sit outside the workspace so the brake hook protects them.
  - The future maintbot reads from here directly. Putting logs
    inside cyberdeck-home/ would tangle the maintbot's access model
    with the construct-workspace boundary.

Why NDJSON:
  - One event per line, machine-parseable with `jq -r .kind`,
    grep-friendly, append-safe, no trailing-comma games. The deck
    already used NDJSON for its older fleet log; this is the same
    shape one level up.
  - A future "view-log" pretty-printer can consume NDJSON directly;
    no need to maintain two parallel formats.

Severity filtering:
  - Default INFO. DEBUG events get dropped at the writer to keep file
    size sane. Override via `CYBERDECK_LOG_LEVEL` env var or
    `--log-level` CLI flag.
  - CRITICAL is always written regardless of level — those events
    represent things the netrunner (or maintbot) absolutely needs to
    see in the file even if the deck was running at a higher
    threshold.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional


# Severity ordering. Higher rank = more severe; events with rank
# below the configured threshold get dropped at write time. CRITICAL
# is the special case — always written regardless of threshold.
_SEVERITY_RANKS: dict[str, int] = {
    "debug":    0,
    "info":     1,
    "warning":  2,
    "error":    3,
    "critical": 4,
}


def _serialize_payload(payload: Any, *, depth: int = 0) -> Any:
    """Best-effort JSON-serializable conversion of a DeckEvent payload.

    Common shapes seen on the bus:
      - None: passes through
      - dict / list / primitive: passes through
      - pathlib.Path: stringified (the OS-native path form). Caught
        on real-deck via the `Y` JSON yank — Profile.source_path and
        Plugin.source_dir are Path objects on dataclasses, and
        without this branch they hit the repr() fallback and got
        baked as `"WindowsPath('C:/...')"` strings into both the
        clipboard JSON and the per-launch .log files. Plain strings
        compose with downstream tools (jq, json.parse, etc.).
      - dataclass-shaped object (FleetEvent, DaemonEvent,
        BlacklistEntry, etc.): converts via __dict__ recursively
      - everything else: repr() fallback

    Bounded recursion via `depth` so a self-referential object can't
    spin forever. The default ceiling of 4 levels is enough for the
    deck's actual payloads (FleetEvent → payload → raw → message →
    content), beyond which we just repr() and move on.
    """
    if depth > 4:
        return repr(payload)
    if payload is None:
        return None
    if isinstance(payload, (str, int, float, bool)):
        return payload
    # Enum → its value. Without this, enum-valued payload fields hit
    # the __dict__ probe below — and Enum instances have an empty
    # __dict__, so they'd serialize as `{}` (silent data loss). Real-
    # deck-observed: brake.change events landed as
    # `"old_state": {}, "new_state": {}` in the per-launch .log
    # files (and y/Y JSON yank). Filed in the discrete bugs queue
    # 2026-05-01; fixed 2026-05-02.
    #
    # `payload.value` rather than `payload.name` because the deck's
    # enums (BrakeState, ConnectionState) define values as the
    # human-readable lowercase strings ("default", "online") that
    # we already use as wire format for state.json + bus event text.
    # Consistent encoding across the deck.
    if isinstance(payload, Enum):
        return payload.value
    # pathlib.Path → str. Avoids `WindowsPath('...')` / `PosixPath('...')`
    # repr leaking into JSON output. Checked before dict/list since
    # Path doesn't match those, but ahead of the __dict__ probe because
    # Path subclasses do have a __dict__ on some Python versions.
    if isinstance(payload, Path):
        return str(payload)
    if isinstance(payload, dict):
        return {
            str(k): _serialize_payload(v, depth=depth + 1)
            for k, v in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [_serialize_payload(v, depth=depth + 1) for v in payload]
    # Dataclass-shaped object: try __dict__. Most of the deck's event
    # payloads (FleetEvent / DaemonEvent / BlacklistEntry / etc.) are
    # dataclasses with simple attribute layouts.
    inner = getattr(payload, "__dict__", None)
    if isinstance(inner, dict) and inner:
        return {
            str(k): _serialize_payload(v, depth=depth + 1)
            for k, v in inner.items()
            if not k.startswith("_")
        }
    # Fallback: stringify. Loses structure but never raises.
    return repr(payload)


class DeckLogger:
    """Per-launch file logger that subscribes to the unified event bus.

    Lifecycle:
        logger = DeckLogger(log_dir=<dir>, level="info", ...)
        logger.attach_to_bus(bus)
        ...
        logger.close(reason="netrunner quit")

    File path: `<log_dir>/cyberdeck-YYYY-MM-DD-HHMMSS.log`. Also
    updates `<log_dir>/latest.log` to point at the current file
    (symlink on Linux/macOS, hard-copy on Windows where symlinks
    require admin).

    Single instance per app. Constructs DO NOT receive a reference —
    they're work units, not observers; the same hermetic-by-default
    contract that keeps them off the bus keeps them off the logger.
    """

    DEFAULT_LEVEL = "info"

    def __init__(
        self,
        *,
        log_dir: Path,
        level: str = DEFAULT_LEVEL,
        argv: Optional[list[str]] = None,
        env_snapshot: Optional[dict[str, str]] = None,
        deck_version: str = "unknown",
        brake_label: str = "default",
        home_dir: Optional[Path] = None,
        blacklist_provider: Optional[Any] = None,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Normalize and rank the level. Unknown levels fall back to INFO
        # rather than raising — bad config shouldn't crash startup.
        self.level = level.lower() if level else self.DEFAULT_LEVEL
        self._level_rank = _SEVERITY_RANKS.get(self.level, 1)

        # Per-launch filename. Timestamp is local time + readable;
        # sortable lexicographically, no `T` or `Z` ceremony.
        ts = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        self.path = self.log_dir / f"cyberdeck-{ts}.log"
        # Line-buffered so events flush as they happen (a crash
        # between flushes would otherwise lose the last few lines —
        # exactly the events the maintbot needs to triage).
        self._fp = open(self.path, "w", encoding="utf-8", buffering=1)
        # Subscription handle, set by attach_to_bus(). Held so close()
        # can unsubscribe cleanly even after bus shutdown.
        # Both must be set BEFORE _write_header — the header write
        # path goes through _safe_write which reads self._closed.
        self._subscription: Optional[Any] = None
        self._closed = False
        # Slice 2 of the safety architecture pass: a callable that
        # returns the current blacklist entries. Called once on
        # close() to write a structured `blacklist_summary` event
        # before the footer. None means no dump (e.g. headless test
        # contexts without a Watchdog). Per the netrunner's ask
        # 2026-04-30 (late) — accumulating blacklist entries across
        # sessions in the per-launch logs gives us cross-run signal
        # for "which fingerprints recur, which are session-unique."
        self._blacklist_provider = blacklist_provider
        self._update_latest_pointer()

        # Header line — self-describing so any single shared file can
        # be triaged without external context. argv + env snapshot
        # answer "how was the deck launched?"; deck_version + python +
        # OS answer "in what environment?"; brake + home_dir answer
        # "with what runtime config?".
        self._write_header(
            argv=argv if argv is not None else list(sys.argv),
            env_snapshot=env_snapshot or {},
            deck_version=deck_version,
            brake_label=brake_label,
            home_dir=home_dir,
        )

    # ---- file management --------------------------------------------

    def _update_latest_pointer(self) -> None:
        """Update `<log_dir>/latest.log` to point at the current file.

        Symlink first (atomic on Linux/macOS); falls back to a copy on
        Windows where symlinks require admin privilege. Best-effort —
        if we can't update the pointer for any reason, the per-launch
        file is still the source of truth and the netrunner can find
        it by timestamp.
        """
        latest = self.log_dir / "latest.log"
        try:
            if latest.exists() or latest.is_symlink():
                latest.unlink()
        except OSError:
            return  # pointer is stuck; per-launch file still authoritative
        try:
            latest.symlink_to(self.path.name)
            return
        except (OSError, NotImplementedError):
            # Windows without dev mode / admin — fall through to copy.
            pass
        try:
            shutil.copy2(self.path, latest)
        except OSError:
            pass

    def _write_header(
        self,
        *,
        argv: list[str],
        env_snapshot: dict[str, str],
        deck_version: str,
        brake_label: str,
        home_dir: Optional[Path],
    ) -> None:
        """Emit a self-describing first line so a shared log file can
        be triaged without out-of-band context."""
        header = {
            "type": "log_header",
            "ts": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "deck_version": deck_version,
            # Deck PID. The Mechanic supervisor reads this to learn
            # which process to watch — sidesteps having to pass the
            # PID over argv, and survives any future scenario where a
            # supervisor attaches mid-flight from `latest.log`. Read
            # by `mechanic.py` v0.
            "pid": os.getpid(),
            "argv": argv,
            "env": env_snapshot,
            "brake": brake_label,
            "home_dir": str(home_dir) if home_dir else None,
            "python": sys.version.split()[0],
            "platform": f"{platform.system()} {platform.release()}",
            "log_level": self.level,
            "log_path": str(self.path),
        }
        self._safe_write(header)

    def _safe_write(self, record: dict) -> None:
        """Write one NDJSON line. Disk / encoding errors are caught
        and dropped — the deck must keep running even if the log
        partition fills up or the file gets locked by an external
        viewer."""
        if self._fp is None or self._closed:
            return
        try:
            self._fp.write(json.dumps(record, default=str) + "\n")
        except (OSError, TypeError, ValueError):
            pass

    # ---- bus integration --------------------------------------------

    def attach_to_bus(self, bus: Any) -> None:
        """Subscribe to the bus with no kind filter — every event
        meeting the severity threshold gets written. Severity filter
        runs in the callback rather than as a bus-side filter so we
        get one centralized cutoff that's easy to adjust at runtime
        if we ever want a `--log-level=debug` toggle from inside the
        deck."""
        if self._subscription is not None or self._closed:
            return
        self._subscription = bus.subscribe(
            self,  # __call__ is the subscriber
            name="deck_logger",
        )

    def __call__(self, event: Any) -> None:
        """Bus callback. Filter by severity, serialize, write."""
        if self._closed or self._fp is None:
            return
        # Severity gate. CRITICAL bypasses the threshold so urgent
        # events always land in the file regardless of the configured
        # level — the netrunner/maintbot needs to see those even if
        # the deck was running at warning+ for noise control.
        rank = _SEVERITY_RANKS.get(
            getattr(event, "severity", "info"), 1,
        )
        if rank < self._level_rank and event.severity != "critical":
            return
        record = {
            "ts": getattr(event, "timestamp", time.time()),
            "kind": getattr(event, "kind", "unknown"),
            "source": getattr(event, "source", ""),
            "construct_id": getattr(event, "construct_id", None),
            "severity": getattr(event, "severity", "info"),
            "text": getattr(event, "text", None),
            "payload": _serialize_payload(getattr(event, "payload", None)),
        }
        self._safe_write(record)

    # ---- shutdown ---------------------------------------------------

    def close(self, *, reason: str = "shutdown") -> None:
        """Write a closing footer and close the file handle.
        Idempotent — safe to call multiple times.

        `reason` distinguishes intentional close shapes:
          - "shutdown": clean exit
          - "eject": deliberate halt via Ctrl+F
          - "crash": uncaught exception (caller decides)
        The future heartbeat sensor + maintbot use this field to
        classify whether to fire triage on next launch.
        """
        if self._closed:
            return
        # Unsubscribe BEFORE writing footer — otherwise a final
        # in-flight bus event could land between footer-write and
        # file-close and look like activity AFTER the clean
        # shutdown marker.
        try:
            if self._subscription is not None:
                self._subscription.unsubscribe()
        except Exception:
            pass
        # Slice 2 of the safety architecture pass: dump the session's
        # blacklist before the footer. This persists the netrunner's
        # session-scoped blacklist into the per-launch log so cross-
        # run analysis can see "which fingerprints fired across many
        # sessions vs. which were one-offs." Best-effort: provider
        # errors don't break shutdown; missing provider just skips
        # the dump.
        if self._blacklist_provider is not None:
            try:
                entries = self._blacklist_provider() or []
                # Walk each entry through _serialize_payload so
                # dataclass shapes (BlacklistEntry) become plain
                # dicts. Same path used by the bus-event writer above
                # — keeps the on-disk shape consistent.
                serialized = [
                    _serialize_payload(e, depth=0) for e in entries
                ]
                self._safe_write({
                    "type": "blacklist_summary",
                    "ts": time.time(),
                    "iso": datetime.now().isoformat(timespec="seconds"),
                    "count": len(serialized),
                    "entries": serialized,
                })
            except Exception:
                # Don't let blacklist serialization failures block the
                # footer write. Silent on purpose — the netrunner can
                # still see the blacklist via the live bus events
                # logged earlier in the session.
                pass
        # Footer line — the marker downstream consumers (heartbeat,
        # maintbot) look for to distinguish clean from unclean exit.
        # Write it via _safe_write so the disk-error guard still
        # applies, then flip _closed AFTER (otherwise _safe_write
        # short-circuits on the closed flag and the footer is lost —
        # bug caught during phase 7a smoke).
        footer = {
            "type": "log_footer",
            "ts": time.time(),
            "iso": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
        }
        self._safe_write(footer)
        self._closed = True
        try:
            if self._fp is not None:
                self._fp.close()
        except OSError:
            pass
        self._fp = None
