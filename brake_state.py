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

        Preserves any other top-level keys in state.json (e.g. the
        'limits' namespace populated by save_limits) so the brake
        save doesn't clobber sibling state. Read-merge-write
        pattern; safe against concurrent writers because the deck
        is single-writer per home dir.
        """
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            existing = {}
            if self.state_path.is_file():
                try:
                    with self.state_path.open("r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        existing = loaded
                except (OSError, json.JSONDecodeError):
                    # Corrupted file — overwrite cleanly. The brake
                    # is the only key we care about preserving on a
                    # corrupt read; sibling namespaces are best-effort.
                    pass
            existing["brake"] = self._state.value
            with self.state_path.open("w", encoding="utf-8") as f:
                json.dump(existing, f, indent=2)
        except OSError as exc:
            print(
                f"brake_state: could not save {self.state_path}: "
                f"{exc!r} — state will not persist across restart",
                file=sys.stderr,
            )


# ---- limits persistence ----------------------------------------------------
#
# Phase 1.5 of the safety architecture pass (filed 2026-05-01 after
# real-deck friction): runtime tunables set via the Limits modal —
# `delay_window_seconds`, `wedge_timeout_seconds` — should survive
# deck restarts. Without this, the netrunner has to re-set the delay
# every launch, and a one-off restart (EJECT, crash, manual quit) loses
# the configuration entirely.
#
# Lives in the same `state.json` file as brake to keep the deck-global
# config surface in one place. Schema:
#
#   {
#     "brake": "default",
#     "limits": {
#       "delay_window_seconds": 10.0,
#       "wedge_timeout_seconds": 30.0,
#       ... (future tunables go here)
#     }
#   }
#
# Free functions rather than a sibling class because the limits don't
# need a listener fan-out — the Limits modal calls save_limits()
# directly when it commits, and the App reads load_limits() once at
# startup. Keeping the surface tiny lets future tunables join the
# `limits` dict without designing a generic store.


def load_limits(state_path: Path) -> dict:
    """Read the limits namespace from state.json. Returns an empty
    dict if the file is missing, unreadable, malformed, or doesn't
    have a 'limits' key — caller falls back to whatever defaults the
    App was constructed with.

    Best-effort: limits are non-essential for boot. A corrupt file
    means "use defaults," not "refuse to launch."
    """
    state_path = Path(state_path)
    if not state_path.is_file():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        limits = data.get("limits")
        return limits if isinstance(limits, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"brake_state: could not load limits from {state_path}: "
            f"{exc!r} — using App defaults for this session",
            file=sys.stderr,
        )
        return {}


def save_limits(state_path: Path, **values) -> None:
    """Update the limits namespace in state.json. Reads-merges-writes
    so the brake key + any future siblings survive. `values` are
    written into the limits dict; pass only the keys the netrunner
    just changed (caller decides — full overwrite vs delta is up to
    them; this function just does an in-place dict.update).

    Best-effort: failures log to stderr but never raise. The in-memory
    values still apply for the current session; the next successful
    save retries.
    """
    state_path = Path(state_path)
    if not values:
        return
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if state_path.is_file():
            try:
                with state_path.open("r", encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                # Same handling as BrakeStateStore._save: overwrite
                # cleanly on corrupt read. Limits are best-effort.
                pass
        limits = existing.get("limits")
        if not isinstance(limits, dict):
            limits = {}
        limits.update(values)
        existing["limits"] = limits
        with state_path.open("w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
    except OSError as exc:
        print(
            f"brake_state: could not save limits to {state_path}: "
            f"{exc!r} — values will not persist across restart",
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
    delay_window_seconds: float = 0.0,
    fast_mode: bool = False,
) -> Optional[Path]:
    """Generate (or reuse) a `claude --settings` JSON file for a spawn.

    Returns the path to the file, or None if no settings are needed
    (YOLO brake AND no delay window AND no fast_mode — the deck
    installs no hook, claude runs unrestricted save for
    --permission-mode).

    `construct_id` is accepted for backwards-compatibility with the
    callsite signature but is NO LONGER used in the file path or in
    the hook command. As of 2026-05-02 (cache-cost fix) the spawn
    settings file is STABLE across spawns — same path, same content,
    given the same brake + delay configuration. Per-spawn variation
    (which the hook still needs for tripwire deny_pending and delay_
    pending lookups) is resolved at hook-runtime via a session_id →
    construct_id mapping written by Fleet when the construct's first
    `system_init` event lands. See `write_session_cid_lookup` below
    and brake_hook.py's stdin-driven cid resolution.

    Why the change: Anthropic's prompt cache was missing ~30k tokens
    per spawn with `cache_miss_reason: 'system_changed'`. The most
    likely culprit is the per-spawn `--settings <cid>.json` flag —
    different path AND different content per spawn means Claude Code
    treats each spawn as a fresh setup, invalidating the cached
    system prompt portion. Stabilizing the settings file removes that
    drift surface. Architectural side benefit: cleaner separation
    between "what the deck tells claude about its config" (stable)
    and "what the deck does per-construct" (looked up at hook time).

    Same hook command for every spawn under a given (brake, delay)
    pair. Different (brake, delay) pairs produce different content,
    but those changes are infrequent (netrunner toggles brake/limits
    rarely vs. construct spawns). Within a stable window, the file
    content doesn't shift — Anthropic's cache should hit cleanly.

    YOLO never installs the hook. Period. YOLO is the live-fast-and-
    die brake — no hooks, no delays, no overrides. The earlier
    "YOLO + delay > 0 installs the hook for a pause-before-allowing
    window" branch was retired in the 2026-05-07 tripwires redesign:
    it forced X to be bidirectional (approve under default/paranoid,
    interrupt under YOLO), and the netrunner's spec is that X is
    unidirectional — always "allow this particular action to ignore
    the rules." Removing the YOLO+delay branch makes that consistent.
    """
    # Hook installs unless YOLO. With fast_mode=True we still need
    # a settings file under YOLO to set "fastMode": true (no CLI flag
    # exists for fast mode; settings.json is the only input surface),
    # but the file in that case carries ONLY the fastMode flag —
    # no hooks block.
    if brake == BrakeState.YOLO and not fast_mode:
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
    # Hook command shape (stable across spawns within a brake+delay
    # window): `python <hook> <brake> <delay_seconds>`. Construct_id
    # is NOT in this command — see docstring + brake_hook.py for the
    # session_id → cid runtime resolution path.
    settings: dict = {}
    # Hook block only when brake is non-YOLO; fast_mode alone doesn't
    # need the hook (it's just a behavioral flag).
    install_hook = brake != BrakeState.YOLO
    if install_hook:
        settings["hooks"] = {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                f'python "{hook_path_str}" '
                                f'{brake.value} {delay_window_seconds:g}'
                            ),
                        },
                    ],
                },
            ],
        }

    # Caliber Phase 2 (2026-05-04): fast_mode emission. Anthropic's
    # surface for fast mode is `"fastMode": true` in settings.json
    # OR the `/fast on|off` slash command — no CLI flag. So when
    # fast_mode=True, we set it here.
    if fast_mode:
        settings["fastMode"] = True

    cyberdeck_dir = Path(home_dir) / ".cyberdeck"
    try:
        cyberdeck_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"brake_state: could not create {cyberdeck_dir}: {exc!r} — "
            f"spawn will run without brake hook",
            file=sys.stderr,
        )
        return None

    # Path selection: cache-stable shared path for the common case;
    # per-spawn override file when fast_mode=True.
    #
    # The shared `spawn_settings.json` is what the cache-cost fix
    # (2026-05-02) stabilized — same path AND same content across
    # spawns under a given (brake, delay) config means Anthropic's
    # prompt cache hits cleanly. Adding a fastMode flag flips the
    # content, so a fast_mode=True spawn that wrote to the shared
    # file would invalidate cache for every subsequent fast_mode=
    # False spawn. To avoid that contamination, fast_mode spawns
    # write to a per-spawn override file (`<cid>.fastmode.json`)
    # that's NOT shared.
    #
    # Cache trade-off: fast_mode spawns pay a cache miss on
    # `--settings`. Acceptable: fast mode is the rare deliberate-
    # cost-for-speed lane; the netrunner already opted into 10x
    # cost for 2.5x speed, an additional ~30k token cache miss is
    # rounding error. Most spawns (fast_mode=False) keep the cache
    # warmth.
    if fast_mode:
        spawns_dir = cyberdeck_dir / "spawns"
        try:
            spawns_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"brake_state: could not create {spawns_dir}: {exc!r} — "
                f"falling back to shared settings (fastMode lost)",
                file=sys.stderr,
            )
            settings_path = cyberdeck_dir / "spawn_settings.json"
        else:
            settings_path = (
                spawns_dir / f"{construct_id}.fastmode.json"
            )
    else:
        # Stable path — same for every spawn under the same (brake,
        # delay) config. Content is idempotent for the same config;
        # if brake or delay flip, the next spawn rewrites the same
        # file with the new content. Multiple concurrent spawns
        # writing the same content are safe (last-writer-wins;
        # content is identical).
        settings_path = cyberdeck_dir / "spawn_settings.json"
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


def write_session_cid_lookup(
    home_dir: Path,
    session_id: str,
    construct_id: str,
) -> None:
    """Record the session_id → construct_id mapping the brake_hook
    needs to resolve its per-construct context (deny_pending /
    delay_pending file keys).

    Called from Fleet's _consume when a construct's `system_init`
    event lands, exposing the claude-side session_id. Before this,
    the hook's per-spawn argv carried the cid directly — which was
    causing per-spawn cache-key drift on the `--settings` flag and
    bleeding ~30k tokens of cache misses per spawn (filed 2026-05-02
    after real-deck cost analysis). Now: settings file is stable;
    cid is resolved at hook runtime via stdin's session_id + this
    lookup file.

    Best-effort — write failures don't break anything; the hook
    falls back to "no cid known" and skips the deny_pending /
    delay_pending checks (degraded mode, but the brake patterns
    still apply).

    Lookup file lives at `<home>/.cyberdeck/spawns/<session_id>.cid`
    (one tiny text file per active session). Cleaned up by
    `cleanup_session_cid_lookup` on construct finalize.
    """
    if not session_id or not construct_id:
        return
    spawns_dir = Path(home_dir) / ".cyberdeck" / "spawns"
    try:
        spawns_dir.mkdir(parents=True, exist_ok=True)
        path = spawns_dir / f"{session_id}.cid"
        path.write_text(construct_id, encoding="utf-8")
    except OSError as exc:
        print(
            f"brake_state: could not write session lookup "
            f"{session_id} → {construct_id}: {exc!r}",
            file=sys.stderr,
        )


def cleanup_session_cid_lookup(
    home_dir: Path,
    session_id: Optional[str],
) -> None:
    """Remove the session_id → cid lookup file. Idempotent — silent
    if the file doesn't exist or `session_id` is None. Called from
    Fleet's _consume on construct finalize.

    Failures are swallowed: a stale lookup file is harmless (next
    launch can purge the spawns dir at startup if desired) and we
    don't want cleanup errors bubbling into the construct
    finalization path.
    """
    if not session_id:
        return
    path = Path(home_dir) / ".cyberdeck" / "spawns" / f"{session_id}.cid"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_spawn_settings(settings_path: Optional[Path]) -> None:
    """Idempotent — silent if the file is already gone, `settings_
    path` is None, or the path points at the new stable shared
    spawn_settings.json (which lives across spawns and must NOT be
    cleaned up — see make_spawn_settings docstring for the cache-
    cost rationale).

    Pre-2026-05-02 behavior: per-spawn settings files (`<cid>.json`)
    got deleted here on construct finalize. Post-cache-fix: the
    settings file is shared across all spawns; deleting it on each
    finalize would force every NEW spawn to re-write it, causing
    spurious mtime updates and (more importantly) potentially racing
    with concurrent spawns reading the file mid-rewrite.

    Backwards-compat: still cleans up legacy per-cid files if any
    remain on disk from a pre-fix session that crashed mid-run
    without finalizing.

    Failures are swallowed: a stale file is unhelpful but not
    harmful, and we don't want cleanup errors to bubble up into the
    construct finalization path (where they'd trip up Fleet shutdown).
    """
    if settings_path is None:
        return
    # Guard the stable shared file. Filename is the unambiguous test
    # — it sits in <home>/.cyberdeck/, NOT under <home>/.cyberdeck/
    # spawns/ where the legacy per-cid files lived.
    if settings_path.name == "spawn_settings.json":
        return
    try:
        settings_path.unlink(missing_ok=True)
    except OSError:
        pass
