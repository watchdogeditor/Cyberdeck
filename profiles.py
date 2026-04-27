"""
Profiles: saved Construct configurations on disk.

A profile is a hand-authored TOML file that captures a way of working —
addendums that steer the daemon and constructs when the profile is
active, plus a list of recommended tools the daemon should suggest.
Profiles are PRESCRIPTIVE templates, not restrictive enforcement: they
save the daemon from re-typing common instructions per-spawn, and they
nudge constructs toward the right tools for the job. They do NOT
narrow capabilities — the deck-global brake state (see brake_state.py)
is the single source of runtime constraint, and it lives outside the
profile system entirely so the netrunner controls it deck-wide.

This module is the *data layer* only — it loads, validates, and exposes
Profile objects. It does NOT:

- Watch the profiles directory for changes (that's the registry).
- Inject addendums into actual prompts (that's the construct + daemon
  spawn paths).
- Track which profile is active for a given construct.
- Enforce any tool restrictions (the brake hook does that, deck-wide).

Profile shape:

    name = "recon_specialist"
    category = "Recon"
    description = "Network and wireless reconnaissance work."

    default_daemon_addendum = \"\"\"...\"\"\"
    default_construct_addendum = \"\"\"...\"\"\"

    recommended_tools = ["Bash", "Read", "WebSearch"]
    default_scripts = ["scan_wifi", "geolocate_subject"]

Required: name, category, description. Everything else has a sensible
default. `recommended_tools` surface in the construct's system-prompt
addendum so the model knows what's idiomatic for this profile;
they're a soft signal, not a hard cap. `default_scripts` is stored
but not yet consumed — forward-compat parking for the deferred
Scripts system.
"""
from __future__ import annotations

import re
import sys
import tomllib  # stdlib; Python 3.11+
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Known Claude Code tools as of this iteration. Used for soft-validation
# of a profile's `recommended_tools` list — unknown tool names warn but
# don't reject, since (a) Anthropic occasionally adds new tools and
# (b) MCP-registered tools have arbitrary names. Update freely; this
# is advisory, not authoritative.
KNOWN_TOOLS: frozenset[str] = frozenset({
    "Bash",
    "BashOutput",
    "KillShell",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "TodoWrite",
    "Task",
    "NotebookEdit",
    "NotebookRead",
})


# Profile names should be safe to use as filenames, dict keys, and CLI
# args. Slug rule: lowercase letters, digits, underscore, hyphen. No
# spaces, no path separators, no leading hyphen. Reasonably permissive.
_NAME_PATTERN = re.compile(r"^[a-z0-9_][a-z0-9_-]*$")


class ProfileValidationError(ValueError):
    """Raised when a profile TOML file fails validation. Always includes
    the source path so the netrunner can find the offending file
    quickly. Wraps detail in the message for human readability."""

    def __init__(self, message: str, *, path: Optional[Path] = None) -> None:
        prefix = f"{path}: " if path is not None else ""
        super().__init__(f"{prefix}{message}")
        self.path = path


@dataclass(frozen=True)
class Profile:
    """A loaded, validated profile.

    Frozen because profiles are immutable once loaded — the registry
    (C1b) replaces them on file change rather than mutating in place.
    Comparison-by-value so the registry can detect "actually changed"
    vs "file touched but identical content."
    """

    # Identity
    name: str
    category: str
    description: str

    # Steering text — appended to baseline daemon and construct prompts
    # when this profile is active. Empty string means "no steering" —
    # use the deck baseline as-is.
    default_daemon_addendum: str = ""
    default_construct_addendum: str = ""

    # Recommended tools for this profile — surfaced in the construct's
    # system-prompt addendum as a soft suggestion ("for this kind of
    # work, prefer X / Y / Z"). NOT a hard cap; the construct still
    # has access to all default tools. The deck-global brake state
    # (brake_state.py) is the only thing that actually narrows
    # capability at runtime, deck-wide. Empty tuple means "no
    # specific recommendation" — let the construct pick.
    recommended_tools: tuple[str, ...] = field(default_factory=tuple)

    # Forward-compat: stored but not yet consumed. Lands when the
    # Scripts registry grows the manifest layer.
    default_scripts: tuple[str, ...] = field(default_factory=tuple)

    # Provenance — where on disk this profile came from. Useful for
    # error messages and the registry's file→profile reverse lookup.
    # Optional because tests may construct Profiles in-memory.
    source_path: Optional[Path] = None

    def __post_init__(self) -> None:
        # Final guard: even if someone constructs a Profile directly
        # bypassing load_profile(), we still want name validation.
        # Frozen dataclasses can call object.__setattr__ in __post_init__
        # if needed, but here we just validate.
        if not _NAME_PATTERN.match(self.name):
            raise ProfileValidationError(
                f"profile name {self.name!r} must match {_NAME_PATTERN.pattern} "
                f"(lowercase, digits, underscore, hyphen; no leading hyphen)",
                path=self.source_path,
            )


