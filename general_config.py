"""
general_config.py — netrunner identity + global preferences for role
injection.

Item 000 phase 2 (2026-05-11). Loads `<deck-source>/roles/general.toml`
and renders a brief prose block that gets prepended to every role-
injected spawn's system prompt. Same folder as the role markdown files
(per netrunner direction 2026-05-11 — "all configs in one dedicated
folder" — keeps configs discoverable).

Currently surfaces three optional fields:

  [identity]
  name = "Watchdog"           # netrunner's preferred handle
  pronouns = "they/them"      # how the agent should refer to them
  notes = "..."               # free-text preferences (concision, style)

When the file is missing, empty, or all fields are absent / commented
out, the identity block is omitted entirely from spawn prompts —
agents see no extra content. The seeded default file ships with all
fields commented-out so the netrunner can scan and pick which ones
they want active.

Bootstrap: if the file doesn't exist on the first call to `load()`,
write the bundled template to disk. Mirror's the role-file pattern:
restoration is one of the registry's jobs. Subsequent loads use disk
content.

NOT hot-reloaded: same as roles, edits to general.toml apply on the
next deck launch. Mid-flight role-prompt changes would produce
confusing half-applied behavior.
"""
from __future__ import annotations

import sys
import tomllib  # stdlib; Python 3.11+
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Bundled default `general.toml` — written to disk if the file is
# missing at load() time. All fields commented out by design: the
# netrunner uncomments + sets values for the bits they want active.
# An empty / all-commented file produces NO identity block in spawn
# prompts (no surprise behavior change just from seeding the file).
DEFAULT_GENERAL_TOML = """\
# Cyberdeck general config — netrunner identity + global preferences.
#
# This file applies to every spawn that gets role-injection turned on
# (daemon, watchdog Q&A, watchdog tripwire authoring, advisor). The
# deck composes the values below into a brief prose block prepended
# to those spawns' system prompts.
#
# Empty or all-commented = no identity block in spawn prompts.
# Uncomment + edit the fields you want active.
#
# Edits take effect on the NEXT deck launch (general.toml is loaded
# once at startup; mid-flight changes are intentionally ignored).
#
# Delete the file to reset to this template on next launch.

[identity]

# Your preferred handle. When set, agents refer to you by this name
# in chat instead of generic "the netrunner."
# name = "Watchdog"

# Pronouns the agent should use when referring to you.
# pronouns = "they/them"

# Free-text notes — anything that doesn't fit a structured field.
# Goes into the identity block as-is. Keep it brief; this rides
# every spawn's system prompt so token cost compounds.
# Examples:
#   - "Prefer concise answers. No preamble."
#   - "I work primarily on Windows with PowerShell."
#   - "Don't apologize for limitations; just state them."
# notes = \"\"\"
# Prefer concise answers. I don't need preamble or explanations
# of obvious things.
# \"\"\"
"""


@dataclass(frozen=True)
class GeneralConfig:
    """Parsed view of general.toml's [identity] table.

    All fields optional and default to None / empty string. The
    renderer omits sections / lines that resolve to no content, so
    an all-defaults config renders as an empty identity block (which
    callers then omit from the composed system prompt entirely).

    `source_path` records where this came from for diagnostics.
    `loaded_from_default` is True when load() wrote the bundled
    template because the file didn't exist — useful signal in
    startup logs ("general.toml was missing; seeded with template").
    """
    name: Optional[str] = None
    pronouns: Optional[str] = None
    notes: str = ""
    source_path: Optional[Path] = None
    loaded_from_default: bool = False


def _parse_general_toml(content: str) -> tuple[Optional[str], Optional[str], str]:
    """Parse general.toml content. Returns (name, pronouns, notes).

    Tolerant of missing keys / wrong types — anything that doesn't
    fit the expected shape comes back as None / "". The renderer
    deals with missing-content gracefully (omits empty lines / sections).
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as e:
        # Malformed file — log + treat as empty. Restoring the
        # default would clobber the netrunner's edits; better to
        # surface the failure and let them fix it.
        print(
            f"general_config: TOML parse error in general.toml: {e!r} — "
            f"using empty identity (deck continues; fix the file to "
            f"restore identity injection)",
            file=sys.stderr,
        )
        return None, None, ""

    identity = data.get("identity") if isinstance(data, dict) else None
    if not isinstance(identity, dict):
        return None, None, ""

    name = identity.get("name")
    if not isinstance(name, str) or not name.strip():
        name = None

    pronouns = identity.get("pronouns")
    if not isinstance(pronouns, str) or not pronouns.strip():
        pronouns = None

    notes = identity.get("notes")
    if not isinstance(notes, str):
        notes = ""

    return name, pronouns, notes.strip()


def load(general_toml_path: Path) -> GeneralConfig:
    """Read general.toml. Returns a populated or empty GeneralConfig.

    If the file doesn't exist, write the bundled DEFAULT_GENERAL_TOML
    template and return an empty config (the template's fields are
    all commented out, so the parsed result has no content). The
    netrunner can uncomment + set values later; next launch picks
    them up.

    Best-effort throughout: read failures fall back to empty config
    with a stderr warning. Empty config means "no identity block in
    spawn prompts" — a silent, harmless default.
    """
    path = Path(general_toml_path)

    # Case 1: file doesn't exist. Seed and return empty config.
    if not path.is_file():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(DEFAULT_GENERAL_TOML, encoding="utf-8")
        except OSError as e:
            print(
                f"general_config: could not seed {path}: {e!r} — "
                f"deck continues without identity injection",
                file=sys.stderr,
            )
        return GeneralConfig(
            source_path=path,
            loaded_from_default=True,
        )

    # Case 2: file exists. Parse it.
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as e:
        print(
            f"general_config: could not read {path}: {e!r} — "
            f"using empty identity",
            file=sys.stderr,
        )
        return GeneralConfig(source_path=path)

    name, pronouns, notes = _parse_general_toml(content)
    return GeneralConfig(
        name=name,
        pronouns=pronouns,
        notes=notes,
        source_path=path,
        loaded_from_default=False,
    )


def render_identity_block(config: GeneralConfig) -> str:
    """Render the identity block to prepend to role-injected spawns.

    Returns "" when all fields are empty — caller should then omit
    the block from the composed system prompt entirely (no empty
    section header).

    Block shape (when populated):

        NETRUNNER IDENTITY:
        The netrunner's chosen handle is "Watchdog" (they/them).
        <notes content, if any>

    Keep this brief; it rides every spawn's system prompt so token
    cost compounds. The agent doesn't need a profile; it needs to
    know how to refer to the human.
    """
    parts: list[str] = []

    if config.name and config.pronouns:
        parts.append(
            f'The netrunner\'s chosen handle is "{config.name}" '
            f"({config.pronouns})."
        )
    elif config.name:
        parts.append(f'The netrunner\'s chosen handle is "{config.name}".')
    elif config.pronouns:
        parts.append(f"The netrunner's pronouns are {config.pronouns}.")

    if config.notes:
        if parts:
            parts.append("")
        parts.append(config.notes)

    if not parts:
        return ""

    return "NETRUNNER IDENTITY:\n" + "\n".join(parts)
