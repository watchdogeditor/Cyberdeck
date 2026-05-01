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
  Argv:   [brake_state, construct_id]
            brake_state — "paranoid" | "default" | "yolo"
            construct_id — "cx-xxxx" passed by brake_state
                           .make_spawn_settings (slice 2 of safety
                           architecture pass; tripwire engine writes
                           per-construct deny_pending flag the hook
                           reads)
  Exit 0: allow
  Exit 2: deny
  Stderr: when denying, the human-readable reason. Claude Code
          surfaces it to the model as the tool_result content.

YOLO is a no-op: the deck doesn't install this hook for YOLO spawns,
so we shouldn't ever be invoked under it. If we are anyway, fail open
(exit 0) — never break a YOLO construct because of a misconfigured
hook.

Tripwire integration (slice 2 of safety architecture pass): when the
watchdog's TripwireEngine fires at warning or critical severity on a
bus event (thinking, assistant, tool_use, tool_result), it writes
`<home>/.cyberdeck/spawns/<cid>.deny_pending.json` with the deny
reason. The hook reads + clears that file at every invocation; if
present, denies the call with the recorded reason. This is what gives
tripwires teeth — without it they were observation-only.

Race-mitigation recheck: when a tripwire fires on the SAME tool_use
event the hook is currently evaluating, the engine's flag-write may
land microseconds after the hook's first read. The hook reads, sleeps
100ms, re-reads — catches the engine's late publish without changing
the hook's protocol. 100ms is small enough to be invisible in normal
construct work and large enough to win the race against asyncio
scheduling on the deck side. Read-only tools (Read/Glob/Grep/etc.)
skip the recheck since they can't be denied by tripwires destructive
of state anyway — only write-class tools wait.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
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
# Both Bash and PowerShell get denied — Claude Code on Windows
# exposes PowerShell as a separate tool that takes the same
# {"command": "..."} shape as Bash, and a construct given Bash-denied
# will pivot to PowerShell automatically (verified on real-deck —
# we caught a screenshot construct doing exactly this without being
# asked). Both shells must be gated equivalently or the brake is
# trivially bypassable.
PARANOID_DENY_TOOLS = frozenset({
    "Write", "Edit", "Bash", "PowerShell", "WebFetch", "NotebookEdit",
})

# Shell-execution tools that need the same destructive-pattern + path
# checks under default brake. Same rationale as PARANOID_DENY_TOOLS:
# don't let PowerShell be the silent escape hatch.
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})

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

# Tokens that indicate a shell command WRITES to or MODIFIES a path,
# rather than just reading it. Used to gate the protected-path match —
# a read-only mention (Test-Path, ls, cat, Get-Item) of a protected
# path is fine; a write/modify needs to be denied.
#
# Real-deck verification surfaced the false positive this addresses:
# a recon_specialist construct doing
#     Test-Path "C:\Program Files (x86)\Nmap\nmap.exe"
# got denied because the command string contained "C:\Program Files."
# The path-protection check didn't distinguish reads from writes, so
# checking whether nmap was installed at its standard location got
# caught the same way as `Remove-Item C:\Windows\thing.dll` would.
# Same class of over-block as the deck-source-dir substring case
# (which got fixed earlier with sentinel filenames).
#
# Heuristic, not airtight — a construct could write via `python -c
# "open('C:/Windows/x','w').write(...)"` to evade. But: (1)
# DESTRUCTIVE_BASH_PATTERNS catches the catastrophic stuff regardless;
# (2) the Write/Edit tool gating already blocks tool-based writes
# to protected paths; (3) the spec is explicit that the threat model
# is "construct goes off-rails," not "construct is adversarial."
WRITE_INDICATOR_TOKENS = (
    # POSIX file-modifying utilities
    "tee", "cp", "mv", "rm", "ln", "dd", "mkdir", "rmdir",
    "touch", "chmod", "chown", "chgrp",
    # Windows cmd verbs (case-insensitive)
    "del", "erase", "copy", "xcopy", "robocopy",
    "move", "ren", "rename", "md", "rd",
    # PowerShell write/modify cmdlets
    "set-content", "add-content", "out-file", "tee-object",
    "new-item", "remove-item", "move-item", "copy-item",
    "rename-item", "set-itemproperty", "set-acl",
    "clear-content", "clear-item",
)


