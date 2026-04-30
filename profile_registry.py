"""
Profile registry: live, hot-reloading view of the profiles/ directory.

Watches a directory of .toml files and keeps an in-memory map of
{name → Profile}. Detects file additions, modifications, and deletions
and emits events so subscribers (the TUI's Tools tab, eventually the
session pool and dispatcher) can react.

Implementation note: polling, not OS notifications. The profiles dir
is small, edits are infrequent, and a 1-second poll has no practical
cost. It works identically across Windows / Linux / macOS / WSL with
no extra dependencies, and the failure modes are easy to reason about:
if the disk doesn't change, nothing fires; if the disk changes, the
next poll catches it.

Public surface:

  ProfileEvent       Notification of a single change.
  ProfileRegistry    The watcher itself; start() / shutdown() / get() / all().

C1b scope: load + watch + dispatch events. Does NOT inject addendums
into prompts (C1c construct-side, C1e daemon-side), does NOT pick which
profile is active for a construct (C1d pool integration), does NOT
expose a UI for picking (C1f).
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from profiles import (
    Profile,
    ProfileValidationError,
    load_profile,
)


# Canonical "default" profile content, seeded to disk if missing.
# Empty addendums + empty recommended_tools mean "use deck baseline
# as-is"; no behavioral change vs running with no profile at all.
# The file exists so the netrunner can EDIT it — tweak the addendum,
# add tool recommendations — and have those changes apply to every
# spawn that doesn't request something else.
#
# This constant is the HARD default. If the netrunner deletes
# default.toml entirely, the next scan re-seeds from this — so
# deletion resets to baseline rather than permanently disabling the
# default-profile mechanism.
#
# Note: profiles do NOT carry brake state. The deck-global brake
# (paranoid/default/yolo, see brake_state.py) is the single source
# of runtime constraint, controlled exclusively by the netrunner via
# the brake modal (`b`). Profiles are prescriptive templates; the
# brake is the enforcement layer.
DEFAULT_PROFILE_TOML: str = '''\
name = "default"
category = "General"
description = """
Baseline profile for general-purpose work. No specialization, no
addendums beyond the deck baseline. Used when no other profile fits
or when the netrunner explicitly wants the unsteered behavior.
"""

# Empty addendums = use deck baseline as-is.
default_daemon_addendum = ""
default_construct_addendum = ""

# Empty recommended_tools = no specific suggestion; construct picks
# from the full default tool set freely.
recommended_tools = []

default_scripts = []
'''


# ---- events ----------------------------------------------------------------


@dataclass(frozen=True)
class ProfileEvent:
    """A single change in the registry's worldview.

    Event kinds:
      'added'         - new profile loaded (file appeared, or first scan)
      'changed'       - existing profile reloaded (file mtime moved)
      'removed'       - profile gone (file deleted, or its name changed)
      'scan_error'    - a file failed to load on this scan; old version
                        retained if any
      'scan_complete' - emitted after a scan finishes IF anything changed.
                        Lets subscribers do a single re-render instead
                        of one per individual event.
    """
    kind: str
    name: str
    profile: Optional[Profile] = None
    error: Optional[str] = None
    source_path: Optional[Path] = None


# Listener type. Synchronous on purpose — listeners are expected to be
# cheap (e.g., set a TUI re-render flag). If a listener needs to do
# real work, it should schedule it via asyncio.create_task itself.
ProfileEventListener = Callable[[ProfileEvent], None]


# ---- registry --------------------------------------------------------------


class ProfileRegistry:
    """Live view of the profiles directory.

    Lifecycle:
      reg = ProfileRegistry(path, on_event=cb)
      await reg.start()    # initial scan + start watcher
      ...                  # registry maintains itself in the background
      await reg.shutdown() # cancel watcher, idempotent

    Reads:
      reg.get(name)        # -> Profile | None
      reg.all()            # -> list[Profile], sorted by name
      reg.by_category()    # -> dict[category, list[Profile]], sorted

    The registry is robust against a missing directory — start() will
    create it if it doesn't exist. A directory that vanishes mid-run
    just produces empty scans; profiles fade out and the registry
    recovers when the directory comes back.
    """

    def __init__(
        self,
        profiles_dir: Path,
        *,
        on_event: Optional[ProfileEventListener] = None,
        poll_interval: float = 1.0,
        bus: Optional[object] = None,
    ) -> None:
        self.profiles_dir = Path(profiles_dir)
        self.poll_interval = poll_interval
        self._listeners: list[ProfileEventListener] = []
        if on_event is not None:
            self._listeners.append(on_event)
        # Phase 5 of the unified-event-stream slice. When wired,
        # `_emit` ALSO publishes a `profile.<kind>` DeckEvent on the
        # bus alongside the legacy listener fan-out.
        self.bus = bus

        # Authoritative state
        self._by_name: dict[str, Profile] = {}
        # Reverse map: file path -> last-loaded profile name. Lets us
        # detect renames (path same, name changed) and deletions
        # (path gone). Profile.source_path is the canonical key.
        self._path_to_name: dict[Path, str] = {}
        self._mtimes: dict[Path, float] = {}

        # Background task
        self._stopped: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._started = False

    # ---- listener wiring ---------------------------------------------------

    def add_listener(self, listener: ProfileEventListener) -> None:
        """Register a listener. Listener is called for every event,
        synchronously, on the asyncio event loop thread."""
        self._listeners.append(listener)

    def remove_listener(self, listener: ProfileEventListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _emit(self, event: ProfileEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as e:
                # Listener errors must never break the watcher loop.
                # Surface to stderr so the netrunner sees it, then
                # carry on.
                print(
                    f"profile_registry: listener error: {e!r}",
                    file=sys.stderr,
                )
        # Phase 5: also publish on the bus when wired. ProfileEvent
        # already has a `kind` field that maps cleanly to dotted-
        # namespace; scan_error escalates to warning severity.
        if self.bus is not None:
            try:
                from event_bus import DeckEvent
                severity = (
                    "warning" if event.kind == "scan_error" else "info"
                )
                self.bus.publish(DeckEvent(
                    kind=f"profile.{event.kind}",
                    source="profile_registry",
                    severity=severity,
                    payload=event,
                ))
            except Exception as e:
                print(
                    f"profile_registry: bus publish error: {e!r}",
                    file=sys.stderr,
                )

    # ---- read API ----------------------------------------------------------

    def get(self, name: str) -> Optional[Profile]:
        """Look up a profile by name. None if not loaded."""
        return self._by_name.get(name)

    def all(self) -> list[Profile]:
        """Snapshot of every loaded profile, sorted by name for stable
        display."""
        return sorted(self._by_name.values(), key=lambda p: p.name)

    def by_category(self) -> dict[str, list[Profile]]:
        """Group profiles by category, sorted within each group.
        Returned dict is sorted by category name."""
        groups: dict[str, list[Profile]] = {}
        for p in self._by_name.values():
            groups.setdefault(p.category, []).append(p)
        for cat in groups:
            groups[cat].sort(key=lambda p: p.name)
        return dict(sorted(groups.items()))

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Initial scan + start the background watcher.

        Creates the profiles directory if it doesn't exist (so the
        netrunner can drop a .toml in later and have it picked up on
        the next tick). The first scan emits 'added' events for every
        profile present, then 'scan_complete'.
        """
        if self._started:
            return
        self._started = True

        try:
            self.profiles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            # If we can't even create the dir, run with empty registry
            # rather than crashing the whole TUI. The watcher loop
            # will retry creation implicitly via _scan() failing
            # gracefully.
            print(
                f"profile_registry: could not create {self.profiles_dir}: "
                f"{e!r} — running with empty registry",
                file=sys.stderr,
            )

        # Initial scan synchronously so subscribers can call .all()
        # immediately after start() returns and see the full set.
        self._scan()

        self._stopped = asyncio.Event()
        self._task = asyncio.create_task(
            self._watch_loop(), name="profile-registry-watch",
        )

    async def shutdown(self) -> None:
        """Stop the watcher. Idempotent."""
        if self._stopped is not None:
            self._stopped.set()
        if self._task is not None:
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---- internals ---------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Poll the directory until shutdown is requested."""
        assert self._stopped is not None
        while not self._stopped.is_set():
            try:
                # Sleep in interruptible chunks: wait_for with timeout
                # returns immediately if the event fires, otherwise
                # times out and we loop.
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.poll_interval,
                )
                # Event fired — exit the loop.
                return
            except asyncio.TimeoutError:
                pass
            try:
                self._scan()
            except Exception as e:
                # The scan itself should be defensive but if anything
                # leaks, surface it without killing the watcher.
                self._emit(ProfileEvent(
                    kind="scan_error",
                    name="",
                    error=f"scan crashed: {e!r}",
                ))

    def _scan(self) -> None:
        """One pass over the directory. Detects added / changed /
        removed files, calls into _load_or_reload for each, and emits
        a 'scan_complete' event if anything changed."""
        if not self.profiles_dir.is_dir():
            # Directory vanished. Treat all loaded profiles as removed.
            if self._by_name:
                for name in list(self._by_name.keys()):
                    self._evict(name)
                self._emit(ProfileEvent(kind="scan_complete", name=""))
            return

        # Seed the hard-default profile to disk if it's missing. Runs
        # every scan — not just at startup — so deletion of default.toml
        # regenerates the file on the next tick rather than permanently
        # disabling default-profile behavior. The seed content is the
        # DEFAULT_PROFILE_TOML constant (empty addendums, empty allowed
        # tools); netrunner edits to default.toml are NEVER overwritten,
        # only the literally-missing case is.
        self._seed_default_if_missing()

        try:
            current_paths = {
                p.resolve() for p in self.profiles_dir.glob("*.toml")
            }
        except OSError as e:
            self._emit(ProfileEvent(
                kind="scan_error",
                name="",
                error=f"glob failed: {e!r}",
            ))
            return

        any_change = False

        # Check for additions and modifications
        for path in current_paths:
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue  # file vanished between glob and stat; ignore
            prior_mtime = self._mtimes.get(path)
            if prior_mtime is not None and prior_mtime == mtime:
                continue  # unchanged, skip
            if self._load_or_reload(path, mtime):
                any_change = True

        # Check for deletions: paths we knew about that are gone now
        known_paths = set(self._path_to_name.keys())
        for missing in known_paths - current_paths:
            old_name = self._path_to_name.pop(missing, None)
            self._mtimes.pop(missing, None)
            if old_name is not None and self._by_name.get(old_name) is not None:
                # Only evict if the by_name entry still points at this
                # file. If a different file took the same name (rare),
                # we'd otherwise wrongly drop the surviving profile.
                current_owner = self._by_name[old_name].source_path
                if current_owner is None or current_owner.resolve() == missing:
                    self._evict(old_name)
                    any_change = True

        if any_change:
            self._emit(ProfileEvent(kind="scan_complete", name=""))

    def _seed_default_if_missing(self) -> None:
        """Write DEFAULT_PROFILE_TOML to <dir>/default.toml if absent.

        Idempotent — checks for file existence first and bails if it's
        there, regardless of contents. The netrunner's edits to
        default.toml are sacred; we only fill in the gap when there's
        no file at all.

        Failures (read-only filesystem, permission denied) are logged
        to stderr and the scan continues. Spawns will then run
        profile-less for the default case, with a clear hint in the
        error stream about why.
        """
        path = self.profiles_dir / "default.toml"
        if path.exists():
            return
        try:
            path.write_text(DEFAULT_PROFILE_TOML, encoding="utf-8")
        except OSError as e:
            print(
                f"profile_registry: warning: could not seed default.toml "
                f"at {path}: {e!r} — default-profile behavior may be "
                f"unavailable until the file exists",
                file=sys.stderr,
            )
            return
        # Don't emit an event here — the next file-walk in this same
        # scan will pick up the new file via the normal mtime path
        # and emit the proper 'added' event. This avoids double-firing.

    def _load_or_reload(self, path: Path, mtime: float) -> bool:
        """Load (or reload) a single file. Updates state and emits
        the appropriate 'added' / 'changed' / 'removed' events.
        Returns True if state changed, False otherwise.

        Handles the rename-in-content edge case: if a file's `name`
        field changed (foo.toml had name='foo', now has name='bar'),
        we remove the old name and add the new one.
        """
        try:
            new_profile = load_profile(path)
        except ProfileValidationError as e:
            # Keep the prior version (if any) and surface the error.
            # The netrunner can fix the TOML and the next mtime
            # change will trigger another reload attempt.
            self._emit(ProfileEvent(
                kind="scan_error",
                name=self._path_to_name.get(path, ""),
                error=str(e),
                source_path=path,
            ))
            # Update mtime so we don't re-emit the same scan_error
            # every poll. We *do* want to retry when the netrunner
            # actually edits the file (mtime moves again); we don't
            # want to spam the chatlog while they figure it out.
            self._mtimes[path] = mtime
            return False

        # Detect name conflict (different file already owns this name)
        existing = self._by_name.get(new_profile.name)
        if (existing is not None
                and existing.source_path is not None
                and existing.source_path.resolve() != path):
            print(
                f"profile_registry: warning: {path.name} declares "
                f"name={new_profile.name!r} which is already owned by "
                f"{existing.source_path.name} — last-write-wins, "
                f"dropping previous owner",
                file=sys.stderr,
            )
            # Evict the old owner's reverse-mapping. The actual by_name
            # slot will be overwritten just below.
            self._path_to_name.pop(existing.source_path.resolve(), None)
            self._mtimes.pop(existing.source_path.resolve(), None)

        # Detect rename-in-content: the file path is the same but the
        # `name` field changed. Need to evict the old name.
        prior_name = self._path_to_name.get(path)
        if prior_name is not None and prior_name != new_profile.name:
            self._evict(prior_name)

        is_change = prior_name == new_profile.name
        self._by_name[new_profile.name] = new_profile
        self._path_to_name[path] = new_profile.name
        self._mtimes[path] = mtime

        self._emit(ProfileEvent(
            kind="changed" if is_change else "added",
            name=new_profile.name,
            profile=new_profile,
            source_path=path,
        ))
        return True

    def _evict(self, name: str) -> None:
        """Remove a profile from the by-name map and emit 'removed'.
        Does NOT touch _path_to_name or _mtimes — caller is expected
        to handle those if a path is involved (deletion vs in-content
        rename have different cleanup needs)."""
        old = self._by_name.pop(name, None)
        if old is not None:
            self._emit(ProfileEvent(
                kind="removed",
                name=name,
                source_path=old.source_path,
            ))
