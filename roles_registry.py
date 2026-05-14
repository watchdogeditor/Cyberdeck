"""
roles_registry.py — load + default-restore the deck's per-role system
prompts from disk.

Item 000 phase 2 (filed 2026-05-05; shipped 2026-05-11).

The deck's "role" subprocesses — daemon, watchdog Q&A, watchdog
tripwire authoring, advisor — each have a system prompt that
describes their job. Pre-phase-2 these lived as Python constants
inside their respective modules (DAEMON_SYSTEM_PROMPT,
WATCHDOG_SYSTEM_PROMPT, etc.). Editing them required rebuilding the
deck. Phase 2 externalizes them to disk-backed Markdown files in
`<deck-source>/roles/` so the netrunner can tune prompts between
launches without touching Python.

Three roles do NOT externalize:
  - Construct — security-relevant; brake-awareness, dispatcher
    protocol, defense-in-depth content stays in code (per netrunner
    direction 2026-05-11)
  - Mechanic v1 (triage) — recently shipped and verified; works
    well as-is (per netrunner direction 2026-05-11)
  - Mechanic v2 (repair) — same rationale

Externalized roles (4):
  - daemon.md           — Daemon (one-shot + streaming backends)
  - watchdog-qa.md      — Watchdog Q&A oracle (cloud-streaming subprocess)
  - watchdog-authoring.md — Watchdog tripwire-authoring one-shot
  - advisor.md          — Per-tool Advisor (Family A; uses placeholder
                          template tokens substituted at spawn time)

Lifecycle: load-once-at-startup, no hot reload. The netrunner edits a
role file; the change takes effect on the next deck launch. Mid-flight
role-file edits are intentionally ignored — role prompts are stable
across a deck session, mirroring how profile addendums get cached at
spawn time. Restart is the right granularity for prompt-level
changes; a half-applied prompt mid-goal is confusing.

Default-restoration: if a role file is "effectively empty" (only
whitespace, or only the comment header), the registry rewrites it
from the bundled default in `roles/_defaults.py`. The bundled default
includes the comment header so the "save blank to restore" hint
survives — netrunner can blank-reset repeatedly without losing the
help text.

Public surface:

  RoleName               Class-as-namespace constants for the 4 roles.
  RoleEntry              Frozen dataclass — name + text + source_path.
  RoleLoadError          Raised on unrecoverable load failure (e.g. dir
                         doesn't exist AND can't be created).
  RolesRegistry          The loader; load() / get(name) / all().
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from roles._defaults import (
    daemon_default,
    watchdog_qa_default,
    watchdog_authoring_default,
    advisor_default,
)


# Class-as-namespace for role identifiers. Strings rather than an enum
# so callers can pass them as plain strings without type-import gymnastics.
class RoleName:
    DAEMON = "daemon"
    WATCHDOG_QA = "watchdog-qa"
    WATCHDOG_AUTHORING = "watchdog-authoring"
    ADVISOR = "advisor"


# Authoritative map: role name → (filename, default-builder function).
# The default-builder is called lazily at load() time (not at module
# import) to dodge circular imports — _defaults.py would otherwise
# need to import daemon/watchdog/tripwires/advisor at import time,
# and those modules (eventually) need to be importable in any order.
# Calling the builder at load() time means all source modules are
# already loaded; the lazy import inside each builder resolves cleanly.
#
# The builder returns HEADER + "\n\n" + IN_CODE_PROMPT, so the seeded
# file on disk includes the "save blank to restore" comment block
# AND the role's prompt content. Restoration writes the whole thing,
# so the header survives every restore cycle.
_ROLE_FILES: dict[str, tuple[str, Callable[[], str]]] = {
    RoleName.DAEMON: ("daemon.md", daemon_default),
    RoleName.WATCHDOG_QA: ("watchdog-qa.md", watchdog_qa_default),
    RoleName.WATCHDOG_AUTHORING: (
        "watchdog-authoring.md", watchdog_authoring_default,
    ),
    RoleName.ADVISOR: ("advisor.md", advisor_default),
}


# HTML comment block extractor for empty-detection. Multiline + dotall:
# matches <!-- ... --> across newlines. Used to strip comments from
# the file's text before checking whether what's left is whitespace.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class RoleLoadError(Exception):
    """Raised when the registry can't even create the roles directory.
    Catastrophic — deck startup should treat this as fatal.

    Per-file load failures don't raise; they fall back to the bundled
    default and log a warning. Only directory-level catastrophe raises.
    """


@dataclass(frozen=True)
class RoleEntry:
    """One loaded role.

    `name` is the role identifier (e.g. "daemon").
    `text` is the FULL file content as loaded from disk (or the
    bundled default, when the file was effectively empty and got
    restored). Callers consume `.text` directly — comment header is
    INCLUDED in the text (Family-A/B spawn paths pass this whole
    string into --system-prompt-file or --append-system-prompt-file;
    the comment is harmless context for the model).
    `source_path` is where it was loaded from (always
    `<roles_dir>/<filename>`). Useful for error messages.
    `restored_from_default` is True when load() rewrote the file
    because it was effectively empty. Lets the registry's caller log
    a "roles/daemon.md was blank — restored to default" notice.
    """

    name: str
    text: str
    source_path: Path
    restored_from_default: bool = False


def _is_effectively_empty(content: str) -> bool:
    """Return True if `content` has nothing meaningful — just HTML
    comments and whitespace.

    The "save blank to restore" UX works like this: netrunner wipes
    the file (or just deletes everything below the comment header).
    On next deck launch, the registry sees the file is effectively
    empty and rewrites it from the bundled default. The comment
    header is part of the bundled default, so it comes back too —
    the netrunner can blank-reset repeatedly.

    Strip strategy:
      1. Remove all HTML comment blocks (<!-- ... -->, even multiline)
      2. Strip whitespace
      3. If remainder is empty, the file is effectively empty
    """
    without_comments = _HTML_COMMENT_RE.sub("", content)
    return without_comments.strip() == ""


class RolesRegistry:
    """One-shot loader for role files.

    Lifecycle:
      registry = RolesRegistry(roles_dir, bus=bus)
      registry.load()           # synchronous; reads all 4 role files
      text = registry.get("daemon")  # str (the file content)

    No hot reload — role files are read once at startup. The netrunner
    edits a file; the change applies on the next deck launch. This is
    intentional: prompt-level changes mid-flight would produce
    confusing half-applied behavior, and the deck philosophy says
    role prompts are deck-curated content with netrunner calibration
    on top (not session-level state).

    Default-restoration runs during load(): for each role file, if
    the on-disk content is effectively empty (only comments +
    whitespace per _is_effectively_empty), the registry rewrites the
    file from the bundled default and uses the default for this
    session.

    Errors during load() are best-effort: a failed file falls back to
    the bundled default and logs a warning. Only catastrophic dir
    failure (can't create + can't read) raises RoleLoadError.
    """

    def __init__(
        self,
        roles_dir: Path,
        *,
        bus: Optional[object] = None,
    ) -> None:
        self.roles_dir = Path(roles_dir)
        self.bus = bus
        self._entries: dict[str, RoleEntry] = {}
        self._loaded = False

    def load(self) -> None:
        """Read all role files, restoring defaults where needed.

        Idempotent — calling twice is fine, the second call repeats
        the load. Useful for tests; not needed in production since
        the App constructs the registry once.

        Raises RoleLoadError ONLY if the directory can't be created
        and doesn't already exist. Per-file failures fall back to
        defaults silently (with a stderr warning).
        """
        try:
            self.roles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise RoleLoadError(
                f"could not create roles dir {self.roles_dir}: {e!r}"
            ) from e

        self._entries.clear()
        for name, (filename, default_builder) in _ROLE_FILES.items():
            path = self.roles_dir / filename
            # Resolve the bundled default lazily — calling the builder
            # here imports the role's source module (daemon /
            # watchdog / tripwires / advisor). At load() time, all
            # source modules are already loaded by the App's import
            # graph, so the import resolves immediately.
            try:
                default_content = default_builder()
            except Exception as e:
                # If a default-builder fails (e.g. its source module
                # raises on import), we can't seed the file. Log and
                # skip this role — registry continues with the other
                # three.
                print(
                    f"roles_registry: default-builder failed for "
                    f"{name!r}: {e!r} — role unavailable",
                    file=sys.stderr,
                )
                continue
            entry = self._load_or_restore(name, path, default_content)
            self._entries[name] = entry
            self._emit_loaded(entry)

        self._loaded = True

    def _load_or_restore(
        self,
        name: str,
        path: Path,
        default_content: str,
    ) -> RoleEntry:
        """Read one role file. Restore from default if missing or
        effectively empty. Returns the resulting RoleEntry.
        """
        # Case 1: file doesn't exist on disk. Seed with bundled default.
        if not path.is_file():
            try:
                path.write_text(default_content, encoding="utf-8")
            except OSError as e:
                print(
                    f"roles_registry: could not seed {path}: {e!r} — "
                    f"using bundled default in memory only",
                    file=sys.stderr,
                )
            return RoleEntry(
                name=name,
                text=default_content,
                source_path=path,
                restored_from_default=True,
            )

        # Case 2: file exists. Read it.
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            print(
                f"roles_registry: could not read {path}: {e!r} — "
                f"falling back to bundled default in memory",
                file=sys.stderr,
            )
            return RoleEntry(
                name=name,
                text=default_content,
                source_path=path,
                restored_from_default=True,
            )

        # Case 3: file exists but is effectively empty. Restore.
        if _is_effectively_empty(content):
            try:
                path.write_text(default_content, encoding="utf-8")
            except OSError as e:
                print(
                    f"roles_registry: could not restore {path}: {e!r} — "
                    f"using bundled default in memory only",
                    file=sys.stderr,
                )
            return RoleEntry(
                name=name,
                text=default_content,
                source_path=path,
                restored_from_default=True,
            )

        # Case 4: file has real content. Use it.
        return RoleEntry(
            name=name,
            text=content,
            source_path=path,
            restored_from_default=False,
        )

    def _emit_loaded(self, entry: RoleEntry) -> None:
        """Publish a 'roles.loaded' bus event for one role.

        Lets the chatlog announce role-file loads at startup — useful
        signal when the registry restored a default ("daemon role was
        blank, restored"). Best-effort; missing bus is fine.
        """
        if self.bus is None:
            return
        try:
            from event_bus import DeckEvent, Severity
            self.bus.publish(DeckEvent(
                kind="roles.loaded",
                source="roles_registry",
                severity=Severity.INFO,
                payload={
                    "name": entry.name,
                    "source_path": str(entry.source_path),
                    "restored_from_default": entry.restored_from_default,
                    "text_bytes": len(entry.text),
                },
            ))
        except Exception as e:
            print(
                f"roles_registry: bus publish error: {e!r}",
                file=sys.stderr,
            )

    def get(self, name: str) -> Optional[RoleEntry]:
        """Look up a role by name. Returns None if the role isn't
        recognized or hasn't been loaded.

        The text content lives in `entry.text`. Family-A spawn paths
        write it to a tempfile and pass via --system-prompt-file;
        Family-B paths via --append-system-prompt-file.
        """
        return self._entries.get(name)

    def all(self) -> list[RoleEntry]:
        """All loaded role entries, sorted by name for stable output."""
        return sorted(self._entries.values(), key=lambda e: e.name)

    def is_loaded(self) -> bool:
        """True after load() has been called. Lets callers verify
        startup completed before reading."""
        return self._loaded