# -- MCP tool gating ---------------------------------------------------------
#
# MCP tools (mcp__<server>__<verb>_<noun>) come from the netrunner's
# claude.ai connector config — Supabase, Gmail, Drive, Calendar, etc.
# The brake hook's existing patterns target tool NAMES (Bash, Edit,
# Write, etc.) literally, so mcp__* tools matched none of them and
# sailed through under default brake unrestricted. Real-deck-discovered
# 2026-04-30 (late) via per-launch log analysis: any default-brake
# construct could call mcp__claude_ai_Supabase__execute_sql,
# mcp__claude_ai_Gmail__send (after auth), mcp__claude_ai_Google_Drive__*
# etc. Today's only defense was Claude's own refusal layer; the brake
# hook contributed nothing.
#
# This addition gates MCP tools by extracting the verb from the tool
# name and bucketing into read-shaped (allow under default) vs.
# destructive (deny). Unknown verbs default-deny: safer to require
# explicit categorization than to allow new MCP tools implicitly as
# the connector ecosystem evolves. Paranoid denies ALL mcp__* wholesale
# (handled in check_paranoid below) — even read-shaped MCP is a
# network-side-effecting query, which paranoid says no to.
#
# Per-spawn allowlist override (netrunner explicitly opts a construct
# into a normally-denied MCP tool) is filed as a follow-up. v1 here is
# the categorical defense.

# Verbs that READ from the connected service. Allowed under default
# brake. Conservative on purpose — when a verb is ambiguous (could be
# read OR write), it does NOT go in this set; ambiguous lands as
# "unknown" → deny.
MCP_READ_VERBS = frozenset({
    "get", "list", "search", "describe", "fetch", "show", "read",
    "view", "peek", "check", "validate", "inspect", "find", "query",
    "lookup", "count", "exists", "has", "is", "diff",
})

# Verbs that WRITE TO or otherwise CAUSE SIDE EFFECTS in the connected
# service. Denied under default brake. Comprehensive on purpose: when
# in doubt, it goes here. The cost of denying a legitimate-but-
# uncategorized verb is "construct gets a tool error and the netrunner
# sees the denial reason"; the cost of allowing one is potentially
# arbitrary destructive action against a connected production service
# (Supabase database, Gmail send, etc.).
MCP_DESTRUCTIVE_VERBS = frozenset({
    "execute", "apply", "send", "delete", "create", "update", "deploy",
    "drop", "merge", "migrate", "pause", "restore", "reset", "rebase",
    "write", "edit", "kill", "terminate", "cancel", "abort", "remove",
    "destroy", "purge", "clear", "revoke", "archive", "unarchive",
    "transfer", "move", "rename", "replace", "override", "add",
    "save", "post", "patch", "put", "push", "publish", "install",
    "uninstall", "enable", "disable", "start", "stop", "run", "invoke",
    "authenticate", "authorize", "login", "logout", "complete",
    "confirm", "approve", "reject", "lock", "unlock", "grant", "deny",
    "subscribe", "unsubscribe", "schedule", "trigger", "fire",
    "build", "compile", "release", "upload", "download",
})


def extract_mcp_verb(tool_name: str):
    """Extract the verb from an mcp__<server>__<verb>_<noun>-style
    tool name. Returns the verb (lowercased) or None if not an MCP
    tool or if the structure doesn't yield a clear verb token.

    Examples:
      mcp__claude_ai_Supabase__execute_sql → 'execute'
      mcp__claude_ai_Gmail__send_message   → 'send'
      mcp__server__list_branches           → 'list'
      mcp__server__authenticate            → 'authenticate'
      Bash                                  → None

    The tool-name format is established by Claude Code's MCP wiring:
    `mcp__<server>__<verb>[_<rest>]`. The double-underscore segment
    boundary is reliable; within the verb-and-rest tail, the first
    single-underscore-bounded token is the verb.
    """
    if not tool_name.startswith("mcp__"):
        return None
    # Split on double-underscore. parts[0] = "mcp", parts[1] = server,
    # parts[2] = verb_and_rest. Limit splits so server names containing
    # double-underscores (unlikely but possible) don't break.
    parts = tool_name.split("__", 2)
    if len(parts) < 3 or not parts[2]:
        return None
    rest = parts[2]
    verb = rest.split("_", 1)[0].lower()
    return verb if verb else None


