"""
Plugins: prescriptive capability bundles on disk.

A plugin lives at <deck-source>/plugins/<name>/ (post-P2 of the
tools/plugins/profiles retool, 2026-05-03 — pre-P2 they lived under
<home>/plugins/). Each plugin folder contains:
  - plugin.toml  — manifest (name, category, description, entry, requires)
  - README.md    — LLM-facing interface docs (loaded into context lazily)
  - <entry>      — executable entry point (typically `plugin.py`; any
                   language that can be invoked via Python works since
                   the bridge dispatcher is what constructs talk to)

Plugins extend the deck's capability surface in directions Bash alone
can't reach: cross-platform interfaces (camera, IR blaster, NFC),
external service integrations (MCP-shaped plugins are a v2 sub-shape),
and stateful sessions (also v2). v1 ships stateless plugins only —
each invocation is a fresh subprocess; output flows back on stdout,
errors on stderr, exit code signals success/failure.

Constructs do NOT invoke plugin entry scripts directly. They go
through the bridge dispatcher at <home>/tools/deck/plugin_bridge.py
(bootstrapped on every deck launch by tui._bootstrap_plugin_bridge),
which forwards `python <bridge> <plugin_name> [args...]` to the
plugin's entry script in deck source. Two reasons:
  1. Constructs don't need to know where deck source lives —
     keeps plugin invocations cache-friendly across deck moves.
  2. Putting plugin code in <deck-source>/ means the brake hook's
     deck-source-write protection (path_is_protected) prevents
     constructs from corrupting plugin files via Write/Edit/Bash.
     The bridge is regenerated on every deck launch, so it can't
     be persistently tampered with either.

Discovery is import-on-startup. PluginRegistry (plugin_registry.py)
walks <deck-source>/plugins/, validates each manifest, checks
`requires` (platforms, python imports), and exposes loaded plugins
for the Tools panel and daemon system prompt. Hot-reload is
deliberately absent — plugins are code, not data, and Python module
reloading is fraught.

This module is the data layer: Plugin dataclass + manifest loader.
Pure data; zero integration with fleet/daemon/TUI.

Manifest shape:

    name = "screenshot"
    category = "Capture"
    description = "Capture the current screen as a PNG."
    entry = "plugin.py"

    [requires]
    platforms = ["windows", "linux", "darwin"]
    python_imports = ["mss"]

Required: name, category, description, entry. Everything else has a
sensible default. `requires` block is optional; absent means "runs
everywhere." The deck checks `requires.platforms` against the host's
platform.system().lower() and `requires.python_imports` via
importlib.util.find_spec — failing checks mark the plugin as
`available=False` with a reason, but it still appears in the registry
so the netrunner sees what's there but can't run yet.

Spec note: this is a deliberate departure from the spec's earlier
"plugins are in-process Python" framing. Real-deck design pushed us
toward subprocess invocation for crash isolation + language-agnostic
entries + structural airgap (when that lands). The trade-off is the
plugin can't extend the TUI directly; that's by design — UI extension
isn't a plugin concern, it's deck source territory.
"""
from __future__ import annotations

import importlib.util
import platform as _platform
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Plugin name slug: same rule as profile names. Lowercase letters,
# digits, underscore, hyphen. Used as the folder name AND the
# manifest's `name` field; the two should match (warn on mismatch,
# don't reject — same posture as profile filename validation).
_NAME_PATTERN = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")


class PluginValidationError(ValueError):
    """Raised when a plugin manifest fails validation. Always
    includes the source path so the netrunner can find the offending
    plugin folder quickly."""

    def __init__(self, message: str, *, path: Optional[Path] = None) -> None:
        prefix = f"{path}: " if path is not None else ""
        super().__init__(f"{prefix}{message}")
        self.path = path


