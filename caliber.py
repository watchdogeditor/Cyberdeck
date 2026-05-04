"""
Caliber — per-spawn model + effort + fast-mode bundle.

A Caliber is the capability/cost grade a construct (or the daemon, or
watchdog) deploys at. Three independent axes wrapped in one dataclass:

  - model:  haiku / sonnet / opus, plus 1M-context variants
  - effort: low / medium / high / xhigh / max
  - fast_mode: bool (Opus 4.6-only latency knob)

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

This module is data + validation only. Zero integration with
fleet/daemon/TUI; pure dataclass.
"""
from __future__ import annotations

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
    "opus",
    "sonnet[1m]",   # 1M-context variants
    "opus[1m]",
})


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
        # Fast mode is Opus 4.6-specific per the design; "fast on
        # haiku" is meaningless, "fast on opus 4.7" isn't supported.
        # Soft warn here — don't reject, since Anthropic's surface
        # may evolve.
        if self.fast_mode and not self.model.startswith("opus"):
            print(
                f"caliber: warning: fast_mode set with model "
                f"{self.model!r} — fast mode is Opus-only; the "
                f"runtime will likely ignore the fast-mode flag",
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

        Phase 1 scope: model + effort go via CLI flags. fast_mode
        is NOT emitted here — it requires settings.json editing
        ("fastMode": true), which is Phase 2 territory (composes
        with the existing brake-hook settings JSON). For Phase 1,
        a Caliber with fast_mode=True falls through silently;
        Phase 2 wires the settings.json path.
        """
        return ["--model", self.model, "--effort", self.effort]

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
      fast_mode | fast | fastMode

    Deliberately tolerant: Claude itself may emit settings-style
    field names (`fastMode`) when paraphrasing the docs, and the
    deck-side parser swallowing those gracefully is cheaper than
    debugging "why did my spawn ignore fast_mode."
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
    fast_mode = raw.get("fast_mode")
    if fast_mode is None:
        fast_mode = raw.get("fast")
    if fast_mode is None:
        fast_mode = raw.get("fastMode")

    # If literally none of the three were specified, return None —
    # caller knows to use deck default. Distinguishes "daemon
    # explicitly said model=haiku" from "daemon said nothing about
    # caliber for this spawn."
    if model is None and effort is None and fast_mode is None:
        return None

    # Fall through to defaults for unspecified fields.
    if model is None:
        model = DEFAULT_MODEL
    if effort is None:
        effort = DEFAULT_EFFORT
    if fast_mode is None:
        fast_mode = DEFAULT_FAST_MODE

    # Type coercion — accept "true"/"false" strings for fast_mode
    # because the daemon occasionally emits bool-as-string.
    if isinstance(fast_mode, str):
        fast_mode = fast_mode.lower() in ("true", "1", "yes", "on")
    fast_mode = bool(fast_mode)

    return Caliber(
        model=str(model),
        effort=str(effort),
        fast_mode=fast_mode,
    )
