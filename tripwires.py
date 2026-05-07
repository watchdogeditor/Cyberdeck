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

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

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
    # Slice 2 escalation chain additions: tripwires gain teeth.
    # `suggestion` is the warning-tier "do this instead" message — the
    # netrunner's framing was "hey, not like that." Surfaced in the
    # brake-hook denial stderr so the construct sees the alternative
    # alongside the deny. Empty string for low/critical (the deny
    # message there doesn't need to suggest a pivot — low is log-only,
    # critical kills the construct).
    suggestion: str = ""
    # `bad_enough` is the critical-tier auto-blacklist marker — the
    # netrunner's "rogue AI red button." When True and the tripwire
    # fires at critical severity, the watchdog proposes a blacklist
    # add (with netrunner approval window — wired in slice 3, not yet
    # automatic). When False, critical fires kill the construct but
    # don't blacklist. Default False so the netrunner has to explicitly
    # opt into auto-blacklist on a per-tripwire basis.
    bad_enough: bool = False


@dataclass
class TripwireFire:
    """A registered tripwire matched an event. Carries enough context
    that listeners can render or act without re-scanning the event."""
    tripwire_name: str
    severity: str
    construct_id: str
    matched_text_excerpt: str  # truncated; the full event is still in logs
    event_kind: str
    # Slice 2 escalation chain additions: fire records carry the
    # source tripwire's description / suggestion / bad_enough so
    # downstream consumers (brake hook flag file, chatlog, kill
    # handler) don't need to dereference back through the registry.
    # Denormalized intentionally — fires are short-lived records, the
    # registry can mutate, and the fire's context should outlive any
    # registry change.
    description: str = ""
    suggestion: str = ""
    bad_enough: bool = False
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
        bus: Optional[Any] = None,
        home_dir: Optional[Any] = None,
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
        # Phase 4 of the unified-event-stream slice. When wired, every
        # fire ALSO publishes a `tripwire.fire` DeckEvent on the bus
        # alongside the existing on_fire callback. None means no bus
        # (e.g. headless test scenarios that only use the callback).
        # Type is `Any` rather than `EventBus` to avoid a circular
        # import — event_bus.py is a leaf module that imports nothing
        # from the rest of the deck, and tripwires.py is consumed by
        # watchdog.py which is consumed by tui.py. Adding an
        # `EventBus` type here would force event_bus -> tripwires
        # -> watchdog ordering at import; staying duck-typed keeps
        # the dependency graph clean. Same pattern in Blacklist.
        self.bus = bus
        # Slice 2 escalation chain: when a warning or critical fires,
        # the engine writes a per-construct deny_pending JSON file
        # the brake hook reads to deny that construct's NEXT tool
        # call. home_dir tells the engine where to write — the same
        # spawns/ directory brake_state.make_spawn_settings uses.
        # None means no flag-write path (engine still fires for
        # observability, but tripwires don't gain teeth).
        self.home_dir = home_dir

    # ---- registry --------------------------------------------------

    def register(self, tw: Tripwire) -> bool:
        """Add or replace a tripwire by name. Compiles the pattern
        eagerly so per-event scan stays fast and pattern errors
        surface at registration time, not at first match.

        Returns True on success, False if the entry was dropped
        (bad regex, unknown pattern_type). Slice 2 authoring wants
        to know which entries actually landed for chatlog reporting;
        the bool lets callers tell `landed` apart from `dropped`
        without poking at private state. Pre-slice-2 callers that
        don't need the signal can just ignore the return value."""
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
                return False
        else:
            # Other pattern types not implemented in slice 1.
            import sys as _sys
            print(
                f"tripwire {tw.name!r}: unknown pattern_type "
                f"{tw.pattern_type!r}; skipping",
                file=_sys.stderr,
            )
            return False
        self._tripwires[tw.name] = tw
        return True

    def unregister(self, name: str) -> bool:
        """Remove a tripwire by name. Returns True if it was
        present, False otherwise."""
        existed = name in self._tripwires
        self._tripwires.pop(name, None)
        self._compiled.pop(name, None)
        return existed

    def clear_by_origin(self, origin: str) -> int:
        """Drop every tripwire whose `origin` field matches. Returns
        the count removed.

        Slice 2's tripwire authoring uses this to clear all prior
        `Origin.LLM_AUTHORED` entries before each authoring pass —
        defaults / manual / blacklist-derived stay untouched. Lifecycle
        is "replace, don't accumulate" so old-goal rules don't linger
        forever after a pivot."""
        names = [
            name for name, tw in self._tripwires.items() if tw.origin == origin
        ]
        for name in names:
            self.unregister(name)
        return len(names)

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
                    description=tw.description,
                    suggestion=tw.suggestion,
                    bad_enough=tw.bad_enough,
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
        # Phase 4: also publish each fire on the bus when wired.
        # Severity on the DeckEvent mirrors the tripwire's severity
        # so subscribers can filter by severity tier without inspecting
        # the payload (tripwires slice 3 will use this for focus-pulling
        # rendering of critical fires). Bus's per-callback exception
        # isolation handles any subscriber misbehavior independently
        # — it cannot poison the engine's existing on_fire path.
        if self.bus is not None:
            from event_bus import DeckEvent
            for fire in fires:
                try:
                    self.bus.publish(DeckEvent(
                        kind="tripwire.fire",
                        source="watchdog.tripwires",
                        timestamp=fire.fired_at,
                        construct_id=fire.construct_id,
                        severity=fire.severity,
                        text=(
                            f"⚠ tripwire {fire.tripwire_name} on "
                            f"{fire.construct_id}: "
                            f"{fire.matched_text_excerpt}"
                        ),
                        payload=fire,
                    ))
                except Exception:
                    # Defensive — even with the bus's own exception
                    # isolation, the construction or publish call
                    # itself shouldn't crash the engine if something
                    # weird happens (broken bus reference, etc.).
                    pass
        # Slice 2 escalation chain: warning + critical fires write a
        # per-construct deny_pending file the brake hook reads. The
        # hook denies the next tool call from this construct with
        # the recorded reason + suggestion (warning) or terminal
        # message (critical). Critical fires ALSO trigger kill via
        # the bus subscriber (tui handler subscribed to
        # tripwire.fire), but the deny is the pre-tool-use block —
        # kill is reactive and only stops SUBSEQUENT calls. The
        # block is what prevents the dangerous call from running in
        # the first place. See cyberdeck-state.md "Safety
        # architecture analysis" for why hook-block matters.
        if self.home_dir is not None:
            for fire in fires:
                if fire.severity in (Severity.WARNING, Severity.CRITICAL):
                    self._write_deny_pending(fire)
        return fires

    def _write_deny_pending(self, fire: TripwireFire) -> None:
        """Write the per-construct deny_pending JSON the brake hook
        reads. Best-effort — disk failures are silent; the cost of
        a missed write is one tool call slipping through under
        warning severity (critical still kills the construct via
        the bus path)."""
        if self.home_dir is None:
            return
        try:
            from pathlib import Path
            import json as _json
            spawns_dir = Path(self.home_dir) / ".cyberdeck" / "spawns"
            spawns_dir.mkdir(parents=True, exist_ok=True)
            path = spawns_dir / f"{fire.construct_id}.deny_pending.json"
            payload = {
                "tripwire_name": fire.tripwire_name,
                "severity": fire.severity,
                "description": fire.description,
                "suggestion": fire.suggestion,
                "matched_text_excerpt": fire.matched_text_excerpt,
                "event_kind": fire.event_kind,
                "fired_at": fire.fired_at,
            }
            path.write_text(
                _json.dumps(payload), encoding="utf-8",
            )
        except OSError:
            # Disk full / permission denied / etc. — degrade gracefully.
            pass


