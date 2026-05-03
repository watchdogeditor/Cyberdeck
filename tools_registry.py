"""
Tools registry: live, hot-reloading view of <home>/tools/tools.toml.

Watches a single TOML file (mtime-keyed polling) and keeps an
in-memory map of {name → Tool}. Detects file modifications and emits
events so subscribers (the TUI's Tools tab, eventually profile-tools
injection in spawn prompts) can react. Single-file shape rather than
a directory walk: tools.toml is the authoritative registry; subdirs
under <home>/tools/ are organizational (multi-part script bundles
declared in the registry by path), not auto-discovered.

Implementation note: polling, not OS notifications. tools.toml is
small, edits are infrequent, and a 1-second poll has no practical
cost. Mirrors profile_registry.py's design choice for the same
reasons. Bus is the only fan-out path (spine Phase 8); subscribers
filter on `tool.*`.

Public surface:

  ToolEvent          Notification of a single change.
  ToolRegistry       The watcher itself; start() / shutdown() / get() / all().

Phase 1 of the tools/plugins/profiles retool. Does NOT yet wire
into profile tools-list rendering (P4) or the daemon system prompt
(also P4). The registry is just the data source; downstream surfaces
land in later phases.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from tools import Tool, ToolValidationError, load_tools


# Canonical default tools.toml content, seeded if missing. Empty
# registry by design — the netrunner declares their own tools by
# editing the file. Comments serve as inline schema docs so the
# netrunner doesn't have to round-trip through the design doc to
# add an entry.
#
# Like the profile registry's default seed, this re-seeds if deleted
# (netrunner can reset to empty by removing the file). Edits to
# tools.toml are sacred — only the literally-missing case re-writes.
DEFAULT_TOOLS_TOML: str = """\
# Cyberdeck — registered system-CLI surface.
#
# Each entry declares awareness of a tool the deck wants to use. The
# deck does NOT install tools — you do, through whatever package
# manager fits your platform. The deck just checks at load time
# whether each declared tool is reachable, and greys-out missing
# entries in the Tools panel.
#
# Schema:
#
#   [[tool]]
#   name        = "ripgrep"        # slug used in profile tools lists
#   kind        = "binary"         # "binary" | "script"
#   command     = "rg"             # what to invoke
#   description = "..."            # one-liner shown in construct prompt
#   # path      = "..."            # optional. Override for binaries,
#                                   #   path-to-script for scripts.
#                                   #   ${tools_dir} substitutes to
#                                   #   <home>/tools/.
#   # help_text = "..."            # optional. Longer description; not
#                                   #   auto-injected into prompts.
#
# Add your own [[tool]] entries below. Examples:
#
#   [[tool]]
#   name        = "ripgrep"
#   kind        = "binary"
#   command     = "rg"
#   description = "Fast recursive grep written in Rust."
#
#   [[tool]]
#   name        = "scan_subnet"
#   kind        = "script"
#   command     = "python"
#   path        = "${tools_dir}/scan_subnet/main.py"
#   description = "Sweep a subnet for live hosts."
"""


# ---- events ----------------------------------------------------------------


@dataclass(frozen=True)
class ToolEvent:
    """A single change in the registry's worldview.

    Event kinds:
      'added'           — new tool entry appeared in tools.toml
      'changed'         — existing entry reloaded (file mtime moved)
      'removed'         — tool entry no longer in tools.toml
      'unavailable'     — entry exists but existence check failed
                          (binary not on PATH, script file missing)
      'scan_error'      — tools.toml failed to load on this scan;
                          old version retained if any
      'scan_complete'   — emitted after a scan finishes IF anything
                          changed. Lets subscribers do a single
                          re-render instead of one per individual event.
    """
    kind: str
    name: str
    tool: Optional[Tool] = None
    error: Optional[str] = None
    source_path: Optional[Path] = None


ToolEventListener = Callable[[ToolEvent], None]


# ---- registry --------------------------------------------------------------


class ToolRegistry:
    """Live view of <home>/tools/tools.toml.

    Lifecycle:
      reg = ToolRegistry(tools_dir, bus=bus)
      bus.subscribe(cb, filter=["tool.*"])
      await reg.start()    # initial scan + start watcher
      ...                  # registry maintains itself in the background
      await reg.shutdown() # cancel watcher, idempotent

    Reads:
      reg.get(name)        # -> Tool | None
      reg.all()            # -> list[Tool], sorted by name
      reg.available()      # -> list[Tool] where available=True
      reg.by_kind()        # -> dict[kind, list[Tool]]

    Robust against a missing tools_dir (created on demand) and a
    missing tools.toml (default-seeded with the inline-doc template).
    A directory that vanishes mid-run produces empty scans; the
    registry recovers when the directory comes back.
    """

    def __init__(
        self,
        tools_dir: Path,
        *,
        poll_interval: float = 1.0,
        bus: Optional[object] = None,
    ) -> None:
        self.tools_dir = Path(tools_dir)
        self.tools_toml = self.tools_dir / "tools.toml"
        self.poll_interval = poll_interval
        # Bus is the only fan-out path (Phase 8 spine cleanup retired
        # the legacy on_event/_listeners pattern across the deck).
        # bus may be None for standalone runs without a TUI.
        self.bus = bus

        # Authoritative state
        self._by_name: dict[str, Tool] = {}
        self._mtime: Optional[float] = None

        # Background task
        self._stopped: Optional[asyncio.Event] = None
        self._task: Optional[asyncio.Task] = None
        self._started = False

    # ---- emission ----------------------------------------------------------

    def _emit(self, event: ToolEvent) -> None:
        # ToolEvent's `kind` field maps cleanly to the bus's dotted-
        # namespace; scan_error and unavailable escalate to warning
        # severity. Per-callback exception isolation lives on the
        # bus itself.
        if self.bus is not None:
            try:
                from event_bus import DeckEvent
                if event.kind in ("scan_error", "unavailable"):
                    severity = "warning"
                else:
                    severity = "info"
                self.bus.publish(DeckEvent(
                    kind=f"tool.{event.kind}",
                    source="tools_registry",
                    severity=severity,
                    payload=event,
                ))
            except Exception as exc:
                print(
                    f"tools_registry: bus publish error: {exc!r}",
                    file=sys.stderr,
                )

    # ---- read API ----------------------------------------------------------

    def get(self, name: str) -> Optional[Tool]:
        """Look up a tool by name. None if not registered."""
        return self._by_name.get(name)

    def all(self) -> list[Tool]:
        """Snapshot of every registered tool, sorted by name for
        stable display."""
        return sorted(self._by_name.values(), key=lambda t: t.name)

    def available(self) -> list[Tool]:
        """Subset of all() where the existence check passed. Profile
        tools-list injection should consult this rather than all()
        so the construct prompt doesn't advertise tools the netrunner
        doesn't have installed."""
        return [t for t in self.all() if t.available]

    def by_kind(self) -> dict[str, list[Tool]]:
        """Group tools by kind ('binary' / 'script'), sorted within
        each group. Returned dict is sorted by kind name for stable
        rendering."""
        groups: dict[str, list[Tool]] = {}
        for t in self._by_name.values():
            groups.setdefault(t.kind, []).append(t)
        for k in groups:
            groups[k].sort(key=lambda t: t.name)
        return dict(sorted(groups.items()))

    # ---- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Initial scan + start the background watcher.

        Creates the tools_dir if it doesn't exist (so the netrunner
        can drop tools.toml in later and have it picked up on the
        next tick). The first scan emits 'added' / 'unavailable'
        events for every tool present, then 'scan_complete'.
        """
        if self._started:
            return
        self._started = True

        try:
            self.tools_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"tools_registry: could not create {self.tools_dir}: "
                f"{exc!r} — running with empty registry",
                file=sys.stderr,
            )

        # Initial scan synchronously so subscribers can call .all()
        # immediately after start() returns and see the full set.
        self._scan()

        self._stopped = asyncio.Event()
        self._task = asyncio.create_task(
            self._watch_loop(), name="tools-registry-watch",
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
        """Poll tools.toml until shutdown is requested."""
        assert self._stopped is not None
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.poll_interval,
                )
                return  # shutdown signalled
            except asyncio.TimeoutError:
                pass
            try:
                self._scan()
            except Exception as exc:
                # Defensive: surface scan crashes without killing the
                # watcher. The scan itself catches per-entry errors;
                # this is for anything else that leaks.
                self._emit(ToolEvent(
                    kind="scan_error",
                    name="",
                    error=f"scan crashed: {exc!r}",
                ))

    def _scan(self) -> None:
        """One pass over tools.toml. Detects added / changed /
        removed entries via mtime + content diff, runs existence
        checks, emits per-entry events + a final 'scan_complete' if
        anything changed.
        """
        if not self.tools_dir.is_dir():
            # Directory vanished. Treat all loaded tools as removed.
            if self._by_name:
                for name in list(self._by_name.keys()):
                    self._evict(name)
                self._emit(ToolEvent(kind="scan_complete", name=""))
            return

        # Seed default tools.toml if absent — runs every scan so
        # deletion regenerates the file on the next tick. Idempotent;
        # netrunner edits are sacred.
        self._seed_default_if_missing()

        if not self.tools_toml.is_file():
            # Seeding failed (rare — read-only filesystem etc.). Run
            # with empty registry; previously-loaded tools get evicted.
            if self._by_name:
                for name in list(self._by_name.keys()):
                    self._evict(name)
                self._emit(ToolEvent(kind="scan_complete", name=""))
            return

        try:
            mtime = self.tools_toml.stat().st_mtime
        except OSError as exc:
            self._emit(ToolEvent(
                kind="scan_error",
                name="",
                error=f"stat failed: {exc!r}",
                source_path=self.tools_toml,
            ))
            return

        if self._mtime is not None and self._mtime == mtime:
            return  # unchanged; common path

        self._mtime = mtime

        # Reload the file. Whole-file replace rather than per-entry
        # diff because tools.toml is small (typically <50 entries)
        # and TOML doesn't preserve entry identity across edits —
        # we'd have no reliable way to match "the same" entry across
        # a reorder anyway. Diff at the name level is cheap.
        try:
            new_tools = load_tools(self.tools_toml, tools_dir=self.tools_dir)
        except ToolValidationError as exc:
            # Whole-file load failed (bad TOML, structural error).
            # Keep the prior state; surface the error.
            self._emit(ToolEvent(
                kind="scan_error",
                name="",
                error=str(exc),
                source_path=self.tools_toml,
            ))
            return

        new_by_name: dict[str, Tool] = {t.name: t for t in new_tools}
        any_change = False

        # Additions and modifications
        for name, tool in new_by_name.items():
            prior = self._by_name.get(name)
            if prior is None:
                self._by_name[name] = tool
                self._emit(ToolEvent(
                    kind="added",
                    name=name,
                    tool=tool,
                    source_path=self.tools_toml,
                ))
                any_change = True
                if not tool.available:
                    # Also fire the unavailable marker so subscribers
                    # filtering on tool.unavailable see it. We don't
                    # collapse with tool.added because the two have
                    # different semantics — added=appeared, unavailable
                    # =appeared-but-broken. Subscribers may want one
                    # or both.
                    self._emit(ToolEvent(
                        kind="unavailable",
                        name=name,
                        tool=tool,
                        error=tool.unavailable_reason,
                        source_path=self.tools_toml,
                    ))
            elif prior != tool:
                self._by_name[name] = tool
                self._emit(ToolEvent(
                    kind="changed",
                    name=name,
                    tool=tool,
                    source_path=self.tools_toml,
                ))
                any_change = True
                # Availability transitions: prior available, now not
                # → fire unavailable. Prior unavailable, now available
                # → fire 'changed' alone is enough (subscribers see
                # tool.available=True via the tool field).
                if prior.available and not tool.available:
                    self._emit(ToolEvent(
                        kind="unavailable",
                        name=name,
                        tool=tool,
                        error=tool.unavailable_reason,
                        source_path=self.tools_toml,
                    ))

        # Deletions
        for name in list(self._by_name.keys()):
            if name not in new_by_name:
                self._evict(name)
                any_change = True

        if any_change:
            self._emit(ToolEvent(kind="scan_complete", name=""))

    def _seed_default_if_missing(self) -> None:
        """Write DEFAULT_TOOLS_TOML to tools.toml if absent. Idempotent.

        Failures (read-only filesystem, permission denied) are logged
        to stderr and the scan continues with an empty registry.
        Netrunner edits to an existing tools.toml are NEVER
        overwritten — only the literally-missing case writes.
        """
        if self.tools_toml.exists():
            return
        try:
            self.tools_toml.write_text(DEFAULT_TOOLS_TOML, encoding="utf-8")
        except OSError as exc:
            print(
                f"tools_registry: warning: could not seed tools.toml at "
                f"{self.tools_toml}: {exc!r} — registry will be empty",
                file=sys.stderr,
            )

    def _evict(self, name: str) -> None:
        """Remove a tool from the by-name map and emit 'removed'."""
        old = self._by_name.pop(name, None)
        if old is not None:
            self._emit(ToolEvent(
                kind="removed",
                name=name,
                tool=old,
                source_path=self.tools_toml,
            ))
