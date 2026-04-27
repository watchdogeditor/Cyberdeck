"""
Plugin registry: discovery + listing of <home>/plugins/.

Walks the plugins directory once at startup, validates each plugin
folder via plugins.load_plugin, and exposes a queryable map. Unlike
ProfileRegistry there's NO file-watch + hot reload here — plugins
are code, and Python's module/path-resolution semantics + cwd
sensitivity + arbitrary native dependency state make hot-reload
fraught. If the netrunner adds a new plugin or edits an existing
one, they restart the deck. (Plugins were not made to be hot-edited
the way profiles are.)

Lifecycle is one-shot: scan() at startup, no background work.
Failures during a plugin's load become 'scan_error' events; the
plugin doesn't make it into the registry but the rest still load.
This way a bad plugin can't take down the whole registry.

Public surface:
  PluginEvent       Notification of a per-plugin scan outcome.
  PluginRegistry    The registry itself; scan() / get() / all().
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from plugins import Plugin, PluginValidationError, load_plugin


@dataclass(frozen=True)
class PluginEvent:
    """A single scan outcome.

    Event kinds:
      'loaded'      — plugin loaded successfully (available or not)
      'scan_error'  — plugin folder failed to load; not registered
      'scan_complete' — emitted after the scan finishes; lets
                        subscribers do a single re-render
    """
    kind: str
    name: str
    plugin: Optional[Plugin] = None
    error: Optional[str] = None
    source_dir: Optional[Path] = None


PluginEventListener = Callable[[PluginEvent], None]


class PluginRegistry:
    """Disk-backed plugin map.

    Lifecycle:
        reg = PluginRegistry(path, on_event=cb)
        reg.scan()   # synchronous; populates the registry
        ...
        reg.all()    # -> list[Plugin], sorted by name

    Reads:
        reg.get(name)         # -> Plugin | None
        reg.all()             # -> list[Plugin] (sorted by name)
        reg.by_category()     # -> dict[category, list[Plugin]] (sorted)
        reg.available()       # -> list[Plugin] where available=True

    No async loop. Scan is one-shot at deck startup; if the
    netrunner adds a plugin while the deck is running, they restart.
    The complexity of safe hot-reload for arbitrary plugin code
    isn't worth eating in v1 — and getting it wrong leaves dangling
    Python module state in unpredictable shapes.
    """

    def __init__(
        self,
        plugins_dir: Path,
        *,
        on_event: Optional[PluginEventListener] = None,
    ) -> None:
        self.plugins_dir = Path(plugins_dir)
        self._listeners: list[PluginEventListener] = []
        if on_event is not None:
            self._listeners.append(on_event)

        # Authoritative state
        self._by_name: dict[str, Plugin] = {}

    # ---- listener wiring ---------------------------------------------------

    def add_listener(self, listener: PluginEventListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: PluginEventListener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def _emit(self, event: PluginEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:
                print(
                    f"plugin_registry: listener error: {exc!r}",
                    file=sys.stderr,
                )

    # ---- read API ----------------------------------------------------------

    def get(self, name: str) -> Optional[Plugin]:
        return self._by_name.get(name)

    def all(self) -> list[Plugin]:
        return sorted(self._by_name.values(), key=lambda p: p.name)

    def by_category(self) -> dict[str, list[Plugin]]:
        groups: dict[str, list[Plugin]] = {}
        for p in self._by_name.values():
            groups.setdefault(p.category, []).append(p)
        for cat in groups:
            groups[cat].sort(key=lambda p: p.name)
        return dict(sorted(groups.items()))

    def available(self) -> list[Plugin]:
        """Subset of all() where requires checks passed. Daemon
        system prompt should use this rather than all() so it
        doesn't suggest plugins the netrunner can't actually run."""
        return [p for p in self.all() if p.available]

    # ---- lifecycle ---------------------------------------------------------

    def scan(self) -> None:
        """Walk the plugins directory once. Idempotent — safe to
        call twice (later calls rebuild from scratch). Creates the
        directory if missing so the netrunner can drop a plugin in
        and restart.
        """
        try:
            self.plugins_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            print(
                f"plugin_registry: could not create {self.plugins_dir}: "
                f"{exc!r} — running with empty registry",
                file=sys.stderr,
            )
            self._emit(PluginEvent(kind="scan_complete", name=""))
            return

        # Reset state — full rebuild, since we don't track per-folder
        # mtimes (no hot reload anyway).
        self._by_name.clear()

        any_loaded = False
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith("."):
                continue
            # Plugin name conflict (unlikely but possible if two
            # folders declare the same `name` field): last-write-wins
            # with a stderr warning. Same posture as profile registry.
            try:
                plugin = load_plugin(entry)
            except PluginValidationError as exc:
                self._emit(PluginEvent(
                    kind="scan_error",
                    name=entry.name,
                    error=str(exc),
                    source_dir=entry,
                ))
                continue

            existing = self._by_name.get(plugin.name)
            if existing is not None:
                print(
                    f"plugin_registry: warning: {entry.name} declares "
                    f"name={plugin.name!r}, already owned by "
                    f"{existing.source_dir} — last-write-wins, "
                    f"dropping previous owner",
                    file=sys.stderr,
                )

            self._by_name[plugin.name] = plugin
            any_loaded = True
            self._emit(PluginEvent(
                kind="loaded",
                name=plugin.name,
                plugin=plugin,
                source_dir=entry,
            ))

        # Always emit scan_complete, even on empty registry — lets
        # the TUI know to re-render the (possibly empty) Plugins
        # section once instead of guessing.
        self._emit(PluginEvent(
            kind="scan_complete", name="",
        ))