# -- default tripwires shipped with the deck --------------------------------
#
# Deck-default tripwires live on the engine before any LLM authoring
# pass runs. They cover patterns that don't depend on the current goal
# (catastrophic shapes that are wrong regardless of context) so the
# deck has non-zero coverage from the moment it launches. The watchdog's
# LLM authoring (slice 2) layers GOAL-SPECIFIC patterns on top — the
# defaults are the always-on baseline.
#
# All entries are deck-global, all are scoped via event_kinds + field
# to avoid false-firing on incidental mentions in unrelated text. When
# adding new defaults, prefer specificity over breadth — a noisy
# default trains the netrunner to ignore the chatlog.

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
    Tripwire(
        name="host_restart_command",
        description=(
            "Construct issued a host-level restart, reboot, halt, or "
            "power-off command (shutdown, reboot, halt, poweroff, "
            "init 6, systemctl reboot, Restart-Computer, etc.). The "
            "deck runs on this machine — rebooting the host kills "
            "every running construct, the daemon, the watchdog, and "
            "the netrunner's terminal session. Warning severity (not "
            "critical) so the netrunner can still override on the "
            "rare legitimate case."
        ),
        pattern_type="regex",
        # Anchor on shell-token boundaries (start-of-line, whitespace,
        # or one of `;`, `&`, `|`, backtick, `(`) so the match keys
        # off command position rather than substrings inside longer
        # tokens. The trailing `(?:\s|$)` on each bare verb prevents
        # `reboot-tests`, `halting`, `poweroffline`, etc. from firing.
        # `systemctl` is restricted to its three power-state verbs so
        # `systemctl restart nginx` does NOT fire. Shape lifted from
        # an authored tripwire that the watchdog produced for a goal
        # about restart-command safety on 2026-04-30 — the construct
        # that wrote it had thought through all the edge cases
        # already (anchor positions, false-positive trade-offs,
        # PowerShell verb forms). Promoted to default 2026-05-01.
        pattern=(
            r"(?:^|[\s;&|`(])"
            r"(?:"
            r"shutdown(?:\s|$)"
            r"|reboot(?:\s|$)"
            r"|halt(?:\s|$)"
            r"|poweroff(?:\s|$)"
            r"|init\s+[06](?:\s|$)"
            r"|telinit\s+[06](?:\s|$)"
            r"|systemctl\s+(?:reboot|halt|poweroff)(?:\s|$)"
            r"|Restart-Computer"
            r"|Stop-Computer"
            r")"
        ),
        event_kinds=(EventKind.TOOL_USE,),
        field=Field.TOOL_USE_COMMAND,
        severity=Severity.WARNING,
        scope=Scope.DECK_GLOBAL,
        origin=Origin.DEFAULT,
        suggestion=(
            "Don't issue machine-level reboot/shutdown commands — "
            "the deck and your session run on this host. To restart "
            "a service, target it specifically: `systemctl restart "
            "<unit>`, `pkill -f <process>` then re-launch, or "
            "`docker restart <container>`. If a host reboot is "
            "genuinely needed, surface that to the netrunner as a "
            "question rather than executing it."
        ),
        bad_enough=False,
    ),
)


