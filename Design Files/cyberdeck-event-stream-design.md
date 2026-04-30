# Cyberdeck — Unified Event Stream Design

*Architecture for the deck's central event spine: one canonical stream
that every event source publishes to, every observer subscribes from
with a role-derived filter, every persistent log consumes. Replaces
the current pattern of N independent callback chains that decay
silently as new event types are added. Filed 2026-04-29 in response
to discovering that the watchdog Q&A context's silent fleet/daemon-only
filter had quietly invalidated half the watchdog's system prompt.*

*Pair with `cyberdeck-spec.md` (the deck's runtime architecture),
`cyberdeck-philosophy.md` (separation-of-concerns reasoning),
`cyberdeck-maintbot-design.md` (the maintbot is one of the stream's
designed consumers), and `cyberdeck-state.md` (current status).*

---

## Why this exists

### The bug pattern that made it inevitable

2026-04-29: slice 2's in-deck self-review caught a duplicate method.
The netrunner pushed harder on the chatlog magnified-view fix and
asked: "is this a bootleg fix that suppresses something we wanted
suppressed?" Investigation found the fleet/daemon-only filter wasn't
a deliberate spam suppressor — it was just an assumption that decayed.
Direct `_chatlog_write` calls (tripwire fires, watchdog markers, brake
transitions, blacklist additions, goal-update markers, connection
state changes) never made it into the chatlog event buffer at all.
Both readers of the buffer — the magnified view AND the watchdog Q&A
context-builder — silently dropped them.

The watchdog system prompt has a TRIPWIRE AWARENESS paragraph that
says "if the netrunner asks 'any tripwires fired?', report what's in
the chatlog." But the chatlog snapshot the watchdog actually received
filtered tripwire fires *out*. The instruction was vestigial — the
markers it was told to read were never visible to it. Same shape for
BRAKE STATE AWARENESS and BLACKLIST AWARENESS.

This is the failure pattern. Not a single bug — a **bug class**.
Every time a new event source landed (tripwires, blacklist, brake,
connection state, plugins, profiles, sessions), every existing
subscriber needed to be re-wired. We never re-wired the buffer
readers. Nothing complained.

### The architectural cause

Today, the deck has at least 11 event sources, each with its own
callback shape:

| Source | Callback shape | Event type |
|---|---|---|
| Fleet | `add_listener(handler)` | FleetEvent |
| DaemonSession | listener function | DaemonEvent |
| TripwireEngine | `on_fire` | TripwireFire |
| Blacklist | `on_event` | dict |
| ConnectionMonitor | `on_state_change` | StateChangeEvent |
| BrakeStateStore | `on_change` | BrakeChangeEvent |
| ProfileRegistry | `on_event` | ProfileEvent |
| PluginRegistry | `on_event` | PluginEvent |
| Watchdog (Q&A resolution) | per-question callback | WatchdogQuestion |
| SessionPool | `on_event` | PoolEvent |
| TUI direct chatlog writes | `_chatlog_write(line)` | (now buffered as `kind="direct"`) |

Plus future sources we already know about: maintbot diagnostics,
morgue session records, list-name generation completions, `Watchdog.author_tripwires` results.

The TUI is the only component that subscribes to all of them. Every
new event source adds another callback chain through the TUI. Every
new subscriber that needs to see "what's happening on the deck" needs
to be hand-wired through every existing source. Quadratic
maintenance, decaying assumptions, silent failures.

### The "15 LLMs shouting over each other" failure mode

The naive answer to "how do we get every agent to see what's
happening?" is to forward every event to every agent. This is the
multi-agent collapse failure mode: token costs explode, context
windows fill with noise irrelevant to any given role, and agents
start reacting to events outside their role (watchdog tries to plan,
daemon tries to observe, constructs try to coordinate). The spec's
role separation collapses because nothing structurally prevents an
agent from seeing what it shouldn't.

Solution: one stream + per-role derived filters. Everyone yells in
the same room; the bus decides programmatically who hears what,
based on the role's declared interest. This makes the spec's role
boundaries enforceable at the visibility layer, not just at the
prompting layer.

---

## Architecture

### The DeckEvent

One canonical event shape:

```python
@dataclass
class DeckEvent:
    kind: str              # categorical: "fleet.spawn", "tripwire.fire", "brake.change", etc.
    source: str            # which subsystem emitted it
    timestamp: float       # epoch seconds, set at publish-time
    construct_id: Optional[str]   # when relevant ("which construct?")
    payload: Any           # the source-specific data
    severity: str = "info" # "debug" / "info" / "warning" / "critical"
    text: Optional[str] = None    # pre-rendered display string when applicable
```

`kind` is dotted-namespace: `<source>.<event_type>`. Examples:
- `fleet.spawn`, `fleet.finalize`, `fleet.event` (per-construct streaming events)
- `daemon.thinking`, `daemon.action`, `daemon.outcome`
- `tripwire.fire`, `tripwire.authored`, `tripwire.author_failed`
- `brake.change`
- `blacklist.added`, `blacklist.spawn_refused`
- `connection.transition`
- `chatlog.direct` (replaces the current `_chatlog_event_buffer` direct kind)
- `watchdog.question_resolved`
- Future: `maintbot.triage_started`, `morgue.session_finalized`,
  `listname.generated`

`severity` lets the file logger and surfaces filter independent of
kind — e.g. CRITICAL events should always render, regardless of role.

### The EventBus

```python
class EventBus:
    def publish(self, event: DeckEvent) -> None: ...
    def subscribe(
        self,
        callback: Callable[[DeckEvent], None],
        *,
        filter: Optional[Callable[[DeckEvent], bool]] = None,
        name: str = "",
    ) -> Subscription: ...
```

- Synchronous publish on the same event loop (matches the deck's
  current threading model — Textual single-loop everywhere)
- Subscribers register with a filter predicate. No filter = "all
  events." Most subscribers will have one.
- Order is preserved (FIFO across publish calls)
- Bus owns a bounded ring buffer (replacing `_chatlog_event_buffer`)
  so retroactive subscribers (e.g. file logger that opens late) can
  catch up
- Defensive: a subscriber raising never poisons the bus; per-callback
  exceptions are caught + logged via the bus's own logger event

### Role-derived filters

Filters are **declared by the consuming component**, derived from
the component's role. Hand-curated subscriptions per use site are
the failure mode we're trying to escape — they decay silently.

Each consumer declares the kinds it cares about:

```python
class Watchdog:
    # Q&A context: "what's happening in the fleet?" — observe everything
    # the netrunner can see, plus tripwire/brake/blacklist context the
    # system prompt explicitly references.
    QA_CONTEXT_KINDS = {
        "fleet.spawn", "fleet.finalize", "fleet.event",
        "daemon.thinking", "daemon.action", "daemon.outcome",
        "tripwire.fire", "tripwire.authored",
        "brake.change",
        "blacklist.added", "blacklist.spawn_refused",
        "connection.transition",
        "chatlog.direct",  # picks up anything else the netrunner saw
    }
```

The watchdog's Q&A context-builder subscribes once with that filter.
When tripwires get a new event subkind (`tripwire.severity_routed`,
say, in slice 3), the watchdog automatically picks it up if it's in
the kinds set — or stays out of scope if it isn't. Either way,
*intentional*, not "did we remember to thread the new event through
the old callback chain."

Display surfaces work the same way:

```python
class CyberdeckApp:
    # Small chatlog: everything the netrunner is supposed to glance at.
    CHATLOG_KINDS = { ... almost everything ... }
    # Fleet log: just the per-construct lifecycle.
    FLEET_LOG_KINDS = { "fleet.spawn", "fleet.finalize", "fleet.kill" }
    # Daemon pane: daemon's own output stream.
    DAEMON_PANE_KINDS = { "daemon.thinking", "daemon.action", "daemon.outcome" }
```

The filter predicate is what makes "everyone yells in the same room
without going insane" structurally true. The bus enforces it.

### Introspectability for free

A side-effect of declarative role filters: **"what is the watchdog
actually seeing right now?" becomes literally answerable.** Today,
the answer requires tracing 11 callback chains through the TUI's
middleware and reasoning about which `if kind == "..."` branches
fire. Under the unified stream, the answer is "check its filter
against the stream tail." That's a one-line introspection.

This unlocks several real workflows:

- **Debugging:** "the watchdog said it didn't see the tripwire fire"
  → check its filter, check the stream history, see whether the event
  was emitted, see whether the filter let it through. Three deterministic
  questions instead of "trace through `_handle_tripwire_fire` and see
  what got dropped where."
- **Testing:** mock a stream of events, plug in a subscriber, assert
  the right ones got through. No subprocess required, no real claude
  required, no flaky timing. Same shape as the slice 1/2 inline
  parser tests but for *visibility* rather than *parsing*.
- **Documentation:** "what does the watchdog see?" answers by
  printing its filter. Self-documenting in a way that callback
  topologies aren't.
- **New-feature confidence:** when adding a new event kind, you can
  enumerate which subscribers will pick it up by matching the new
  kind against their filters. The "did we remember to thread this
  through?" question becomes formal — either the filter matches or
  it doesn't.
- **Maintbot diagnostics:** when the netrunner asks the maintbot
  "what was the watchdog doing when X happened?", the maintbot can
  read the watchdog's filter, replay the stream tail through it, and
  show the exact view the watchdog saw. No guesswork, no reverse-
  engineering.

The current sprawl makes none of this tractable. The unified stream
makes it free.

### What's NOT a subscriber

**Constructs.** They are work units, not observers. They receive task
text and injected messages. They don't see the stream. This is the
spec's hermetic-by-default contract; the bus enforces it by simply
never being passed to constructs.

**The brake hook.** It's a one-shot subprocess invoked by Claude Code
on each tool call. No room in its lifecycle for stream subscription.

**Plugins (today).** Stateless v1. Future persistent plugins might
become subscribers if a use case warrants it, but no plans now.

---

## Migration plan

Big-bang refactors break things. This is N independent migrations,
each safe in isolation:

### Phase 1: Bus + DeckEvent
Add `event_bus.py` with the dataclass + bus + subscription primitives.
Bus is initially unused. New module, no behavior change.

### Phase 2: Fleet → bus
Fleet emits FleetEvents on the bus AS WELL AS through `add_listener`.
Existing subscribers unchanged. New code (anything new this slice)
subscribes via the bus. Eventually `add_listener` becomes a deprecated
shim that internally subscribes through the bus, then a removal.

### Phase 3: Daemon → bus
Same shape. DaemonEvents publish to bus + existing listeners.

### Phase 4: Tripwires + Blacklist → bus
TripwireEngine.on_fire becomes a bus publish. Same for
Blacklist.on_event. Existing TUI handlers convert to bus subscribers.

### Phase 5: Brake + Connection + Profiles + Plugins → bus
Each of these has a small `on_*` callback today. Each migrates to a
bus publish. Existing handlers become subscribers.

### Phase 6: Direct chatlog writes → bus
`_chatlog_write` publishes a `chatlog.direct` event AND writes to the
RichLog. Buffer goes away — bus's ring buffer replaces it. Magnified
view becomes a subscriber that re-renders from the bus tail.

### Phase 7: File logger as a bus subscriber
`logger.py` subscribes with no filter (logs everything). Per-launch
file with timestamps, NDJSON, levels. The "logger + quit discipline"
slice from earlier becomes a bus subscriber + a SIGINT handler — the
bus part is most of the work, and it's done.

### Phase 8: Cleanup
Remove the deprecated `add_listener` / `on_*` shims. Documentation
pass. Verify the bus's ring-buffer cap is appropriate.

Total: ~3-5 sessions of work, but each phase ships independently and
the deck stays usable throughout. No long-running broken state.

---

## How this composes with the existing roadmap

### Logger + quit discipline (was: next chapter)
**Absorbed.** The logger lands in Phase 7 as a bus subscriber. The
quit-discipline half (silent SIGINT swallow, smart Ctrl+Q toast,
EJECT responsiveness) tucks in alongside since it touches lifecycle —
publish a `lifecycle.shutting_down` event with reason, the file
logger flushes on receiving it, the heartbeat sensor reads it from
the file as the "clean exit" marker.

### Maintbot v1
**Cleaner.** The maintbot becomes "subscriber that reads the bus's
ring buffer + the file log on demand." No special access protocol;
just `bus.snapshot(last_n=...)` plus reading `logs/latest.log`. When
the deck dies and the heartbeat fires the maintbot, the maintbot
opens the file log and (in headless mode where it wraps the deck)
attaches to the new deck's bus directly when relaunching.

### Tripwires slice 3 (severity-aware rendering)
**Easier.** Severity is already a field on DeckEvent. The chatlog
filter for "critical-tier tripwires get focus" becomes a one-line
predicate: `lambda e: e.kind == "tripwire.fire" and e.severity == "critical"`.

### Watchdog Q&A
**Smarter.** The Q&A context-builder subscribes with the
QA_CONTEXT_KINDS filter and gets the right view for free. Future
tripwire kinds, brake events, etc., land in context automatically as
long as the watchdog's role-filter includes them.

### Morgue / watchdog log (deck history infrastructure)
**Trivial.** Both become bus subscribers that persist their kinds.
The morgue subscribes to `fleet.finalize` and writes records. The
watchdog log subscribes to `watchdog.question_resolved`. Same
substrate, different filters.

### Universal list-names (netrunner direction)
**Composes.** List-name generation publishes `listname.generated`
events; UI surfaces subscribe and patch their list rows. Async LLM
authoring fits naturally — subscriber sees the event, updates the
displayed name, no polling.

### B2 fleet synthesizer (D1-blocked)
**Has a substrate.** The synthesizer reads the bus's tail and
summarizes. No new event-collection plumbing.

---

## Why this isn't an emergency right now

The tactical fix shipped with slice 2 (push direct writes to the
buffer + teach both readers about the new kind) closes the immediate
visible bugs. Real-deck slice 2 works, the magnified view shows
markers, the watchdog's Q&A context sees what its system prompt
references. The deck is not broken.

The unified stream is the *next* slice — the right architectural
move, but the deck stays usable on the tactical fix in the meantime.
Doing this refactor inside the slice 2 commit would have been an
11-module rewrite with a much larger blast radius if anything went
sideways during testing.

The right sequencing:

1. ✓ Slice 2 ships with tactical buffer fix (commits land 2026-04-29)
2. **Next slice:** unified event stream (this doc) — phases 1-8 above
3. After: maintbot v1 (uses the stream)
4. After: tripwires slice 3 (uses the stream)
5. ... everything else composes cleanly under the new substrate

---

## Open questions

Captured for design-time, not solving now:

1. **Naming.** Working name "event stream" / "bus" is sterile. The
   deck has a body — sidebar, main, daemon-bar, fleet, daemon,
   watchdog. The unified stream is its central nervous system. *Spine*
   fits the body metaphor; collides with nothing existing. *The wire*
   is tempting but collides with construct wiring (the routing
   primitive `r`). Going with *spine* in the implementation unless
   something better surfaces.

2. **Filter declaration shape.** Set-of-kind-strings vs. predicate
   function vs. declarative grammar (`KindFilter("fleet.*", "tripwire.*")`).
   Set-of-strings is simplest and probably enough; predicates handle
   composite logic (kind + severity + construct_id) when needed.
   Lean: kinds-or-callable union — accept either.

3. **Ring buffer size.** Current `_chatlog_event_buffer` is
   `maxlen=1000`. The unified bus needs a larger window because more
   events flow through it (every fleet stream event, every daemon
   thinking event). Probably 10k-50k. Cap by event count + memory
   ceiling.

4. **Ordering across publishers.** Single-loop synchronous publish
   gives total order. If we ever need multi-loop (e.g. watchdog
   running on a separate event loop), need to decide: per-source
   FIFO, total order via timestamps, or something else. Not now.

5. **Backpressure.** A misbehaving subscriber that takes 5s per event
   blocks publish. Today this is theoretical; matters more if we ever
   add async-heavy subscribers. Punt.

6. **Replay semantics for late subscribers.** When the file logger
   opens at startup, should it get the bus's ring buffer history or
   only events from now-onward? Lean: the bus exposes
   `subscribe_with_replay(filter, replay_n=N)` for the case where it
   matters; default is "from now."

7. **Should constructs publish?** The model says no — they're hermetic.
   But Claude Code's stream events ARE essentially what fleet emits as
   `fleet.event`. So constructs *do* contribute to the stream, just
   indirectly via the fleet adapter. Documenting this so the spec's
   "constructs are hermetic" rule isn't read as "constructs are
   silent" — they're observed, not observers.

8. **Per-event token cost when persisted.** The file logger writes
   every event. Per-event size is bounded (~1-10 KB for stream events,
   ~100 bytes for control events). At fleet-burst peaks (10 constructs
   each emitting 1 event/sec, ~100 KB/sec to disk) the log can grow
   fast. Probably fine on SSD; matters when we eventually port to Pi.
   Per-launch file rotation already handles long-session bloat.

---

## Cyberpunk vocabulary alignment

Per `cyberdeck-philosophy.md`, naming choices in this codebase pull
from a fictional aesthetic that captures the *feel* of operating a
multi-agent system under time pressure. The unified stream is the
deck's nervous system: signals propagate from sensory inputs (fleet
events, daemon outputs, tripwire fires) through a central spine to
specialized processors (display surfaces, agent contexts, persistent
log). Each processor has a derived view tailored to its role. The
human reads the chatlog and the magnified view; the watchdog reads
its Q&A snapshot; the file logger and future maintbot read the whole
stream as a permanent record.

That's not just an architecture — it's the deck's body language.
"Spine" is the right name. Nothing collides; it composes; it's
short; and it accurately describes what the thing is.

---

*Filed 2026-04-29 in response to the buffer-decay bug class. Picks
up as the next implementation slice after slice 2 commits. Substrate
for everything still queued: maintbot, file logger, quit discipline,
tripwire severity routing, morgue, list-names, B2 synthesizer.*
