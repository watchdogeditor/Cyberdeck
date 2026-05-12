"""
Mechanic v2 LLM-session — config-file repair proposals.

Pairs with mechanic_triage.py (v1, diagnose-only) and mechanic.py (v0,
supervisor). v2 extends the LLM-session half from "read and explain"
to "read and PROPOSE FIXES the netrunner approves with one keystroke."

Scope is deliberately narrow — three config-file shapes only:

  - <home>/.cyberdeck/state.json   (brake state + limits namespace)
  - <home>/profiles/*.toml         (construct profile templates)
  - <home>/tools/tools.toml        (system-CLI registry)

NOT in scope: deck source (*.py), design docs, log files, plugin code,
the dispatcher script. The brake hook protects deck source from
constructs; v2 mirrors that boundary for the mechanic's WRITE surface
(its READ surface stays full per v1). If something in deck source
needs editing, that's a netrunner-driven task, not a mechanic action.

Activation paths (item 0h, 2026-05-08):

  Triage-coupled — after iterative triage finishes (success or partial),
                   the mechanic offers a config repair scan via stderr
                   prompt. The triage report's "Repair recommendation"
                   section sets the prompt's default ([Y/n] when
                   recommended, [y/N] otherwise). Repair runs as a FRESH
                   spawn (own system prompt, own user prompt with config
                   contents inlined) — not --resume off the triage
                   session. Cleaner separation; the LLM gets a fresh
                   context with the right instructions for the repair
                   role rather than carrying the triage role's framing
                   into the new task.

  Standalone summon — `python mechanic.py --repair` skips the supervisor
                      loop entirely and fires a one-shot repair scan.
                      Useful for "I think my profile got mangled, scan
                      and propose fixes."

Trust separation (key design principle):

  The LLM is READ-ONLY (Read/Glob/Grep tools) and proposes via OUTPUT.
  The mechanic APPLIES — diff display, per-proposal y/N approval, hard
  path-allowlist check, backup-before-write. The LLM's output is text;
  the mechanic's Python is what touches disk. Same shape as tripwires
  (LLM authors patterns; deterministic engine enforces).

Sanity-check semantics (per netrunner direction):

  Propose fixes ONLY when the value SHAPE is wrong:
    - File doesn't parse (JSON/TOML syntax error)
    - Required field missing
    - Field type wrong (e.g. delay_window_seconds = "five" — should be float)
    - Value violates documented enum (e.g. brake = "potato")
    - Reference to nonexistent file (broken path)

  Do NOT propose fixes for non-default values that are still type-valid.
  Acknowledge them in the report's "Non-default values noticed" section
  (so the netrunner sees them) but treat as informational only. The
  netrunner is allowed to set delay_window_seconds=0; that's a setting,
  not a corruption.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# Caliber: same as triage. Repair is reasoning-heavy (validate a TOML
# against a schema, identify what's structurally broken, propose
# fixes that round-trip the file format) but not novel-synthesis.
# Sonnet+medium follows narrowly-scoped instruction lists reliably.
REPAIR_MODEL = "sonnet"
REPAIR_EFFORT = "medium"


# Per-call timeout. Repair walks 1-5 small config files + may Read
# the deck source's seed-default constants for comparison. 240s is
# generous; the model usually finishes in 60-120s. Bumped above
# triage's 180s default because repair's structured output (full
# proposed_content for each file) is longer than a triage report.
DEFAULT_TIMEOUT = 240.0


# Read-only tool subset. Repair has the same surface as triage v1 —
# Read/Glob/Grep, no Bash, no Write, no Edit, no Web, no subagents.
# The "write" half of v2 is implemented in this file's apply_proposal
# function, not via tool access.
REPAIR_ALLOWED_TOOLS = "Read,Glob,Grep"


# Hard whitelist of writable path patterns. The mechanic refuses to
# apply any proposal whose target file doesn't match one of these,
# regardless of what the LLM says. This is the trust boundary —
# the LLM is told the whitelist (via system prompt) AND the deck
# enforces it (via is_writable_path below).
#
# Three patterns:
#   1. exact: <home>/.cyberdeck/state.json
#   2. exact: <home>/tools/tools.toml
#   3. glob:  <home>/profiles/*.toml  (any TOML file in profiles dir)
#
# Path equality uses os.path.normcase + normpath to handle Windows's
# forward-vs-backslash + drive-letter-case quirks (filed gotcha:
# "String equality on file paths is wrong on Windows").


def _normalize(p: Path) -> str:
    """Normalize a path for stable comparison.

    Resolve to absolute, then normcase + normpath so Windows
    differences (forward/backslash, drive-letter case) compare equal.
    Used by is_writable_path for allowlist matching.
    """
    try:
        return os.path.normcase(os.path.normpath(str(Path(p).resolve())))
    except OSError:
        # Path doesn't exist yet — resolve() can still work but on
        # some platforms returns a non-existent absolute. Fall back
        # to absolute() which doesn't require the file to exist.
        return os.path.normcase(os.path.normpath(str(Path(p).absolute())))


def is_writable_path(home_dir: Path, target: Path) -> bool:
    """Return True iff `target` matches one of the three writable
    patterns under `home_dir`. False otherwise — the mechanic will
    refuse to apply any proposal that returns False here.

    Pattern 1: <home>/.cyberdeck/state.json (exact)
    Pattern 2: <home>/tools/tools.toml      (exact)
    Pattern 3: <home>/profiles/<slug>.toml  (any TOML in profiles dir)
    """
    home_norm = _normalize(home_dir)
    target_norm = _normalize(target)

    if target_norm == _normalize(home_dir / ".cyberdeck" / "state.json"):
        return True
    if target_norm == _normalize(home_dir / "tools" / "tools.toml"):
        return True

    # Pattern 3: target's parent is the profiles dir, AND extension is .toml.
    profiles_dir_norm = _normalize(home_dir / "profiles")
    target_parent_norm = _normalize(Path(target).parent)
    if target_parent_norm == profiles_dir_norm and Path(target).suffix.lower() == ".toml":
        return True

    return False


# ---- dataclasses ----------------------------------------------------------


@dataclass(frozen=True)
class RepairRequest:
    """One repair scan activation.

    `home_dir` is the deck home. The mechanic scans config files
    relative to this (.cyberdeck/state.json, profiles/*.toml,
    tools/tools.toml). Required.

    `deck_source_dir` is the dir containing tui.py + Design Files/.
    The LLM walks the source for seed-default constants (e.g.
    profile_registry.DEFAULT_PROFILE_TOML, tools_registry.DEFAULT_TOOLS_TOML)
    so it can compare a corrupted file against what it should look like.
    Optional; when unset the LLM has only the current config state.

    `triage_report_path` carries the just-finished triage report (when
    repair fires as a triage follow-on). The repair prompt includes a
    pointer + an excerpt so the LLM knows what kind of failure
    triggered the scan. None for standalone summons.

    `log_path` is the just-died deck's log (mirrors TriageRequest); used
    only for naming the report file (`<log-basename>-repair.md` next to
    the triage report). None for standalone summons — falls back to
    `<home>/.cyberdeck/repair-<timestamp>.md`.
    """
    home_dir: Path
    deck_source_dir: Optional[Path] = None
    triage_report_path: Optional[Path] = None
    log_path: Optional[Path] = None
    report_dir: Optional[Path] = None
    claude_bin: str = "claude"
    timeout: float = DEFAULT_TIMEOUT


@dataclass(frozen=True)
class RepairProposal:
    """One proposed config-file change.

    `kind` is one of "overwrite" / "restore_default" / "delete":
      - overwrite        : replace file contents with proposed_content
      - restore_default  : same shape; LLM supplies the auto-seeded
                           version. Distinct in the report so the
                           netrunner sees "this fix reverts to deck
                           default" vs "this is a custom fix"
      - delete           : remove the file (file gets backed up first;
                           the registry will re-seed on next scan)

    `proposed_content` is the COMPLETE post-fix file contents (str)
    for overwrite/restore_default, or None for delete.

    `severity` is informational — used only for stderr formatting.
    Doesn't gate any behavior; netrunner sees them all and decides.
    """
    file_path: Path
    kind: str  # "overwrite" | "restore_default" | "delete"
    proposed_content: Optional[str]
    rationale: str
    severity: str = "warning"  # "low" | "warning" | "critical"


@dataclass
class RepairResult:
    """Aggregate result of one repair scan + apply pass.

    `success` mirrors the LLM call (True on parsed-output, False on
    spawn / timeout / parse failure). Note: success=True with
    proposals_seen=0 is the GOOD case — clean scan, nothing broken.

    `proposals_*` counters: applied = wrote successfully;
    rejected_by_user = netrunner declined; rejected_by_path = mechanic
    refused (allowlist violation); failed = write error.

    `report_path` is the on-disk location of the LLM's full output
    (Markdown). Always written when the LLM call produced output, even
    on parse failure (the raw is preserved for debugging).

    `session_id` is the claude-side session uuid from system/init.
    Captured for parity with triage — not currently used for anything
    in v2 (repair doesn't deepen-loop), but the field exists if a
    future iteration wants to chain repair passes.
    """
    success: bool
    report_text: str = ""
    report_path: Optional[Path] = None
    proposals_seen: int = 0
    proposals_applied: int = 0
    proposals_rejected_by_user: int = 0
    proposals_rejected_by_path: int = 0
    proposals_failed: int = 0
    error: Optional[str] = None
    summary_line: str = ""
    elapsed_s: float = 0.0
    session_id: Optional[str] = None


# ---- system prompt --------------------------------------------------------


# Mechanic v2 system prompt. Family A — full replace via
# --system-prompt-file (the multi-line argv truncation gotcha bites
# every spawn site that uses argv for prompts on Windows). Carries
# the architecture vocabulary AND the v2-specific scope/rules/format.
MECHANIC_REPAIR_SYSTEM_PROMPT = """\
You are the Mechanic of a Cyberdeck operating in REPAIR MODE. The deck
just experienced a problem (or the netrunner asked for a routine config
sanity scan). Your job is to scan the deck's CONFIG FILES for clear
structural errors and propose specific fixes the netrunner can approve
with one keystroke.

You have READ-ONLY filesystem access via Read, Glob, and Grep. You
CANNOT execute commands, write files, edit anything, spawn subagents,
or take any other action. Your structured output goes through a
deck-side allowlist + per-proposal netrunner approval — the deck does
the actual writing on your behalf, only after the netrunner says yes.

================================================================
SCOPE — CONFIG FILES ONLY
================================================================

You can propose changes to these three locations only:

  - <home>/.cyberdeck/state.json   — brake state + limits namespace
  - <home>/profiles/*.toml         — construct profile templates
  - <home>/tools/tools.toml        — system-CLI registry

Anything outside this list — Python source, design docs, logs, plugin
code, the dispatcher script, the heartbeat file, the brake-hook
config — is OUT OF SCOPE for repair. The deck will REJECT any proposal
pointing outside this allowlist regardless of what you say. If you
notice something broken outside the allowlist while reading source for
context, mention it as a recommendation in your "Notes" section but
do NOT include it as a proposal.

================================================================
WHAT COUNTS AS "BROKEN"
================================================================

You are looking for STRUCTURAL ERRORS and clear CORRUPTION, not value
preferences. The netrunner's settings are sacred — they tune the deck
for their workflow, and many "non-default" values are intentional. The
rule:

  ✅ Propose fixes for these:
     - File doesn't parse (JSON syntax error, TOML parse error)
     - Required field MISSING entirely (e.g., profile TOML has no
       `name` field)
     - Field TYPE WRONG (e.g., delay_window_seconds = "five" — must
       be a float, not a string; or brake = 5 — must be a string)
     - Value violates a DOCUMENTED ENUM
       (e.g., brake = "potato" — must be "paranoid"/"default"/"yolo";
        kind = "binarry" — must be "binary" or "script")
     - Profile filename doesn't match its `name` field (semantic
       corruption — the registry looks the file up by basename)
     - Reference to a path that DOESN'T EXIST on disk (broken link;
       e.g., a tool whose `path` points at a file that was moved)

  ❌ Do NOT propose fixes for these:
     - Non-default values that are still TYPE-valid. The auto-seeded
       state.json has delay_window_seconds=0.0 and the netrunner is
       allowed to set it to 5.0 or 10.0 or whatever they want — those
       are settings, not corruption. Acknowledge non-defaults in your
       "Non-default values noticed" section so the netrunner sees you
       saw them, but DO NOT propose changes.
     - Style preferences (whitespace in TOML, JSON key ordering, comment
       additions/removals)
     - Empty-but-allowed fields (an empty `tools = []` list is fine;
       an empty `default_daemon_addendum = ""` is fine)
     - Fields not yet documented in the schema (forward-compat siblings
       like `theme` or `last_session_id` — leave them alone)

The discipline: SHAPE check, not value check. If the value matches the
documented type pattern (float/string/enum-member/bool/list-of-string),
it is allowed regardless of what number/text/choice it is.

================================================================
SCHEMAS — what valid looks like
================================================================

state.json (JSON):
  {
    "_comment": <any string; deck-owned-note marker, ignore>,
    "brake": "paranoid" | "default" | "yolo",   // enum, required
    "limits": {                                  // dict, optional
      "delay_window_seconds": float >= 0,        // default 0.0
      "wedge_timeout_seconds": float >= 0,       // default 30.0
      "fast_mode": bool,                         // default false
      "daemon_effort": "low" | "medium" | "high" | "xhigh",  // default "high"
      // Forward-compat: unknown sibling keys are FINE; leave them alone.
    }
  }
  Any missing key uses its default. Empty-or-missing limits namespace
  is fine. brake key MUST be present once the file exists.

profile TOML (<home>/profiles/<slug>.toml):
  Required:
    name         = "<slug>"   # must match [a-z0-9_][a-z0-9_-]*; must equal filename minus .toml
    category     = "..."      # non-empty string
    description  = "..."      # non-empty string

  Optional:
    default_daemon_addendum    = "..."   # str, default ""
    default_construct_addendum = "..."   # str, default ""
    tools                      = [...]   # list of str (tool registry names), default []
    recommended_tools          = [...]   # legacy str list — accept silently
    default_scripts            = [...]   # legacy str list — accept silently

tools.toml (<home>/tools/tools.toml):
  This file is OPTIONAL — the registry re-seeds it from
  DEFAULT_TOOLS_TOML if missing. So a missing tools.toml is normal,
  not an error.

  When present, the schema is an array of [[tool]] tables:
    [[tool]]
    name        = "<slug>"          # required, slug
    kind        = "binary" | "script"  # required, enum
    command     = "..."             # required, non-empty string
    description = "..."             # required, non-empty string
    path        = "..."             # optional string
    help_text   = "..."             # optional string

  An empty file (just the comment block from the seed) is fine — the
  netrunner registers tools by adding [[tool]] entries; an empty
  registry is the default state.

You can Read the deck source (Design Files/, profiles.py,
tools_registry.py, brake_state.py, preferences.py) to find the seed
defaults if you need to compare a corrupt file against what it should
look like. Recommended to do so when you're proposing `restore_default`.

================================================================
TRIAGE-COUPLED CONTEXT
================================================================

If your user message includes a TRIAGE EXCERPT block, the deck just
finished a v1 triage run on a crashed deck and the supervisor is
offering repair as a follow-on. Use the triage's findings to focus
your scan — if the triage said "the brake state file looks malformed,"
spend more time on state.json than on profile TOMLs. If the triage
said "no config issues identified," your scan should still run but
will probably produce zero proposals (the clean-scan case).

If there's no TRIAGE EXCERPT block, this is a STANDALONE SUMMON — the
netrunner asked for a routine sanity scan via `mechanic.py --repair`.
Scan all three config-file locations equally; report what you find.

================================================================
OUTPUT FORMAT
================================================================

Markdown. Open with a 1-2 sentence summary, then non-default values in
plain prose, then issues, then notes, then EXACTLY ONE fenced JSON
block with proposals at the end:

# Repair scan — <log basename or yyyy-mm-dd>

## Summary
<1-2 sentences. What was found, in plain prose. If clean, say so plainly.>

## Non-default values noticed (informational only)
<bullet list. Each entry: file, field, current value, default, note. NO
proposal — just acknowledgment so the netrunner sees you saw the
divergence. If everything is at default, say "All values at default."
or omit this section.>

## Issues
<bullet list of structural problems found. Each item: file path, what's
broken, what the fix is. If none, say "No issues found.">

## Notes
<optional. Out-of-scope concerns, recommendations the netrunner might
want to act on but that aren't in v2's allowlist (e.g., "old logs are
filling disk — consider deleting cyberdeck-2026-04-*.log"). Brief.>

## Proposals

```json
{
  "proposals": [
    {
      "file_path": "<absolute path>",
      "kind": "overwrite" | "restore_default" | "delete",
      "proposed_content": "<COMPLETE post-fix file contents as a string; null when kind='delete'>",
      "rationale": "<one paragraph explaining why this fix is needed>",
      "severity": "low" | "warning" | "critical"
    }
  ]
}
```

Keep total length under ~1500 words. The netrunner is reading this
AFTER something already went wrong; respect their time. The proposed_
content for each fix can be as long as the file requires (no
truncation), but the prose around it should be tight.

================================================================
HARD CONSTRAINTS
================================================================

- READ-ONLY tools (Read/Glob/Grep). No Bash, no Write, no Edit, no
  NotebookEdit, no WebFetch, no spawning subagents.
- Stay in scope: state.json + profiles/*.toml + tools.toml ONLY.
  Anything else is a Notes-section recommendation at most.
- Conservative bias: if you are not SURE a value is structurally
  broken, do NOT propose. The cost of a missed fix is "netrunner
  notices and reports it"; the cost of a wrong fix is "netrunner has
  to restore from backup."
- Honest about clean state: ZERO PROPOSALS is the right answer when
  nothing's broken. Do not manufacture work to look productive.
- Empty proposals list: include the JSON block anyway with
  "proposals": []. The deck's parser expects the envelope.
"""


# ---- user prompt builder --------------------------------------------------


def _gather_config_state(home_dir: Path) -> str:
    """Build a "current config state" snapshot for the user prompt.

    Reads each config-file location and inlines its contents (or notes
    the file is absent). Bounded — config files are small (state.json
    is sub-1KB, profile TOMLs are sub-1KB each, tools.toml caps a few
    KB at the seeded-empty state). Cheaper than spawning Read tool
    turns for each.
    """
    parts: list[str] = []
    home = Path(home_dir)

    def _inline(label: str, p: Path) -> None:
        parts.append("")
        parts.append(f"### {label}: `{p}`")
        if not p.is_file():
            parts.append("*(file does not exist on disk)*")
            return
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as e:
            parts.append(f"*(read failed: {e})*")
            return
        # Trim absurdly long content. Real configs are tiny; if
        # something's >100KB the prompt budget is a real concern AND
        # the file shape is suspicious. Truncate-with-note rather
        # than refuse outright.
        if len(content) > 100_000:
            content = content[:100_000] + "\n... [truncated to 100KB]"
        parts.append("```")
        parts.append(content)
        parts.append("```")

    # state.json
    _inline("state.json", home / ".cyberdeck" / "state.json")

    # profiles/*.toml — list every profile we find
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        profile_files = sorted(profiles_dir.glob("*.toml"))
        if profile_files:
            for p in profile_files:
                _inline(f"profile: {p.name}", p)
        else:
            parts.append("")
            parts.append(f"### profiles dir: `{profiles_dir}`")
            parts.append("*(directory exists but contains no .toml files)*")
    else:
        parts.append("")
        parts.append(f"### profiles dir: `{profiles_dir}`")
        parts.append("*(directory does not exist)*")

    # tools.toml
    _inline("tools.toml", home / "tools" / "tools.toml")

    return "\n".join(parts)


def _load_text_tail(p: Optional[Path], cap_bytes: int = 20_000) -> str:
    """Read the tail of a text file. Used for triage report excerpts.
    Best-effort: returns "" on miss / read failure."""
    if p is None or not p.is_file():
        return ""
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > cap_bytes:
                f.seek(size - cap_bytes)
            data = f.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def build_user_prompt(req: RepairRequest, config_state: str) -> str:
    """Compose the user-side prompt for one repair call.

    Carries:
      - The home_dir path (so the LLM resolves relative references)
      - Current contents of all three config-file locations (inlined —
        cheaper than tool reads for files this small)
      - Optional triage report excerpt (when running as triage follow-on)
      - Deck source dir (for the LLM to Read seed defaults if needed)
    """
    parts = [
        "DECK CONFIG REPAIR SCAN",
        "",
        f"Home dir: `{req.home_dir}`",
    ]
    if req.deck_source_dir:
        parts.append(f"Deck source dir: `{req.deck_source_dir}`")
        parts.append(
            "(You can Read / Glob / Grep against deck source. The seed "
            "defaults live in profiles.py / profile_registry.py / "
            "tools_registry.py / brake_state.py / preferences.py — "
            "consult them when proposing `restore_default`.)"
        )
    parts.append("")

    # Triage excerpt (when fired as triage follow-on).
    if req.triage_report_path:
        excerpt = _load_text_tail(req.triage_report_path)
        if excerpt:
            parts.append("=" * 64)
            parts.append("TRIAGE EXCERPT — what the v1 triage just found")
            parts.append("=" * 64)
            parts.append(f"Source: `{req.triage_report_path}`")
            parts.append("")
            parts.append(excerpt)
            parts.append("")

    parts.append("=" * 64)
    parts.append("CURRENT CONFIG STATE — inlined from disk")
    parts.append("=" * 64)
    parts.append(config_state)
    parts.append("")
    parts.append("=" * 64)
    parts.append(
        "Produce the repair report per the format in your system prompt. "
        "Remember: SHAPE check, not value check. Zero proposals is the "
        "right answer when nothing is structurally broken."
    )
    return "\n".join(parts)


# ---- response parsing -----------------------------------------------------


def parse_repair_response(raw: str) -> tuple[list[RepairProposal], list[str]]:
    """Parse the LLM's repair output into RepairProposal objects.

    Returns (proposals, errors). `proposals` are entries that look
    structurally valid (the apply path still validates path allowlist
    + writability). `errors` are per-entry rejection reasons keyed by
    label-or-index — surfaced to the netrunner so they can see why an
    entry was dropped.

    Tolerant of light wrapping (mirrors tripwires.parse_authoring_response):
    strict JSON → fenced ```json block → balanced-brace fallback.
    Returns ([], ["<reason>"]) when nothing JSON-shaped is found.
    """
    candidate = (raw or "").strip()
    parsed_obj = None

    # Pass 1: strict — model output IS bare JSON.
    try:
        parsed_obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        parsed_obj = None

    # Pass 2: fenced ```json ... ``` block (claude often does this
    # despite explicit "fenced JSON at end" instructions; here we
    # actually want it fenced, so this is the expected path).
    if parsed_obj is None:
        # Match the LAST fenced block in the response — the proposals
        # JSON is at the end per the system prompt, but earlier
        # sections may have inline ```json examples for documentation.
        matches = list(re.finditer(
            r"```(?:json)?\s*\n?(.*?)```", candidate, re.DOTALL,
        ))
        if matches:
            # Try last match first (most likely the proposals block);
            # fall through to earlier matches if last doesn't parse.
            for m in reversed(matches):
                try:
                    parsed_obj = json.loads(m.group(1).strip())
                    break
                except (json.JSONDecodeError, ValueError):
                    continue

    # Pass 3: first balanced {...} block. Crude — assumes outer
    # structure is a JSON object (which our schema requires). Uses
    # find/rfind to bracket; json.loads validates the slice.
    if parsed_obj is None:
        first_brace = candidate.find("{")
        last_brace = candidate.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            try:
                parsed_obj = json.loads(
                    candidate[first_brace:last_brace + 1]
                )
            except (json.JSONDecodeError, ValueError):
                parsed_obj = None

    if parsed_obj is None:
        return [], ["could not locate JSON proposals block in response"]

    if not isinstance(parsed_obj, dict):
        return [], ["JSON root is not an object"]
    entries = parsed_obj.get("proposals")
    if not isinstance(entries, list):
        return [], ["missing or non-list 'proposals' field"]

    proposals: list[RepairProposal] = []
    errors: list[str] = []

    for idx, entry in enumerate(entries):
        label = (
            entry.get("file_path", f"#{idx}")
            if isinstance(entry, dict) else f"#{idx}"
        )

        if not isinstance(entry, dict):
            errors.append(f"{label}: entry is not an object")
            continue

        file_path_raw = entry.get("file_path")
        if not isinstance(file_path_raw, str) or not file_path_raw.strip():
            errors.append(f"{label}: missing or non-string 'file_path'")
            continue
        try:
            file_path = Path(file_path_raw)
        except (TypeError, ValueError) as e:
            errors.append(f"{label}: bad path {file_path_raw!r}: {e}")
            continue

        kind = entry.get("kind")
        if kind not in ("overwrite", "restore_default", "delete"):
            errors.append(f"{label}: invalid kind {kind!r}")
            continue

        proposed_content = entry.get("proposed_content")
        if kind == "delete":
            # delete must have null/missing proposed_content; ignore if
            # the model included one anyway.
            proposed_content = None
        else:
            if not isinstance(proposed_content, str):
                errors.append(
                    f"{label}: kind={kind} requires string 'proposed_content'"
                )
                continue

        rationale = entry.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(f"{label}: missing or empty 'rationale'")
            continue

        severity = entry.get("severity", "warning")
        if severity not in ("low", "warning", "critical"):
            severity = "warning"

        proposals.append(RepairProposal(
            file_path=file_path,
            kind=kind,
            proposed_content=proposed_content,
            rationale=rationale.strip(),
            severity=severity,
        ))

    return proposals, errors


# ---- presentation + apply -------------------------------------------------


def _diff_for_display(
    file_path: Path,
    proposed_content: Optional[str],
    kind: str,
) -> str:
    """Compute a unified diff between the file's current state and
    the proposal. Re-reads the file at apply-prompt time (not at
    propose time) so the netrunner sees what would ACTUALLY change
    right now — defends against TOCTOU surprises if they edited the
    file in between.

    For kind=delete, returns a synthetic "[file would be removed]"
    block.
    """
    if kind == "delete":
        return f"[deletion]\nFile would be moved to backup and removed:\n  {file_path}"

    if file_path.is_file():
        try:
            current = file_path.read_text(encoding="utf-8")
        except OSError as e:
            current = f"<read failed: {e}>"
    else:
        current = ""  # treat absent file as empty for diff purposes

    proposed = proposed_content or ""

    diff_lines = list(difflib.unified_diff(
        current.splitlines(keepends=True),
        proposed.splitlines(keepends=True),
        fromfile=str(file_path) + " (current)",
        tofile=str(file_path) + " (proposed)",
        lineterm="",
    ))
    if not diff_lines:
        return "[no-op] proposed content matches current file exactly."
    return "".join(diff_lines)


def present_proposal(
    proposal: RepairProposal,
    *,
    index: int,
    total: int,
) -> str:
    """Show the proposal + diff on stderr and prompt for approval.

    Returns one of: "y" (apply), "n" (skip), "q" (quit the loop).

    Plain stderr only — no ANSI color (per netrunner direction:
    "We can avoid color. We want the barest support possible — this
    isn't a fun tool, it's a fallback plan."). Severity is shown as
    a bracket tag, not a color.

    Non-TTY stdin returns "n" without prompting (treats every
    proposal as declined; same policy as the doctor's plugin-dep
    prompt). Headless deployments without a netrunner at the
    terminal get clean degradation.
    """
    sev = proposal.severity.upper()
    sys.stderr.write("\n")
    sys.stderr.write("=" * 72 + "\n")
    sys.stderr.write(f"[mechanic.repair] proposal {index}/{total}  [{sev}]\n")
    sys.stderr.write(f"  file: {proposal.file_path}\n")
    sys.stderr.write(f"  kind: {proposal.kind}\n")
    sys.stderr.write("=" * 72 + "\n")
    sys.stderr.write("Rationale:\n")
    for line in proposal.rationale.splitlines():
        sys.stderr.write(f"  {line}\n")
    sys.stderr.write("\nDiff:\n")
    diff = _diff_for_display(
        proposal.file_path,
        proposal.proposed_content,
        proposal.kind,
    )
    for line in diff.splitlines():
        sys.stderr.write(f"  {line}\n")
    sys.stderr.write("\n")
    sys.stderr.flush()

    if not sys.stdin.isatty():
        sys.stderr.write(
            "[mechanic.repair] non-TTY stdin — auto-declining "
            "(use --auto-apply for headless approval if added later)\n"
        )
        sys.stderr.flush()
        return "n"

    while True:
        try:
            sys.stderr.write(
                f"Apply? [y]es / [n]o / [q]uit remaining: "
            )
            sys.stderr.flush()
            ans = input("").strip().lower()
        except (EOFError, KeyboardInterrupt):
            sys.stderr.write("\n[mechanic.repair] input interrupted; "
                             "treating as quit\n")
            sys.stderr.flush()
            return "q"
        except Exception:
            return "q"
        if ans in ("y", "yes"):
            return "y"
        if ans in ("n", "no", ""):
            return "n"
        if ans in ("q", "quit"):
            return "q"
        sys.stderr.write(f"  unrecognized: {ans!r}\n")


def _backup_path(home_dir: Path, target: Path) -> Path:
    """Compute the backup destination for `target`.

    Format: <home>/.cyberdeck/repair-backups/<YYYY-MM-DD-HHMMSS>-<basename>
    Backups never overwrite — if the timestamp collides (sub-second
    apply on the same file), append a suffix.
    """
    backups_dir = Path(home_dir) / ".cyberdeck" / "repair-backups"
    ts = time.strftime("%Y-%m-%d-%H%M%S")
    base = f"{ts}-{target.name}"
    candidate = backups_dir / base
    if not candidate.exists():
        return candidate
    # Sub-second collision (or two backups of the same file in the
    # same applied batch). Add a numeric suffix.
    i = 2
    while True:
        candidate = backups_dir / f"{ts}-{target.name}.{i}"
        if not candidate.exists():
            return candidate
        i += 1


def apply_proposal(
    home_dir: Path,
    proposal: RepairProposal,
) -> tuple[bool, str]:
    """Apply one repair proposal. Returns (ok, message).

    Steps:
      1. Path allowlist check — reject if not in WRITABLE_PATHS shape.
      2. Backup the current file (if it exists) to repair-backups/.
      3. For overwrite/restore_default: write proposed_content.
         For delete: remove the file (after backup).

    All operations are best-effort with explicit error reporting.
    Any failure leaves the original file in place (we back up BEFORE
    we write, so a write failure isn't recoverable from the backup —
    but the original wasn't touched yet either).

    Idempotent for "no-op" overwrites: if the file's current content
    already matches proposed_content exactly, we skip the write and
    backup, and return ok with a "no-op (already matches)" message.
    """
    target = proposal.file_path

    # Allowlist enforcement — the LLM's claim doesn't matter; we check.
    if not is_writable_path(home_dir, target):
        return False, (
            f"path outside repair allowlist: {target} "
            f"(allowed: <home>/.cyberdeck/state.json, "
            f"<home>/profiles/*.toml, <home>/tools/tools.toml)"
        )

    # No-op detection (only meaningful for overwrite/restore_default).
    if proposal.kind in ("overwrite", "restore_default") and target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
            if current == (proposal.proposed_content or ""):
                return True, "no-op (file already matches proposed content)"
        except OSError:
            # If we can't even read the current, fall through to the
            # backup+write path; the write may itself surface the error.
            pass

    # Backup step.
    backup = _backup_path(home_dir, target)
    backup_taken = False
    if target.is_file():
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup)
            backup_taken = True
        except OSError as e:
            return False, f"backup failed (no write attempted): {e}"

    # Write / delete step.
    try:
        if proposal.kind == "delete":
            if target.is_file():
                target.unlink()
            # else: nothing to delete; treat as no-op success.
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(proposal.proposed_content or "", encoding="utf-8")
    except OSError as e:
        msg = f"write failed: {e}"
        if backup_taken:
            msg += f" (backup preserved at {backup})"
        return False, msg

    if backup_taken:
        return True, f"applied (backup at {backup})"
    return True, "applied (no prior file to backup)"


# ---- run_repair orchestrator ----------------------------------------------


def _emit(line: str) -> None:
    """Write one line to stderr with flush. Mirrors mechanic_triage._emit."""
    try:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()
    except Exception:
        pass


def _truncate(text: str, n: int = 80) -> str:
    """Single-line truncation. Mirrors mechanic_triage._truncate."""
    s = " ".join((text or "").split())
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


def _print_repair_event(event: dict) -> None:
    """Pretty-print one stream-json event to stderr — mirrors
    mechanic_triage._print_triage_event but with a [mechanic.repair]
    tag so log readers can distinguish triage and repair output."""
    et = event.get("type")

    if et == "system":
        if event.get("subtype") == "init":
            model = event.get("model", "?")
            _emit(f"[mechanic.repair] session started · model={model}")
        return

    if et == "assistant":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "thinking":
                txt = _truncate(block.get("thinking", ""), 100)
                if txt:
                    _emit(f"[mechanic.repair] thinking: {txt}")
            elif btype == "text":
                txt = _truncate(block.get("text", ""), 100)
                if txt:
                    _emit(f"[mechanic.repair] writing: {txt}")
            elif btype == "tool_use":
                tool = block.get("name", "?")
                inp = block.get("input", {}) or {}
                if tool == "Read":
                    fp = inp.get("file_path", "?")
                    _emit(f"[mechanic.repair] Read: {fp}")
                elif tool == "Glob":
                    pat = inp.get("pattern", "?")
                    where = inp.get("path", "")
                    suffix = f" in {where}" if where else ""
                    _emit(f"[mechanic.repair] Glob: {pat}{suffix}")
                elif tool == "Grep":
                    pat = inp.get("pattern", "?")
                    where = inp.get("path", "")
                    suffix = f" in {where}" if where else ""
                    _emit(f"[mechanic.repair] Grep: {pat!r}{suffix}")
                else:
                    _emit(f"[mechanic.repair] {tool} tool")
        return

    if et == "user":
        msg = event.get("message", {})
        for block in msg.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            content = block.get("content", "")
            if isinstance(content, list):
                content = "".join(
                    c.get("text", "") for c in content
                    if isinstance(c, dict)
                )
            clen = len(str(content or ""))
            is_err = bool(block.get("is_error"))
            tag = "tool error" if is_err else "tool result"
            _emit(f"[mechanic.repair] {tag}: {clen} chars")
        return

    if et == "result":
        result_text = event.get("result", "") or ""
        _emit(
            f"[mechanic.repair] result received "
            f"({len(result_text)} chars)"
        )
        return


def _build_report_path(req: RepairRequest) -> Path:
    """Where to write the repair report. Format mirrors triage:
    `<log-basename>-repair.md` next to the log when log_path set;
    fall back to `<home>/.cyberdeck/repair-<YYYY-MM-DD-HHMMSS>.md`
    for standalone summons."""
    if req.report_dir is not None:
        parent = req.report_dir
        if req.log_path is not None:
            return parent / f"{req.log_path.stem}-repair.md"
        ts = time.strftime("%Y-%m-%d-%H%M%S")
        return parent / f"repair-{ts}.md"
    if req.log_path is not None:
        return req.log_path.parent / f"{req.log_path.stem}-repair.md"
    ts = time.strftime("%Y-%m-%d-%H%M%S")
    fallback = Path(req.home_dir) / ".cyberdeck" / f"repair-{ts}.md"
    return fallback


def _summary_line(report_text: str, max_chars: int = 100) -> str:
    """Extract a one-line summary from the repair report. Same pattern
    as mechanic_triage._summary_line — first non-empty line under
    "## Summary", with fallbacks."""
    lines = report_text.splitlines()
    in_summary = False
    for line in lines:
        if line.strip().lower().startswith("## summary"):
            in_summary = True
            continue
        if in_summary:
            if not line.strip():
                continue
            if line.strip().startswith("#"):
                break
            text = line.strip()
            if text:
                return text[:max_chars]
    for line in lines:
        text = line.strip()
        if text and not text.startswith("#") and not text.startswith("```"):
            return text[:max_chars]
    return "repair scan completed"


def run_repair(
    req: RepairRequest,
    *,
    apply_fn: Optional[Callable[[Path, RepairProposal], tuple[bool, str]]] = None,
    present_fn: Optional[Callable[..., str]] = None,
) -> RepairResult:
    """Spawn one claude -p subprocess for the repair scan, parse the
    proposals, and walk the netrunner through per-proposal approval.

    Synchronous (the supervisor is in its post-cleanup path; no async
    runtime). Best-effort throughout — failures produce a RepairResult
    with success=False and an error message; the supervisor logs the
    summary either way.

    Same Family A spawn recipe as triage — --system-prompt-file,
    --tools "Read,Glob,Grep", --disable-slash-commands,
    --no-session-persistence, env-var belt for CLAUDE.md /
    auto-memory / git-instructions suppression. v2 has its own
    curated system prompt; no need to free-ride on auto-load.

    Apply path:
      - Parse the LLM's structured output (parse_repair_response)
      - For each proposal: present_fn(proposal) for y/N/q approval,
        apply_fn(home_dir, proposal) on y
      - "q" exits the approval loop; remaining proposals are counted
        as rejected_by_user

    Both `apply_fn` and `present_fn` are injectable for testability;
    defaults call apply_proposal / present_proposal.
    """
    started_at = time.time()
    if apply_fn is None:
        apply_fn = apply_proposal
    if present_fn is None:
        present_fn = present_proposal

    # Resolve the binary path early.
    bin_path = shutil.which(req.claude_bin) or req.claude_bin

    # Build user prompt with config state inlined.
    config_state = _gather_config_state(req.home_dir)
    user_prompt = build_user_prompt(req, config_state)

    # System prompt → temp file (argv-newline-truncation gotcha).
    sysprompt_path: Optional[str] = None
    try:
        fd, sysprompt_path = tempfile.mkstemp(
            suffix=".txt", prefix="mechanic-repair-",
        )
        os.close(fd)
        Path(sysprompt_path).write_text(
            MECHANIC_REPAIR_SYSTEM_PROMPT, encoding="utf-8",
        )
    except Exception as e:
        return RepairResult(
            success=False,
            error=f"failed to write system-prompt file: {e}",
            elapsed_s=time.time() - started_at,
            summary_line=f"repair failed: prompt write {e!r}",
        )

    cmd = [
        bin_path, "-p",
        "--system-prompt-file", sysprompt_path,
        "--tools", REPAIR_ALLOWED_TOOLS,
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model", REPAIR_MODEL,
        "--effort", REPAIR_EFFORT,
        "--permission-mode", "bypassPermissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    if req.deck_source_dir:
        cmd += ["--add-dir", str(req.deck_source_dir)]
    # Also add home_dir so Read can pull config files directly if the
    # LLM wants to (the prompt inlines them, but Read tool is still
    # available for any file under the added dirs).
    cmd += ["--add-dir", str(req.home_dir)]

    env = {
        **os.environ,
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=sys.stderr,
            env=env,
        )
    except FileNotFoundError:
        if sysprompt_path:
            try:
                os.unlink(sysprompt_path)
            except OSError:
                pass
        return RepairResult(
            success=False,
            error=f"claude binary not found: {req.claude_bin}",
            elapsed_s=time.time() - started_at,
            summary_line="repair failed: claude not found",
        )
    except Exception as e:
        if sysprompt_path:
            try:
                os.unlink(sysprompt_path)
            except OSError:
                pass
        return RepairResult(
            success=False,
            error=f"subprocess spawn failed: {e}",
            elapsed_s=time.time() - started_at,
            summary_line=f"repair failed: spawn {e!r}",
        )

    try:
        if proc.stdin is not None:
            proc.stdin.write(user_prompt.encode("utf-8"))
            proc.stdin.close()
    except Exception:
        pass

    # Stream-json reader thread — mirrors triage's reader pattern.
    reader_state: dict = {
        "final_text": "",
        "events_seen": 0,
        "session_id": None,
    }

    def _reader_thread() -> None:
        try:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                if not raw:
                    break
                try:
                    event = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                reader_state["events_seen"] += 1
                _print_repair_event(event)

                et = event.get("type")
                if et == "system" and event.get("subtype") == "init":
                    sid = event.get("session_id")
                    if isinstance(sid, str) and sid:
                        reader_state["session_id"] = sid
                if et == "result":
                    reader_state["final_text"] = event.get("result", "") or ""
        except Exception as e:
            _emit(f"[mechanic.repair] reader thread error: {e}")

    reader = threading.Thread(target=_reader_thread, daemon=True)
    reader.start()

    timed_out = False
    try:
        proc.wait(timeout=req.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass

    reader.join(timeout=5)

    if sysprompt_path:
        try:
            os.unlink(sysprompt_path)
        except OSError:
            pass

    elapsed = time.time() - started_at

    if timed_out:
        # No partial-recovery here. Repair's value comes from the
        # structured proposals at the END of the response; partial
        # output mid-thinking isn't actionable. Surface as failure
        # with a clear stub.
        report_text = (
            f"# Repair scan — TIMED OUT\n\n"
            f"## Summary\n\nRepair scan timed out after "
            f"{req.timeout:.0f}s. No proposals applied. Re-run with a "
            f"longer `--repair-timeout` if the deck has substantial "
            f"config state to walk.\n"
        )
        report_path = _build_report_path(req)
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report_text, encoding="utf-8")
        except OSError:
            report_path = None
        return RepairResult(
            success=False,
            report_text=report_text,
            report_path=report_path,
            error=f"claude -p timed out after {req.timeout}s",
            summary_line=f"repair timed out at {req.timeout:.0f}s",
            elapsed_s=elapsed,
            session_id=reader_state.get("session_id"),
        )

    if proc.returncode != 0:
        return RepairResult(
            success=False,
            error=(
                f"claude exited {proc.returncode} "
                f"(events seen: {reader_state['events_seen']})"
            ),
            summary_line=f"repair failed: claude exit {proc.returncode}",
            elapsed_s=elapsed,
            session_id=reader_state.get("session_id"),
        )

    report_text = (reader_state["final_text"] or "").strip()
    if not report_text:
        return RepairResult(
            success=False,
            error=(
                f"claude returned no result event "
                f"(events seen: {reader_state['events_seen']})"
            ),
            summary_line="repair failed: no result event",
            elapsed_s=elapsed,
            session_id=reader_state.get("session_id"),
        )

    # Write the LLM's full report to disk regardless of parse outcome.
    # If parse fails, the netrunner still has the raw text to read.
    report_path = _build_report_path(req)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_text, encoding="utf-8")
    except OSError:
        report_path = None

    # Parse proposals.
    proposals, parse_errors = parse_repair_response(report_text)
    if parse_errors:
        for err in parse_errors:
            _emit(f"[mechanic.repair] parse error: {err}")

    result = RepairResult(
        success=True,
        report_text=report_text,
        report_path=report_path,
        proposals_seen=len(proposals),
        elapsed_s=elapsed,
        session_id=reader_state.get("session_id"),
        summary_line=_summary_line(report_text),
    )

    # Empty proposals → clean scan; no apply loop.
    if not proposals:
        _emit(
            f"[mechanic.repair] scan complete · 0 proposals · "
            f"{elapsed:.1f}s · report -> {report_path}"
        )
        return result

    _emit(
        f"[mechanic.repair] scan complete · {len(proposals)} proposal(s) "
        f"· {elapsed:.1f}s · report -> {report_path}"
    )
    _emit(
        f"[mechanic.repair] entering per-proposal approval loop. "
        f"y=apply, n=skip, q=quit remaining."
    )

    # Per-proposal approval loop. "q" short-circuits the rest.
    quit_requested = False
    for idx, proposal in enumerate(proposals, start=1):
        if quit_requested:
            result.proposals_rejected_by_user += 1
            continue
        # Pre-screen for allowlist before bothering the netrunner —
        # if the LLM proposed something out of scope, reject silently
        # and continue. Recording it as rejected_by_path so the
        # summary surfaces the count.
        if not is_writable_path(req.home_dir, proposal.file_path):
            _emit(
                f"[mechanic.repair] proposal {idx}/{len(proposals)} "
                f"REJECTED (path outside allowlist): {proposal.file_path}"
            )
            result.proposals_rejected_by_path += 1
            continue

        answer = present_fn(proposal, index=idx, total=len(proposals))
        if answer == "y":
            ok, msg = apply_fn(req.home_dir, proposal)
            if ok:
                _emit(f"[mechanic.repair] applied: {proposal.file_path} — {msg}")
                result.proposals_applied += 1
            else:
                _emit(f"[mechanic.repair] apply failed: {proposal.file_path} — {msg}")
                result.proposals_failed += 1
        elif answer == "q":
            _emit(f"[mechanic.repair] quit requested; "
                  f"skipping remaining {len(proposals) - idx + 1} proposal(s)")
            result.proposals_rejected_by_user += 1
            quit_requested = True
        else:
            result.proposals_rejected_by_user += 1

    # Final summary.
    _emit(
        f"[mechanic.repair] done · "
        f"applied={result.proposals_applied} "
        f"rejected_by_user={result.proposals_rejected_by_user} "
        f"rejected_by_path={result.proposals_rejected_by_path} "
        f"failed={result.proposals_failed}"
    )

    return result


# ---- triage report parsing ------------------------------------------------
#
# When repair fires as a triage follow-on, the supervisor reads the
# triage report's "Repair recommendation" section to pick the prompt
# default ([Y/n] when recommended, [y/N] otherwise). Per netrunner
# direction (item 0h): "the report can include a section that says
# (Recommend Repair: Y/N - reasoning)". The section is opportunistic
# — if the model didn't include it, we fall back to [y/N].


def parse_repair_recommendation(triage_report_text: str) -> Optional[bool]:
    """Read the triage report and return:
      - True if the triage recommends running repair
      - False if it recommends skipping repair
      - None if the recommendation can't be located (no section, or
        ambiguous wording)

    The supervisor uses this to set the default for the post-triage
    prompt. None → safe default ([y/N], decline-by-Enter).

    Tolerant matcher: searches for a section heading like "## Repair
    recommendation" or "## Repair Recommendation" or any heading
    containing both "repair" and "recommend", then looks for an
    explicit Y/N marker on the next non-empty line. Falls back to
    keyword scanning ("recommend repair: yes" / "recommend skipping").
    """
    if not triage_report_text:
        return None

    lines = triage_report_text.splitlines()
    in_section = False
    section_lines: list[str] = []
    for line in lines:
        s = line.strip()
        # Heading-shape detection — match if the line starts with `#`
        # (any level) and mentions both 'repair' and 'recommend'.
        if s.startswith("#"):
            heading_text = s.lstrip("#").strip().lower()
            if "repair" in heading_text and "recommend" in heading_text:
                in_section = True
                continue
            elif in_section:
                # Different heading ends the section.
                break
        if in_section:
            section_lines.append(line)

    section = "\n".join(section_lines).strip().lower()

    # Try explicit Y/N markers first.
    if section:
        # Pattern 1: "(Recommend Repair: Y - ...)"  or "Y/N: yes - ..."
        m = re.search(r"\b(?:recommend(?:ation|ed|s)?[:\s]*|repair[:\s]*)\s*(yes|y|true|no|n|false)\b", section)
        if m:
            answer = m.group(1).lower()
            return answer in ("yes", "y", "true")
        # Pattern 2: standalone "yes"/"no" token
        m2 = re.search(r"^(?:[-*]\s*)?(yes|y|no|n)\b", section, re.MULTILINE)
        if m2:
            return m2.group(1).lower() in ("yes", "y")

    # Fallback: keyword scan over the WHOLE report. Less reliable but
    # catches "Repair recommended: skip" without a heading.
    rt = triage_report_text.lower()
    if "recommend repair: yes" in rt or "recommend repair: y" in rt:
        return True
    if "recommend repair: no" in rt or "recommend repair: n" in rt:
        return False
    if "no repair recommended" in rt or "skip repair" in rt:
        return False
    if "repair recommended" in rt:
        return True

    return None


def prompt_repair(*, recommended: Optional[bool]) -> bool:
    """Stderr prompt: "Run config repair scan? [Y/n]" (when
    recommended=True) or "[y/N]" (otherwise).

    Returns True on yes (apply default when Enter pressed and
    recommended=True; otherwise require explicit y).

    Non-TTY stdin → returns False without prompting (same policy as
    prompt_keep_delving in mechanic_triage).
    """
    if not sys.stdin.isatty():
        return False
    if recommended is True:
        prompt = "Run config repair scan? [Y/n]: "
        default_yes = True
    else:
        # recommended=False or None — both default to N. None case
        # also surfaces "(triage didn't recommend)" so the netrunner
        # knows why the default flipped.
        if recommended is False:
            prompt = "Run config repair scan? (triage recommended skip) [y/N]: "
        else:
            prompt = "Run config repair scan? [y/N]: "
        default_yes = False
    try:
        sys.stderr.write("\n[mechanic] " + prompt)
        sys.stderr.flush()
        ans = input("").strip().lower()
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        return False
    except Exception:
        return False
    if ans == "":
        return default_yes
    return ans in ("y", "yes")