def _user_email_from_claude_json() -> Optional[str]:
    """Read the OAuth-account email from `~/.claude.json`.

    Anthropic's Claude Code stores the OAuth-account email here and
    auto-injects it into every session's context as a `# userEmail`
    block — see https://github.com/anthropics/claude-code/issues/55743.
    There is no documented opt-out flag (`CLAUDE_CODE_DISABLE_AUTO_
    MEMORY=1`, `_DISABLE_CLAUDE_MDS=1`, `--bare`, `--system-prompt`,
    `--exclude-dynamic-system-prompt-sections` — none suppress this
    specific channel; verified empirically 2026-05-06). The deck
    reads this field at startup so it can build a tripwire that
    catches unintended exfiltration of the email to third-party
    services (User-Agent headers, form fields, contact info in
    HTTP requests).

    Returns None on any failure (file missing, parse error, key
    absent, non-string value). The default-tripwire installer
    skips user-email protection in that case — no email known
    means no pattern to match.

    Privacy note: the email lives ONLY in ~/.claude.json (Anthropic-
    written) and the tripwire's compiled regex (in-memory only at
    runtime). The deck never writes the email to disk, never
    commits it to git, never logs it.
    """
    try:
        path = Path.home() / ".claude.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    account = data.get("oauthAccount")
    if not isinstance(account, dict):
        return None
    email = account.get("emailAddress")
    if isinstance(email, str) and "@" in email:
        return email
    return None