def has_write_indicator(cmd: str) -> bool:
    """True if `cmd` contains a redirect operator or a known
    file-modifying utility/cmdlet token. Word-boundary matched on
    tokens to avoid substring false positives (e.g. won't flag
    "remove-item-related" or paths containing token names).

    `>` in any form (>, >>, *>, *>>) counts. Comparison in PowerShell
    uses `-gt`/`-lt` syntax, not `>`, so a `>` in a shell command
    almost always indicates redirection."""
    if not cmd:
        return False
    if ">" in cmd:
        return True
    lower = cmd.lower()
    for token in WRITE_INDICATOR_TOKENS:
        if re.search(r"\b" + re.escape(token) + r"\b", lower):
            return True
    return False


# -- decision logic -----------------------------------------------------------


def deck_source_dir() -> Path:
    """Where the deck's own .py files live. Used to protect the deck
    from accidental self-modification under default brake. Computed
    from this file's location: brake_hook.py sits next to tui.py,
    daemon.py, etc., so its parent IS the deck source dir."""
    return Path(__file__).resolve().parent


def cyberdeck_home_dir() -> Path:
    """Resolve the deck's runtime workspace.

    Default location: <deck source>/cyberdeck-home/. Can be overridden
    via the $CYBERDECK_HOME environment variable, matching the deck's
    CLI/env contract for --home.

    Why this matters: the workspace sits *inside* the deck source dir
    by layout, so the deck-source-protection check would otherwise
    deny every legitimate write into the workspace (the place where
    constructs are actually supposed to do their work). Real-deck
    verified — a synthesis construct trying to write a research
    report into the workspace got denied because Path.parents picked
    up the deck source. Same class of over-block as the shell-path
    substring case (already fixed in `bash_touches_protected_path`)."""
    env = os.environ.get("CYBERDECK_HOME")
    if env:
        try:
            return Path(env).resolve()
        except (OSError, ValueError):
            pass
    return deck_source_dir() / "cyberdeck-home"


def bash_touches_protected_path(cmd: str) -> tuple[bool, str]:
    """Detect shell commands that WRITE TO or MODIFY a protected path
    or deck-owned filename. Catches the redirect/copy/move/tee class
    of bypass — `echo > brake_hook.py`, `cp foo /usr/bin/`, `tee
    C:\\Windows\\thing.dll`, etc. — without trying to fully parse
    the shell.

    Returns (deny, reason). Deny=False if the command doesn't touch
    a protected target OR if it only reads from one.

    Two-prong gate:
      1. Does the command contain a write indicator? (redirect, mv,
         cp, Remove-Item, etc.) If no, allow — reads of protected
         paths are fine. The construct can `Test-Path "C:\\Program
         Files\\..."` to check whether a tool is installed without
         tripping this.
      2. If yes, does it ALSO mention a protected OS path or deck
         sentinel filename? If yes, deny.

    The first prong is what was missing in slice 1 of this file. A
    plain substring match for protected paths denied legitimate read
    operations (real-deck verification: a recon_specialist construct
    checking for nmap at its standard install location got blocked).

    Threat model is "construct goes off-rails," not "construct is
    adversarial" — a determined adversary can hide intent (variable
    substitution, base64'd commands, python -c open(...).write).
    The defense here is structural enough to catch the casual-drift
    failure mode while staying simple. Tighter sandboxing belongs at
    the OS layer (AppContainer / namespaces / different uid), out of
    scope for this hook.
    """
    if not cmd:
        return False, ""

    # First prong: no write indicator → no denial. Reads of protected
    # paths (Test-Path, ls, cat, Get-Item, ...) are allowed.
    if not has_write_indicator(cmd):
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
                return True, f"writes to protected OS path '{prefix}'"
    else:
        for prefix in PROTECTED_UNIX_PREFIXES:
            if prefix in haystack:
                return True, f"writes to protected OS path '{prefix}'"

    # Deck-owned filenames (sentinel substring match). Catches the
    # "construct cd's into the deck source then runs `> brake_hook.py`"
    # path even when the deck source dir prefix isn't in the same
    # command string.
    #
    # Note: we deliberately do NOT match the deck source dir as a
    # substring. Earlier versions did, but cyberdeck-home/ is a
    # subdirectory of the deck source dir, which meant every
    # legitimate plugin invocation (`python <deck>/cyberdeck-home/
    # plugins/.../run.py`) and dispatcher call got denied. Legitimate
    # use sits inside the deck-source tree by design (the layout
    # reorg that would move cyberdeck-home/ outside is deferred).
    # Sentinel filenames are precise enough: a construct writing to
    # brake_hook.py necessarily mentions that filename, regardless
    # of which path leads there.
    #
    # The write-indicator gate also covers reads here: a construct
    # legitimately reading brake_hook.py to inspect the policy
    # (`cat brake_hook.py`) won't trip this. Writes still get caught.
    for fname in PROTECTED_DECK_FILENAMES:
        needle = fname.lower() if on_windows else fname
        if needle in haystack:
            return True, (
                f"writes to protected deck file '{fname}' "
                f"(brake-config tampering attempt)"
            )

    return False, ""


