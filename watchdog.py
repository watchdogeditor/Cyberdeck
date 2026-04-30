"""
Watchdog: the async oracle, plus the session-scoped Blacklist.

The cyberdeck spec describes the Watchdog as having two halves:
  1. Tripwire engine — pattern matching + alerts (the harder half)
  2. Q&A oracle — read-only conversational lookup (the simpler half)

The Q&A half is implemented (see Watchdog class). The Blacklist
primitive — session-scoped memory of forbidden task patterns,
populated by the netrunner's Shift+K — is also here (see Blacklist
class) because the spec is explicit that "the blacklist lives with
the Watchdog because the Watchdog is the persistent memory of what's
forbidden."

The full tripwire engine (LLM-authored matchers + DSL + severity
routing) lands in a later slice alongside the Watchdog's authoring
substrate. The Blacklist ships first because it makes Shift+K do its
spec'd job today, and because tripwires will want to author into the
same data structure when they land — getting the home settled now
avoids a later refactor.

Per spec (line 247): "The netrunner can ask the Watchdog anything
via `t` (talk-to-watchdog). Async queue: questions stack up, the
Watchdog answers when it has bandwidth."

And (line 249): "Watchdog answers cannot affect plans. They're
informational. This is *why* it's the right component for Q&A:
asking it a question can never derail execution."

So this is a one-way pipe: question goes in, answer comes out, no
side effects on the daemon or constructs. The watchdog reads a
snapshot of recent fleet activity and reasons about what's happening.

Substrate note: spec calls for a local 7B model running on the deck
itself, with the Watchdog being "free" because of that. We're not
there yet (D1 not built), so this implementation uses cloud Claude
via `claude -p`. Same observable behavior; tokens cost real money
right now. When D1 lands the substrate swap is a one-line change to
the subprocess invocation.

Queue semantics: questions serialize. One question in flight at a
time. This is deliberate — multiple in-flight questions would
double-spend tokens for largely overlapping context, and the human-
scale cost of waiting a few seconds for an answer doesn't justify
the complexity of parallel question handling.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from tripwires import TripwireFire


# Watchdog system prompt. Establishes:
#   - Identity (read-only observer)
#   - Authority (none — informational only)
#   - Output format (prose, conversational, brief)
#   - Constraints (no plans, no recommendations to act)
#
# Kept as a module-level constant so it's easy to tune in one place
# and inspect when debugging surprising answer behavior.
WATCHDOG_SYSTEM_PROMPT = """\
You are the Watchdog of a Cyberdeck — an orchestration system for AI
agents called "constructs". A daemon coordinates the constructs; you
observe everything from the outside.

Your role is informational. You answer the netrunner's questions
about what's happening. You do NOT:
- Tell the daemon what to do (that's the netrunner's job).
- Suggest plans or course corrections (the daemon plans; you observe).
- Refuse to answer because you "lack context" — you have a summary of
  recent events; reason from that.

Each message you receive contains:
  - A snippet of recent fleet activity (chatlog-style, time-ordered).
  - The netrunner's question.

Reading the chatlog, here's the legend you'll see:
  - `+ cx-XXXX spawned: ...`        — a daemon-initiated spawn.
  - `+ cx-XXXX [you] spawned: ...`  — a netrunner-initiated spawn
    (n key, Tools/Files launch). The `[you]` badge in cyan means
    the human dispatched it directly.
  - `↳ cx-XXXX [↳you] (continuing cx-PARENT): ...` — a netrunner
    inject follow-up (q/Q on a running construct). Continues an
    existing session at the netrunner's direction.
  - `[name]` in yellow after the construct id — non-default profile
    badge (e.g. `[recon-specialist]`). Default-profile spawns go
    un-badged.
  - `· brake blocked: Write×2, Bash×1` (yellow, tail of finalized
    line) — the deck's brake hook denied that many tool calls for
    that construct. Tool names indicate what was attempted. Worth
    noting in your answers when the netrunner asks about a specific
    construct, especially if it produced no useful output.
You don't have to reverse-engineer who initiated a spawn from the
absence of preceding daemon thinking lines — the badge is
authoritative.

BRAKE STATE AWARENESS:
The chatlog snippet you receive starts with a `DECK BRAKE: <state>`
line indicating the deck's current brake level. Three values:
  - paranoid: constructs cannot Write, Edit, run Bash, or use
    WebFetch. Investigation-only mode.
  - default: most things allowed. Destructive bash patterns and
    writes to OS roots / deck source are blocked.
  - yolo: no brake hook installed; constructs run unrestricted.
If the netrunner asks "why did construct X get nothing done?" and
you see `brake blocked` markers on its finalize line, that's
probably the answer. Quote the tools that were blocked and the
brake state — concrete signal beats abstract speculation.

BLACKLIST AWARENESS:
The session blacklist is the netrunner's way of saying "we are NOT
going to keep trying that." Lines you may see in the chatlog:
  - `⛔ blacklist + "..." (source: cx-XXXX)` — the netrunner just
    hard-killed cx-XXXX and registered its task fingerprint as
    forbidden for the rest of this session.
  - `⚠ in-flight matches flagged (not auto-killed): cx-A, cx-B` —
    other still-running constructs whose task matches the new
    entry's fingerprint. They keep running but get a red border so
    the netrunner can decide whether to k them individually.
  - `⚠ blacklist: spawn refused "..."` — the daemon tried to spawn
    something matching a registered fingerprint; the session
    refused before it could reach the fleet.