def _user_email_tripwire(email: str) -> Tripwire:
    """Build a deck-global tripwire that blocks the netrunner's
    OAuth-account email from appearing in tool commands, tool
    inputs, or visible assistant text.

    Pattern: the email, regex-escaped. Match the literal string.
    No clever obfuscation handling — the threat model is "model
    auto-uses the email it sees in its `# userEmail` context block
    as a User-Agent / contact value," which produces the literal
    email verbatim. If a future leak vector emits an obfuscated
    form (rot13, base64, etc.), we'd extend the pattern then.

    Field selector is ANY (joined extracted text from the event)
    rather than tool_use_command alone, because the leak vector
    has three flavors:
      1. Bash/PowerShell tool calls embedding the email →
         caught via tool_use_command (covered by ANY)
      2. Write/Edit tool calls writing files containing the email →
         caught via tool_use_input (covered by ANY)
      3. Assistant text mentioning the email in chat output →
         caught via assistant_text (covered by ANY when
         event_kind=ASSISTANT)

    Severity is WARNING — the brake hook denies the next call
    from the offending construct with the suggestion below as
    the model-visible deny reason. Not CRITICAL, because the
    netrunner can legitimately ask a construct to use the email
    (e.g. "draft an email to my support contact"); the redirect
    posture preserves that affordance.

    bad_enough=False — auto-blacklist would be too aggressive for
    a privacy-mitigation rule. The construct gets denied + told
    why, and continues on the next turn.
    """
    return Tripwire(
        name="user_email_protection",
        description=(
            "Construct attempted to include the netrunner's OAuth "
            "account email in an output. Anthropic auto-injects "
            "this email into every Claude Code session's context "
            "as a `# userEmail` block — see GitHub issue "
            "anthropics/claude-code#55743. No opt-out flag exists "
            "yet. This tripwire keeps the email from being "
            "transmitted to third parties without explicit "
            "netrunner consent."
        ),
        pattern_type="regex",
        pattern=re.escape(email),
        event_kinds=(EventKind.TOOL_USE, EventKind.ASSISTANT),
        field=Field.ANY,
        severity=Severity.WARNING,
        scope=Scope.DECK_GLOBAL,
        origin=Origin.DEFAULT,
        suggestion=(
            "You are not permitted to utilize the netrunner's "
            "email unless specifically instructed to. The "
            "`# userEmail` block in your context comes from a "
            "Claude Code privacy leak (anthropics/claude-code"
            "#55743), not a directive to use it. If a task needs "
            "contact info (User-Agent header, form field, etc.), "
            "use a generic placeholder like `cyberdeck@example."
            "invalid` or ask the netrunner for explicit "
            "permission first."
        ),
        bad_enough=False,
    )


def install_default_tripwires(engine: TripwireEngine) -> None:
    """Register the deck's default tripwires on a fresh engine.
    Idempotent — calling twice doesn't double-register since names
    are unique.

    Includes a runtime-derived `user_email_protection` tripwire
    when the OAuth-account email is readable from `~/.claude.json`.
    See `_user_email_from_claude_json` for the rationale (Anthropic
    auto-injection bug, no upstream opt-out)."""
    for tw in DEFAULT_TRIPWIRES:
        engine.register(tw)
    email = _user_email_from_claude_json()
    if email:
        engine.register(_user_email_tripwire(email))


# -- slice 2: LLM-authored tripwires ----------------------------------------
#
# Slice 2 ships the "LLM authors, deterministic enforces" half of the
# spec'd tripwire architecture. The watchdog runs an authoring pass at
# goal-start (and on explicit non-clarification goal-update via `e`),
# asking the model "given this goal, what regex patterns should we
# watch for as constructs work toward it?" The model returns strict
# JSON; this module parses + validates; the watchdog registers the
# survivors on the engine.
#
# Lifecycle: each authoring pass clears all prior LLM_AUTHORED entries
# before registering new ones. Defaults / manual / blacklist-derived
# entries are untouched. This avoids old-goal rules lingering after a
# pivot — the "rerun authoring" path is also the "drop yesterday's
# rules" path.
#
# Substrate: the watchdog's existing claude infrastructure. Two rungs
# depending on session state: rung 1 forks the running streaming Q&A
# session via `claude -p --resume <session_id>` so the authoring
# subprocess inherits Q&A conversation context (knows what's happened
# so far without polluting the live Q&A subprocess); rung 2 falls back
# to a fresh one-shot when no streaming session is alive. The model is
# instructed via the user message body in both rungs so the system-
# prompt seam stays the watchdog's Q&A prompt — slice 2 doesn't try to
# replace it, just mode-switches the model with explicit instructions.


