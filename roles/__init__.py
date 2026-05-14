"""roles/ — externalized per-role system prompts.

Item 000 phase 2 (2026-05-11). Disk-backed Markdown files containing
the system prompts for the deck's user-editable role subprocesses:
daemon, watchdog Q&A, watchdog tripwire authoring, advisor.

The netrunner edits these files between launches to tune prompts
without touching Python source. Constructs / pool warmers / Mechanic
all stay in code (per netrunner direction 2026-05-11): defense-in-
depth content + recently-verified prompts shouldn't be user-editable.

`general.toml` lives in this folder too — netrunner identity block
that gets prepended to every role-injected spawn's system prompt.

See `roles_registry.py` for the loader + default-restore logic and
`Design Files/in-flight/cyberdeck-spawn-context-isolation.md` for
the full design rationale.
"""
