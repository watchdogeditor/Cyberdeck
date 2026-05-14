"""
quota_reader.py — deck-side reader for the quota signal.

Build-plan item 13 (2026-05-11). Companion to `quota_statusline.py`
(the writer). The writer is a Claude Code statusLine command invoked
inside every deck-spawned claude subprocess; the reader runs deck-
side and exposes the latest known quota for daemon decisions +
sidebar surfacing.

Read-time only — no background watching. The daemon calls
`load(home_dir)` per turn in `_format_outcomes`; the sidebar
re-reads on its existing refresh cadence. Cheap (sub-millisecond
JSON parse of a tiny file); no need for caching.

Stale detection: the daemon flags quota readings older than
DEFAULT_STALE_THRESHOLD_SECONDS as "stale" in its per-turn user
message ("QUOTA: ... (stale by 45 min)"). The daemon decides what
to do with stale data; the reader just labels it.

Cold-start: quota.json doesn't exist until the first deck-spawned
claude subprocess's statusLine fires. Pool warmers handle this at
deck startup as a side effect. If the daemon's first turn fires
before any spawn has happened, `load()` returns None and the
daemon's user message omits the QUOTA: line entirely (graceful
degradation; pre-phase-13 behavior).

Public surface:
  QuotaWindow    Frozen dataclass: used_percentage + resets_at + window_name.
  QuotaSnapshot  Frozen dataclass: five_hour + seven_day + captured_at + path + age_seconds + is_stale.
  load(home_dir, *, stale_threshold_seconds=DEFAULT) -> Optional[QuotaSnapshot]
  format_for_daemon(snapshot) -> str  # the QUOTA: line for _format_outcomes
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Stale threshold: 60 minutes. Within the 5h window's granularity
# (rate limits don't tick every minute; they update on each model
# call) but tight enough that "no spawn has happened in an hour"
# is a real signal worth flagging to the daemon. Tunable per-call
# if the netrunner wants tighter / looser.
DEFAULT_STALE_THRESHOLD_SECONDS: float = 3600.0


@dataclass(frozen=True)
class QuotaWindow:
    """One rate-limit window (5h or 7d).

    `used_percentage` is what Claude Code reports — 0-100 inclusive.
    `resets_at` is the ISO8601 string Claude Code provides; deck
    doesn't parse it today (display-only), but stored for future use
    (e.g. "you have X hours until 5h window resets" UX).
    `window_name` is informational ("five_hour" / "seven_day") — useful
    for log lines.
    """
    used_percentage: float
    resets_at: str
    window_name: str


@dataclass(frozen=True)
class QuotaSnapshot:
    """A single load() result.

    `five_hour` and `seven_day` are the two rate-limit windows. Either
    may be None if the captured payload didn't include that window
    (cold-start edge case, or shape changes upstream).

    `captured_at` mirrors the writer's timestamp — the moment the
    statusLine script ran. `path` is where it was loaded from; useful
    for diagnostics ("quota at /dev/shm vs cyberdeck-home").

    `age_seconds` is computed at load() time: time.time() - file mtime.
    `is_stale` is True when age_seconds exceeds the stale threshold —
    daemon flags this in its per-turn user message.
    """
    five_hour: Optional[QuotaWindow]
    seven_day: Optional[QuotaWindow]
    captured_at: str
    path: Path
    age_seconds: float
    is_stale: bool


def _resolve_quota_path(home_dir: Path) -> Path:
    """Pick where to read quota.json from.

    Mirrors quota_statusline._resolve_output_path priority:
      1. $CYBERDECK_QUOTA_PATH  — env override (Pi tmpfs)
      2. <home>/.cyberdeck/quota.json — standard path
    """
    env_explicit = os.environ.get("CYBERDECK_QUOTA_PATH")
    if env_explicit:
        return Path(env_explicit)
    return Path(home_dir) / ".cyberdeck" / "quota.json"


def _parse_window(raw: Optional[dict], window_name: str) -> Optional[QuotaWindow]:
    """Parse one rate-limit window dict from quota.json into a
    QuotaWindow. Tolerant: returns None on missing / malformed
    content; logs nothing (quota.json is best-effort throughout)."""
    if not isinstance(raw, dict):
        return None
    pct = raw.get("used_percentage")
    if isinstance(pct, bool):
        return None
    if not isinstance(pct, (int, float)):
        return None
    pct_value = float(pct)
    if pct_value < 0 or pct_value > 100:
        return None
    resets_at = raw.get("resets_at")
    if not isinstance(resets_at, str):
        resets_at = ""
    return QuotaWindow(
        used_percentage=pct_value,
        resets_at=resets_at,
        window_name=window_name,
    )


def load(
    home_dir: Path,
    *,
    stale_threshold_seconds: float = DEFAULT_STALE_THRESHOLD_SECONDS,
) -> Optional[QuotaSnapshot]:
    """Load the current quota signal from disk.

    Returns None when:
      - quota.json doesn't exist yet (cold start, before any
        deck-spawned claude subprocess has fired its statusLine)
      - The file is unreadable / malformed
      - The file has neither rate-limit window in usable shape

    Otherwise returns a QuotaSnapshot with whatever windows are
    available. Either window may be None individually; if both are
    None we return None (no useful signal).

    Stale flagging: `is_stale` is True when the file's mtime is more
    than `stale_threshold_seconds` ago. The daemon uses this to
    flag the QUOTA: line in its user message; it doesn't gate the
    return value.
    """
    path = _resolve_quota_path(home_dir)
    if not path.is_file():
        return None

    try:
        mtime = path.stat().st_mtime
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None

    try:
        data = json.loads(content)
    except (json.JSONDecodeError, ValueError):
        return None

    if not isinstance(data, dict):
        return None

    five_hour = _parse_window(data.get("five_hour"), "five_hour")
    seven_day = _parse_window(data.get("seven_day"), "seven_day")
    if five_hour is None and seven_day is None:
        return None

    captured_at = data.get("captured_at")
    if not isinstance(captured_at, str):
        captured_at = ""

    age_seconds = max(0.0, time.time() - mtime)
    is_stale = age_seconds > stale_threshold_seconds

    return QuotaSnapshot(
        five_hour=five_hour,
        seven_day=seven_day,
        captured_at=captured_at,
        path=path,
        age_seconds=age_seconds,
        is_stale=is_stale,
    )


def format_for_daemon(snapshot: Optional[QuotaSnapshot]) -> str:
    """Render the QUOTA: line to inject into the daemon's per-turn
    user message via `_format_outcomes`.

    Returns "" when snapshot is None — caller should then omit the
    line entirely (graceful degradation, daemon proceeds with no
    quota awareness).

    Format:
      QUOTA: 5h=47% 7d=12%
      QUOTA: 5h=47% 7d=12% (stale by 45 min)
      QUOTA: 5h=47% (7d unavailable)

    The daemon's QUOTA AWARENESS section in its system prompt
    explains the policy (>75% → tier down on caliber; >90% →
    refuse non-essential spawns). The user-message QUOTA: line
    provides the current values for that policy to apply against.
    """
    if snapshot is None:
        return ""

    parts: list[str] = []
    if snapshot.five_hour is not None:
        parts.append(f"5h={snapshot.five_hour.used_percentage:.0f}%")
    elif snapshot.seven_day is not None:
        parts.append("(5h unavailable)")
    if snapshot.seven_day is not None:
        parts.append(f"7d={snapshot.seven_day.used_percentage:.0f}%")
    elif snapshot.five_hour is not None:
        parts.append("(7d unavailable)")

    if not parts:
        return ""

    line = f"QUOTA: {' '.join(parts)}"
    if snapshot.is_stale:
        # Round to nearest minute for a human-friendly stale tag.
        # 60s+ → "stale by N min"; sub-60s shouldn't trigger stale
        # at default threshold but we handle it gracefully if a
        # caller passes a tighter threshold for testing.
        minutes = max(1, int(snapshot.age_seconds / 60))
        line += f" (stale by {minutes} min)"

    return line
