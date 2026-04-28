"""
tripwires.py — deterministic pattern matchers that the watchdog uses
to surface concerning behavior in real time.

Spec model: "LLM authors, deterministic enforces" — same architecture
as the brake hook. The matcher engine here is the deterministic
half. Slice 1 (this file) ships:

  - `Tripwire` dataclass with a small DSL: regex pattern + event-kind
    scope + field selector. Designed to grow more pattern types
    (silence thresholds, count-based drift indicators) without
    breaking existing entries.
  - `TripwireEngine` — registry of active tripwires + per-event scan.
    Sub-millisecond per scan on small N. Emits fire events via an
    on_fire callback so listeners (chatlog, future severity routing)
    can react without polling.
  - Two default tripwires shipped with the deck: a credentials-
    keyword sniffer (low severity) and a destructive-SQL detector
    (warning severity). These demonstrate the engine without
    requiring LLM authoring — that lands in slice 2.

What slice 1 does NOT ship:
  - LLM-authored tripwires (slice 2 — authoring at goal start /
    goal update, hook into watchdog Q&A path)
  - Severity-aware routing — single tier today; critical / warning /
    low all render the same way in the chatlog (slice 3)
  - Persistence — tripwires are session-scoped like the blacklist
    (slice 4 if there's pull for a persistent library)
  - Daemon-side severity hints — slice 5
  - Timer-based patterns (`silent_for_seconds`) — slice 6 if
    deferred from slice 1's regex-only matcher

Engine ownership: lives on the Watchdog (per spec — "the watchdog
authors hook policy and observes"). Fleet feeds events in via a
listener; TUI subscribes to fire events for chatlog rendering.
Mirrors the Blacklist data ownership pattern from yesterday.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from construct import EventKind


# Severity tiers. Single rendering today (slice 1) — listed here so
# the data model is forward-compatible with slice 3's focus-pulling /
# badge-only / log-only routing.
class Severity:
    LOW = "low"
    WARNING = "warning"
    CRITICAL = "critical"


# Field selectors. Tripwires don't match against arbitrary event
# JSON — they match against extracted text from specific kinds of
# fields. This keeps matchers precise (no false-fire on assistant
# text mentioning a dangerous command pattern that's only a problem
# when actually executed) and the DSL discoverable (small fixed set
# of known fields rather than JSONPath traversal).
class Field:
    ANY = "any"  # all extracted text from the event, joined
    TOOL_USE_COMMAND = "tool_use_command"  # bash/PowerShell command field
    TOOL_USE_INPUT = "tool_use_input"  # all tool input as JSON-ish string
    TOOL_RESULT_CONTENT = "tool_result_content"  # tool output text
    THINKING_TEXT = "thinking_text"  # thinking blocks
    ASSISTANT_TEXT = "assistant_text"  # plain assistant text
    USER_TEXT = "user_text"  # user-side text (rare; mostly inject paths)


# Origins. Where did this tripwire come from? Useful for the netrunner
# to tell deck-shipped defaults apart from LLM-authored ones from
# manually-registered ones.
class Origin:
    DEFAULT = "default"  # ships with the deck
    MANUAL = "manual"  # netrunner-registered (slice 4 UI; today via API)
    LLM_AUTHORED = "llm_authored"  # watchdog authored at goal-start (slice 2)
    BLACKLIST_DERIVED = "blacklist_derived"  # auto-generated from blacklist (deferred)


# Scope. Per-construct tripwires fire on events from one specific
# construct (tracked by id). Deck-global tripwires apply to every
# construct. Default tripwires are deck-global; LLM-authored
# tripwires are typically per-construct.
class Scope:
    DECK_GLOBAL = "deck_global"
    PER_CONSTRUCT = "per_construct"


@dataclass
class Tripwire:
    """One deterministic matcher.

    Designed as a discriminated-union-by-string-field pattern: the
    `pattern_type` field selects which matcher implementation runs.
    Today only "regex" is implemented; future types ("silence",
    "count_threshold", etc.) extend the engine without changing
    existing entries.
    """
    name: str
    description: str
    pattern_type: str  # "regex" today; extensible
    pattern: str  # the regex itself for pattern_type=="regex"
    event_kinds: tuple[str, ...]  # which EventKinds to scan; empty tuple = all
    field: str  # which extracted text to match; see Field constants
    severity: str = Severity.WARNING
    scope: str = Scope.DECK_GLOBAL
    construct_id: Optional[str] = None  # only meaningful when scope=PER_CONSTRUCT
    origin: str = Origin.MANUAL
    authored_at: float = field(default_factory=time.time)


@dataclass
class TripwireFire:
    """A registered tripwire matched an event. Carries enough context
    that listeners can render or act without re-scanning the event."""
    tripwire_name: str
    severity: str
    construct_id: str
    matched_text_excerpt: str  # truncated; the full event is still in logs
    event_kind: str
    fired_at: float = field(default_factory=time.time)


# -- text extraction --------------------------------------------------------
#
# Each Field constant maps to a function that pulls the relevant text
# out of a raw stream-json event. Returns "" when the field doesn't
# apply to this event shape — matchers see no text and skip.

def _extract_assistant_text(raw: dict) -> str:
    """All `text` blocks from an assistant message."""
    parts: list[str] = []
    for block in raw.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)


def _extract_thinking_text(raw: dict) -> str:
    """All `thinking` blocks from an assistant message."""
    parts: list[str] = []
    for block in raw.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "thinking":
            parts.append(block.get("thinking", ""))
    return "\n".join(parts)


def _extract_tool_use_command(raw: dict) -> str:
    """The `command` field from any tool_use block (Bash, PowerShell).
    Other tool_use shapes return empty (their input lives in
    TOOL_USE_INPUT, not in a command field)."""
    parts: list[str] = []
    for block in raw.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input", {}) or {}
            cmd = inp.get("command", "")
            if cmd:
                parts.append(str(cmd))
    return "\n".join(parts)


def _extract_tool_use_input(raw: dict) -> str:
    """All tool_use input fields as a single string. Catches
    file paths in Read/Write tool calls, URL params, etc."""
    import json as _json
    parts: list[str] = []
    for block in raw.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_use":
            inp = block.get("input", {}) or {}
            try:
                parts.append(_json.dumps(inp, default=str))
            except (TypeError, ValueError):
                parts.append(str(inp))
    return "\n".join(parts)


def _extract_tool_result_content(raw: dict) -> str:
    """The `content` field from any tool_result block."""
    parts: list[str] = []
    for block in raw.get("message", {}).get("content", []):
        if isinstance(block, dict) and block.get("type") == "tool_result":
            content = block.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                # tool_result content can be a list of {type, text} blocks
                for sub in content:
                    if isinstance(sub, dict):
                        text = sub.get("text", "")
                        if text:
                            parts.append(str(text))
    return "\n".join(parts)


def _extract_user_text(raw: dict) -> str:
    """Plain text content from a user message (string or block list)."""
    msg = raw.get("message", {})
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "\n".join(parts)
    return ""


_FIELD_EXTRACTORS: dict[str, Callable[[dict], str]] = {
    Field.TOOL_USE_COMMAND: _extract_tool_use_command,
    Field.TOOL_USE_INPUT: _extract_tool_use_input,
    Field.TOOL_RESULT_CONTENT: _extract_tool_result_content,
    Field.THINKING_TEXT: _extract_thinking_text,
    Field.ASSISTANT_TEXT: _extract_assistant_text,
    Field.USER_TEXT: _extract_user_text,
}


def extract_field(raw: dict, field_name: str) -> str:
    """Pull the requested field's text out of a raw event. Returns
    "" for unknown fields or events that don't carry that field."""
    if field_name == Field.ANY:
        # Concatenate all known fields. Bounded by the event's actual
        # content; matchers can still scope via event_kinds.
        return "\n".join(
            extractor(raw) for extractor in _FIELD_EXTRACTORS.values()
        )
    extractor = _FIELD_EXTRACTORS.get(field_name)
    if extractor is None:
        return ""
    return extractor(raw)