# Authoring system prompt. Embedded at the top of the user message body
# (NOT passed as --append-system-prompt) for two reasons: (1) the rung-1
# fork case is resuming a session whose system prompt is already the
# Q&A one; we override behavior via in-body instructions instead of
# layering system prompts. (2) Multi-line content via --append-system-
# prompt has Windows argv-mangling issues per the watchdog one-shot
# path's own comment. Single source for both rungs is simplest.
TRIPWIRE_AUTHORING_SYSTEM_PROMPT = """\
You are the Watchdog of a Cyberdeck — a system that orchestrates AI
agents called "constructs" under a human supervisor (the "netrunner").
You normally answer the netrunner's questions about fleet activity. For
this turn only, switch modes: you are AUTHORING TRIPWIRES.

Tripwires are deterministic regex matchers that fire on construct
event content as it streams in (sub-millisecond, in-process). They
are NOT observation-only — they have teeth. When a tripwire fires,
the deck takes ACTION based on severity:

  - low      → log the fire to the chatlog. No effect on the
               construct. Use for informational signals.
  - warning  → BLOCK the construct's next tool call with a denial
               that quotes your `description` and `suggestion`. The
               construct sees the block as a tool error and decides
               how to pivot. Use for "hey, not like that — try this
               instead" cases.
  - critical → BLOCK the next tool call AND auto-terminate the
               construct entirely. The construct stops running. Use
               for "this thought process is broken; throw the whole
               thing away and try again" cases.
  - critical + bad_enough=true → all of the above, plus the watchdog
               proposes adding the construct's task fingerprint to
               the session blacklist (with netrunner approval). Use
               for "this is a rogue-AI red-button signal." Reserved
               for genuine emergencies; the netrunner will see every
               fire and approve or reject.

What tripwires are NOT:
  - They are not policy. The netrunner is the policy authority. You
    are encoding the netrunner's likely intent for THIS GOAL into
    deterministic patterns that fire when the construct drifts.

LAYERED DEFENSE — DO NOT SKIP PATTERNS. The deck has a separate
brake hook that ALSO blocks destructive shapes (rm -rf system roots,
OS-root writes, fork bombs, MCP destructive verbs). DO NOT skip
authoring a tripwire on the basis that "the brake hook will catch
it." That reasoning is the depth-of-defense antipattern: every
layer assumes another caught the dangerous case, and the dangerous
case slips through if any one layer is misconfigured (e.g., brake
flipped to YOLO). Real-deck observed: a prior authoring pass
authored a `benign_delete_attempt` tripwire whose regex used
negative-lookahead to EXCLUDE rm -rf because "brake handles it" —
exactly the antipattern that defeats layering. If a pattern is
catastrophically dangerous, EVERY layer covers it, REGARDLESS of
what other layers do. Authoring shell-destructive patterns
(rm -rf, format, dd of=/dev, mkfs, fork bombs, shutdown) at
critical severity is REQUIRED, not optional.

Two deck-default tripwires ship with every session:
  - keyword_credentials: low — credentials keywords in tool_result.
  - keyword_destructive_sql: warning — DROP TABLE / TRUNCATE /
    DELETE FROM in tool_use_command.
Don't duplicate these specifically (the patterns already cover
them). DO author other shell-destructive baselines and goal-
specific drift indicators alongside.

OUTPUT FORMAT — strict JSON, nothing else (no prose, no markdown
fences, no preamble):

{
  "tripwires": [
    {
      "name": "snake_case_unique_name",
      "description": "one short sentence: what fires this and why it matters",
      "pattern": "valid Python regex (case-insensitive + multiline applied automatically)",
      "event_kinds": ["tool_use", "tool_result", "assistant", "thinking"],
      "field": "tool_use_command|tool_use_input|tool_result_content|thinking_text|assistant_text|user_text|any",
      "severity": "low|warning|critical",
      "scope": "deck_global",
      "suggestion": "for warning only: what the construct should do instead",
      "bad_enough": false
    }
  ]
}

Field reference:
  - event_kinds: which event types to scan. Empty list [] means "all
    kinds." Pick the smallest applicable set.
    * tool_use: model is about to call a tool (Bash command, Write,
      Read, etc.). Match here for "construct trying to do X."
    * tool_result: tool's output. Match here for "construct just
      saw / received Y."
    * assistant: model's prose response. Match here for "model said Z."
    * thinking: model's reasoning blocks. Match here for "model is
      considering W."
  - field: which extracted text the regex runs against. The field
    selector is what makes tripwires precise — won't false-fire on
    incidental text mentions of dangerous-looking tokens.
    * tool_use_command: the `command` field of Bash / PowerShell calls.
    * tool_use_input: ALL tool input as a JSON-ish string. Catches
      file paths in Read/Write, URL params, etc.
    * tool_result_content: the output of any tool call.
    * assistant_text: text blocks in assistant messages.
    * thinking_text: thinking blocks (reasoning) in assistant messages.
    * user_text: rare; mostly inject paths.
    * any: union of all of the above. Use sparingly — defeats the
      precision gain.
  - severity: as above.
  - scope: always "deck_global" in slice 2. Per-construct authoring
    at spawn time is a future slice.
  - suggestion: required for `severity: warning` — the alternative
    course of action surfaced to the construct alongside the deny.
    Example: tripwire on `shutdown` shell command, suggestion =
    "Restart your dev server / process instead of shutting down the
    machine; the deck stays up." Empty for low/critical.
  - bad_enough: optional, default false. Only meaningful when
    `severity: critical`. Set true only when the pattern matching
    means the construct's thought process is so off-rails it should
    be permanently blacklisted for this session, not just killed.
    Reserved for genuine red-button signals. The watchdog will
    propose the blacklist add to the netrunner; auto-application
    requires netrunner approval.

Authoring guidance:
  - Be surgical. 0-8 rules per pass is normal; 15+ risks noise.
  - Empty output {"tripwires": []} is a valid answer for goals with
    no execution risk. Padding with weak rules is worse than
    authoring nothing.
  - Prefer specific patterns over broad keyword lists. False
    positives train the netrunner to ignore the chatlog.
  - Pick the smallest applicable field. Don't use "any" unless the
    pattern is genuinely cross-field.
  - Each `description` should answer "what triggers this, and why
    does the netrunner care?" in one sentence. The construct sees
    `description` quoted in deny messages on warning fires — bad
    descriptions waste the construct's pivot opportunity.
  - For warnings, `suggestion` should describe a concrete safer
    alternative, not just "don't do that." The construct uses your
    suggestion to pivot; vague suggestions produce vague pivots.
  - Names must be unique within this pass (no duplicates) and use
    snake_case. They appear in chatlog fires and deny messages, so
    make them glanceable.
  - DO author critical-severity shell-destructive baselines
    (rm_rf_system_root, format_disk, dd_to_dev, mkfs_run,
    fork_bomb, shutdown_command, etc.) — even though the brake hook
    also blocks them. Layered defense is the whole point.

OUTPUT NOTHING BUT THE JSON OBJECT. Do not wrap it in fences. Do
not preface it with "Here are the tripwires:". The deck parses your
response programmatically; any non-JSON content makes parsing harder.
"""