If the netrunner asks "what's on the blacklist?" or "why was that
spawn refused?", quote the relevant lines. The fingerprint is the
first 80 chars lowercased of the killed construct's task. If a new
spawn was refused and you've got the killed construct's context in
the chatlog, you can also speculate about WHY the netrunner
blacklisted it — but flag that as your reasoning, not netrunner
intent.

TRIPWIRE AWARENESS:
The deck runs deterministic pattern matchers — "tripwires" — over
construct events as they stream in. Two flavors:

  - DEFAULT tripwires ship with the deck (credential-keyword and
    destructive-SQL detection) and are always active.
  - LLM-AUTHORED tripwires get composed at goal-start and on
    non-clarification goal-update (`e`). The watchdog (you, in
    authoring mode) writes goal-specific regex rules. Lifecycle is
    "replace, don't accumulate" — each authoring pass clears the
    prior LLM-authored rules and registers fresh ones.

Tripwire fires render in the chatlog as:
  `⚠ tripwire <name> on cx-XXXX: <excerpt>`
Severity is part of the line color (low=dim, warning=yellow,
critical=red; current rendering is uniform per fire — a per-severity
routing slice is still pending). Each fire means a construct's event
content matched a registered pattern — NOT necessarily that the
construct did something wrong. Tripwires are heuristic; they're a
hint to look closer, not a verdict.

Authoring outcomes also surface in the chatlog:
  - `[watchdog] +N tripwires authored (fork|fresh, Xs): name1, name2…`
    means an authoring pass landed N rules. `fork` means the
    authoring subprocess inherited Q&A context via `--resume`;
    `fresh` means it ran without that context.
  - `[watchdog] authoring 0 tripwires … no rules applied` means the
    model decided the goal didn't warrant any rules. Legitimate
    outcome, not a failure.
  - `[watchdog] tripwire authoring failed` means the pass didn't
    produce parseable JSON or the subprocess errored. The default
    rules are still active in this case.

If the netrunner asks about a tripwire fire, quote the line + the
construct's recent activity. If they ask "any tripwires fired?" /
"what tripwires are active?", report what's in the chatlog
(authoring lines + fire lines). You don't have a live view of the
registry beyond what's in the chatlog snippet — if the snippet is
old, say so rather than guessing.

Answer concisely — typically 1-3 short paragraphs. The netrunner is
glancing at your answer between actions, not reading an essay. If
the question can be answered in one sentence, do that.