def path_is_protected(path: str) -> bool:
    """True if `path` is under an OS root or the deck source dir.

    Workspace exemption: paths inside the cyberdeck-home/ workspace
    are NOT protected even though that directory sits inside the deck
    source dir by layout. Constructs are supposed to write there —
    that's the whole point of the workspace. Real-deck verified: a
    synthesis construct trying to write a research report into the
    workspace got denied because the workspace's parents include the
    deck source dir.

    Sub-exemption inside the exemption: <home>/.cyberdeck/ stays
    protected. That's where deck-internal state lives (brake state
    file, per-spawn settings JSON), and a construct that writes
    state.json to YOLO would change the next spawn's permissions —
    not a path the brake should leave open.

    Bias toward over-protection elsewhere: when normalization fails,
    we don't deny by default (the file might be a relative path
    that's fine; better to allow ambiguous cases and let the
    watchdog catch what we miss).
    """
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
    # Deck source dir check — but with workspace exemption.
    try:
        target = Path(path).resolve()
        deck = deck_source_dir()
        home = cyberdeck_home_dir()
        # Workspace exemption checked FIRST: if the target is inside
        # the workspace, it's allowed unless it's specifically inside
        # the deck-internal state subdirectory.
        if target == home or home in target.parents:
            internal = home / ".cyberdeck"
            if target == internal or internal in target.parents:
                # Deck-internal state — protected even inside workspace.
                return True
            return False
        # Outside the workspace but inside the deck source dir =
        # genuine deck source (tui.py, daemon.py, Design Files/, etc.)
        # = protected.
        if target == deck or deck in target.parents:
            return True
    except (OSError, ValueError):
        # Can't resolve — assume not protected.
        pass
    return False


def check_paranoid(tool: str, inp: dict) -> tuple[bool, str]:
    """Returns (deny, reason). Paranoid is wholesale: any side-effect
    tool is denied. The construct can read and reason; it cannot act.

    Both Bash and PowerShell are in PARANOID_DENY_TOOLS — they're
    equivalent shells from a "construct can act on the system"
    perspective, and a construct denied one will silently route to
    the other unless both are gated. Don't let the brake be a soft
    request that the construct can negotiate with.

    All `mcp__*` tools are denied under paranoid regardless of verb.
    Even read-shaped MCP (`get_*`, `list_*`, etc.) is a network query
    against an external connected service — paranoid is "no external
    side effects, no external traffic," and querying a Supabase
    project is still talking to a Supabase project. Constructs can
    still use Read/Glob/Grep/WebSearch on the local workspace and
    reason about what they find."""
    if tool in PARANOID_DENY_TOOLS:
        return True, (
            f"PARANOID brake: {tool} is not permitted in this mode. "
            f"Switch to default brake (b key) if you need to act on "
            f"the system."
        )
    if tool.startswith("mcp__"):
        return True, (
            f"PARANOID brake: MCP tool {tool} denied (no external "
            f"connector traffic permitted in paranoid mode). Switch "
            f"to default brake (b key) for read-shaped MCP access."
        )
    return False, ""