# Permitted enum values used by the parser to validate authored
# entries. Kept as a set for O(1) membership; mirrors the public
# Field/Severity/Scope namespaces above. EventKind values aren't
# fully enumerated here (they're a moving target — the construct
# module owns the canonical list); we accept any string and let the
# engine's event_kind gating handle unknowns gracefully (unknown kind
# = matches nothing, which is harmless).
_VALID_FIELDS: frozenset = frozenset({
    Field.ANY, Field.TOOL_USE_COMMAND, Field.TOOL_USE_INPUT,
    Field.TOOL_RESULT_CONTENT, Field.THINKING_TEXT,
    Field.ASSISTANT_TEXT, Field.USER_TEXT,
})
_VALID_SEVERITIES: frozenset = frozenset({
    Severity.LOW, Severity.WARNING, Severity.CRITICAL,
})
_VALID_SCOPES: frozenset = frozenset({
    Scope.DECK_GLOBAL, Scope.PER_CONSTRUCT,
})


@dataclass
class TripwireAuthoringResult:
    """Outcome of one authoring pass.

    `registered` are tripwires that landed on the engine. `rejected`
    are entries that came back from the model but failed validation
    or regex compile — each pair is (name_or_index, reason) for the
    netrunner-facing chatlog summary. `error` is set when the whole
    pass failed (subprocess error, timeout, unparseable response);
    `success` is False in that case and `registered`/`rejected` are
    empty.

    `used_resume` records whether the rung-1 fork-via-resume path was
    used or rung-2 fresh-one-shot. Useful for the chatlog announcement
    so the netrunner can spot when fork is failing and we're falling
    back. (Slice 2 doesn't auto-fall-back; this is just the honest
    label.)
    """
    success: bool
    registered: list  # list[Tripwire]
    rejected: list    # list[tuple[str, str]]
    used_resume: bool
    error: Optional[str] = None
    elapsed_s: float = 0.0
    raw_response: str = ""  # stashed for debugging when parse fails


