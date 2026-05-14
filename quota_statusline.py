"""
quota_statusline.py — Claude Code statusLine hook for quota capture.

Build-plan item 13 (2026-05-11). Wired into every deck-spawned
claude subprocess via the `statusLine` block in the per-spawn
settings JSON (see brake_state.make_spawn_settings). Claude Code
invokes this script after each turn's first model call, passing
session context as JSON on stdin. We extract the rate_limits
fields and write them to `<home>/.cyberdeck/quota.json` (or the
path passed as argv[1]); the daemon reads that file each turn
to make quota-aware caliber decisions.

Architecture rationale (per item 13 design):
  - Anthropic doesn't expose rate limits through the API directly;
    they only surface via Claude Code's statusLine mechanism. So
    we piggyback on it.
  - Every claude subprocess the deck spawns gets the same statusLine
    config (shared settings.json path stays cache-stable). Each
    subprocess's first model call fires statusLine, which updates
    quota.json. Latest-writer-wins; concurrent writers are handled
    by atomic-rename (write to .tmp, rename over).
  - The script ALSO echoes a brief one-line status to stdout —
    Claude Code requires the statusLine command to return something
    for display. Our output is human-readable but the deck doesn't
    consume it (deck reads the JSON file, not the stdout).

Cold-start: rate_limits aren't populated until AFTER the first
model call in a session. The first deck-spawn that runs a model
call writes the first quota.json. Pool warmers do this naturally
at deck startup; by the time the daemon decomposes a goal,
quota.json typically exists.

stdin shape (per Anthropic docs as of 2026-05; may evolve):
  {
    "session_id": "...",
    "model": {...},
    "transcript_path": "...",
    "rate_limits": {
      "five_hour": {"used_percentage": 47, "resets_at": "...", ...},
      "seven_day": {"used_percentage": 12, "resets_at": "...", ...}
    },
    ... (other context fields we ignore)
  }

Tolerant parsing: missing rate_limits block, unexpected shapes,
non-numeric percentages — all produce a no-op exit (don't write
the file; emit a minimal stdout line so Claude Code is happy).
Defensive throughout: this script runs inside every claude
subprocess; a crash here would surface as "Claude Code statusline
script failed" noise the netrunner doesn't need.

Usage:
  python quota_statusline.py [output_path]

If `output_path` is omitted, falls back to:
  $CYBERDECK_QUOTA_PATH (env var)
  → $CYBERDECK_HOME/.cyberdeck/quota.json
  → <this script>/cyberdeck-home/.cyberdeck/quota.json

The env-var override is the Pi tmpfs path
(`/dev/shm/cyberdeck-quota.json`) to avoid SD-card wear on
long-running deployments where statusLine writes every model call.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional


def _resolve_output_path(argv_path: Optional[str]) -> Path:
    """Pick where to write quota.json.

    Priority (highest first):
      1. argv[1]                 — explicit path from the settings.json
                                   command line; deck-installed default
      2. $CYBERDECK_QUOTA_PATH   — env-var override (Pi tmpfs case)
      3. $CYBERDECK_HOME/.cyberdeck/quota.json — fallback for ad-hoc runs
      4. <script_dir>/cyberdeck-home/.cyberdeck/quota.json — final fallback
    """
    if argv_path:
        return Path(argv_path)
    env_explicit = os.environ.get("CYBERDECK_QUOTA_PATH")
    if env_explicit:
        return Path(env_explicit)
    env_home = os.environ.get("CYBERDECK_HOME")
    if env_home:
        return Path(env_home) / ".cyberdeck" / "quota.json"
    return (
        Path(__file__).resolve().parent
        / "cyberdeck-home" / ".cyberdeck" / "quota.json"
    )


def _read_stdin_json() -> Optional[dict]:
    """Read all of stdin and parse as JSON. Returns None on parse
    failure or empty input — defensive against Claude Code passing
    unexpected shapes or no input at all (some session types may
    skip statusLine input)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return None
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _extract_rate_limits(payload: dict) -> Optional[dict]:
    """Pull the rate_limits block out of Claude Code's session payload.

    Returns a dict with `five_hour` and `seven_day` keys (each a small
    dict with `used_percentage` + `resets_at`), or None if the payload
    doesn't include usable rate-limit data.

    Tolerant: missing block, missing fields, non-numeric values all
    produce None. Only writes when there's something actually useful.
    """
    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    out: dict = {}
    for window_key in ("five_hour", "seven_day"):
        block = rate_limits.get(window_key)
        if not isinstance(block, dict):
            continue
        pct = block.get("used_percentage")
        # Accept int / float / numeric string. Reject anything else.
        if isinstance(pct, bool):
            # bool is technically int in Python; explicit reject.
            continue
        elif isinstance(pct, (int, float)):
            pct_value: Optional[float] = float(pct)
        elif isinstance(pct, str):
            try:
                pct_value = float(pct)
            except ValueError:
                continue
        else:
            continue

        # Clamp to plausible range. Out-of-range values are ignored
        # (probably a shape change upstream); don't write garbage.
        if pct_value < 0 or pct_value > 100:
            continue

        resets_at = block.get("resets_at")
        if not isinstance(resets_at, str):
            resets_at = ""

        out[window_key] = {
            "used_percentage": pct_value,
            "resets_at": resets_at,
        }

    if not out:
        return None
    return out


