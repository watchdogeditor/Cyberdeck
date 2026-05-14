"""
Preferences — typed accessor over <home>/.cyberdeck/state.json.

Build-plan item 0b (filed 2026-05-03; shipped 2026-05-04).

Today's persistent settings are scattered across two function pairs in
brake_state.py:
  - `BrakeStateStore.load() / save()` — current brake tier
  - `load_limits(state_path) / save_limits(state_path, **kw)` — runtime
    tunables (delay_window_seconds, wedge_timeout_seconds, fast_mode,
    daemon_effort)

Both write to the same `state.json` file, but the API surface scales
linearly with each new setting. Future settings (theme, default
profile, keybind overrides, agent defaults) need a unified home and
an ergonomic accessor.

This module is the thin wrapper. It does NOT change persistence
semantics — internally it delegates to brake_state's load_limits /
save_limits — but it gives the App and modals a single import
surface with typed properties + clear schema documentation.

Schema:

    {
      "brake": "paranoid" | "default" | "yolo",
      "limits": {
        "delay_window_seconds": float,    # variable-outcome pause UX
        "wedge_timeout_seconds": float,   # post-stdout-close kill ceiling
        "fast_mode": bool,                # netrunner cost governor
        "daemon_effort": str,             # daemon power level
        "role_injection": bool,           # item 000 phase 2 — read role
                                          # prompts from <deck-source>/roles/
                                          # instead of in-code constants.
                                          # Default False; netrunner flips
                                          # on per launch to A/B verify.
        # ----- future placeholders (not yet wired) -----
        # "theme": str,                   # color theme name
        # "default_profile": str,         # auto-select profile at startup
        # "keybind_overrides": {key: action},
        # "agent_defaults": {...},        # per-agent caliber prefs etc.
        # "last_session_id": str,         # for the morgue when it lands
      }
    }

State file lives at `<home>/.cyberdeck/state.json` and is brake-hook
protected (constructs cannot write to <home>/.cyberdeck/ — see
`brake_hook.path_is_protected`). Reads stay allowed (an agent
inspecting current brake state isn't a problem; an agent CHANGING it
is).

Migration policy: existing callers of `brake_state.load_limits` /
`save_limits` keep working — those functions stay as-is for backward
compat, and Preferences delegates to them. New code should reach
for `Preferences(home_dir).foo` instead.

Public surface:
  Preferences(home_dir)
    .brake          → current brake string ("paranoid"/"default"/"yolo")
    .delay_window_seconds → float, default 0.0
    .wedge_timeout_seconds → float, default 30.0
    .fast_mode      → bool, default False
    .daemon_effort  → str, default "high"
    .role_injection → bool, default False (item 000 phase 2)
    .save(**kwargs) → write deltas to limits namespace
    .reload()       → invalidate the in-process cache; read fresh
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from brake_state import (
    BrakeState,
    load_limits as _load_limits,
    save_limits as _save_limits,
)


# Default values when a setting isn't present in state.json. Keep these
# in sync with the App's __init__ defaults — Preferences is meant to
# round-trip cleanly with the App, so a fresh install reads back the
# same values the App constructed with.
DEFAULT_DELAY_WINDOW_SECONDS: float = 0.0
DEFAULT_WEDGE_TIMEOUT_SECONDS: float = 30.0
DEFAULT_FAST_MODE: bool = False
DEFAULT_DAEMON_EFFORT: str = "high"
# Item 000 phase 2 (2026-05-11): default OFF for first ship. Netrunner
# flips on per-launch to A/B verify the role-file path before we
# consider flipping the default. When False, every spawn site uses the
# in-code system-prompt constants (current behavior, unchanged). When
# True, daemon / watchdog Q&A / watchdog tripwire-authoring / advisor
# all read their system prompts from <deck-source>/roles/*.md +
# general.toml. Constructs + Mechanic stay in code regardless.
DEFAULT_ROLE_INJECTION: bool = False


# Header comment that gets dropped into state.json on first save IF
# the file doesn't yet have one. Marks the file as deck-owned so the
# netrunner sees the disclaimer if they go reading it. Best-effort —
# state.json is JSON, not TOML, so we can't drop a real comment line;
# instead we use a `_comment` key that consumers ignore. Annoying but
# unobtrusive.
_DECK_OWNED_NOTE = (
    "DECK-OWNED PREFERENCES — managed by preferences.py / "
    "brake_state.py. Manual edits survive restarts but may be "
    "overwritten on the next setting change. Edit at your own risk."
)


class Preferences:
    """Typed accessor for persistent deck preferences.

    Lifecycle:
        prefs = Preferences(home_dir)
        prefs.fast_mode             # bool
        prefs.save(fast_mode=True)  # writes through to state.json
        prefs.fast_mode             # True (cache invalidated on save)

    The object is stateless beyond a small in-process cache; it's safe
    to construct multiple instances against the same home_dir. The
    cache only survives until the next save() / reload() — read-after-
    write is consistent.
    """

    def __init__(self, home_dir: Path) -> None:
        self.home_dir = Path(home_dir)
        self.state_path = self.home_dir / ".cyberdeck" / "state.json"
        self._limits_cache: Optional[dict] = None

    # ---- read accessors ---------------------------------------------------

    def _limits(self) -> dict:
        """Load + cache the limits namespace. Cache invalidated on
        save() / reload(). Best-effort: returns {} on read errors so
        callers always see a dict (downstream `.get()` calls work
        regardless of file state)."""
        if self._limits_cache is None:
            self._limits_cache = _load_limits(self.state_path)
        return self._limits_cache

    @property
    def brake(self) -> Optional[BrakeState]:
        """Current brake state, or None if not yet persisted (fresh
        install). The App's BrakeStateStore is the canonical owner;
        this property reads through to its load() output for
        introspection / Limits-modal display.

        Note: setting brake goes through `BrakeStateStore.set(state)`,
        not Preferences.save() — brake mutation has its own bus event
        flow that listeners depend on. This property is read-only on
        purpose."""
        try:
            import json
            if not self.state_path.is_file():
                return None
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            value = data.get("brake")
            if not isinstance(value, str):
                return None
            try:
                return BrakeState(value)
            except ValueError:
                return None
        except (OSError, json.JSONDecodeError):
            return None

    @property
    def delay_window_seconds(self) -> float:
        v = self._limits().get("delay_window_seconds")
        return (
            float(v) if isinstance(v, (int, float)) and v >= 0
            else DEFAULT_DELAY_WINDOW_SECONDS
        )

    @property
    def wedge_timeout_seconds(self) -> float:
        v = self._limits().get("wedge_timeout_seconds")
        return (
            float(v) if isinstance(v, (int, float)) and v >= 0
            else DEFAULT_WEDGE_TIMEOUT_SECONDS
        )

    @property
    def fast_mode(self) -> bool:
        v = self._limits().get("fast_mode")
        return v if isinstance(v, bool) else DEFAULT_FAST_MODE

    @property
    def daemon_effort(self) -> str:
        v = self._limits().get("daemon_effort")
        return v if isinstance(v, str) else DEFAULT_DAEMON_EFFORT

    @property
    def role_injection(self) -> bool:
        """Item 000 phase 2 (2026-05-11). When True, role-bearing
        spawn sites (daemon, watchdog Q&A, watchdog tripwire authoring,
        advisor) read their system prompts from <deck-source>/roles/
        instead of in-code constants. Construct + Mechanic stay in
        code regardless. Default False — netrunner flips on
        per-launch to A/B before we consider flipping the default.
        """
        v = self._limits().get("role_injection")
        return v if isinstance(v, bool) else DEFAULT_ROLE_INJECTION

    # ---- write surface ----------------------------------------------------

    def save(self, **kwargs: Any) -> None:
        """Write deltas to the limits namespace. Pass only the keys
        you're changing — the existing brake-state.save_limits handles
        read-merge-write internally so siblings survive.

        Invalidates the in-process cache so the next read picks up
        the fresh values.

        Best-effort: failures (read-only fs, permission denied) log to
        stderr via brake_state.save_limits but never raise. The
        in-memory state on the App is the authoritative current
        value; persistence is for next-launch carry-over.
        """
        if not kwargs:
            return
        _save_limits(self.state_path, **kwargs)
        self._limits_cache = None  # invalidate

    def reload(self) -> None:
        """Drop the in-process cache; next read fetches fresh from
        disk. Useful when an external process (the Limits modal in a
        nested screen, future Mechanic LLM-session, manual edit by
        the netrunner) might have changed the file."""
        self._limits_cache = None

    # ---- introspection ----------------------------------------------------

    def all_limits(self) -> dict:
        """Return a snapshot of all known limits as a plain dict.
        Useful for debug surfaces (Limits modal display, logging,
        future preferences-export feature). Values fall back to
        defaults for any missing keys, so the dict is always
        complete."""
        return {
            "delay_window_seconds": self.delay_window_seconds,
            "wedge_timeout_seconds": self.wedge_timeout_seconds,
            "fast_mode": self.fast_mode,
            "daemon_effort": self.daemon_effort,
            "role_injection": self.role_injection,
        }