def check_default(tool: str, inp: dict) -> tuple[bool, str]:
    """Returns (deny, reason). Default is opinionated permissive: deny
    Write/Edit to OS roots and the deck source, deny destructive bash
    patterns, gate MCP tools by verb (read-shaped allowed, destructive
    denied, unknown denied), allow everything else."""
    if tool in ("Write", "Edit"):
        path = str(inp.get("file_path", ""))
        if path_is_protected(path):
            return True, (
                f"DEFAULT brake: {tool} to protected path denied "
                f"(OS root or deck source): {path}"
            )
    elif tool in SHELL_TOOLS:
        cmd = str(inp.get("command", ""))
        for pattern, label in DESTRUCTIVE_BASH_PATTERNS:
            if re.search(pattern, cmd, re.IGNORECASE):
                # Truncate the echoed command so a 50KB one-liner
                # doesn't wreck the tool_result the model sees.
                preview = cmd[:120] + ("..." if len(cmd) > 120 else "")
                return True, (
                    f"DEFAULT brake: {tool} command denied "
                    f"({label}): {preview}"
                )
        # Path-aware second pass: catches shell commands that bypass
        # the Write/Edit path check via redirect / cp / mv / tee /
        # python inline / etc. Substring match for protected paths
        # and deck-owned filenames. Applies equally to Bash and
        # PowerShell — same patterns work because both shells use
        # similar redirect/path syntax for the cases we care about.
        touches, label = bash_touches_protected_path(cmd)
        if touches:
            preview = cmd[:120] + ("..." if len(cmd) > 120 else "")
            return True, (
                f"DEFAULT brake: {tool} command denied ({label}): {preview}"
            )
    elif tool.startswith("mcp__"):
        # MCP tool gating — verb-based bucketing. See the MCP_*_VERBS
        # constants above for the rationale + categorization. Default-
        # deny on unknown verbs is intentional: when a new MCP server
        # gets connected to claude.ai, its tools should require explicit
        # categorization in this file rather than auto-flowing through.
        verb = extract_mcp_verb(tool)
        if verb is None:
            return True, (
                f"DEFAULT brake: MCP tool {tool} denied (unrecognized "
                f"name structure; cannot determine read vs. destructive). "
                f"Expected mcp__<server>__<verb>_<noun> format."
            )
        if verb in MCP_DESTRUCTIVE_VERBS:
            return True, (
                f"DEFAULT brake: MCP tool {tool} denied (verb '{verb}' "
                f"is destructive / side-effecting). Read-shaped MCP "
                f"verbs (get_*, list_*, search_*, etc.) are allowed; "
                f"this one isn't."
            )
        if verb not in MCP_READ_VERBS:
            return True, (
                f"DEFAULT brake: MCP tool {tool} denied (verb '{verb}' "
                f"not in read-only allowlist; default-deny for unknown "
                f"verbs). If this verb is genuinely read-only, add it "
                f"to MCP_READ_VERBS in brake_hook.py."
            )
        # Verb is in MCP_READ_VERBS — allow. Fall through to the
        # default-allow return at the bottom.
    return False, ""


def deny_pending_path(construct_id: str) -> Path:
    """Where the watchdog's TripwireEngine writes its
    deny_pending.json files. Mirrors the per-spawn settings convention
    used by `brake_state.make_spawn_settings` so both files live in
    the same directory."""
    return cyberdeck_home_dir() / ".cyberdeck" / "spawns" / f"{construct_id}.deny_pending.json"