def load_profile(path: Path) -> Profile:
    """Load and validate a single profile TOML file.

    Raises ProfileValidationError on any structural problem (missing
    required field, wrong type, malformed name, etc.). Warnings — for
    things that are suspicious but not fatal, like unknown tool names
    or filename/name mismatch — are printed to stderr and the load
    continues. The netrunner sees them on launch but isn't blocked.

    The returned Profile carries `source_path` so downstream code
    (registry, error messages, hot-reload) can find the file.
    """
    if not path.is_file():
        raise ProfileValidationError(
            f"profile file not found", path=path,
        )

    try:
        with path.open("rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProfileValidationError(
            f"TOML parse error: {e}", path=path,
        ) from e

    if not isinstance(raw, dict):
        raise ProfileValidationError(
            "TOML root must be a table (key-value mapping)", path=path,
        )

    # Required fields
    name = _require_str(raw, "name", path)
    if not _NAME_PATTERN.match(name):
        raise ProfileValidationError(
            f"profile name {name!r} must match {_NAME_PATTERN.pattern} "
            f"(lowercase, digits, underscore, hyphen; no leading hyphen)",
            path=path,
        )
    category = _require_str(raw, "category", path)
    if not category.strip():
        raise ProfileValidationError(
            "category must be non-empty", path=path,
        )
    description = _require_str(raw, "description", path)
    if not description.strip():
        raise ProfileValidationError(
            "description must be non-empty", path=path,
        )

    # Optional fields with type checking
    daemon_addendum = _optional_str(raw, "default_daemon_addendum", path, default="")
    construct_addendum = _optional_str(raw, "default_construct_addendum", path, default="")
    recommended_tools = _optional_str_list(raw, "recommended_tools", path)
    default_scripts = _optional_str_list(raw, "default_scripts", path)

    # Soft warnings (write to stderr, don't fail)
    _warn_unknown_keys(raw, path)
    _warn_filename_mismatch(name, path)
    _warn_unknown_tools(recommended_tools, path)

    return Profile(
        name=name,
        category=category,
        description=description,
        default_daemon_addendum=daemon_addendum,
        default_construct_addendum=construct_addendum,
        recommended_tools=tuple(recommended_tools),
        default_scripts=tuple(default_scripts),
        source_path=path,
    )


# ---- internal helpers ------------------------------------------------------


# All TOML fields the loader knows about. Keys outside this set get
# warned about — usually a typo (`descripton` instead of `description`)
# or a stale field from an older spec. Add new fields here as the
# profile schema grows.
_KNOWN_KEYS: frozenset[str] = frozenset({
    "name",
    "category",
    "description",
    "default_daemon_addendum",
    "default_construct_addendum",
    "recommended_tools",
    "default_scripts",
})


def _require_str(raw: dict, key: str, path: Path) -> str:
    """Pull a required string field. Raise if missing or wrong type."""
    if key not in raw:
        raise ProfileValidationError(
            f"missing required field {key!r}", path=path,
        )
    val = raw[key]
    if not isinstance(val, str):
        raise ProfileValidationError(
            f"field {key!r} must be a string, got {type(val).__name__}",
            path=path,
        )
    return val


def _optional_str(raw: dict, key: str, path: Path, *, default: str) -> str:
    """Pull an optional string field. Use default if absent. Raise on
    type mismatch — silently swallowing wrong types would mask typos
    in the TOML."""
    if key not in raw:
        return default
    val = raw[key]
    if not isinstance(val, str):
        raise ProfileValidationError(
            f"field {key!r} must be a string, got {type(val).__name__}",
            path=path,
        )
    return val


def _optional_str_list(raw: dict, key: str, path: Path) -> list[str]:
    """Pull an optional list-of-strings field. Default is empty list.
    Reject non-list types and non-string elements outright — typos
    here silently break tool whitelists or script references."""
    if key not in raw:
        return []
    val = raw[key]
    if not isinstance(val, list):
        raise ProfileValidationError(
            f"field {key!r} must be a list, got {type(val).__name__}",
            path=path,
        )
    for i, item in enumerate(val):
        if not isinstance(item, str):
            raise ProfileValidationError(
                f"field {key!r}[{i}] must be a string, got "
                f"{type(item).__name__}",
                path=path,
            )
    return list(val)


def _warn_unknown_keys(raw: dict, path: Path) -> None:
    """Warn about TOML keys we don't recognize. Probably typos."""
    unknown = set(raw.keys()) - _KNOWN_KEYS
    if unknown:
        keys_csv = ", ".join(sorted(unknown))
        print(
            f"profiles: warning: {path.name}: unknown field(s) {keys_csv} "
            f"— typo, or schema drift?",
            file=sys.stderr,
        )


def _warn_filename_mismatch(name: str, path: Path) -> None:
    """Warn if `name` field doesn't match the filename stem.

    Convention is `recon_specialist.toml` contains `name = "recon_specialist"`.
    The `name` field is authoritative — we don't reject mismatches —
    but a mismatch is almost always a copy/rename mistake the netrunner
    wants to know about."""
    if path.stem != name:
        print(
            f"profiles: warning: {path.name}: filename stem {path.stem!r} "
            f"does not match name field {name!r} — rename one to match?",
            file=sys.stderr,
        )


def _warn_unknown_tools(tools: list[str], path: Path) -> None:
    """Warn about tool names not in our known set. Could be typos
    (`WebFetcher` instead of `WebFetch`), could be MCP-registered
    tools we don't know about, could be tools added since this
    KNOWN_TOOLS constant was last updated. Either way, surface."""
    unknown = [t for t in tools if t not in KNOWN_TOOLS]
    if unknown:
        tools_csv = ", ".join(unknown)
        print(
            f"profiles: warning: {path.name}: unknown tool name(s): "
            f"{tools_csv} — typo, custom MCP tool, or stale validator?",
            file=sys.stderr,
        )