# -- engine -----------------------------------------------------------------


class TripwireEngine:
    """Deterministic matcher registry + per-event scanner.

    Owned by the Watchdog (per spec — "watchdog is the persistent
    memory of what's forbidden / what to watch for"). Fleet listeners
    feed events in via `scan(construct_id, event_kind, raw)`. Fires
    are dispatched to the registered `on_fire` callback synchronously.

    Performance: O(N) per event in registered tripwires. Cap of ~50
    active tripwires recommended (well within sub-millisecond scan
    budget on regex-only matchers; bookkeeping for higher counts
    can come if a real workload demands it).

    Thread/async safety: assumes single-threaded event-loop access,
    same as the rest of the deck. Register/scan are not safe to call
    concurrently from different threads — wrap externally if that
    ever becomes a need.
    """

    def __init__(
        self,
        on_fire: Optional[Callable[[TripwireFire], None]] = None,
    ) -> None:
        # Indexed by name. Names must be unique; re-registering an
        # existing name updates in place (useful for LLM authoring
        # that wants to refresh a tripwire's pattern at goal-update
        # time without unregistering first).
        self._tripwires: dict[str, Tripwire] = {}
        # Compiled regex cache, keyed by name. Recompiled on register
        # (cheap — regexes here are short).
        self._compiled: dict[str, re.Pattern] = {}
        self.on_fire = on_fire

    # ---- registry --------------------------------------------------

    def register(self, tw: Tripwire) -> None:
        """Add or replace a tripwire by name. Compiles the pattern
        eagerly so per-event scan stays fast and pattern errors
        surface at registration time, not at first match."""
        if tw.pattern_type == "regex":
            try:
                self._compiled[tw.name] = re.compile(
                    tw.pattern, re.IGNORECASE | re.MULTILINE,
                )
            except re.error as exc:
                # Don't break the engine over a bad pattern; surface
                # via stderr and skip. The netrunner sees the
                # tripwire isn't registered and can fix the pattern.
                import sys as _sys
                print(
                    f"tripwire {tw.name!r}: bad regex {tw.pattern!r}: {exc}",
                    file=_sys.stderr,
                )
                return
        else:
            # Other pattern types not implemented in slice 1.
            import sys as _sys
            print(
                f"tripwire {tw.name!r}: unknown pattern_type "
                f"{tw.pattern_type!r}; skipping",
                file=_sys.stderr,
            )
            return
        self._tripwires[tw.name] = tw

    def unregister(self, name: str) -> bool:
        """Remove a tripwire by name. Returns True if it was
        present, False otherwise."""
        existed = name in self._tripwires
        self._tripwires.pop(name, None)
        self._compiled.pop(name, None)
        return existed

    @property
    def tripwires(self) -> list[Tripwire]:
        """Snapshot of currently-registered tripwires. Returns a
        fresh list so callers can iterate without worrying about
        concurrent register/unregister."""
        return list(self._tripwires.values())

    def __len__(self) -> int:
        return len(self._tripwires)

    # ---- scan ------------------------------------------------------

    def scan(
        self, construct_id: str, event_kind: str, raw: dict,
    ) -> list[TripwireFire]:
        """Scan one event against all registered tripwires. Returns
        the list of fires (also dispatched to on_fire callback if
        set). Empty list = no fires.

        Scope and event_kinds gating happens here, before the regex
        runs — keeps the per-event cost bounded by the small subset
        of tripwires that actually apply to this event shape.
        """
        fires: list[TripwireFire] = []
        for tw in self._tripwires.values():
            # Scope check: per-construct tripwires only fire for
            # their target construct.
            if tw.scope == Scope.PER_CONSTRUCT and tw.construct_id != construct_id:
                continue
            # Event-kind gating: empty tuple means "all kinds";
            # otherwise the event's kind must be in the list.
            if tw.event_kinds and event_kind not in tw.event_kinds:
                continue
            # Extract the field we care about.
            text = extract_field(raw, tw.field)
            if not text:
                continue
            # Run the matcher.
            if tw.pattern_type == "regex":
                rx = self._compiled.get(tw.name)
                if rx is None:
                    continue
                m = rx.search(text)
                if m is None:
                    continue
                # Build the fire record. Truncate the matched excerpt
                # so a 50KB tool_result doesn't bloat the log.
                excerpt = m.group(0)
                # Add a little surrounding context if we have it,
                # bounded.
                start = max(0, m.start() - 30)
                end = min(len(text), m.end() + 30)
                excerpt_with_ctx = text[start:end].replace("\n", " ").strip()
                if len(excerpt_with_ctx) > 120:
                    excerpt_with_ctx = excerpt_with_ctx[:117] + "..."
                fire = TripwireFire(
                    tripwire_name=tw.name,
                    severity=tw.severity,
                    construct_id=construct_id,
                    matched_text_excerpt=excerpt_with_ctx,
                    event_kind=event_kind,
                )
                fires.append(fire)
        # Dispatch all fires after the scan loop (so on_fire callbacks
        # can register/unregister tripwires safely without iteration
        # invalidation).
        if self.on_fire is not None:
            for fire in fires:
                try:
                    self.on_fire(fire)
                except Exception:
                    # Listener errors must not corrupt the engine.
                    pass
        return fires


