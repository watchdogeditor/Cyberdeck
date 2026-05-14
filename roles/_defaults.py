"""
roles/_defaults.py — bundled per-role default content.

Item 000 phase 2 (2026-05-11). Returns the content the roles registry
seeds to disk when a role file is missing or effectively empty.

Two parts per role:

  1. An HTML comment HEADER explaining what the role does + the
     "save blank to restore" hint. Eagerly defined as module-level
     constants — no dependencies on the role's source module.

  2. A PROMPT BODY — the actual system prompt content. Sourced from
     the role's in-code constant (e.g. `daemon.DAEMON_SYSTEM_PROMPT`)
     via LAZY IMPORT: imports happen inside the default-builder
     functions, not at module-load time. This avoids the circular
     import that would otherwise arise from
     `roles_registry` -> `roles._defaults` -> `daemon` -> (eventually)
     `roles_registry`.

The bundled default a role file gets seeded with is HEADER + BODY,
concatenated by the per-role `*_default()` function. The registry
calls these functions at `load()` time, after all source modules
have been imported by the App.

Adding a new role: add an HTML header constant + a default-builder
function below, and register it in `roles_registry._ROLE_FILES`.

NOT externalized (per netrunner direction 2026-05-11):
  - Construct (`construct.py`) — defense-in-depth content; brake
    awareness, dispatcher protocol, security architecture stay
    in code where the netrunner can't accidentally weaken them
  - Mechanic v1 triage (`mechanic_triage.py`) — recently verified;
    works as-is, no ergonomic gain from externalizing
  - Mechanic v2 repair (`mechanic_repair.py`) — same rationale
"""
from __future__ import annotations


# ---- HTML headers (eagerly defined; no dependencies) ----------------------


DAEMON_HEADER = """\
<!--
  Role: Daemon (the persistent coordinator)
  ============================================================
  This file is the system prompt for the deck's daemon
  subprocess — the long-running coordinator that decomposes
  the netrunner's goals into per-construct tasks, observes
  outcomes, and iterates to done.

  Edit freely. Changes take effect on the NEXT deck launch
  (role files are loaded once at startup; mid-flight edits
  are intentionally ignored — a half-applied prompt mid-goal
  is confusing).

  If you save this file with everything below this comment
  block deleted (or if you wipe the whole file), the deck
  will restore the bundled default on the next launch. The
  comment block comes back too, so the "save blank to
  restore" hint persists.
-->"""

WATCHDOG_QA_HEADER = """\
<!--
  Role: Watchdog Q&A (read-only oracle)
  ============================================================
  This file is the system prompt for the watchdog's Q&A
  subprocess — the persistent streaming claude session that
  answers the netrunner's questions about fleet activity.

  The Q&A oracle is the one role that KEEPS Anthropic's
  CLAUDE.md auto-load (per item-000 phase-1 selective policy,
  2026-05-05). It benefits from knowing the deck's filed
  gotchas, design decisions, and in-flight slices when
  answering "what's going on?" questions.

  Edit freely. Changes take effect on the NEXT deck launch.

  Save this file blank (or with only this comment block)
  to restore the bundled default on the next launch.
-->"""

WATCHDOG_AUTHORING_HEADER = """\
<!--
  Role: Watchdog Tripwire Authoring (one-shot regex author)
  ============================================================
  This file is the system prompt for the watchdog's tripwire-
  authoring one-shot subprocess. Fires at goal-set + on non-
  clarification goal updates; the model reads the new goal +
  brake state + existing rules and proposes goal-specific
  deterministic regex tripwires.

  This is a DIFFERENT prompt from the Q&A oracle — same
  watchdog role, different mode. The authoring spawn KILLS
  CLAUDE.md auto-load (no project memory leaks into rule
  authoring); content here is the model's complete context.

  Edit freely. Changes take effect on the NEXT deck launch.

  Save this file blank (or with only this comment block)
  to restore the bundled default on the next launch.
-->"""

ADVISOR_HEADER = """\
<!--
  Role: Advisor (per-tool Q&A bot, Family A)
  ============================================================
  This file is the system-prompt TEMPLATE for the modal-scoped
  Advisor — the bot that answers questions about ONE specific
  tool (binary, script, or plugin) when the netrunner presses
  `h` on a tool's info modal.

  IMPORTANT: this template uses str.format-style placeholders
  that the deck fills in at spawn time. DO NOT remove the
  placeholder tokens or the deck will fail to compose the
  prompt. Valid placeholders:

    {target_name}            tool's name slug
    {target_kind_label}      "a binary CLI tool" / "a deck plugin" etc.
    {target_command}         invocation surface
    {target_description}     one-line description
    {src_block}              pre-rendered SOURCE line (or empty)
    {avail_line}             pre-rendered availability blurb
    {help_block}             pre-rendered HELP TEXT section (or empty)
    {extended_block}         pre-rendered EXTENDED DOCS (or empty)
    {sibling_block}          pre-rendered list of other tool names

  Edit the prose around them freely.

  Changes take effect on the NEXT deck launch. Save this file
  blank (or with only this comment block) to restore the
  bundled default on the next launch.
-->"""


# ---- default-builder functions (lazy imports inside) ----------------------


def daemon_default() -> str:
    """Return the bundled default content for daemon.md.

    Composes HEADER + the in-code DAEMON_SYSTEM_PROMPT. Imports
    daemon lazily so this module is safe to import from
    roles_registry without triggering circular imports.
    """
    from daemon import DAEMON_SYSTEM_PROMPT
    return DAEMON_HEADER + "\n\n" + DAEMON_SYSTEM_PROMPT


def watchdog_qa_default() -> str:
    """Return the bundled default content for watchdog-qa.md.

    Composes HEADER + the in-code WATCHDOG_SYSTEM_PROMPT.
    """
    from watchdog import WATCHDOG_SYSTEM_PROMPT
    return WATCHDOG_QA_HEADER + "\n\n" + WATCHDOG_SYSTEM_PROMPT


def watchdog_authoring_default() -> str:
    """Return the bundled default content for watchdog-authoring.md.

    Composes HEADER + the in-code TRIPWIRE_AUTHORING_SYSTEM_PROMPT.
    """
    from tripwires import TRIPWIRE_AUTHORING_SYSTEM_PROMPT
    return WATCHDOG_AUTHORING_HEADER + "\n\n" + TRIPWIRE_AUTHORING_SYSTEM_PROMPT


def advisor_default() -> str:
    """Return the bundled default content for advisor.md.

    Composes HEADER + the in-code ADVISOR_TEMPLATE. The template
    contains {placeholder} tokens that build_system_prompt() fills
    in per-spawn; the role file preserves these tokens verbatim.
    """
    from advisor import ADVISOR_TEMPLATE
    return ADVISOR_HEADER + "\n\n" + ADVISOR_TEMPLATE
