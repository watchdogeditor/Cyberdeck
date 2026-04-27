#!/usr/bin/env python3
"""brake_hook.py — Claude Code PreToolUse hook for the deck's brake.

Spawned per construct via Claude Code's `--settings` mechanism. Reads
the proposed tool call from stdin, consults the brake state passed via
argv, and decides allow / deny based on a small hand-curated policy
plus path-prefix and destructive-bash-regex matching.

This file is intentionally self-contained: no imports beyond stdlib,
no dependence on any other deck module, no caches or state. Each
invocation is a fresh subprocess that lives milliseconds. If this file
ever needs more than ~150 lines or external imports, that's a smell —
the brake stays elegant or it stops being a brake.

Protocol (per real-deck verification on claude 2.1.118):
  Stdin:  JSON {tool_name, tool_input, hook_event_name, ...}
  Argv:   [brake_state]   — "paranoid" | "default" | "yolo"
  Exit 0: allow
  Exit 2: deny
  Stderr: when denying, the human-readable reason. Claude Code
          surfaces it to the model as the tool_result content.

YOLO is a no-op: the deck doesn't install this hook for YOLO spawns,
so we shouldn't ever be invoked under it. If we are anyway, fail open
(exit 0) — never break a YOLO construct because of a misconfigured
hook.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


# -- patterns -----------------------------------------------------------------
#
# Short, opinionated, hand-curated. Each entry catches something
# universally catastrophic — wrong regardless of goal, profile, or
# netrunner intent. Anything contextual lives in the watchdog's
# tripwire layer (deferred), NOT here.
#
# When in doubt about adding a pattern, ask: "would this catch
# something that's wrong even when the netrunner explicitly asked for
# it?" If no, it's contextual; leave it out.

# Bash command patterns (regex). Case-insensitive matching applied at
# search time. Each pattern is keyed by a short label that goes in the
# stderr denial reason so the model knows what shape was caught.
DESTRUCTIVE_BASH_PATTERNS: list[tuple[str, str]] = [
    # Recursive delete on dangerous roots. Allow rm in general; only
    # block when targeting / or system roots.
    (r"\brm\s+(-[rRfF]+\s+)?(/\s|/$|C:[\\/]|.*[\\/]Windows|.*[\\/]Program Files)",
     "rm targeting system root"),
    # Windows recursive delete.
    (r"\b(del|rd|rmdir)\s+/[sqSQ]", "Windows recursive delete"),
    # Disk format.
    (r"\bformat\s+[a-zA-Z]:", "disk format"),
    # Raw disk write.
    (r"\bdd\s+.*of=/dev/[a-z]", "raw disk write"),
    # Filesystem creation.
    (r"\bmkfs(\.\w+)?\b", "filesystem creation"),
    # Fork bomb (literal pattern).
    (r":\s*\(\)\s*\{.*:\s*\|\s*:\s*&.*\}\s*;\s*:", "fork bomb"),
    # System power control.
    (r"\b(shutdown|reboot|halt|poweroff)\b", "system power control"),
    # Windows service control.
    (r"\bnet\s+stop\b", "Windows service stop"),
    (r"\bsc\s+(delete|stop)\b", "Windows service control"),
]

# Path prefixes whose contents must not be Written or Edited.
# Case-insensitive comparison on Windows; case-sensitive on Unix.
PROTECTED_WINDOWS_PREFIXES = (
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
)

PROTECTED_UNIX_PREFIXES = (
    "/usr/",
    "/etc/",
    "/bin/",
    "/sbin/",
    "/var/",
    "/lib/",
    "/lib64/",
    "/opt/",
    "/boot/",
)

# Tools that paranoid brake denies wholesale. Read/Glob/Grep/WebSearch/
# TodoWrite are the read-only / agent-internal kit and stay allowed.
PARANOID_DENY_TOOLS = frozenset({
    "Write", "Edit", "Bash", "WebFetch", "NotebookEdit",
})

# Filenames the deck owns that bash should never modify, even when
# the construct uses a relative path or a clever indirection. These
# are sentinel filenames — they don't appear in legitimate workflows
# the construct is asked to do, so a bash command that mentions them
# is almost certainly an attempt to circumvent the brake. Substring
# match (case-insensitive on Windows) catches them even when the
# construct cd's into the deck source dir to drop the absolute path.
PROTECTED_DECK_FILENAMES = (
    "brake_hook.py",
    "brake_state.py",
    "brake_patterns.py",
)


# -- decision logic -----------------------------------------------------------


def deck_source_dir() -> Path:
    """Where the deck's own .py files live. Used to protect the deck
    from accidental self-modification under default brake. Computed
    from this file's location: brake_hook.py sits next to tui.py,
    daemon.py, etc., so its parent IS the deck source dir."""
    return Path(__file__).resolve().parent


def bash_touches_protected_path(cmd: str) -> tuple[bool, str]:
    """Detect bash commands that mention a protected path or deck-
    owned filename. Catches the redirect/copy/move/tee class of
    bypass — `echo > brake_hook.py`, `cp foo /usr/bin/`, `tee
    C:\\Windows\\thing.dll`, etc. — without trying to fully parse
    bash. Substring match is over-broad on purpose: a construct
    that needs to mention a protected path in a command can do
    legitimately read-only work via Read/Glob/Grep instead.

    Returns (deny, reason). Deny=False if the command doesn't touch
    a protected target.

    Threat model is "construct goes off-rails," not "construct is
    adversarial" — a determined adversary can hide intent (variable
    substitution, base64'd commands, scripts that compose paths
    dynamically). The defense here is structural enough to catch
    the casual-drift failure mode while staying simple. Tighter
    sandboxing belongs at the OS layer (AppContainer / namespaces /
    different uid), out of scope for this hook.
    """
    if not cmd:
        return False, ""

    on_windows = sys.platform.startswith("win")
    haystack = cmd.lower() if on_windows else cmd
    # Normalize backslashes to forward slashes on Windows so we
    # match regardless of which separator the construct used.
    if on_windows:
        haystack_alt = haystack.replace("\\", "/")
    else:
        haystack_alt = haystack

    # OS-root prefixes (case-insensitive on Windows).
    if on_windows:
        for prefix in PROTECTED_WINDOWS_PREFIXES:
            p_norm = prefix.lower().replace("\\", "/")
            if p_norm in haystack_alt:
                return True, f"bash references protected OS path '{prefix}'"
    else:
        for prefix in PROTECTED_UNIX_PREFIXES:
            if prefix in haystack:
                return True, f"bash references protected OS path '{prefix}'"

    # Deck source dir — the parent of brake_hook.py itself.
    deck = str(deck_source_dir())
    deck_norm = deck.lower().replace("\\", "/") if on_windows else deck
    if deck_norm in haystack_alt:
        return True, f"bash references deck source dir"

    # Deck-owned filenames (sentinel substring match). Catches the
    # "construct cd's into the deck source then runs `> brake_hook.py`"
    # path even when the deck source dir prefix isn't in the same
    # command string.
    for fname in PROTECTED_DECK_FILENAMES:
        needle = fname.lower() if on_windows else fname
        if needle in haystack:
            return True, (
                f"bash references protected deck file '{fname}' "
                f"(brake-config tampering attempt)"
            )

    return False, ""


def path_is_protected(path: str) -> bool:
    """True if `path` is under an OS root or the deck source dir.
    Bias toward over-protection — when normalization fails, we don't
    deny by default (file might be a relative path that's fine; we
    return False rather than guess)."""
    if not path:
        return False
    if sys.platform.startswith("win"):
        norm = os.path.normpath(path).lower().replace("/", "\\")
        for prefix in PROTECTED_WINDOWS_PREFIXES:
            if norm.startswith(prefix):
                return True
    else:
        norm = os.path.normpath(path)
        for prefix in PROTECTED_UNIX_PREFIXES:
            if norm.startswith(prefix):
                return True
    # Deck source dir check — same on both platforms.
    try:
        target = Path(path).resolve()
        deck = deck_source_dir()
        if target == deck or deck in target.parents:
            return True
    except (OSError, ValueError):
        # Can't resolve — assume not protected. Better to allow
        # ambiguous cases than to break the construct over a path
        # quirk; the watchdog catches what we miss.
        pass
    return False


def check_paranoid(tool: str, inp: dict) -> tuple[bool, str]:
    """Returns (deny, reason). Paranoid is wholesale: any side-effect
    tool is denied. The construct can read and reason; it cannot act."""
    if tool in PARANOID_DENY_TOOLS:
        return True, (
            f"PARANOID brake: {tool} is not permitted in this mode. "
            f"Switch to default brake (b key) if you need to act on "
            f"the system."
        )
    return False, ""


def check_default(tool: str, inp: dict) -> tuple[bool, str]:
    """Returns (deny, reason). Default is opinionated permissive: deny
    Write/Edit to OS roots and the deck source, deny destructive bash
    patterns, allow everything else."""
    if tool in ("Write", "Edit"):
        path = str(inp.get("file_path", ""))
        if path_is_protected(path):
            return True, (
                f"DEFAULT brake: {tool} to protected path denied "
                f"(OS root or deck source): {path}"
            )
    elif tool == "Bash":
        cmd = str(inp.get("command", ""))
        for pattern, label in DESTRUCTIVE_BASH_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                # Truncate the echoed command so a 50KB one-liner
                # doesn't wreck the tool_result the model sees.
                preview = cmd[:120] + ("..." if len(cmd) > 120 else "")
                return True, (
                    f"DEFAULT brake: bash command denied "
                    f"({label}): {preview}"
                )
        # Path-aware second pass: catches bash commands that bypass
        # the Write/Edit path check via redirect / cp / mv / tee /
        # python inline / etc. Substring match for protected paths
        # and deck-owned filenames.
        touches, label = bash_touches_protected_path(cmd)
        if touches:
            preview = cmd[:120] + ("..." if len(cmd) > 120 else "")
            return True, (
                f"DEFAULT brake: bash command denied ({label}): {preview}"
            )
    return False, ""


def main() -> int:
    """Read stdin, dispatch on brake, emit stderr + exit code."""
    brake = sys.argv[1] if len(sys.argv) > 1 else "default"

    # YOLO short-circuits to allow. This shouldn't actually be invoked
    # under YOLO (the deck omits --settings), but we fail open if it is.
    if brake == "yolo":
        return 0

    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        # Malformed input from claude — should never happen, but if it
        # does, we don't want to break the construct over our own
        # parsing failure. Log to stderr (becomes a tool_result hint
        # to the model) and allow.
        print("brake_hook: could not parse hook input; allowing", file=sys.stderr)
        return 0

    tool = str(payload.get("tool_name", ""))
    inp = payload.get("tool_input") or {}
    if not isinstance(inp, dict):
        inp = {}

    if brake == "paranoid":
        deny, reason = check_paranoid(tool, inp)
    elif brake == "default":
        deny, reason = check_default(tool, inp)
    else:
        # Unknown brake state — log and allow. The deck shouldn't
        # produce this, but better to fail open than to silently
        # hose every tool call.
        print(
            f"brake_hook: unknown brake state {brake!r}; allowing",
            file=sys.stderr,
        )
        return 0

    if deny:
        print(reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