def read_and_clear_deny_pending(construct_id: str):
    """Read the per-construct deny_pending.json if present and
    delete it (so the next call doesn't keep denying after the
    construct has already been redirected once). Returns the parsed
    dict on success, None if the file doesn't exist or is unreadable.

    Atomic-ish: read then unlink in sequence. A concurrent write from
    the engine while we're reading is theoretically possible but
    requires the engine to fire mid-hook-invocation on a different
    thread, which doesn't happen in practice (engine runs on the
    deck's asyncio loop, hook runs in a separate Python subprocess).
    """
    path = deny_pending_path(construct_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Corrupted or unreadable — treat as absent. The construct
        # gets the normal allow/deny based on built-in patterns.
        return None
    # Delete the file so subsequent calls aren't denied repeatedly
    # for the same fire. The construct only deserves one denial per
    # tripwire fire — if the engine wants to deny again, it'll
    # write a fresh file.
    try:
        path.unlink()
    except OSError:
        pass
    if not isinstance(data, dict):
        return None
    return data


def format_deny_pending_reason(flag: dict) -> str:
    """Build the human-readable stderr reason from a deny_pending
    flag. Severity-aware: warning gets the suggestion appended;
    critical announces termination."""
    name = flag.get("tripwire_name", "?")
    description = flag.get("description", "") or "(no description recorded)"
    suggestion = flag.get("suggestion", "")
    severity = flag.get("severity", "warning")
    excerpt = flag.get("matched_text_excerpt", "")
    if severity == "critical":
        msg = (
            f"⚠ tripwire {name} (CRITICAL): {description}. "
            f"Construct will be terminated."
        )
    else:
        msg = f"⚠ tripwire {name}: {description}."
        if suggestion:
            msg += f" Suggested alternative: {suggestion}"
    if excerpt:
        msg += f" [matched: {excerpt}]"
    return msg


# Tools that can change deck or world state. The deny_pending recheck
# (100ms sleep + re-read) only fires for these — read-only tools
# can't be denied by tripwires productively (a tripwire on Read
# would fire after the read already happened) and shouldn't pay the
# latency. This is the same intuition behind slice 3's variable-
# outcome pause UX (write-class only) but applied here as a cheaper
# pre-implementation of the race-mitigation mechanism.
_WRITE_CLASS_TOOLS = frozenset({
    "Write", "Edit", "NotebookEdit", "Bash", "PowerShell",
})


def is_write_class(tool: str) -> bool:
    """Whether this tool can change deck or world state. MCP tools
    are write-class if their verb is in the destructive set; the
    read-shaped ones are auto-allowed by check_default and don't
    need the recheck either."""
    if tool in _WRITE_CLASS_TOOLS:
        return True
    if tool.startswith("mcp__"):
        verb = extract_mcp_verb(tool)
        return verb is not None and verb not in MCP_READ_VERBS
    return False


# =============================================================================
# Slice 3 of the safety architecture pass: variable-outcome delay window.
#
# When the deck's `delay_window_seconds` is > 0, this hook holds
# "interesting" tool calls for up to that many seconds, watching for a
# netrunner X-keypress override file. The call's default action depends
# on the brake state:
#
#   YOLO     — every side-effect call delayed; default = allow; X = deny
#   Default  — only would-deny calls delayed; default = deny;  X = approve
#   Paranoid — only would-deny calls delayed; default = deny;  X = approve
#
# Communication with the deck is via two files in the spawns/ dir:
#   <cid>.delay_pending.json   — written by hook, read by deck
#   <cid>.delay_override.json  — written by deck (on X press), read by hook
#
# The hook owns cleanup: it deletes both files on resolution (override
# applied or expiry). Tripwire-driven denies (deny_pending.json from
# slice 2) bypass the delay entirely — those are hard-stop signals
# from the watchdog, not negotiable.
# =============================================================================


def delay_pending_path(construct_id: str) -> Path:
    """Where the hook writes its delay_pending.json — read by the
    deck's DelayMonitor + DelayPanel. Mirrors the per-spawn settings
    convention used by make_spawn_settings + deny_pending."""
    return (
        cyberdeck_home_dir() / ".cyberdeck" / "spawns"
        / f"{construct_id}.delay_pending.json"
    )


def delay_override_path(construct_id: str) -> Path:
    """Where the deck writes its delay_override.json on X-press —
    polled by the hook every ~100ms during the delay window."""
    return (
        cyberdeck_home_dir() / ".cyberdeck" / "spawns"
        / f"{construct_id}.delay_override.json"
    )


def tool_input_summary(tool: str, tool_input: dict) -> str:
    """Short, human-readable summary of a tool call for the DelayPanel.
    Truncated to 200 chars so the JSON blob the deck reads stays small.
    Tool-specific shaping where it helps (Bash command vs. Write path)."""
    if tool in ("Bash", "PowerShell"):
        cmd = str(tool_input.get("command", ""))
        return cmd[:200]
    if tool in ("Write", "Edit", "NotebookEdit"):
        path = str(tool_input.get("file_path", ""))
        return path[:200]
    if tool == "WebFetch":
        url = str(tool_input.get("url", ""))
        return url[:200]
    if tool.startswith("mcp__"):
        # MCP input is arbitrary JSON; just dump the keys + first
        # values to give the netrunner enough to recognize the call.
        try:
            parts = []
            for k, v in tool_input.items():
                vs = str(v)
                parts.append(f"{k}={vs[:60]}")
            return ("; ".join(parts))[:200]
        except Exception:
            return ""
    # Fallback: dump tool_input compactly.
    try:
        return json.dumps(tool_input, ensure_ascii=False)[:200]
    except Exception:
        return ""


def should_delay(
    brake: str,
    tool: str,
    would_deny: bool,
    delay_window_seconds: float,
) -> bool:
    """Decide whether a call should enter the delay window.

    Matrix per brake state (assumes delay_window_seconds > 0):

      Default / Paranoid: delay only would-deny calls. Allow-class
        calls pass through immediately — they were going to allow
        anyway, no override needed.

      YOLO: delay every side-effect-capable call. Default action is
        allow (would have been allowed anyway); the delay gives the
        netrunner a chance to interrupt with X. Read-class calls
        skip the delay (Read/Glob/Grep/etc. — uninteresting under
        any brake). This means YOLO + delay is opt-in friction
        proportional to the netrunner's intent: "I want ceremony
        on the things that touch state."
    """
    if delay_window_seconds <= 0:
        return False
    if would_deny:
        return True
    if brake == "yolo" and is_write_class(tool):
        return True
    return False


def run_delay_window(
    construct_id: str,
    tool: str,
    tool_input: dict,
    brake: str,
    default_action: str,
    default_reason: str,
    delay_window_seconds: float,
) -> tuple[str, str]:
    """Run the delay window for one tool call.

    Writes <cid>.delay_pending.json with all the context the deck
    needs to render the panel entry. Polls every 100ms for
    <cid>.delay_override.json. Returns (final_action, reason) where
    final_action is "allow" or "deny" and reason is the deny stderr
    text (empty for allow). Cleans up both files before returning.

    The panel-side flow:
      1. DelayMonitor sees the new delay_pending.json appear, emits
         brake.delay_opened bus event.
      2. DelayPanel renders an entry with countdown.
      3. Netrunner focuses entry, presses X.
      4. action_x_focused writes delay_override.json with the
         flipped action.
      5. We poll, see the override, apply it, clean up.

    If steps 3-5 don't happen within delay_window_seconds, we apply
    default_action and clean up. Either way the construct sees a
    deterministic outcome — same protocol the brake hook always had,
    just with timing.
    """
    pending_path = delay_pending_path(construct_id)
    override_path = delay_override_path(construct_id)

    opened_at = time.time()
    deadline = opened_at + delay_window_seconds

    # The flipped-default is what X means for this delay. We pre-
    # compute it so the deck doesn't have to know the matrix; the
    # panel just shows "press X to <override_action>".
    override_action = "deny" if default_action == "allow" else "allow"

    pending = {
        "construct_id": construct_id,
        "tool_name": tool,
        "tool_input_summary": tool_input_summary(tool, tool_input),
        "brake": brake,
        "default_action": default_action,
        "default_reason": default_reason,
        "override_action": override_action,
        "opened_at": opened_at,
        "deadline_ts": deadline,
        "delay_window_seconds": delay_window_seconds,
    }
    try:
        pending_path.write_text(
            json.dumps(pending), encoding="utf-8",
        )
    except OSError:
        # Can't write the panel signal — fall through to default.
        # No silent break: the construct still gets the deterministic
        # brake outcome, the netrunner just doesn't see the delay
        # surface for this call. Filed as a degraded mode.
        return default_action, default_reason

    # Poll for the override file every 100ms. 100ms matches the
    # tripwire deny_pending recheck interval (consistent friction
    # ceiling) and is well below the typical netrunner reaction
    # time, so missing an X-press by 100ms is not realistic.
    poll_interval = 0.1
    final_action = default_action
    final_reason = default_reason

    while time.time() < deadline:
        try:
            if override_path.exists():
                try:
                    raw = override_path.read_text(encoding="utf-8")
                    payload = json.loads(raw) if raw else {}
                except (OSError, json.JSONDecodeError):
                    payload = {}
                requested = payload.get("action", "")
                if requested in ("allow", "deny"):
                    final_action = requested
                    if requested == "deny":
                        final_reason = (
                            f"netrunner X-press during delay window "
                            f"(would have allowed under {brake} brake)"
                        )
                    else:
                        final_reason = ""
                # Clear the override file regardless of validity so
                # the next call from this construct starts clean.
                try:
                    override_path.unlink()
                except OSError:
                    pass
                break
        except OSError:
            pass
        time.sleep(poll_interval)

    # Clean up the pending file. The DelayMonitor sees its
    # disappearance and emits brake.delay_resolved.
    try:
        pending_path.unlink(missing_ok=True)
    except OSError:
        pass
    # Best-effort cleanup of any stale override file (e.g., netrunner
    # pressed X after deadline; we ignored their press but should
    # still clear the file so next call doesn't pick it up).
    try:
        override_path.unlink(missing_ok=True)
    except OSError:
        pass

    return final_action, final_reason


def main() -> int:
    """Read stdin, dispatch on brake, emit stderr + exit code."""
    brake = sys.argv[1] if len(sys.argv) > 1 else "default"
    construct_id = sys.argv[2] if len(sys.argv) > 2 else ""
    # Slice 3: delay_window_seconds is the third argv arg. Float so
    # the deck can express sub-second delays if it ever wants. 0 =
    # no delay = pre-slice-3 behavior. Bad input → 0.0 (fail safe;
    # don't punish the construct for our argv parsing).
    try:
        delay_window_seconds = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
    except ValueError:
        delay_window_seconds = 0.0

    # YOLO + no delay short-circuits to allow. The deck normally
    # omits --settings under YOLO so we wouldn't be invoked at all,
    # but with delay > 0 the deck installs the hook even under YOLO
    # so we run the delay window. Without a delay configured, we're
    # back to the original "fail open under YOLO" behavior.
    if brake == "yolo" and delay_window_seconds <= 0:
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

    # Slice 2 of the safety architecture pass: tripwire deny_pending
    # check. The watchdog's TripwireEngine writes the flag when a
    # warning or critical tripwire matches a bus event; we read it
    # here at the top of every invocation so the next tool call from
    # this construct gets denied with the recorded reason.
    #
    # Race mitigation: when the engine fires on the SAME tool_use
    # event we're currently evaluating (engine sees it via bus, we
    # see it via Claude Code's hook invocation), the engine's flag-
    # write may land just after our first read. For write-class
    # tools, sleep 100ms and re-check. Read-only tools skip the
    # recheck — tripwires don't usefully deny Read/Glob/Grep/etc.
    # (the read already happened by the time anyone could match
    # against it), so the recheck would just add latency for no
    # safety gain.
    #
    # Tripwire denies BYPASS the slice-3 delay window. Tripwires are
    # hard-stop signals from the watchdog (the construct just fired
    # a critical pattern); the delay UX is for the brake's own
    # default-action, not for tripwire-driven denials. The auto-
    # blacklist proposal that critical+bad_enough fires (deferred
    # in slice 2) DOES go through a delay-shaped approval window,
    # but that's a separate file the engine owns, not the brake
    # hook's call.
    if construct_id:
        flag = read_and_clear_deny_pending(construct_id)
        if flag is None and is_write_class(tool):
            time.sleep(0.1)
            flag = read_and_clear_deny_pending(construct_id)
        if flag is not None:
            print(format_deny_pending_reason(flag), file=sys.stderr)
            return 2

    # Compute the brake's default outcome. YOLO falls through to
    # would_deny=False — every call is allow-by-default, but the
    # delay window can still wrap it (see should_delay).
    if brake == "paranoid":
        would_deny, default_reason = check_paranoid(tool, inp)
    elif brake == "default":
        would_deny, default_reason = check_default(tool, inp)
    elif brake == "yolo":
        would_deny, default_reason = False, ""
    else:
        # Unknown brake state — log and allow. The deck shouldn't
        # produce this, but better to fail open than to silently
        # hose every tool call.
        print(
            f"brake_hook: unknown brake state {brake!r}; allowing",
            file=sys.stderr,
        )
        return 0

    # Slice 3: variable-outcome delay window. When delay > 0 and
    # this call qualifies (see should_delay matrix), pause the call
    # and watch for a netrunner X-press override. The deck's
    # DelayMonitor sees the delay_pending.json appear, the
    # DelayPanel renders it with a countdown, the netrunner can
    # focus + press X to flip the default action.
    if construct_id and should_delay(
        brake, tool, would_deny, delay_window_seconds
    ):
        default_action = "deny" if would_deny else "allow"
        final_action, final_reason = run_delay_window(
            construct_id=construct_id,
            tool=tool,
            tool_input=inp,
            brake=brake,
            default_action=default_action,
            default_reason=default_reason,
            delay_window_seconds=delay_window_seconds,
        )
        if final_action == "deny":
            print(final_reason or "denied", file=sys.stderr)
            return 2
        return 0

    if would_deny:
        print(default_reason, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