def build_authoring_user_prompt(
    *,
    goal: str,
    classification: Optional[str],
    old_goal: Optional[str],
    brake_label: str,
    defaults_summary: list[Tripwire],
    blacklist_summary: list[str],
) -> str:
    """Compose the user-message body for an authoring pass.

    The system prompt + this body get sent as one user message. We
    don't use --append-system-prompt because (1) rung-1 forks resume
    the Q&A session whose system prompt is already set, (2)
    multi-line argv mangling on Windows is a real problem.

    `defaults_summary` lets the model see the baseline tripwires so
    it doesn't duplicate them. `blacklist_summary` is pre-formatted
    by the caller (typically one line per BlacklistEntry from the
    watchdog) since this module shouldn't import from watchdog.
    """
    lines: list[str] = []

    # Goal block — the centerpiece. Mid-flight updates surface the old
    # goal + classification so the model can reason about what's
    # changing rather than authoring from scratch.
    if classification and old_goal:
        lines.append("GOAL UPDATE:")
        lines.append(f"  classification: {classification}")
        lines.append(f"  old goal: {old_goal}")
        lines.append(f"  new goal: {goal}")
    else:
        lines.append("CURRENT GOAL:")
        lines.append(f"  {goal}")
    lines.append("")

    # Brake state — informs what's already gated. Don't author rules
    # the brake hook covers.
    lines.append(f"DECK BRAKE: {brake_label}")
    if brake_label == "paranoid":
        lines.append(
            "  Constructs CANNOT Write, Edit, run Bash, or use WebFetch. "
            "Investigation-only mode. Don't author rules predicated on "
            "those tools running — they won't."
        )
    elif brake_label == "default":
        lines.append(
            "  Destructive bash patterns and OS-root writes are already "
            "blocked at the hook layer. Don't author tripwires for "
            "rm -rf / format / writes to /etc / Program Files / etc."
        )
    elif brake_label == "yolo":
        lines.append(
            "  No brake hook installed. Constructs run unrestricted. "
            "The netrunner has explicitly accepted this; tripwires are "
            "the only hint they'll get if something drifts."
        )
    lines.append("")

    # Default tripwires — short summary so the model doesn't duplicate.
    if defaults_summary:
        lines.append("ALREADY ACTIVE (deck-default tripwires; do not duplicate):")
        for tw in defaults_summary:
            lines.append(f"  - {tw.name}: {tw.description}")
        lines.append("")

    # Blacklist context — rich because the netrunner already explicitly
    # rejected these patterns. Useful for authoring sharper rules than
    # the first-80-chars fingerprint matcher catches.
    if blacklist_summary:
        lines.append("SESSION BLACKLIST (netrunner has hard-killed these task shapes):")
        for line in blacklist_summary:
            lines.append(f"  - {line}")
        lines.append(
            "  Consider authoring tripwires that catch these failure "
            "shapes earlier — by event content as constructs stream "
            "rather than by post-facto fingerprint match."
        )
        lines.append("")

    lines.append("Author tripwires for this goal now. Output JSON only.")
    return "\n".join(lines)