def _atomic_write_json(path: Path, payload: dict) -> bool:
    """Write `payload` as JSON to `path` atomically.

    Strategy: write to a sibling `.tmp` file, then rename over the
    target. POSIX rename is atomic; Windows rename-over-existing is
    atomic on NTFS since Vista. Concurrent writers (multiple claude
    subprocesses' statusLine scripts firing simultaneously) race for
    the rename, but each .tmp uses a per-PID suffix to avoid clobbers
    on the staging file. Latest writer wins on the target — that's
    what we want for "freshest quota."

    Returns True on success, False on any I/O error (silently — the
    script's job is best-effort; deck handles missing quota.json as
    "pre-first-call" state).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    pid = os.getpid()
    tmp_path = path.with_name(f"{path.name}.{pid}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # fsync not supported on every filesystem (e.g. tmpfs
                # in some configs); harmless if it fails.
                pass
        os.replace(tmp_path, path)
        return True
    except OSError:
        # Clean up the .tmp if we left one around.
        try:
            tmp_path.unlink()
        except OSError:
            pass
        return False


def _emit_status_line(rate_limits: Optional[dict]) -> None:
    """Echo a brief human-readable status line on stdout.

    Claude Code's statusLine mechanism uses our stdout for the
    rendered status display. The deck reads quota.json (not our
    stdout), so this is purely for Claude Code's own UI when the
    netrunner happens to be looking at the claude subprocess
    directly. Most of the time they aren't — they're looking at
    the deck — so this stays minimal.

    Format examples:
      no quota → "cyberdeck-quota: no data"
      rate_limits → "cyberdeck-quota: 5h=47% 7d=12%"
    """
    if rate_limits is None:
        sys.stdout.write("cyberdeck-quota: no data\n")
        return
    parts = []
    fh = rate_limits.get("five_hour", {}).get("used_percentage")
    if isinstance(fh, (int, float)):
        parts.append(f"5h={fh:.0f}%")
    sd = rate_limits.get("seven_day", {}).get("used_percentage")
    if isinstance(sd, (int, float)):
        parts.append(f"7d={sd:.0f}%")
    if parts:
        sys.stdout.write(f"cyberdeck-quota: {' '.join(parts)}\n")
    else:
        sys.stdout.write("cyberdeck-quota: no data\n")


def main(argv: list[str]) -> int:
    """Main entry. argv[0] is the script name; argv[1] (optional) is
    the output path. Always returns 0 — the statusLine command
    failing would surface as a Claude Code error the netrunner
    doesn't need to see."""
    output_path = _resolve_output_path(
        argv[1] if len(argv) > 1 else None,
    )
    payload = _read_stdin_json()
    if payload is None:
        # No usable stdin — could be a session type that skips the
        # statusLine input. Emit a minimal stdout line so Claude
        # Code's render is happy, don't touch quota.json.
        _emit_status_line(None)
        return 0

    rate_limits = _extract_rate_limits(payload)
    if rate_limits is None:
        # Payload didn't include rate_limits — common before the
        # first model call within a session, or for session types
        # that don't carry rate-limit data. Don't write the file.
        _emit_status_line(None)
        return 0

    captured_at = _iso_now()
    quota_payload = {
        "five_hour": rate_limits.get("five_hour"),
        "seven_day": rate_limits.get("seven_day"),
        "captured_at": captured_at,
        # Schema-versioning for forward-compat. quota_reader pins
        # to version 1; future schema changes bump this and the
        # reader adopts new shapes.
        "schema_version": 1,
    }
    _atomic_write_json(output_path, quota_payload)
    _emit_status_line(rate_limits)
    return 0


def _iso_now() -> str:
    """ISO8601 UTC timestamp with seconds precision. Stable + sortable
    across all platforms (no Windows-specific quirks)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
