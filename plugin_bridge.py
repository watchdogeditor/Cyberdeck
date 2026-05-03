"""plugin_bridge — construct → plugin invocation dispatcher.

This file is BOTH:
  1. The canonical bridge source in <deck-source>/plugin_bridge.py
     (this file).
  2. Bootstrapped to <home>/tools/deck/plugin_bridge.py at deck
     startup, with the __PLUGINS_DIR__ token replaced by the absolute
     path to <deck-source>/plugins/. The construct invokes the
     bootstrapped copy through Bash; the canonical copy is the source
     of truth and gets refreshed on every deck launch.

Why a bridge at all (vs constructs invoking plugin entry scripts
directly)?

- Constructs don't know where the deck source lives. Telling them
  via the system prompt addendum would leak deck layout into every
  spawn and break across deck-source moves. The bridge lives at a
  stable construct-visible path (<home>/tools/deck/) and resolves
  plugins via the stamped __PLUGINS_DIR__ constant.

- Single stable invocation surface: every plugin is callable via the
  same `python <bridge> <name> [args...]` shape, so the construct
  prompt grows one example regardless of plugin count.

- The brake hook gates one bridge command line, not N plugin paths.
  Plus, putting plugin code in <deck-source>/plugins/ — outside the
  workspace — means the brake hook's deck-source-write protection
  prevents constructs from corrupting plugin code at the filesystem
  layer. The bridge is the only legitimate construct-side surface
  into plugin invocations.

Marker shape:
    python <home>/tools/deck/plugin_bridge.py <plugin_name> [args...]

Behavior:
  - Resolves <plugin_name> to <plugins_dir>/<plugin_name>/plugin.py
    (where <plugins_dir> is the stamped __PLUGINS_DIR__ constant, or
    a development fallback to <this-file>/plugins/ when the token
    hasn't been replaced).
  - Spawns a subprocess: `python <plugin.py> [args...]`.
  - Inherits stdin/stdout/stderr from the calling Bash so the
    plugin's output reaches the construct verbatim, and the plugin's
    exit code passes through.

Exit codes:
    Whatever the plugin's entry script exits with, passed through
    verbatim. Bridge-specific errors exit 4:
      - unknown plugin (no folder at <plugins_dir>/<plugin_name>/)
      - plugin folder missing plugin.py
      - subprocess spawn failure (OSError)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


PROTOCOL_VERSION = "v1"


# Stamped at bootstrap time by the deck. The token form here is the
# canonical-source default; when the file is bootstrapped to
# <home>/tools/deck/plugin_bridge.py the deck rewrites this to the
# absolute path of <deck-source>/plugins/.
#
# When this file runs from the canonical location (development,
# direct invocation from deck source) the token stays unreplaced and
# _resolve_plugins_dir() falls back to <this-file>/plugins/.
_PLUGINS_DIR_TOKEN = "__PLUGINS_DIR__"
PLUGINS_DIR = _PLUGINS_DIR_TOKEN


def _resolve_plugins_dir() -> Path:
    """Return the directory that holds plugin folders.

    Priority:
      1. Stamped PLUGINS_DIR constant (set at bootstrap time when the
         bridge gets installed at <home>/tools/deck/plugin_bridge.py).
      2. Fallback: <this-file>/plugins/ — works when running the
         canonical copy from deck source (development), since
         <deck-source>/plugins/ sits next to plugin_bridge.py.
    """
    if PLUGINS_DIR != _PLUGINS_DIR_TOKEN:
        return Path(PLUGINS_DIR)
    return Path(__file__).resolve().parent / "plugins"


def _usage() -> str:
    return (
        "usage: python plugin_bridge.py <plugin_name> [args...]\n"
        "       python plugin_bridge.py --help\n"
        "       python plugin_bridge.py --version\n"
        "       python plugin_bridge.py --list\n"
    )


def _list_plugins(plugins_dir: Path) -> int:
    if not plugins_dir.is_dir():
        print(
            f"plugin_bridge: plugins directory not found: {plugins_dir}",
            file=sys.stderr,
        )
        return 4
    names: list[str] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        if (entry / "plugin.py").is_file():
            names.append(entry.name)
    if not names:
        print(
            f"plugin_bridge: no plugins found under {plugins_dir}",
            file=sys.stderr,
        )
        return 0
    for n in names:
        print(n)
    return 0


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        print()
        print(_usage())
        return 0
    if argv[0] == "--version":
        print(PROTOCOL_VERSION)
        return 0

    plugins_dir = _resolve_plugins_dir()

    if argv[0] == "--list":
        return _list_plugins(plugins_dir)

    plugin_name = argv[0]
    rest = argv[1:]

    plugin_dir = plugins_dir / plugin_name
    if not plugin_dir.is_dir():
        print(
            f"plugin_bridge: unknown plugin {plugin_name!r} "
            f"(no folder at {plugin_dir}). "
            f"Try `python {Path(__file__).name} --list` to see what's "
            f"available.",
            file=sys.stderr,
        )
        return 4

    plugin_py = plugin_dir / "plugin.py"
    if not plugin_py.is_file():
        print(
            f"plugin_bridge: plugin {plugin_name!r} has no plugin.py "
            f"at {plugin_py}",
            file=sys.stderr,
        )
        return 4

    # Forward the invocation. Inherit stdin/stdout/stderr from the
    # caller (the construct's Bash) so the plugin's output reaches
    # the construct verbatim and the plugin can read stdin if it
    # wants. Exit code passes through.
    try:
        proc = subprocess.run(
            [sys.executable, str(plugin_py), *rest],
        )
        return proc.returncode
    except OSError as exc:
        print(
            f"plugin_bridge: failed to spawn plugin {plugin_name!r}: "
            f"{exc}",
            file=sys.stderr,
        )
        return 4


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