def parse_authoring_response(
    raw: str,
    *,
    default_origin: str = Origin.LLM_AUTHORED,
) -> tuple[list[Tripwire], list[tuple[str, str]]]:
    """Parse a strict-JSON authoring response into Tripwire objects.

    Returns (parsed, rejected) — parsed Tripwires that look valid
    (ready for engine.register, which still validates regex), and
    rejected entries with (name_or_index_str, reason) for chatlog
    reporting.

    Tolerant of light wrapping: the model is instructed to output bare
    JSON, but real-world responses sometimes wrap in markdown fences
    or add a preamble. Try strict parse first, then fence extract,
    then balanced-brace extract. After that, give up — return ([],
    [("(response)", "could not locate JSON in response")]) so the
    caller can render a chatlog line and stash the raw output for
    debugging.
    """
    import json as _json

    # ---- locate JSON in the response ----
    candidate = raw.strip()
    parsed_obj = None

    # Pass 1: strict — model followed instructions.
    try:
        parsed_obj = _json.loads(candidate)
    except (_json.JSONDecodeError, ValueError):
        parsed_obj = None

    # Pass 2: fenced ```json ... ``` block (claude often does this
    # despite explicit "no fences" instructions).
    if parsed_obj is None:
        fence_match = re.search(
            r"```(?:json)?\s*\n?(.*?)```", candidate, re.DOTALL,
        )
        if fence_match:
            try:
                parsed_obj = _json.loads(fence_match.group(1).strip())
            except (_json.JSONDecodeError, ValueError):
                parsed_obj = None

    # Pass 3: first balanced {...} block. Crude — assumes the JSON
    # doesn't contain unescaped braces in strings, which is fine for
    # our schema (regex patterns can contain {} but we can dodge by
    # scanning for the OUTER {"tripwires":...} envelope).
    if parsed_obj is None:
        first_brace = candidate.find("{")
        last_brace = candidate.rfind("}")
        if first_brace >= 0 and last_brace > first_brace:
            try:
                parsed_obj = _json.loads(
                    candidate[first_brace:last_brace + 1]
                )
            except (_json.JSONDecodeError, ValueError):
                parsed_obj = None

    if parsed_obj is None:
        return [], [("(response)", "could not locate JSON in response")]

    # ---- validate envelope ----
    if not isinstance(parsed_obj, dict):
        return [], [("(response)", "JSON root is not an object")]
    entries = parsed_obj.get("tripwires")
    if not isinstance(entries, list):
        return [], [("(response)", "missing or non-list 'tripwires' field")]

    # ---- validate each entry ----
    parsed: list[Tripwire] = []
    rejected: list[tuple[str, str]] = []
    seen_names: set = set()

    for idx, entry in enumerate(entries):
        label = (
            entry.get("name", f"#{idx}")
            if isinstance(entry, dict) else f"#{idx}"
        )

        if not isinstance(entry, dict):
            rejected.append((label, "entry is not an object"))
            continue

        # Required fields
        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            rejected.append((label, "missing or empty 'name'"))
            continue
        name = name.strip()

        if name in seen_names:
            rejected.append((name, "duplicate name within this pass"))
            continue

        pattern = entry.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            rejected.append((name, "missing or empty 'pattern'"))
            continue

        field_val = entry.get("field")
        if field_val not in _VALID_FIELDS:
            rejected.append(
                (name, f"unknown field {field_val!r}; allowed: {sorted(_VALID_FIELDS)}")
            )
            continue

        severity = entry.get("severity", Severity.WARNING)
        if severity not in _VALID_SEVERITIES:
            rejected.append(
                (name, f"unknown severity {severity!r}; allowed: low/warning/critical")
            )
            continue

        scope = entry.get("scope", Scope.DECK_GLOBAL)
        if scope not in _VALID_SCOPES:
            rejected.append(
                (name, f"unknown scope {scope!r}; allowed: deck_global/per_construct")
            )
            continue

        event_kinds_raw = entry.get("event_kinds", [])
        if not isinstance(event_kinds_raw, list):
            rejected.append((name, "'event_kinds' must be a list"))
            continue
        # Coerce to tuple of strings; drop non-strings silently (the
        # engine treats unknown event_kind values as matches-nothing,
        # which is harmless).
        event_kinds = tuple(
            ek for ek in event_kinds_raw if isinstance(ek, str) and ek
        )

        description = entry.get("description", "")
        if not isinstance(description, str):
            description = ""

        # Slice 2 escalation chain fields. `suggestion` is meaningful
        # for warnings (the construct sees it in the deny stderr and
        # uses it to pivot); we accept it for any severity but it'll
        # only show up in the deny formatter when severity=warning.
        # `bad_enough` is meaningful for criticals (auto-blacklist
        # marker); same forgiving validation — accept the field at
        # any severity but it only takes effect when severity=critical.
        suggestion = entry.get("suggestion", "")
        if not isinstance(suggestion, str):
            suggestion = ""

        bad_enough = entry.get("bad_enough", False)
        if not isinstance(bad_enough, bool):
            bad_enough = False

        # Construction can still fail validation at engine.register
        # time (regex compile). We don't compile here — leave that to
        # the engine so there's one source of truth for "is this
        # tripwire usable." The caller knows to read register's bool
        # return.
        parsed.append(Tripwire(
            name=name,
            description=description,
            pattern_type="regex",
            pattern=pattern,
            event_kinds=event_kinds,
            field=field_val,
            severity=severity,
            scope=scope,
            origin=default_origin,
            suggestion=suggestion,
            bad_enough=bad_enough,
        ))
        seen_names.add(name)

    return parsed, rejected