# -- default tripwires shipped with the deck --------------------------------
#
# Two examples that demonstrate the engine without requiring LLM
# authoring (which lands in slice 2). Both are deck-global, both fire
# on patterns that the existing brake hook + chatlog don't surface
# specifically, and both are scoped via event_kinds + field so they
# don't false-fire on incidental mentions.

DEFAULT_TRIPWIRES: tuple[Tripwire, ...] = (
    Tripwire(
        name="keyword_credentials",
        description=(
            "Construct's tool output contains a credential-related "
            "keyword (password, api_key, secret, etc.). Low severity — "
            "informational, mostly for the netrunner to spot accidental "
            "secret exposure in logs / responses."
        ),
        pattern_type="regex",
        # Word-boundary matched, case-insensitive. Catches `password`,
        # `Passwords`, `API_KEY`, `api key`, `secret`, `credential`,
        # `credentials`. Won't false-fire on `pass` or `passport`.
        pattern=r"\b(password|api[_\s-]?key|secret|credentials?)\b",
        event_kinds=(EventKind.TOOL_RESULT,),
        field=Field.TOOL_RESULT_CONTENT,
        severity=Severity.LOW,
        scope=Scope.DECK_GLOBAL,
        origin=Origin.DEFAULT,
    ),
    Tripwire(
        name="keyword_destructive_sql",
        description=(
            "Construct issued a destructive SQL command (DROP TABLE / "
            "DELETE FROM / TRUNCATE) via a Bash or PowerShell tool "
            "call. Warning severity — the brake hook's destructive "
            "patterns are bash-shaped (rm -rf, format, etc.); SQL is "
            "a different vector with similar blast radius."
        ),
        pattern_type="regex",
        # Match on shell commands that pipe SQL through a client.
        # Word-boundary + case-insensitive. Allows whitespace-tolerant
        # matching.
        pattern=(
            r"\b("
            r"DROP\s+TABLE|"
            r"TRUNCATE\s+TABLE|"
            r"DELETE\s+FROM\s+\w+\s*(WHERE|;|$)"
            r")"
        ),
        event_kinds=(EventKind.TOOL_USE,),
        field=Field.TOOL_USE_COMMAND,
        severity=Severity.WARNING,
        scope=Scope.DECK_GLOBAL,
        origin=Origin.DEFAULT,
    ),
)


def install_default_tripwires(engine: TripwireEngine) -> None:
    """Register the deck's default tripwires on a fresh engine.
    Idempotent — calling twice doesn't double-register since names
    are unique."""
    for tw in DEFAULT_TRIPWIRES:
        engine.register(tw)
