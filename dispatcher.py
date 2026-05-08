"""cyberdeck dispatcher — UI side-effect protocol for AI constructs.

This file is BOTH:
  1. A Python module that defines the dispatcher logic (importable
     from the cyberdeck package for tests and bootstrap).
  2. A self-contained script. When `__name__ == "__main__"` it runs
     as the dispatcher with no cyberdeck package dependencies — the
     full source of this file gets copied verbatim to
     <cyberdeck-home>/tools/deck/cyberdeck.py at bootstrap time.

The script's job is small: receive a subcommand from an AI construct
(invoked via Claude Code's Bash tool), validate arguments, and emit a
magic marker line on stdout. The cyberdeck deck process, which sees
construct stdout via the event stream, recognizes the marker and
performs the corresponding UI action.

The protocol is one-way (script -> deck) by design. The script never
reads from the deck or affects construct execution — it just signals.

Marker format:
    __CYBERDECK::v1::ACTION::PAYLOAD__

Each marker occupies one line. The deck parser strips markers from
displayed output before rendering, so the netrunner never sees the
protocol bytes — only the side effect (e.g., a file appearing in the
Files panel).

Subcommands:
    cyberdeck files add <path>       Surface <path> in the Files panel
    cyberdeck files remove <path>    Remove <path> from the Files panel
    cyberdeck --help                 Show usage
    cyberdeck --version              Show protocol version

Exit codes:
    0  Marker emitted successfully
    2  Bad arguments (caller's fault — show stderr to construct)
    3  Internal dispatch error
"""
from __future__ import annotations

import sys
from pathlib import Path


PROTOCOL_VERSION = "v1"
MARKER_FMT = "__CYBERDECK::{version}::{action}::{payload}__"


def emit(action: str, payload: str) -> None:
    """Print a marker line on stdout.

    The deck's parser regex requires each marker on its own line with
    the `__` bookends intact. We unconditionally emit a trailing
    newline so the marker is line-terminated even if the construct's
    Bash tool wraps stdout oddly.
    """
    line = MARKER_FMT.format(
        version=PROTOCOL_VERSION,
        action=action,
        payload=payload,
    )
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def cmd_files_add(args: list) -> int:
    """`cyberdeck files add <path>` — surface a file in the panel.

    Path is resolved to absolute (relative paths resolve against the
    construct's cwd, which is cyberdeck-home). The deck shortens the
    display to `~/...` form when applicable; we just emit the
    absolute path so the deck has the unambiguous version."""
    if len(args) != 1:
        print("usage: cyberdeck files add <path>", file=sys.stderr)
        return 2
    abs_path = str(Path(args[0]).resolve())
    emit("FILES_ADD", abs_path)
    return 0


def cmd_files_remove(args: list) -> int:
    """`cyberdeck files remove <path>` — remove a file from the
    panel. Same path resolution as add. Removes the first matching
    FileListItem; if multiple constructs added the same path, only
    the earliest goes (the others stay as provenance until removed
    individually)."""
    if len(args) != 1:
        print("usage: cyberdeck files remove <path>", file=sys.stderr)
        return 2
    abs_path = str(Path(args[0]).resolve())
    emit("FILES_REMOVE", abs_path)
    return 0


# Dispatcher table: (category, action) -> handler. Adding new commands
# is a one-entry change — keep handlers self-contained and the
# protocol versioned per ACTION (PROTOCOL_VERSION bumps if we ever
# break the wire format).
COMMANDS = {
    ("files", "add"):    cmd_files_add,
    ("files", "remove"): cmd_files_remove,
}


def main(argv: list) -> int:
    """Entry point. argv is sys.argv[1:] (excluding program name)."""
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] == "--version":
        print(PROTOCOL_VERSION)
        return 0
    if len(argv) < 2:
        print(
            "cyberdeck: incomplete command. Try --help.",
            file=sys.stderr,
        )
        return 2
    category = argv[0]
    action = argv[1]
    rest = argv[2:]
    handler = COMMANDS.get((category, action))
    if handler is None:
        print(
            f"cyberdeck: unknown command '{category} {action}'. "
            f"Try --help.",
            file=sys.stderr,
        )
        return 2
    try:
        return handler(rest)
    except Exception as e:
        print(f"cyberdeck: internal error: {e}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