@dataclass(frozen=True)
class Plugin:
    """A loaded, validated plugin.

    Frozen because plugins are immutable once loaded. The registry
    rebuilds the entire view if anything changes (rather than
    mutating in place), since plugin code can't safely hot-reload.

    `available` reflects whether `requires` checks passed at scan
    time. False-but-listed plugins surface in the Tools panel with
    a dim treatment so the netrunner sees what's installed but
    can't yet activate. Daemon system prompt skips unavailable
    plugins so it doesn't suggest things that won't work.
    """

    # Identity (required)
    name: str
    category: str
    description: str

    # Entry point — filename relative to `source_dir`. The deck
    # invokes `python <source_dir>/<entry>` for Python plugins; the
    # construct's system-prompt addendum carries the full invocation
    # pattern so the model knows what to type into Bash.
    entry: str

    # Requires. Empty tuples mean "no constraint".
    requires_platforms: tuple[str, ...] = field(default_factory=tuple)
    requires_python_imports: tuple[str, ...] = field(default_factory=tuple)

    # Provenance — where on disk this plugin lives. The plugin
    # folder, NOT the manifest file. README and entry are resolved
    # against this. Optional so tests can construct in-memory plugins.
    source_dir: Optional[Path] = None

    # README contents, loaded at scan time. Stored so the daemon
    # system prompt and ExpandModal don't have to re-read disk per
    # query. Empty string when README.md is absent.
    readme: str = ""

    # Availability gate — set to False when a `requires` check
    # fails at load time. The plugin is still registered (so the
    # netrunner sees it exists) but the daemon shouldn't be told to
    # use it.
    available: bool = True
    unavailable_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            raise PluginValidationError(
                f"plugin name {self.name!r} must match {_NAME_PATTERN.pattern}",
                path=self.source_dir,
            )


def load_plugin(plugin_dir: Path) -> Plugin:
    """Load and validate a single plugin from its folder.

    Resolves `plugin.toml` and `README.md` relative to `plugin_dir`.
    Raises PluginValidationError on structural problems (missing
    manifest, malformed TOML, missing required fields, name slug
    mismatch). `requires` failures are NOT fatal — they downgrade
    the plugin to `available=False` with an explanatory reason but
    still return a usable Plugin object.

    Caller (PluginRegistry._scan) is responsible for the directory
    walk; this function just handles one plugin.
    """
    if not plugin_dir.is_dir():
        raise PluginValidationError(
            "plugin path is not a directory", path=plugin_dir,
        )

    manifest_path = plugin_dir / "plugin.toml"
    if not manifest_path.is_file():
        raise PluginValidationError(
            "missing plugin.toml", path=plugin_dir,
        )

    try:
        with manifest_path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise PluginValidationError(
            f"TOML parse error in plugin.toml: {exc}",
            path=plugin_dir,
        ) from exc

    if not isinstance(raw, dict):
        raise PluginValidationError(
            "plugin.toml root must be a table", path=plugin_dir,
        )

    # Required fields
    name = _require_str(raw, "name", plugin_dir)
    if not _NAME_PATTERN.match(name):
        raise PluginValidationError(
            f"plugin name {name!r} must match {_NAME_PATTERN.pattern}",
            path=plugin_dir,
        )
    category = _require_str(raw, "category", plugin_dir).strip()
    if not category:
        raise PluginValidationError(
            "category must be non-empty", path=plugin_dir,
        )
    description = _require_str(raw, "description", plugin_dir).strip()
    if not description:
        raise PluginValidationError(
            "description must be non-empty", path=plugin_dir,
        )
    entry = _require_str(raw, "entry", plugin_dir).strip()
    if not entry:
        raise PluginValidationError(
            "entry must be non-empty", path=plugin_dir,
        )
    entry_path = plugin_dir / entry
    if not entry_path.is_file():
        raise PluginValidationError(
            f"entry file {entry!r} not found in plugin folder",
            path=plugin_dir,
        )

    # Soft warnings
    if plugin_dir.name != name:
        print(
            f"plugins: warning: folder {plugin_dir.name!r} contains "
            f"plugin name {name!r} — rename one to match?",
            file=sys.stderr,
        )

    # Optional `requires` block
    requires = raw.get("requires") or {}
    if not isinstance(requires, dict):
        raise PluginValidationError(
            "[requires] must be a table if present", path=plugin_dir,
        )
    platforms = _optional_str_list(requires, "platforms", plugin_dir)
    python_imports = _optional_str_list(
        requires, "python_imports", plugin_dir,
    )

    # README — best-effort read. Missing or unreadable falls back to
    # the manifest's description; not an error.
    readme_path = plugin_dir / "README.md"
    readme_text = ""
    if readme_path.is_file():
        try:
            readme_text = readme_path.read_text(encoding="utf-8")
        except OSError as exc:
            print(
                f"plugins: warning: could not read {readme_path}: {exc!r}",
                file=sys.stderr,
            )

    # Availability checks. These are non-fatal — failing plugins still
    # land in the registry so the netrunner sees them, but get marked
    # unavailable with a reason.
    available = True
    reason: Optional[str] = None
    if platforms:
        host = _platform.system().lower()
        if host not in {p.lower() for p in platforms}:
            available = False
            reason = (
                f"requires platforms={list(platforms)} but host is "
                f"{host!r}"
            )
    if available and python_imports:
        missing = [m for m in python_imports
                   if importlib.util.find_spec(m) is None]
        if missing:
            available = False
            reason = (
                f"missing python module(s): {missing} "
                f"(install: pip install {' '.join(missing)})"
            )

    # Soft warning for unknown manifest keys — typo guard.
    _warn_unknown_keys(raw, plugin_dir)
    _warn_unknown_requires_keys(requires, plugin_dir)

    return Plugin(
        name=name,
        category=category,
        description=description,
        entry=entry,
        requires_platforms=tuple(platforms),
        requires_python_imports=tuple(python_imports),
        source_dir=plugin_dir,
        readme=readme_text,
        available=available,
        unavailable_reason=reason,
    )


