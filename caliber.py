"""
Caliber — per-spawn model + effort + fast-mode bundle.

A Caliber is the capability/cost grade a construct (or the daemon, or
watchdog) deploys at. Three independent axes wrapped in one dataclass:

  - model:  haiku / sonnet / opus, plus 1M-context variants
  - effort: low / medium / high / xhigh / max
  - fast_mode: bool (Opus 4.6-only latency knob — beta)

Phase 1 of the caliber slice (2026-05-04) — see
`Design Files/cyberdeck-model-effort-design.md`. This module is the
PRIMITIVE: dataclass + validation + CLI-arg formatter + a couple of
helpers. Per-spawn plumbing lives in fleet.py + daemon_session.py +
construct.py.

Why this exists, in two sentences: the deck spawns every construct at
Claude Code's default (sonnet+high) which over-spends on cheap parallel
recon and under-delivers on heavy synthesis. Caliber lets the daemon
pick the right grade per task, with quota-aware fallback when the
netrunner's window approaches its cap (Phase 4, queued behind
build-plan item 13).

Fast mode reality check (verified 2026-05-04 against Anthropic's
fast-mode docs):
  - Currently supported on Claude Opus 4.6 ONLY (`claude-opus-4-6`).
    Opus 4.7 is NOT supported. Sending fast=true with any other
    model errors at the API.
  - Beta / research preview — requires the waitlist
    (https://claude.com/fast-mode) and the `anthropic-beta:
    fast-mode-2026-02-01` header to even attempt.
  - Speed: up to 2.5x higher output tokens per second (OTPS) at
    SAME intelligence/capability — Anthropic explicitly says it's
    the same model weights, just faster inference. NOT time-to-
    first-token; OTPS only.
  - Cost: 6x standard Opus rates ($30/MTok input, $150/MTok
    output vs Opus standard $15/$75). Stacks with prompt-caching
    multipliers and data-residency multipliers.
  - Switching fast↔standard between calls invalidates prompt
    cache (separate cache pools).
  - Rate limits: separate dedicated bucket from standard Opus.
    Recommended pattern is "try fast, on 429 fall back to
    standard" — a hot fast-mode spawn that's rate-limited shouldn't
    block on retry.
  - API signature: top-level `speed: "fast"` param. Anthropic
    response carries `usage.speed = "fast"|"standard"` so the
    caller can verify fast actually engaged.
  - Claude Code's settings.json wrapper for fast mode is presumed
    to be `"fastMode": true` based on (a) the design doc's prior
    research and (b) the `fast_mode_state` field in Claude Code's
    `system_init` event payloads. The exact key is UNVERIFIED;
    real-deck testing required to confirm.

This module is data + validation only. Zero integration with
fleet/daemon/TUI; pure dataclass.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from typing import Optional


# Model aliases the deck recognizes. These are the strings that flow
# through `--model <name>` to Claude Code. Claude Code accepts both
# short aliases ("haiku", "sonnet", "opus") and canonical IDs
# (claude-sonnet-4-6, etc.); we use aliases at the deck level for
# brevity and let Claude Code resolve to the canonical name.
#
# Soft validation: an unknown value warns to stderr but doesn't reject.
# Anthropic occasionally adds new models and the deck shouldn't gate
# the netrunner on whether the constants list was updated.
KNOWN_MODELS: frozenset[str] = frozenset({
    "haiku",
    "sonnet",
    "opus",            # current default Opus (4.7 as of 2026-05-04)
    "opus[4.6]",       # Opus 4.6 — required for fast mode
    "sonnet[1m]",      # 1M-context variants
    "opus[1m]",
})


# Models the deck recognizes as fast-mode-compatible. Per Anthropic's
# docs (2026-05-04) only Opus 4.6 supports fast mode. The match
# accepts the deck's `opus[4.6]` alias plus a couple of literal forms
# the daemon might emit (Anthropic's canonical id, or the
# `claude-opus-4-6` short form). If Anthropic adds more in the future,
# extend this set.
_FAST_MODE_MODELS: frozenset[str] = frozenset({
    "opus[4.6]",
    "claude-opus-4-6",
    "opus-4-6",
    "opus-4.6",
})


# Deck-side alias → Claude Code canonical model identifier. Applied in
# Caliber.to_claude_args() so the `--model <name>` flag carries
# something Claude Code's CLI recognizes. Aliases the daemon might
# pick that need translation:
#
#   opus[4.6]  → claude-opus-4-6  (Opus 4.6 — fast mode only)
#
# Other aliases (haiku/sonnet/opus, and the 1M variants) pass through
# unchanged because Claude Code accepts them directly. If a model
# name isn't in this map, it's emitted verbatim — Claude Code's
# error path is more authoritative than the deck's constants table.
_MODEL_ALIAS_MAP: dict[str, str] = {
    "opus[4.6]": "claude-opus-4-6",
}


def _is_fast_mode_compatible(model: str) -> bool:
    """True if the model identifier resolves to one of the
    fast-mode-supporting models. Lowercase + bracket-tolerant."""
    return model.lower().strip() in _FAST_MODE_MODELS


def _resolve_model_alias(model: str) -> str:
    """Map deck-side aliases to Claude Code's `--model` flag values.

    Pass-through for anything not in the alias map — Claude Code
    accepts most short forms (haiku/sonnet/opus) directly, and bare
    canonical IDs (claude-opus-4-7, etc.) work too. Only aliases
    that wouldn't resolve at the CLI need translation.
    """
    return _MODEL_ALIAS_MAP.get(model, model)


# Effort levels per Anthropic's effort-flag documentation. The
# behavioral signal each level produces (paraphrased from the docs):
#
#   low     — most efficient; significant token savings with some
#             capability reduction. Best for short, scoped tasks
#             paired with explicit checklists. Opus 4.7 respects
#             `low` more strictly than 4.6 — the model scopes to
#             what's asked rather than going above and beyond.
#   medium  — balanced approach with moderate token savings. The
#             drop-in for the average workflow when good results
#             are wanted with reduced costs.
#   high    — API DEFAULT. Equivalent to not setting the parameter.
#             Strong reasoning balanced with token efficiency — often
#             the sweet spot.
#   xhigh   — extended capability for long-horizon work (Opus 4.7
#             ONLY). Recommended starting point for coding and
#             agentic tasks; meaningfully higher token usage than
#             `high`. Set max_tokens generously (~64k) so the model
#             has room.
#   max     — maximum capability with no constraints on token
#             spending. Available on Sonnet 4.6, Opus 4.6, Opus 4.7,
#             and Mythos Preview. Reserve for genuinely frontier
#             problems — on most workloads `max` adds significant
#             cost for relatively small quality gains; on
#             structured-output or less intelligence-sensitive
#             tasks it can lead to overthinking.
#
# Levels not supported by the chosen model clamp at the runtime to
# the highest supported (e.g. xhigh on Sonnet → high). The deck
# doesn't try to be smarter than the runtime — pass the level
# through and let it clamp.
KNOWN_EFFORTS: frozenset[str] = frozenset({
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
})


# Deck baseline. Used when nothing else specifies a caliber: pool
# warming, daemon-without-override, and the deck's command-line
# defaults all start from here. Lazy convention: mutable default
# in the App's __init__, immutable here so the constant stays a
# constant.
DEFAULT_MODEL: str = "sonnet"
DEFAULT_EFFORT: str = "high"
DEFAULT_FAST_MODE: bool = False


@dataclass(frozen=True)
class Caliber:
    """Bundle of model + effort + fast-mode.

    Frozen (hashable) so callers can dict-key by caliber if they
    want, and because mutating a caliber mid-spawn would produce
    surprising behavior (the construct's command line is built
    once at spawn time).

    Validation is in __post_init__ — unknown strings warn to stderr
    but don't reject. The daemon may emit a model/effort the deck
    doesn't have in its constants table yet; better to pass it
    through to Claude Code (which knows the live model list) and
    let the runtime decide than to crash a spawn over a stale
    deck-side constant.

    Construction patterns:
      Caliber()                         # all defaults
      Caliber(model="haiku")            # haiku at default effort
      Caliber(model="opus", effort="xhigh")
      Caliber.default()                 # explicit named-default builder
    """

    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    fast_mode: bool = DEFAULT_FAST_MODE

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model:
            raise ValueError(
                f"Caliber.model must be a non-empty string, got "
                f"{self.model!r}"
            )
        if not isinstance(self.effort, str) or not self.effort:
            raise ValueError(
                f"Caliber.effort must be a non-empty string, got "
                f"{self.effort!r}"
            )
        if not isinstance(self.fast_mode, bool):
            raise ValueError(
                f"Caliber.fast_mode must be a bool, got "
                f"{type(self.fast_mode).__name__}"
            )
        # Soft warnings — don't reject, just surface.
        if self.model not in KNOWN_MODELS:
            print(
                f"caliber: warning: model {self.model!r} not in "
                f"known set {sorted(KNOWN_MODELS)} — passing "
                f"through to Claude Code anyway",
                file=sys.stderr,
            )
        if self.effort not in KNOWN_EFFORTS:
            print(
                f"caliber: warning: effort {self.effort!r} not in "
                f"known set {sorted(KNOWN_EFFORTS)} — passing "
                f"through to Claude Code anyway",
                file=sys.stderr,
            )
        # Fast mode is Opus 4.6-only per Anthropic's docs (verified
        # 2026-05-04). Opus 4.7 is NOT supported; the API errors on
        # fast+opus-4.7. Haiku/Sonnet are obviously wrong. Soft warn
        # here — don't reject, since the runtime will surface the
        # error itself and we don't want to gate on a known-evolving
        # surface (fast mode is in beta / research preview).
        if self.fast_mode:
            if not _is_fast_mode_compatible(self.model):
                print(
                    f"caliber: warning: fast_mode=True with model "
                    f"{self.model!r} — fast mode requires Opus 4.6 "
                    f"specifically (`opus[4.6]` / `claude-opus-4-6`). "
                    f"Other models will error at the API. Set the "
                    f"model to opus 4.6 or drop fast_mode.",
                    file=sys.stderr,
                )

    @classmethod
    def default(cls) -> "Caliber":
        """The deck-baseline caliber. Use when callers want the
        named default rather than constructing with no args (which
        works but reads less clearly at call sites)."""
        return cls()

    def to_claude_args(self) -> list[str]:
        """Render the CLI argument list for this caliber.

        Returns the args to APPEND to the existing claude command:
            ["--model", "sonnet", "--effort", "high"]

        Deck-side aliases get resolved to Claude Code's canonical
        forms here (e.g. `opus[4.6]` → `claude-opus-4-6`) so the
        `--model` flag carries something the CLI recognizes. See
        _resolve_model_alias for the translation map.

        Phase 2 scope: model + effort go via CLI flags. fast_mode
        is emitted via the per-spawn settings.json file
        (`"fastMode": true`) — see brake_state.make_spawn_settings.
        Anthropic's raw API surface is `speed: "fast"`; Claude
        Code's settings.json wrapper for fast mode is presumed to
        be `fastMode: true` (matches the `fast_mode_state` field
        in Claude Code's `system_init` event payload). The exact
        key is UNVERIFIED at the deck level — real-deck testing
        will confirm or correct.
        """
        return [
            "--model", _resolve_model_alias(self.model),
            "--effort", self.effort,
        ]

    def merge(self, override: Optional["Caliber"]) -> "Caliber":
        """Return a new Caliber with override's fields applied on
        top of self's. None override returns self unchanged.

        Used by the override hierarchy: deck default merged with
        daemon's per-spawn pick merged with netrunner's chat
        directive. Each layer can specify just the fields it cares
        about and let the rest fall through.

        Today's Caliber has only three fields and merge is total
        (override replaces wholesale), but the call shape is
        future-friendly: when we grow Caliber to include
        sub-feature toggles (e.g. cache_breakpoint flags),
        per-field merging stays correct.
        """
        if override is None:
            return self
        return Caliber(
            model=override.model,
            effort=override.effort,
            fast_mode=override.fast_mode,
        )

    def display(self) -> str:
        """Render a short human-readable form for chatlog markers,
        sidebar, and pane headers. Format: `model·effort` with a
        `·fast` suffix when fast_mode is on.

        Examples:
            "sonnet·high"
            "opus·xhigh"
            "opus·high·fast"
        """
        base = f"{self.model}·{self.effort}"
        if self.fast_mode:
            base += "·fast"
        return base


def parse_caliber_directive(text: str) -> Optional[dict]:
    """Detect a caliber-shift directive in netrunner free-text (T-chat).

    Returns a dict with `model` and/or `effort` keys when the message
    looks like an explicit caliber directive. Returns None when the
    message doesn't match any known directive pattern — caller treats
    it as ordinary chat content.

    The matching is intentionally narrow:
      - Phrases must mention "switch", "use", "drop to", "go to",
        "set" + "daemon" or just the bare model/effort with explicit
        verbs. Naked `opus` in a sentence shouldn't trigger.
      - Both an explicit model and effort can appear in one phrase
        ("switch the daemon to opus xhigh").
      - At least one of (model, effort) must be present and recognized.

    Examples that match:
      "switch the daemon to opus"           → {"model": "opus"}
      "use sonnet for the daemon"           → {"model": "sonnet"}
      "drop to medium effort"               → {"effort": "medium"}
      "switch daemon to opus xhigh"         → {"model": "opus",
                                              "effort": "xhigh"}
      "set daemon caliber to opus[4.6] high" → {"model": "opus[4.6]",
                                                "effort": "high"}

    Examples that DON'T match (return None):
      "the opus result was good"             → too oblique
      "i think haiku would be cheaper"       → no directive verb
      "what's the current model?"            → it's a question

    Phase 3 of the caliber slice (2026-05-04). Built deliberately
    simple — if the netrunner reaches for grammar this misses,
    we'll grow it. A wide regex up front would catch too many
    false positives.
    """
    if not text:
        return None
    lowered = text.strip().lower()

    # Verb gate: at least one of these tokens must appear, ANCHORED
    # to a directive intent. "switch / use / drop to / go to / set"
    # plus "daemon" or "caliber" or "effort" / "model" as the noun.
    verb_pattern = re.compile(
        r"\b(?:switch|use|drop\s+to|go\s+to|set|run|put)\b",
    )
    if not verb_pattern.search(lowered):
        return None

    result: dict = {}

    # Model match: try the bracket-suffix form first (e.g.
    # `opus[4.6]`, `sonnet[1m]`). Falls through to the bare-alias
    # form if no bracket suffix is present. The trailing `\b` was
    # dropped because `]` + space isn't a word boundary, so a single
    # combined regex with an optional bracket group + trailing `\b`
    # silently dropped the suffix.
    bracketed_model = re.search(
        r"\b(haiku|sonnet|opus)(\[[^\]]+\])(?=\s|$|[.,!?;])",
        lowered,
    )
    if bracketed_model:
        result["model"] = bracketed_model.group(1) + bracketed_model.group(2)
        model_match_end = bracketed_model.end()
    else:
        bare_model = re.search(
            r"\b(haiku|sonnet|opus)\b",
            lowered,
        )
        if bare_model:
            result["model"] = bare_model.group(1)
            model_match_end = bare_model.end()
        else:
            model_match_end = -1

    # Effort match. Two valid shapes:
    #   (a) "X effort" — explicit suffix word
    #   (b) directive cue (preposition / verb) just before X
    #   (c) X immediately follows the matched model (e.g.
    #       "opus xhigh") — once we've seen a model in directive
    #       context, an adjacent effort token is implicitly part of
    #       the same directive.
    effort_pattern = re.compile(
        r"\b(low|medium|high|xhigh|max)(\s+effort)?\b"
    )
    for effort_match in effort_pattern.finditer(lowered):
        candidate = effort_match.group(1)
        has_effort_word = effort_match.group(2) is not None
        # Check the ~25 chars before the match for a directive cue.
        before = lowered[max(0, effort_match.start() - 25):
                          effort_match.start()]
        directive_cues = re.compile(
            r"\b(?:to|at|in|run|use|switch|drop|set|put)\b\s*$"
        )
        cue_present = directive_cues.search(before) is not None
        # Adjacent to the matched model: the effort token starts
        # within ~3 chars (whitespace) of where the model ended.
        adjacent_to_model = (
            model_match_end >= 0
            and 0 < effort_match.start() - model_match_end <= 3
        )
        if has_effort_word or cue_present or adjacent_to_model:
            result["effort"] = candidate
            break

    return result if result else None


def caliber_from_dict(raw: Optional[dict]) -> Optional[Caliber]:
    """Parse a Caliber from a dict shape (typically the daemon's
    spawn-action JSON). Returns None when raw is None or empty —
    callers fall back to the deck default.

    Tolerant: missing fields default; non-string model/effort raise
    cleanly with the field name; unknown enum values warn (per
    __post_init__) but don't reject.

    Field aliases for the daemon's convenience:
      model     | model_alias
      effort    | effort_level

    `fast_mode` is INTENTIONALLY NOT PARSED here. Per the netrunner's
    2026-05-04 reframing, fast_mode is a deck-wide cost governor —
    it's a 6x-cost-for-2.5x-speed budget switch, not a routing
    decision. The daemon picks model + effort based on task; the
    netrunner toggles fast_mode at the deck level. If the daemon
    emits fast_mode in its spawn JSON, we ignore it silently — the
    deck applies fast_mode from its own state at spawn time, gated
    on Opus 4.6 model eligibility.
    """
    if not raw:
        return None
    if not isinstance(raw, dict):
        # Daemon emitted something weird in the model/effort field —
        # don't crash; let the spawn fall through to default. The
        # daemon-session loop will surface the malformed action in
        # the next outcome turn.
        return None

    model = raw.get("model") or raw.get("model_alias")
    effort = raw.get("effort") or raw.get("effort_level")

    # If neither model nor effort was specified, return None —
    # caller knows to use deck default. Distinguishes "daemon
    # explicitly said model=haiku" from "daemon said nothing about
    # caliber for this spawn." fast_mode never participates in
    # this distinction (it's deck-side, not daemon-side).
    if model is None and effort is None:
        return None

    # Fall through to defaults for unspecified fields. fast_mode
    # always starts False here — the deck-side governor overlays
    # the actual value at fleet-spawn time.
    if model is None:
        model = DEFAULT_MODEL
    if effort is None:
        effort = DEFAULT_EFFORT

    return Caliber(
        model=str(model),
        effort=str(effort),
        fast_mode=False,
    )
