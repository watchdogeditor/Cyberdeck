"""
Tools: the registered system-CLI surface the deck knows about.

A "tool" is a system-installed binary (on PATH) or a script (on disk)
that the deck doesn't ship with — it just declares awareness of it
and surfaces the declaration to constructs through profile tool
lists. The deck does NOT install tools. The netrunner installs them
through whatever package manager fits the platform; the deck merely
checks at load time whether each declared tool is reachable, and
greys-out missing entries in the Tools panel.

This module is the data layer: the Tool dataclass + loader for the
single `tools.toml` registry file. Pure data; zero integration with
fleet/daemon/TUI. The registry layer (tools_registry.py) handles
the on-disk file watcher + bus events.

Manifest shape (per entry; tools.toml carries an array):

    [[tool]]
    name        = "ripgrep"
    kind        = "binary"       # "binary" | "script"
    command     = "rg"
    description = "Fast recursive grep written in Rust."
    # path      = "..."          # optional; for binaries an override,
                                  #   for scripts the script path. The
                                  #   token ${tools_dir} substitutes to
                                  #   <home>/tools/.
    # help_text = "..."          # optional; longer text, not auto-
                                  #   injected into construct prompts.

Required: name, kind, command, description. `path` is required for
kind="script" (no PATH-resolution fallback for scripts), optional for
kind="binary" (overrides the PATH lookup if set). `help_text` is
optional everywhere.

Existence check at load time:
  - kind="binary": shutil.which(path or command). Hit → available;
    miss → available=False with reason "cannot locate <command> on PATH".
  - kind="script":  Path(path).exists() AND is_file(). Hit → available;
    miss → available=False with reason "script not found at <path>".

Failing checks downgrade the entry to `available=False` but keep it
in the registry so the netrunner sees what's declared but missing.
The Tools panel renders unavailable tools with a red ✗ glyph + dim
name, mirroring the plugin-availability pattern.

Spec note: this is the post-retool shape (filed 2026-05-02 in
`Design Files/cyberdeck-tools-plugins-profiles-retool.md`). Pre-
retool, "tools" meant three different things in three places (panel
header, profile field, directory tree). Tools, plugins, profiles
now have clean three-way separation.
"""
from __future__ import annotations

import re
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Tool name slug: same rule as profile/plugin names. Lowercase letters,
# digits, underscore, hyphen. Used as the lookup key in profile tools
# lists, so it has to round-trip through TOML cleanly.
_NAME_PATTERN = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")

_VALID_KINDS: frozenset[str] = frozenset({"binary", "script"})


class ToolValidationError(ValueError):
    """Raised when a tool entry in tools.toml fails validation. Always
    includes the source path so the netrunner can find the offending
    file quickly."""

    def __init__(self, message: str, *, path: Optional[Path] = None) -> None:
        prefix = f"{path}: " if path is not None else ""
        super().__init__(f"{prefix}{message}")
        self.path = path


@dataclass(frozen=True)
class Tool:
    """A loaded, validated tool declaration.

    Frozen because tool entries are immutable once loaded. The
    registry rebuilds the entire view if the tools.toml file changes
    (rather than mutating in place).

    `available` reflects whether the existence check passed at load
    time. False-but-listed tools surface in the Tools panel with a
    dim treatment so the netrunner sees what's declared but absent.
    Profile tool lists silently skip unavailable tools when injected
    into a construct's prompt addendum (so the construct doesn't see
    invocations it can't actually make).
    """

    # Identity (required)
    name: str
    kind: str               # "binary" | "script"
    command: str
    description: str

    # Optional fields
    path: Optional[str] = None
    help_text: str = ""

    # Provenance — which tools.toml declared this entry. Optional so
    # tests can construct in-memory tools.
    source_path: Optional[Path] = None

    # Resolved invocation. For binaries this is the absolute path
    # `shutil.which` returned; for scripts, the resolved `path` field
    # (with ${tools_dir} substituted). None when unavailable.
    resolved_command: Optional[str] = None

    # Availability gate.
    available: bool = True
    unavailable_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not _NAME_PATTERN.match(self.name):
            raise ToolValidationError(
                f"tool name {self.name!r} must match {_NAME_PATTERN.pattern}",
                path=self.source_path,
            )
        if self.kind not in _VALID_KINDS:
            raise ToolValidationError(
                f"tool {self.name!r}: kind must be one of "
                f"{sorted(_VALID_KINDS)}, got {self.kind!r}",
                path=self.source_path,
            )


def load_tools(tools_toml: Path, *, tools_dir: Path) -> list[Tool]:
    """Load and validate every entry in a tools.toml file.

    Returns a list of Tool objects in declaration order. Entries
    that fail structural validation raise ToolValidationError; the
    registry layer catches per-entry errors and surfaces them as
    `tool.scan_error` events so a single bad entry doesn't blow the
    whole file. Existence-check failures are NOT fatal — they
    downgrade the entry to `available=False`.

    `tools_dir` is the absolute `<home>/tools/` directory; used to
    substitute `${tools_dir}` in the `path` field. Anything else
    inside `path` is left literal.

    Empty files (no `[[tool]]` entries) return an empty list — that's
    a valid state, just means no tools are registered yet.
    """
    if not tools_toml.is_file():
        # File doesn't exist — registry layer handles seeding; return
        # empty here so callers can call this before seeding.
        return []

    try:
        with tools_toml.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ToolValidationError(
            f"TOML parse error in tools.toml: {exc}",
            path=tools_toml,
        ) from exc

    if not isinstance(raw, dict):
        raise ToolValidationError(
            "tools.toml root must be a table", path=tools_toml,
        )

    entries = raw.get("tool")
    if entries is None:
        # Empty registry — file exists but no [[tool]] entries declared.
        return []
    if not isinstance(entries, list):
        raise ToolValidationError(
            "[[tool]] must be an array of tables", path=tools_toml,
        )

    tools: list[Tool] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ToolValidationError(
                f"[[tool]] entry {i} must be a table",
                path=tools_toml,
            )
        tool = _load_one_tool(entry, tools_toml, tools_dir, index=i)
        if tool.name in seen_names:
            # Duplicate names in the same file: last-write-wins with
            # a warning. Same posture as profile name conflicts.
            print(
                f"tools: warning: {tools_toml}: duplicate tool name "
                f"{tool.name!r} at entry {i} — last-write-wins, "
                f"dropping previous entry",
                file=sys.stderr,
            )
            tools = [t for t in tools if t.name != tool.name]
        seen_names.add(tool.name)
        tools.append(tool)
    return tools


