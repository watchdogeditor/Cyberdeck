"""brake_state.py — deck-global brake state.

Three levels: PARANOID / DEFAULT / YOLO. The brake gates what
constructs are permitted to do at runtime via Claude Code's PreToolUse
hook mechanism (see brake_hook.py + brake_patterns.py). This module
is the *state holder* — it loads, persists, and broadcasts changes;
it does NOT implement enforcement (that's the hook) or pattern
matching (that's brake_patterns).

Brake state is captured at construct spawn time and baked into that
construct's hook config. Mid-flight brake changes do NOT propagate to
already-running constructs — Claude Code subprocesses can't have
their --permission-mode or --settings mutated post-spawn. Changes
take effect on subsequent spawns only.

Persistence: <home>/.cyberdeck/state.json. Loaded at deck launch,
saved on every transition. Defaults to DEFAULT on first launch (file
absent or unreadable).

Listener pattern mirrors ConnectionMonitor: synchronous callbacks
fire on each transition. No-op transitions (set X when state is
already X) skip persist + listeners to avoid chatlog spam.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Optional


class BrakeState(Enum):
    """Three discrete brake levels. Ordering implied: PARANOID is most
    restrictive, YOLO is no restriction at all. The hook layer
    interprets these — this enum is just the symbol."""
    PARANOID = "paranoid"
    DEFAULT = "default"
    YOLO = "yolo"


@dataclass(frozen=True)
class BrakeChangeEvent:
    """Emitted when the brake state transitions.

    `reason` carries provenance — useful for the chatlog announcement
    so the netrunner can tell at a glance whether a change was their
    own action, a watchdog ratchet (when that lands), or a startup
    load. Today: 'netrunner' for modal-driven changes, 'startup' for
    initial load (NOT fired — load doesn't transition), reserved for
    future use otherwise.
    """
    old_state: BrakeState
    new_state: BrakeState
    reason: str = "netrunner"


# Synchronous on purpose — listeners are expected to be cheap (update
# a UI label, write a chatlog line, refresh hook config for the next
# spawn). If a listener needs real work, it should schedule it via
# asyncio.create_task itself.
BrakeListener = Callable[[BrakeChangeEvent], None]


class BrakeStateStore:
    """Deck-global brake state with persistence + listener fan-out.

    Single instance per deck. The TUI creates one at startup, calls
    load() once, registers listeners (sidebar indicator update,
    chatlog announcement, future: hook-config refresh trigger), and
    then mutates via set() when the netrunner submits the brake
    modal.

    Errors during load or save are logged to stderr but never raise —
    the deck stays usable even if state.json gets corrupted. The
    in-memory state is authoritative for the current session;
    persistence is best-effort.
    """

    # Hard default. Used when state.json is missing, unreadable, or
    # contains garbage. The netrunner can change it via the modal at
    # any time; this is just the cold-start value.
    DEFAULT_STATE: BrakeState = BrakeState.DEFAULT

    def __init__(
        self,
        state_path: Path,
        *,
        bus: Optional[object] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self._state: BrakeState = self.DEFAULT_STATE
        # Bus is now the only fan-out path (Phase 8 of the
        # unified-event-stream slice retired the legacy `on_change=`
        # callback + `_listeners` list + add_listener/remove_listener
        # shims). Object type rather than EventBus to avoid a circular
        # import — bus-as-duck-type is the standing pattern across
        # producers. None when running standalone (e.g. console tests
        # without a TUI to host the bus); set() silently no-ops the
        # publish in that case.
        self.bus = bus

    # ---- read API ----------------------------------------------------------

    @property
    def state(self) -> BrakeState:
        """Current brake state. Read-only property; mutate via set()."""
        return self._state

    # ---- lifecycle ---------------------------------------------------------

    def load(self) -> BrakeState:
        """Load state from disk. Returns the loaded value.

        Does NOT fire listeners — load is initialization, not a
        transition. If the file is missing, unreadable, or contains
        an unrecognized brake value, falls back to DEFAULT_STATE
        silently (warns to stderr but doesn't raise).
        """
        if not self.state_path.is_file():
            self._state = self.DEFAULT_STATE
            return self._state
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("brake") if isinstance(data, dict) else None
            if isinstance(raw, str):
                # BrakeState(raw) raises ValueError on unknown values —
                # caught below and downgraded to a warning.
                self._state = BrakeState(raw)
            else:
                self._state = self.DEFAULT_STATE
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(
                f"brake_state: could not load {self.state_path}: "
                f"{exc!r} — defaulting to {self.DEFAULT_STATE.value}",
                file=sys.stderr,
            )
            self._state = self.DEFAULT_STATE
        return self._state

    def set(
        self,
        new_state: BrakeState,
        *,
        reason: str = "netrunner",
    ) -> None:
        """Transition to `new_state`, persist, fire listeners.

        No-op if new_state equals current state — saves disk I/O and
        keeps the chatlog clean of duplicate transition lines if a
        netrunner submits the brake modal without actually changing
        the value.

        Persistence happens BEFORE bus publish so any subscriber that
        re-reads from disk (unlikely but possible) sees the new value.
        The bus's per-callback exception isolation handles subscriber
        errors — the producer doesn't need its own catch-and-log loop
        anymore (Phase 8 retired that legacy fan-out).
        """
        old_state = self._state
        if new_state == old_state:
            return
        self._state = new_state
        self._save()
        event = BrakeChangeEvent(
            old_state=old_state,
            new_state=new_state,
            reason=reason,
        )
        # Severity escalates with destination tier — yolo gets warning
        # so subscribers can react to "constructs are now unrestricted"
        # without inspecting payload state names. paranoid and default
        # stay info; both are tightening or staying-baseline
        # transitions.
        if self.bus is not None:
            try:
                from event_bus import DeckEvent
                severity = (
                    "warning" if new_state == BrakeState.YOLO else "info"
                )
                self.bus.publish(DeckEvent(
                    kind="brake.change",
                    source="brake_state",
                    severity=severity,
                    text=(
                        f"\\[brake] {old_state.value} → "
                        f"{new_state.value} ({reason})"
                    ),
                    payload=event,
                ))
            except Exception as exc:
                print(
                    f"brake_state: bus publish error: {exc!r}",
                    file=sys.stderr,
                )

    # ---- internals ---------------------------------------------------------

    def _save(self) -> None:
        """Persist current state. Creates parent dir if missing.
        Failures are logged but non-fatal — the in-memory state is
        still authoritative for the current session, and the next
        successful set() will retry the save.

        Schema is namespaced under a 'brake' key so other deck-global
        state can join state.json later without a format break.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump({"brake": self._state.value}, f, indent=2)
        except OSError as exc:
            print(
                f"brake_state: could not save {self.state_path}: "
                f"{exc!r} — state will not persist across restart",
                file=sys.stderr,
            )


# ---- spawn-settings generation ------------------------------------------------
#
# Each construct spawn that runs under PARANOID or DEFAULT brake gets
# a per-spawn claude --settings file pointing at brake_hook.py with
# the current brake passed as argv. The file is transient — written
# right before spawn, deleted right after the construct finalizes.
# YOLO brake skips this entirely (no hook = no gating).
#
# Files live at <home>/.cyberdeck/spawns/<construct_id>.json. The
# directory is auto-created. Cleanup is the caller's responsibility
# (Fleet._consume calls cleanup_spawn_settings on construct finalize)
# so a deck crash doesn't leak files indefinitely; if it does, the
# next launch can mass-purge the directory at startup.


def make_spawn_settings(
    brake: BrakeState,
    home_dir: Path,
    construct_id: str,
) -> Optional[Path]:
    """Generate a transient `claude --settings` JSON file for a spawn.

    Returns the path to the written file, or None if no settings are
    needed (YOLO brake — the deck installs no hook, claude runs
    unrestricted save for --permission-mode).

    The hook command is computed from this module's location: brake_hook.py
    sits next to brake_state.py in the deck source dir. The constructed
    JSON has a single PreToolUse hook with matcher "*" so every tool
    call routes through it; the hook itself is dumb and consults the
    brake state passed via argv to decide allow/deny.
    """
    if brake == BrakeState.YOLO:
        return None

    hook_path = Path(__file__).resolve().parent / "brake_hook.py"
    # Forward-slash + quoted path: Claude Code invokes hook commands
    # via a POSIX-style shell on every platform; on Windows that means
    # backslashes get eaten as escapes (verified on 2.1.118 — the path
    # `C:\Users\...\brake_hook.py` collapsed to
    # `<cwd>/UsersWatchdogDocumentsCyberdeckbrake_hook.py` when Python
    # tried to open it). Forward slashes + double quotes survives the
    # shell pass cleanly. Same fix the dispatcher uses elsewhere.
    hook_path_str = str(hook_path).replace("\\", "/")
    # Slice 2 of the safety architecture pass: construct_id is now
    # passed to brake_hook.py as a second argv arg. The hook reads
    # `<home>/.cyberdeck/spawns/<construct_id>.deny_pending.json`
    # written by the watchdog's TripwireEngine on warning/critical
    # fires — that's how tripwires get teeth (they extend brake by
    # writing per-construct flags the hook reads). Without this
    # arg, the hook can't identify which spawn it's enforcing
    # against, and tripwire-based denies wouldn't work.
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f'python "{hook_path_str}" '
                                f'{brake.value} {construct_id}'
                            ),
                        },
                    ],
                },
            ],
        },
    }

    spawns_dir = Path(home_dir) / ".cyberdeck" / "spawns"
    try:
        spawns_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"brake_state: could not create {spawns_dir}: {exc!r} — "
            f"spawn will run without brake hook",
            file=sys.stderr,
        )
        return None

    settings_path = spawns_dir / f"{construct_id}.json"
    try:
        settings_path.write_text(
            json.dumps(settings, indent=2), encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"brake_state: could not write {settings_path}: {exc!r} — "
            f"spawn will run without brake hook",
            file=sys.stderr,
        )
        return None

    return settings_path


def cleanup_spawn_settings(settings_path: Optional[Path]) -> None:
    """Remove a transient spawn-settings file. Idempotent — silent
    if the file is already gone or `settings_path` is None.

    Failures are swallowed: a stale file is unhelpful but not harmful,
    and we don't want cleanup errors to bubble up into the construct
    finalization path (where they'd trip up Fleet shutdown).
    """
    if settings_path is None:
        return
    try:
        settings_path.unlink(missing_ok=True)
    except OSError:
        pass