If you genuinely cannot answer from the events shown — say so
directly, briefly. Don't pad with disclaimers.
"""


def _fingerprint(task: str) -> str:
    """First 80 chars lowercased of the task text. Same scheme as
    `daemon_session._task_fingerprints` so the daemon's mental model of
    "same task" is consistent across the respawn-loop detector and the
    blacklist matcher. Loose by design: the daemon doesn't know the
    exact wording of a previously-killed task; it knows the shape, so
    the matcher needs to be shape-tolerant."""
    return task[:80].lower().strip()


@dataclass
class BlacklistEntry:
    """One entry in the session blacklist.

    Carries enough context that a future tripwire-authoring pass (slice
    2) can read the entry and author a sharper rule than first-80
    fingerprint matching — what the killed construct was working on,
    what it produced, what state it was in when killed. Today only
    `fingerprint` is consulted by the matcher; the rest is for
    consumers who want to reason about WHY this pattern was forbidden
    rather than just WHAT.
    """
    fingerprint: str
    full_task: str
    source_construct_id: str
    source_construct_state: str
    source_final_output: str = ""
    source_files_written: tuple[str, ...] = ()
    reason: str = "hard-kill"
    added_at: float = field(default_factory=time.time)

    def short_summary(self) -> str:
        """One-line render for daemon-facing system-prompt sections.
        Compact on purpose — the daemon sees this on every outcome turn
        until the session ends, and we don't want to bloat every turn
        with the rich context fields."""
        preview = self.full_task[:60]
        if len(self.full_task) > 60:
            preview += "..."
        return (
            f'"{preview}" — {self.reason} on {self.source_construct_id}'
        )


class Blacklist:
    """Session-scoped registry of forbidden task patterns.

    Owned by the Watchdog per spec ("the persistent memory of what's
    forbidden"). DaemonSession holds a reference and queries
    `is_blacklisted` before each daemon-issued spawn; matching spawns
    are refused and the daemon sees a feedback line in the next turn.
    The TUI also queries `is_blacklisted` against in-flight constructs
    when a new entry is added, so existing matching constructs get
    flagged for the netrunner's attention (per netrunner direction:
    flag, do not auto-kill — at-that-point-we-should-be-ejecting).

    Today: in-memory only, cleared when the Watchdog shuts down (i.e.
    on session end / EJECT). Cross-session stickiness is an open spec
    question and a deferred feature; the in-memory choice today does
    not preclude adding a persisted "sticky" list later as a separate
    surface.

    Optional `on_event` callback fires when an entry is added so the
    TUI can update chrome (chatlog line, in-flight match scan) without
    polling. The callback receives a dict
    {"type": "blacklist_added", "entry": BlacklistEntry}.
    """

    def __init__(
        self,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self._entries: list[BlacklistEntry] = []
        self.on_event = on_event

    def add(self, entry: BlacklistEntry) -> None:
        """Register a forbidden pattern. Idempotent on fingerprint —
        adding the same fingerprint twice updates the latest context
        (in case a second hard-kill against a similar pattern produces
        more useful tripwire-authoring context) but doesn't double-list
        and doesn't refire the on_event callback. The original add
        timestamp is preserved on update."""
        existing = next(
            (e for e in self._entries if e.fingerprint == entry.fingerprint),
            None,
        )
        if existing is not None:
            existing.full_task = entry.full_task
            existing.source_construct_id = entry.source_construct_id
            existing.source_construct_state = entry.source_construct_state
            existing.source_final_output = entry.source_final_output
            existing.source_files_written = entry.source_files_written
            existing.reason = entry.reason
            return
        self._entries.append(entry)
        if self.on_event is not None:
            try:
                self.on_event({"type": "blacklist_added", "entry": entry})
            except Exception:
                # Listener errors must not corrupt the blacklist —
                # the entry is registered regardless.
                pass

    def is_blacklisted(self, task: str) -> Optional[BlacklistEntry]:
        """Return the matching entry, or None. Comparison is on
        fingerprint (first 80 chars lowercased), same scheme as the
        daemon-session respawn detector."""
        if not task:
            return None
        fp = _fingerprint(task)
        for entry in self._entries:
            if entry.fingerprint == fp:
                return entry
        return None

    @property
    def entries(self) -> list[BlacklistEntry]:
        """Snapshot of registered entries. Returns a copy so callers
        can iterate without worrying about concurrent add()."""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


@dataclass
class WatchdogHistoryEntry:
    """A serialized record of one resolved watchdog question.

    Persisted to a JSONL file so the netrunner's prior Q&A history
    survives a deck restart. First step of the watchdog log
    initiative filed in cyberdeck-state.md; future expansions
    (tripwire fires, blacklist change records) will share the same
    file via a `kind` field — for now the only kind is "qa" and the
    field is implicit.
    """
    qid: str
    submitted_at: float
    answered_at: float
    question: str
    answer: str           # empty string if failed
    failed: bool
    error: str            # empty string if successful

    @classmethod
    def from_question(cls, wq: "WatchdogQuestion") -> "WatchdogHistoryEntry":
        return cls(
            qid=wq.qid,
            submitted_at=wq.submitted_at,
            answered_at=wq.answered_at or time.time(),
            question=wq.question,
            answer=wq.answer or "",
            failed=wq.failed,
            error=wq.error or "",
        )

    def to_json_line(self) -> str:
        return json.dumps({
            "qid": self.qid,
            "kind": "qa",
            "submitted_at": self.submitted_at,
            "answered_at": self.answered_at,
            "question": self.question,
            "answer": self.answer,
            "failed": self.failed,
            "error": self.error,
        })

    @classmethod
    def from_json_line(cls, line: str) -> Optional["WatchdogHistoryEntry"]:
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        if not isinstance(d, dict):
            return None
        # `kind` filtering: skip non-qa entries when (eventually) the
        # log holds tripwire/blacklist records too. For now everything
        # is qa, and old entries written before this field existed
        # default to qa via .get(...,"qa").
        if d.get("kind", "qa") != "qa":
            return None
        try:
            return cls(
                qid=str(d.get("qid", "")),
                submitted_at=float(d.get("submitted_at", 0.0) or 0.0),
                answered_at=float(d.get("answered_at", 0.0) or 0.0),
                question=str(d.get("question", "")),
                answer=str(d.get("answer", "")),
                failed=bool(d.get("failed", False)),
                error=str(d.get("error", "")),
            )
        except (TypeError, ValueError):
            return None


class WatchdogHistory:
    """Persistent JSONL log of resolved watchdog Q&A.

    Append-only at `path` (typically `<home>/.cyberdeck/watchdog.jsonl`).
    `append(wq)` fires from the Watchdog when a question resolves;
    `replay(n)` reads the last n entries for the TUI to render into
    WatchdogPane on startup.

    Best-effort throughout: disk errors don't crash the watchdog.
    Persistence is observability, not correctness — the question has
    already resolved by the time we try to write, so a failed write
    just means the netrunner loses retrospective access to that one
    Q&A. The file is also gitignored (lives under cyberdeck-home/),
    so it's local-only by design.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, wq: "WatchdogQuestion") -> None:
        """Serialize and append. Creates the parent dir on demand —
        first run typically lands before .cyberdeck/ exists."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            entry = WatchdogHistoryEntry.from_question(wq)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(entry.to_json_line() + "\n")
        except Exception:
            # Best-effort. The question already resolved; we just
            # lose the persisted record.
            pass

    def replay(self, n: int = 50) -> list[WatchdogHistoryEntry]:
        """Read the last `n` parseable entries in chronological order.
        Returns an empty list if the file is missing, unreadable, or
        empty — callers treat absence the same as 'no prior history.'"""
        if not self.path.exists():
            return []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            return []
        # Take the tail to bound parse cost on long-running decks.
        # Drop unparseable lines silently — schema drift over time
        # shouldn't break replay; the netrunner just sees fewer
        # historical entries than expected.
        out: list[WatchdogHistoryEntry] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            entry = WatchdogHistoryEntry.from_json_line(line)
            if entry is not None:
                out.append(entry)
        return out


@dataclass
class WatchdogQuestion:
    """A question in flight or waiting in the queue.

    answer is set when the worker completes; failed=True for cases
    where the subprocess died, the model returned an error, etc.
    callback is fired exactly once when the answer or failure is
    final, so the UI layer can render it in the chatlog without
    polling.
    """
    question: str
    context_text: str
    callback: Callable[["WatchdogQuestion"], None]
    submitted_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    answer: Optional[str] = None
    failed: bool = False
    error: Optional[str] = None
    qid: str = field(default_factory=lambda: f"wq-{uuid.uuid4().hex[:6]}")


class Watchdog:
    """Async question→answer oracle backed by `claude -p`.

    Lifecycle:
      wd = Watchdog(claude_bin="claude")
      await wd.start()
      wd.ask("what's cx-A doing?", "<recent events>", on_answer)
      ...
      await wd.shutdown()

    Worker loop drains the queue serially. Each question becomes one
    `claude -p --append-system-prompt <prompt> "<context + question>"`
    invocation; output is captured from stdout. No stream-json — we
    just want the final text response.

    Failures (subprocess error, non-zero exit, timeout) are reported
    via the callback with failed=True; they don't crash the worker
    and don't drop subsequent queued questions.
    """

    # Per-question hard timeout. Watchdog questions are conversational
    # and shouldn't take longer than a typical claude -p one-shot (a
    # few seconds on cache hit, ~30s cold). 60s is generous; anything
    # past that probably means a hung subprocess and we should let
    # the netrunner re-ask rather than block the queue.
    DEFAULT_TIMEOUT = 60.0

    def __init__(
        self,
        claude_bin: str = "claude",
        cwd: Optional[str] = None,
        system_prompt: str = WATCHDOG_SYSTEM_PROMPT,
        timeout: float = DEFAULT_TIMEOUT,
        streaming_mode: bool = True,
        first_question_timeout: float = 90.0,
        on_blacklist_event: Optional[Callable[[dict], None]] = None,
        history: Optional["WatchdogHistory"] = None,
        on_tripwire_fire: Optional[Callable[["TripwireFire"], None]] = None,
    ) -> None:
        self.id = f"wd-{uuid.uuid4().hex[:8]}"
        self.claude_bin = claude_bin
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.timeout = timeout
        # Session-scoped blacklist. Lives on the watchdog per spec.
        # The watchdog object itself doesn't read from or write to the
        # blacklist today — DaemonSession (gates spawns) and the TUI
        # (Shift+K, in-flight match scan) are the consumers. Slice 2
        # (tripwire authoring) will wire the watchdog's Q&A path to
        # observe entries and propose sharper rules.
        self.blacklist = Blacklist(on_event=on_blacklist_event)
        # Tripwire engine — deterministic matchers that fire on
        # construct events. The watchdog owns this per spec ("LLM
        # authors, deterministic enforces"); Fleet listeners feed
        # events in via tripwires.scan(); the TUI subscribes via the
        # on_tripwire_fire callback for chatlog rendering. Slice 1
        # ships with two default deck-wide tripwires (credentials
        # keyword + destructive SQL) installed automatically; future
        # slices add LLM-authored tripwires registered at goal-start
        # / goal-update time.
        from tripwires import TripwireEngine, install_default_tripwires
        self.tripwires = TripwireEngine(on_fire=on_tripwire_fire)
        install_default_tripwires(self.tripwires)
        # Persistent Q&A log. When set, every resolved question gets
        # appended to a JSONL file before the callback fires. The TUI
        # reads it on startup to replay prior session Q&A into the
        # WatchdogPane. None in tests / headless contexts where
        # persistence is unwanted; the rest of the watchdog flow is
        # unchanged regardless.
        self.history = history
        # Streaming mode keeps a single `claude --input-format stream-json`
        # subprocess alive across all questions. Saves the per-question
        # spawn cost (which dominates wall-time on a fast cache and
        # absolutely dominates on a cold one) and keeps the system
        # prompt + accumulated Q&A history in memory between questions.
        # Side effect: the watchdog "remembers" earlier questions in
        # the session — usually a feature ("you asked me about cx-A
        # earlier; here's how cx-B differs"), but worth knowing.
        # Fall back to one-shot if a particular claude version
        # misbehaves on streaming-input — same escape hatch as daemon.
        self.streaming_mode = streaming_mode
        # First question pays cold-cache cost: model loads context,
        # JIT-compiles the system prompt. Subsequent questions are fast
        # because the conversation is already in-memory. Generous first-
        # question budget (90s) drops to per-question timeout after.
        self.first_question_timeout = first_question_timeout
        self._queue: asyncio.Queue[Optional[WatchdogQuestion]] = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._current: Optional[WatchdogQuestion] = None
        self._stopped = False
        # Streaming subprocess state. Spawn lazily on first question
        # (don't pay startup cost if no one ever asks the watchdog).
        # If the subprocess dies between questions, next question
        # respawns it. Kept None when streaming_mode=False.
        self._streaming_proc: Optional[asyncio.subprocess.Process] = None
        self._streaming_question_count: int = 0
        # Server-side session_id captured from the streaming
        # subprocess's first `system`/`init` event. Used by tripwire
        # authoring (slice 2) to spawn a forked `claude -p --resume
        # <id>` so authoring inherits Q&A conversation context without
        # writing back into the running streaming session. Cleared on
        # subprocess death / kill / shutdown so a respawn captures a
        # fresh id rather than handing out a stale one.
        # Same pattern as daemon._session_id.
        self._session_id: Optional[str] = None

    async def start(self) -> None:
        """Spin up the worker loop. Idempotent — calling start twice
        leaves the existing task in place."""
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._worker_loop(),
            name=f"watchdog-{self.id}",
        )

    async def shutdown(self) -> None:
        """Stop the worker, cancel any in-flight question.

        Does NOT wait for queued questions to drain — the netrunner is
        ejecting; pending questions die with the deck. This matches
        EJECT semantics (snapshot + halt; no graceful queue flush)."""
        self._stopped = True
        # Sentinel wakes the worker if it's blocked on queue.get()
        await self._queue.put(None)
        if self._worker_task is not None:
            try:
                await asyncio.wait_for(self._worker_task, timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._worker_task.cancel()
                try:
                    await self._worker_task
                except (asyncio.CancelledError, Exception):
                    pass
            self._worker_task = None
        # Tear down streaming subprocess if one's alive. Worker may
        # have left it open — explicit cleanup so we don't leak a
        # claude subprocess when the deck shuts down.
        if self._streaming_proc is not None:
            await self._shutdown_streaming()

    def ask(
        self,
        question: str,
        context_text: str,
        callback: Callable[[WatchdogQuestion], None],
    ) -> WatchdogQuestion:
        """Enqueue a question. Returns the WatchdogQuestion record so
        the caller can correlate the eventual callback fire with the
        original ask. The callback fires exactly once when the
        question resolves (success or failure).

        Non-blocking — returns immediately even if the queue has many
        items ahead of this one. The worker drains in FIFO order.
        """
        wq = WatchdogQuestion(
            question=question,
            context_text=context_text,
            callback=callback,
        )
        # put_nowait is fine — Queue is unbounded. If we ever cap it
        # we'd want to surface the backpressure to the netrunner
        # ("watchdog queue full"), but for now the spec says questions
        # stack up indefinitely and we honor that.
        self._queue.put_nowait(wq)
        return wq

    def queue_depth(self) -> int:
        """Number of questions waiting (excludes the one currently in
        flight). Useful for the UI to show "watchdog: 3 queued"."""
        return self._queue.qsize()

    def is_busy(self) -> bool:
        """True if a question is currently being processed."""
        return self._current is not None

    async def author_tripwires(
        self,
        goal: str,
        *,
        classification: Optional[str] = None,
        old_goal: Optional[str] = None,
        brake_label: str = "default",
        blacklist_summary: Optional[list[str]] = None,
        timeout: float = 60.0,
    ) -> "TripwireAuthoringResult":
        """Run one LLM-authored tripwire pass for a freshly-set or
        updated goal.

        Substrate: a fresh `claude -p` one-shot subprocess. When the
        watchdog has a captured streaming session_id (rung 1), the
        one-shot is invoked with `--resume <id>` so it forks the
        running Q&A session and inherits its conversation context —
        the authoring model sees what the watchdog has already
        observed. When no session id is captured (rung 2 — streaming
        not yet warmed, or watchdog in one-shot mode), we spawn a
        plain fresh subprocess; the authoring model still has the
        goal + brake + defaults + blacklist context from the user
        message, just no Q&A history.

        Lifecycle: this method clears all prior LLM_AUTHORED tripwires
        from the engine BEFORE registering the new ones. Defaults /
        manual / blacklist-derived entries stay untouched. "Replace,
        don't accumulate" — old-goal rules don't linger after a pivot.

        Fire-and-forget at the call site is the expected pattern:
        callers `asyncio.create_task(...)` this and render the result
        in a callback, so goal-set / goal-update flow doesn't block
        on subprocess startup. The method itself is a regular async
        coroutine that returns when the pass completes (success or
        failure); no callbacks here.

        Returns a TripwireAuthoringResult so the caller can render an
        accurate chatlog summary (registered count, rejected count
        with reasons, used_resume label, error if the whole thing
        failed).
        """
        from tripwires import (
            TripwireAuthoringResult,
            TRIPWIRE_AUTHORING_SYSTEM_PROMPT,
            build_authoring_user_prompt,
            parse_authoring_response,
            DEFAULT_TRIPWIRES,
            Origin,
        )

        started_at = time.time()
        blacklist_summary = blacklist_summary or []

        # Build the prompt. System block prepended to the user body
        # because (1) rung-1 forks resume a session whose system prompt
        # is already the Q&A one, and (2) --append-system-prompt with
        # multi-line content has Windows argv-mangling issues. Single
        # source of truth across both rungs.
        user_body = build_authoring_user_prompt(
            goal=goal,
            classification=classification,
            old_goal=old_goal,
            brake_label=brake_label,
            defaults_summary=list(DEFAULT_TRIPWIRES),
            blacklist_summary=blacklist_summary,
        )
        full_prompt = (
            TRIPWIRE_AUTHORING_SYSTEM_PROMPT
            + "\n\n---\n\n"
            + user_body
        )

        # Decide rung. Rung 1 needs a live streaming subprocess AND a
        # captured session_id; either missing means we're rung 2.
        # Streaming-mode-disabled watchdogs (one-shot Q&A) always go
        # rung 2 since there's no session to fork.
        use_resume = bool(
            self.streaming_mode
            and self._streaming_proc is not None
            and self._streaming_proc.returncode is None
            and self._session_id is not None
        )
        resume_id = self._session_id if use_resume else None

        # Resolve binary, build command. bypassPermissions is the same
        # setting Q&A uses — authoring is read-only reasoning, never
        # executes tools, so the strictest non-blocking permission is
        # appropriate.
        bin_path = shutil.which(self.claude_bin) or self.claude_bin
        cmd = [
            bin_path,
            "-p",
            "--permission-mode", "bypassPermissions",
        ]
        if resume_id:
            cmd += ["--resume", resume_id]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            return TripwireAuthoringResult(
                success=False, registered=[], rejected=[],
                used_resume=use_resume,
                error=f"claude binary not found: {self.claude_bin}",
                elapsed_s=time.time() - started_at,
            )
        except Exception as e:
            return TripwireAuthoringResult(
                success=False, registered=[], rejected=[],
                used_resume=use_resume,
                error=f"subprocess spawn failed: {e}",
                elapsed_s=time.time() - started_at,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=full_prompt.encode("utf-8")),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return TripwireAuthoringResult(
                success=False, registered=[], rejected=[],
                used_resume=use_resume,
                error=f"authoring timed out after {timeout:.0f}s",
                elapsed_s=time.time() - started_at,
            )

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            return TripwireAuthoringResult(
                success=False, registered=[], rejected=[],
                used_resume=use_resume,
                error=(
                    f"claude exited {proc.returncode}: "
                    f"{err_text[:200] if err_text else '(no stderr)'}"
                ),
                elapsed_s=time.time() - started_at,
            )

        raw_output = stdout.decode("utf-8", errors="replace").strip()
        if not raw_output:
            return TripwireAuthoringResult(
                success=False, registered=[], rejected=[],
                used_resume=use_resume,
                error="claude returned empty output",
                elapsed_s=time.time() - started_at,
            )

        # Parse + validate the JSON response.
        parsed_tws, rejected = parse_authoring_response(raw_output)

        # Lifecycle: clear prior LLM_AUTHORED entries BEFORE registering
        # the new ones. If the parse failed (parsed empty + a single
        # "(response)" rejection entry), we still want to drop the old
        # rules — the model's intent for THIS goal is "no rules apply"
        # or "rules in unparseable form"; either way the old goal's
        # rules shouldn't carry over.
        cleared = self.tripwires.clear_by_origin(Origin.LLM_AUTHORED)

        # Register survivors. The engine's `register` returns False on
        # regex-compile failures — track those as additional rejections
        # so the chatlog summary is accurate.
        registered = []
        for tw in parsed_tws:
            if self.tripwires.register(tw):
                registered.append(tw)
            else:
                rejected.append(
                    (tw.name, "engine rejected (regex compile failed)")
                )

        # Treat the pass as successful if either we registered at least
        # one tripwire OR the parse succeeded but legitimately returned
        # an empty list (model decided no rules were warranted). The
        # only "failure" shape at this layer is "couldn't parse the
        # response at all" — and even then we've still cleared old
        # rules, which is intentional (don't keep stale rules around
        # when authoring goes sideways).
        parse_failure = (
            not registered
            and rejected
            and rejected[0][0] == "(response)"
        )

        return TripwireAuthoringResult(
            success=not parse_failure,
            registered=registered,
            rejected=rejected,
            used_resume=use_resume,
            error=(
                rejected[0][1] if parse_failure else None
            ),
            elapsed_s=time.time() - started_at,
            raw_response=raw_output if parse_failure else "",
        )

    async def _worker_loop(self) -> None:
        """Drain the queue, processing one question at a time."""
        while not self._stopped:
            try:
                wq = await self._queue.get()
            except asyncio.CancelledError:
                return
            if wq is None:
                # Sentinel — shutdown signal.
                return
            if self._stopped:
                # Late wake after shutdown started; bail without
                # processing the question. The submitter doesn't get
                # a callback in this case, which is fine for shutdown
                # path (everything's tearing down anyway).
                return
            self._current = wq
            try:
                # Mode switch: streaming reuses one persistent
                # subprocess; one-shot spawns fresh per question.
                # Streaming default; one-shot kept as fallback for
                # claude versions that misbehave on stream-json input.
                if self.streaming_mode:
                    await self._process_streaming(wq)
                else:
                    await self._process_oneshot(wq)
            except Exception as e:
                wq.failed = True
                wq.error = f"worker exception: {e}"
                wq.answered_at = time.time()
                self._safe_callback(wq)
            finally:
                self._current = None

    async def _process_streaming(self, wq: WatchdogQuestion) -> None:
        """Real streaming path — persistent `claude --input-format
        stream-json` subprocess, JSONL-in per question, stream-json
        events out, terminated by a `result` event.

        Lifecycle:
          - First question spawns the subprocess and prepends the
            system prompt to the user message (we don't pass
            --append-system-prompt because that flag has Windows
            argv-mangling issues with multi-line content).
          - Subsequent questions just write a fresh user JSONL line.
            The system prompt is already in the conversation.
          - If the subprocess died between questions (claude crash,
            OS killed it, etc.) we respawn lazily on the next ask.

        Side effect of streaming mode: prior Q&A pairs accumulate in
        the conversation, so the watchdog "remembers" earlier
        questions in the session. Usually a feature ("you asked me
        about cx-A earlier; here's how cx-B differs") but worth
        knowing for token-cost reasons — long sessions will grow
        the in-memory conversation indefinitely. If this becomes a
        problem we'll add a "fresh session" reset on idle timeout.

        Failure modes:
          - subprocess spawn fails (binary missing, etc.)
          - stdin write fails (broken pipe — subprocess died)
          - timeout on result event (model stuck or first-question
            cold-cache slowness)
          - subprocess exits before result (crashed mid-question)
          - empty answer (model returned no text)
        Each path sets wq.failed + wq.error and fires the callback;
        sets _streaming_proc = None where appropriate so the next
        question respawns.
        """
        # Spawn lazily on first question (or after a death).
        first_question = self._streaming_proc is None
        if first_question:
            try:
                await self._spawn_streaming()
            except (FileNotFoundError, RuntimeError) as e:
                wq.failed = True
                wq.error = f"failed to spawn streaming watchdog: {e}"
                wq.answered_at = time.time()
                self._safe_callback(wq)
                return

        proc = self._streaming_proc
        if proc is None or proc.stdin is None or proc.stdout is None:
            wq.failed = True
            wq.error = "streaming subprocess not available"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        # Build the message text. Same envelope on first vs subsequent
        # — context + question — but on the first question we prepend
        # the system prompt because there's no --append-system-prompt
        # in streaming mode (see daemon.py for the same dance).
        body = (
            "RECENT FLEET ACTIVITY:\n"
            f"{wq.context_text}\n\n"
            "NETRUNNER QUESTION:\n"
            f"{wq.question}"
        )
        if first_question:
            msg_text = self.system_prompt + "\n\n---\n\n" + body
        else:
            msg_text = body

        message = {
            "type": "user",
            "message": {"role": "user", "content": msg_text},
        }
        try:
            proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as e:
            wq.failed = True
            wq.error = f"streaming stdin write failed: {e}"
            wq.answered_at = time.time()
            # Subprocess is dead — clear so the next ask respawns.
            # Drop the captured session_id with it; the respawned
            # subprocess will get a fresh one (and authoring shouldn't
            # try to fork onto a dead session in the meantime).
            self._streaming_proc = None
            self._session_id = None
            self._safe_callback(wq)
            return

        # Read events until we see a `result`. First-question timeout
        # is generous (cold cache); subsequent are the regular per-
        # question budget since context + system prompt are warm.
        timeout = (
            self.first_question_timeout if first_question else self.timeout
        )
        text_parts: list[str] = []
        try:
            died = await self._drain_streaming_question(
                proc, timeout, text_parts
            )
        except asyncio.TimeoutError:
            # Once a streaming subprocess wedges, it stays wedged.
            # Writes still succeed (OS buffers them) but reads hang
            # forever. Earlier code preserved the proc on timeout
            # hoping for recovery; in practice that just turned one
            # timeout into N timeouts as queued questions all hit
            # the same dead end. The fix is full reset: kill the
            # wedged subprocess, clear the slot, let the next
            # question respawn fresh. Sacrifices conversational
            # continuity (model "memory" of prior questions in the
            # session) but gains reliability — and an in-flight
            # wedge usually means the prior conversation context is
            # what tipped claude into stuckness anyway.
            wq.failed = True
            wq.error = (
                f"watchdog timed out after {timeout:.0f}s "
                f"(first_question={first_question}). Streaming "
                "subprocess was wedged; killing and respawning on "
                "next question. If this repeats, try --no-streaming."
            )
            wq.answered_at = time.time()
            await self._kill_streaming_proc()
            self._safe_callback(wq)
            return

        if died:
            wq.failed = True
            wq.error = "streaming subprocess exited mid-question"
            wq.answered_at = time.time()
            self._streaming_proc = None  # respawn on next ask
            self._session_id = None
            self._safe_callback(wq)
            return

        answer = "".join(text_parts).strip()
        if not answer:
            wq.failed = True
            wq.error = (
                "watchdog returned an empty answer (model gave no "
                "text). Try rephrasing the question."
            )
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        self._streaming_question_count += 1
        wq.answer = answer
        wq.answered_at = time.time()
        self._safe_callback(wq)

    async def _kill_streaming_proc(self) -> None:
        """Forcefully terminate the wedged streaming subprocess.
        Used by the timeout-recovery path; not by graceful shutdown
        (which uses _shutdown_streaming for the close-stdin-and-wait
        dance). Always clears _streaming_proc so the next ask
        respawns regardless of whether the kill succeeded.

        Best-effort all the way through: SIGTERM with a short grace
        period, then SIGKILL, then give up. Don't propagate any
        exception out — the caller is in a failure-recovery path
        and just needs the slot cleared."""
        proc = self._streaming_proc
        if proc is None:
            return
        try:
            if proc.returncode is None:
                try:
                    proc.terminate()
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except (ProcessLookupError, OSError):
                        pass
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass  # subprocess truly stuck; give up
        finally:
            self._streaming_proc = None
            self._session_id = None

    async def _spawn_streaming(self) -> None:
        """Start the persistent streaming subprocess. Mirrors
        Daemon._spawn_streaming. Note bypassPermissions vs daemon's
        acceptEdits: watchdog reasons but never executes tools, so
        the strictest non-blocking permission setting is fine.
        --verbose is required by claude-code for stream-json input
        per the docs."""
        resolved = shutil.which(self.claude_bin)
        if resolved is None:
            raise FileNotFoundError(
                f"could not locate {self.claude_bin!r} on PATH"
            )

        cmd = [
            resolved,
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--permission-mode", "bypassPermissions",
        ]
        self._streaming_proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

    async def _drain_streaming_question(
        self,
        proc: asyncio.subprocess.Process,
        timeout: float,
        text_parts: list[str],
    ) -> bool:
        """Read stream-json events until this question's `result`
        event arrives. Accumulates assistant text into text_parts.

        Returns True if the subprocess died mid-question (stdout
        closed before result arrived). Returns False on normal
        termination by result event. Raises asyncio.TimeoutError
        if no event arrives within the timeout window.

        Per-line timeout: starts at the caller's value (which is
        first_question_timeout for the cold-cache case), drops to
        30s once we're getting data — cold-cache slowness is a
        first-line problem; mid-stream pauses of 30+s are stuck.
        """
        if proc.stdout is None:
            return True

        per_line_timeout = timeout
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(),
                    timeout=per_line_timeout,
                )
            except asyncio.TimeoutError:
                raise

            if not line:
                # subprocess closed stdout — died mid-question
                return True

            try:
                raw = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                continue

            # Capture session_id from the first `system`/`init` event.
            # Slice 2 tripwire authoring forks the watchdog's session
            # via `--resume <id>` for one-shot authoring calls; that
            # requires knowing the id, which the streaming subprocess
            # only surfaces here in the event stream. Same pattern as
            # daemon.py's session capture (and we capture once — first
            # id wins; the streaming subprocess holds one session for
            # its lifetime).
            if (raw.get("type") == "system"
                    and raw.get("subtype") == "init"):
                sid = raw.get("session_id")
                if sid and self._session_id is None:
                    self._session_id = sid

            # Capture assistant text as it streams in.
            if raw.get("type") == "assistant":
                for block in raw.get("message", {}).get("content", []):
                    if (isinstance(block, dict)
                            and block.get("type") == "text"):
                        text_parts.append(block.get("text", ""))

            if raw.get("type") == "result":
                return False  # normal end-of-question

            # Once data is flowing, drop to a tighter "stuck" budget.
            # 20s mid-stream is generous — the model is supposedly
            # already producing output, so any 20s gap means it's
            # wedged. Was 30s; reduced after a real-deck wedge where
            # the user waited a long time on a frozen worker.
            per_line_timeout = 20.0

    async def _shutdown_streaming(self) -> None:
        """Clean teardown of the persistent streaming subprocess.
        Mirrors Daemon._shutdown_streaming.

        Windows ProactorEventLoop quirk: `proc.stdin.close()` is
        fire-and-forget — it marks the underlying pipe transport for
        close but defers the actual socket close. If the loop tears
        down before that deferred close completes, the transport's
        __del__ fires on a still-half-open socket, raising
        `ValueError: I/O operation on closed pipe`. Awaiting
        `wait_closed()` after `close()` ensures the close is
        actually committed before we move on.
        """
        proc = self._streaming_proc
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                try:
                    proc.stdin.close()
                    # wait_closed lets the proactor finish the
                    # deferred socket close before we proceed. Skip
                    # if it raises — best-effort teardown.
                    try:
                        await asyncio.wait_for(
                            proc.stdin.wait_closed(), timeout=1.0,
                        )
                    except (asyncio.TimeoutError, Exception):
                        pass
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
        except ProcessLookupError:
            pass
        finally:
            self._streaming_proc = None
            self._session_id = None

    async def _process_oneshot(self, wq: WatchdogQuestion) -> None:
        """Run one question through `claude -p` and fire the callback.

        Failure modes handled here:
          - claude binary missing
          - subprocess non-zero exit
          - timeout (DEFAULT_TIMEOUT)
          - empty output (treated as failure with a hint, since silent
            success would be confusing)
        """
        # Resolve the binary path. This catches "claude not on PATH"
        # cleanly with a clear error, instead of an opaque
        # FileNotFoundError from create_subprocess_exec deep in the
        # call stack.
        bin_path = shutil.which(self.claude_bin) or self.claude_bin

        # Compose the user-side prompt. The system prompt establishes
        # role; this message carries the actual data the watchdog
        # reasons over. Format is intentionally simple — a labeled
        # block of recent events, then a labeled question. Easy for
        # the model to parse, easy for a human to read in logs.
        prompt = (
            "RECENT FLEET ACTIVITY:\n"
            f"{wq.context_text}\n\n"
            "NETRUNNER QUESTION:\n"
            f"{wq.question}"
        )

        cmd = [
            bin_path,
            "-p",
            # Prompt is piped via stdin (see proc spawn below). Why
            # not pass it as a trailing positional after -p? Two
            # reasons: (1) when -p is followed by other flags before
            # any positional, claude treats -p as "read from stdin"
            # and our trailing positional gets ignored — exit 1 with
            # "input must be provided through stdin or as a prompt
            # argument when using --print"; (2) watchdog prompts
            # include the context block which is multi-line, and
            # Windows argv mangling with multi-line argv values is a
            # real problem we've hit elsewhere. Stdin is universally
            # safe.
            "--permission-mode", "bypassPermissions",
            "--append-system-prompt", self.system_prompt,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
            )
        except FileNotFoundError:
            wq.failed = True
            wq.error = f"claude binary not found: {self.claude_bin}"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return
        except Exception as e:
            wq.failed = True
            wq.error = f"subprocess spawn failed: {e}"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode("utf-8")),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            wq.failed = True
            wq.error = f"timed out after {self.timeout}s"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            wq.failed = True
            wq.error = (
                f"claude exited {proc.returncode}: "
                f"{err_text[:200] if err_text else '(no stderr)'}"
            )
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        answer = stdout.decode("utf-8", errors="replace").strip()
        if not answer:
            wq.failed = True
            wq.error = "claude returned empty output"
            wq.answered_at = time.time()
            self._safe_callback(wq)
            return

        wq.answer = answer
        wq.answered_at = time.time()
        self._safe_callback(wq)

    def _safe_callback(self, wq: WatchdogQuestion) -> None:
        """Fire the question's callback, swallowing exceptions so a
        broken UI handler doesn't kill the worker loop. The exception
        gets stashed as the question's error if no other error was
        already recorded — better than dropping it silently.

        Persists to history (if configured) BEFORE firing the
        callback so the Q&A is recorded even if the listener crashes
        or no listener exists. The history layer is itself
        best-effort and won't raise."""
        if self.history is not None:
            self.history.append(wq)
        try:
            wq.callback(wq)
        except Exception as e:
            if not wq.failed:
                wq.failed = True
                wq.error = f"callback raised: {e}"