# ---- internals -------------------------------------------------------------


_KNOWN_TOOL_KEYS = frozenset({
    "name", "kind", "command", "description", "path", "help_text",
})


def _load_one_tool(
    entry: dict, source: Path, tools_dir: Path, *, index: int,
) -> Tool:
    """Validate one [[tool]] table and run its existence check."""
    # Required fields
    name = _require_str(entry, "name", source, index)
    if not _NAME_PATTERN.match(name):
        raise ToolValidationError(
            f"[[tool]] entry {index}: name {name!r} must match "
            f"{_NAME_PATTERN.pattern}",
            path=source,
        )
    kind = _require_str(entry, "kind", source, index).strip()
    command = _require_str(entry, "command", source, index).strip()
    if not command:
        raise ToolValidationError(
            f"[[tool]] {name!r}: command must be non-empty",
            path=source,
        )
    description = _require_str(entry, "description", source, index).strip()
    if not description:
        raise ToolValidationError(
            f"[[tool]] {name!r}: description must be non-empty",
            path=source,
        )

    # Optional fields
    path_raw = entry.get("path")
    if path_raw is not None and not isinstance(path_raw, str):
        raise ToolValidationError(
            f"[[tool]] {name!r}: path must be a string if present",
            path=source,
        )
    path_resolved: Optional[str] = None
    if path_raw is not None:
        path_resolved = _substitute_path(path_raw, tools_dir)

    help_text = entry.get("help_text", "") or ""
    if not isinstance(help_text, str):
        raise ToolValidationError(
            f"[[tool]] {name!r}: help_text must be a string",
            path=source,
        )

    # kind="script" requires path; kind="binary" accepts path as override.
    # Validation here covers the structural constraint; existence check
    # below is what determines `available`.
    if kind == "script" and not path_resolved:
        raise ToolValidationError(
            f"[[tool]] {name!r}: kind='script' requires a path field",
            path=source,
        )

    # Existence check + resolved_command computation. Failures
    # downgrade to unavailable but DON'T raise — the entry stays in
    # the registry as a missing-tool marker.
    available, reason, resolved_command = _check_existence(
        name=name,
        kind=kind,
        command=command,
        path=path_resolved,
    )

    # Soft warning for unknown keys — typo guard.
    unknown = set(entry.keys()) - _KNOWN_TOOL_KEYS
    if unknown:
        print(
            f"tools: warning: {source}: [[tool]] {name!r} has unknown "
            f"key(s) {sorted(unknown)} — typo, or schema drift?",
            file=sys.stderr,
        )

    return Tool(
        name=name,
        kind=kind,
        command=command,
        description=description,
        path=path_resolved,
        help_text=help_text,
        source_path=source,
        resolved_command=resolved_command,
        available=available,
        unavailable_reason=reason,
    )


def _check_existence(
    *, name: str, kind: str, command: str, path: Optional[str],
) -> tuple[bool, Optional[str], Optional[str]]:
    """Returns (available, reason, resolved_command).

    For binaries: prefer `path` if set (caller validated it as a
    string); otherwise PATH-lookup `command`. For scripts: `path`
    is required (caller already enforced it).
    """
    if kind == "binary":
        if path:
            # Caller-supplied override path. Don't run shutil.which —
            # that would re-search PATH and ignore the explicit
            # request. Just check the path exists and is executable.
            p = Path(path)
            if p.is_file():
                return True, None, str(p)
            return (
                False,
                f"binary {command!r}: path {path!r} not found",
                None,
            )
        which = shutil.which(command)
        if which is not None:
            return True, None, which
        return (
            False,
            f"cannot locate {command!r} on PATH",
            None,
        )

    if kind == "script":
        # path was enforced by caller; this is just a sanity check.
        assert path is not None
        p = Path(path)
        if p.is_file():
            # resolved_command is `<command> <path>` — e.g.,
            # `python /home/user/tools/scan/main.py`. Construct
            # invocation site stitches in any args.
            return True, None, f"{command} {p}"
        return (
            False,
            f"script {name!r}: file not found at {path}",
            None,
        )

    # Unreachable — kind validated by Tool.__post_init__. Defensive.
    return False, f"unknown kind {kind!r}", None


def _substitute_path(raw: str, tools_dir: Path) -> str:
    """Substitute ${tools_dir} → absolute tools_dir path. Other
    tokens left literal. Single substitution token; we're not
    building a templating language here."""
    return raw.replace("${tools_dir}", str(tools_dir))


def _require_str(raw: dict, key: str, source: Path, index: int) -> str:
    if key not in raw:
        raise ToolValidationError(
            f"[[tool]] entry {index}: missing required field {key!r}",
            path=source,
        )
    val = raw[key]
    if not isinstance(val, str):
        raise ToolValidationError(
            f"[[tool]] entry {index}: field {key!r} must be a string, "
            f"got {type(val).__name__}",
            path=source,
        )
    return val