# ---- internal helpers ------------------------------------------------------


_KNOWN_TOP_KEYS = frozenset({
    "name", "category", "description", "entry", "requires",
})

_KNOWN_REQUIRES_KEYS = frozenset({
    "platforms", "python_imports",
})


def _require_str(raw: dict, key: str, path: Path) -> str:
    if key not in raw:
        raise PluginValidationError(
            f"missing required field {key!r}", path=path,
        )
    val = raw[key]
    if not isinstance(val, str):
        raise PluginValidationError(
            f"field {key!r} must be a string, got {type(val).__name__}",
            path=path,
        )
    return val


def _optional_str_list(raw: dict, key: str, path: Path) -> list[str]:
    if key not in raw:
        return []
    val = raw[key]
    if not isinstance(val, list):
        raise PluginValidationError(
            f"field {key!r} must be a list, got {type(val).__name__}",
            path=path,
        )
    for i, item in enumerate(val):
        if not isinstance(item, str):
            raise PluginValidationError(
                f"field {key!r}[{i}] must be a string",
                path=path,
            )
    return list(val)


def _warn_unknown_keys(raw: dict, path: Path) -> None:
    unknown = set(raw.keys()) - _KNOWN_TOP_KEYS
    if unknown:
        print(
            f"plugins: warning: {path}/plugin.toml: unknown top-level "
            f"key(s) {sorted(unknown)} — typo, or schema drift?",
            file=sys.stderr,
        )


def _warn_unknown_requires_keys(requires: dict, path: Path) -> None:
    unknown = set(requires.keys()) - _KNOWN_REQUIRES_KEYS
    if unknown:
        print(
            f"plugins: warning: {path}/plugin.toml: unknown [requires] "
            f"key(s) {sorted(unknown)} — typo, or schema drift?",
            file=sys.stderr,
        )
