# Cyberdeck — Model + Effort Selection Design

*Architecture for the deck's per-spawn model and effort selection: the
daemon picks `--model` and `--effort` per construct based on task
needs and remaining quota; the daemon's own caliber is markable and
netrunner-overridable. Filed 2026-04-30 in response to "we'll
probably eventually want to pick the model we use for constructs."
Pair with `cyberdeck-spec.md` (the runtime architecture),
`cyberdeck-philosophy.md` (separation-of-concerns reasoning),
`cyberdeck-state.md` (current status), and `cyberdeck-build-plan.md`
(where this slots).*

*Implementation deferred behind Mechanic v0. The complexity is real;
the value compounds with quota-aware throttling and the eventual
multi-model substrate. Designing first means we don't burn a session
discovering the pool/profile/brake interactions mid-implementation.*

---

## Why this exists

The deck currently spawns every construct (and runs the daemon, the
watchdog, and pool warming subprocesses) at Claude Code's default
model + default effort. That default is Sonnet at `high` effort —
a sensible everyday choice but the wrong knob for two real workloads:

- **Cheap, parallel recon.** A daemon spawning eight constructs to
  grep eight different subsystems for a string doesn't need Opus on
  any of them. Haiku at `low` effort would finish faster, cost
  ~30x less per construct, and produce identical findings. Today
  the deck has no way to express this.
- **Heavy synthesis or research.** A construct asked to read fifteen
  files, reason about the architecture, and write a structured
  review benefits from Opus 4.7 at `xhigh` or `max` effort. Today
  it gets Sonnet+high, which is *fine* — but consistently
  under-delivers on the cases where the netrunner actually wants
  the heavyweight.

This is an axis the daemon can decide *better than the netrunner can
guess at goal-set time.* The daemon already classifies tasks by
profile (recon_specialist, code_reviewer, etc.); model+effort is the
same shape of decision: the daemon assigns based on task properties,
the netrunner can override.

The third factor that makes this load-bearing rather than just nice:
**quota awareness.** Max plan has hard 5h and weekly windows. When
the netrunner's quota is at 80% with four hours left, the daemon
should be able to ratchet down to Haiku+medium for non-critical
spawns rather than blasting through the remaining budget on
unnecessary Opus calls. Without per-spawn model/effort, the deck has
no lever to pull.

---

## The shape

Three independent axes, each settable per spawn:

### Axis 1: model

Five models in the current Anthropic lineup, accessible via Claude
Code's `--model <name>` flag:

| Alias  | Canonical name           | Use shape                                     |
|--------|--------------------------|------------------------------------------------|
| haiku  | `claude-haiku-4-5-...`   | Fast, cheap, narrow recon / format conversion |
| sonnet | `claude-sonnet-4-6`      | Default — versatile, capable, reasonable cost |
| opus   | `claude-opus-4-7`        | Heavy reasoning, synthesis, hardest tasks     |

Plus the 1M-context variants (`opus[1m]`, `sonnet[1m]`) for tasks
that need to read genuinely large contexts (whole-codebase reviews,
multi-file synthesis), and `opus[1m]` Legacy for fallback. We treat
the 1M variants as separate aliases the daemon can pick when
context size warrants it.

### Axis 2: effort

Five levels, settable via `--effort <level>`, `/effort`, settings.json
`effortLevel`, or the `CLAUDE_CODE_EFFORT_LEVEL` env var. Per
Anthropic's effort docs:

| Level    | Shape                                                      |
|----------|------------------------------------------------------------|
| low      | Skips thinking for simple cases; minimum reasoning depth  |
| medium   | Balanced — default-ish for cheap models                    |
| high     | Default for Sonnet/Opus 4.6 — strong reasoning             |
| xhigh    | Extra high — Opus 4.7 only (or falls back to high)        |
| max      | Maximum reasoning depth — Opus 4.7 only, session-only     |

Effort is **not a strict token budget.** It's a behavioral signal —
at low effort Claude may skip extended thinking entirely; at max it
spends generously on reasoning. The deck doesn't need to model the
internals; it just picks the level and hands it to Claude Code.

Constraint: `xhigh` and `max` are Opus 4.7-only. If we set
`max` on Sonnet, Claude Code clamps to the highest supported level
(`high`). The daemon doesn't have to know this — it picks
independently and the runtime clamps.

### Axis 3: fast mode

A separate Opus 4.6-only axis that trades cost for latency
(~2.5x faster, ~10x more expensive). Set via `/fast on|off` or
`"fastMode": true` in settings.json. Not orthogonal to model — picking
fast mode forces the model to Opus 4.6.

For the deck, this is a corner of the decision space worth supporting
but not the primary axis. **Daemon decision logic should treat fast
mode as a last-resort latency knob:** "the netrunner is waiting
for this specific construct's output and Sonnet is slow today —
switch this one spawn to Opus 4.6 fast." Most spawns will leave it
off.

### What this combines into

A construct's "caliber" — the combined model + effort + fast-mode
choice — is a small dataclass:

```python
@dataclass(frozen=True)
class Caliber:
    model: str            # "haiku" | "sonnet" | "opus" | "sonnet[1m]" | ...
    effort: str           # "low" | "medium" | "high" | "xhigh" | "max"
    fast_mode: bool = False
```

This is what the daemon picks per spawn and what the netrunner can
override. "Caliber" as the collective term lands lightly in the
codebase's cyberpunk vocabulary without overloading anything (brake
tiers stay tiers; profile stays profile; model and effort stay their
literal names; *caliber* is just the bundle).

---

## Pool implications

**The warm pool is locked to one caliber.** Warmed Claude Code
sessions are tied to the model + effort + fast-mode they spawned
with. A construct asking for `opus + xhigh` cannot resume a session
warmed at `sonnet + high` — model mismatch breaks `--resume`.

Two paths considered:

1. **Per-caliber pools.** Maintain N pools, one per common caliber.
   Each warms to its own target. Pull picks the matching pool;
   non-matching skips pool entirely. Complex — explosion of caliber
   combinations means most pools either run cold or compete for
   warming budget.
2. **Default-caliber-only warm pool.** Pool warms one caliber (the
   netrunner's default everyday choice — likely `sonnet + high`).
   Constructs whose daemon-picked caliber matches default reuse the
   pool; non-matching constructs spawn fresh. Same shape as the
   existing "non-default profile spawns fresh" pattern.

**Lean (2).** Matches the existing pattern, predictable warm cost,
falls back gracefully. The netrunner picks the pool's caliber
(probably surfaced in the `l` Limits modal — "pool caliber: sonnet+
high"). Daemon-picked spawns that match get the warm-spawn speedup;
daemon-picked spawns that don't get a fresh spawn — same as today's
behavior for non-default profiles.

The interaction with profile is straightforward: profile narrows
*system-prompt addendums and tool suggestions*; caliber narrows
*which model and effort budget*. They compose. Both are inputs to
`fleet.spawn()`. Pool reuse requires both to match the pool's
defaults.

---

## Daemon decision logic

The daemon system prompt grows a CALIBER SELECTION section
explaining when to pick each combination. The decision factors the
netrunner identified:

- **Speed requirements.** Time-critical work (netrunner waiting,
  blocking the next decision) → smaller model + lower effort, or
  fast mode if the task warrants Opus.
- **Performance / complexity.** Genuinely hard reasoning, synthesis
  across many inputs, code review of complex logic → Opus + high
  or xhigh.
- **Remaining quota.** As the 5h or weekly window approaches its
  cap, ratchet down. Below 50% remaining, prefer Sonnet over Opus
  for routine work. Below 20%, prefer Haiku for simple tasks. The
  daemon doesn't make this decision in isolation — the netrunner's
  goal still wins, but for daemon-discretionary spawns (recon,
  synthesis sub-steps), conservative calibers stretch the budget.

The daemon's existing per-spawn JSON action shape grows two fields:

```json
{
  "action": "spawn",
  "task": "...",
  "profile": "recon_specialist",
  "model": "haiku",
  "effort": "low",
  "fast_mode": false
}
```

Both `model` and `effort` are optional — defaults fall through to
the deck's configured pool caliber (see below).

The system prompt explains:

- **Default to the pool caliber.** Most spawns should match the pool
  to get warm-spawn benefit. Only pick a non-default caliber when
  the task genuinely warrants it.
- **Match caliber to task shape.** Suggested mappings:
  - Single-file read + grep + report → haiku + low
  - Multi-file recon + structured report → sonnet + medium
  - Synthesis, code review, hard reasoning → opus + high
  - Whole-architecture pass → opus[1m] + xhigh
- **Quota-aware fallback.** If the deck reports quota at >75%, prefer
  one tier down on model. >90% → cancel non-essential spawns,
  surface the reason in the next outcome turn for netrunner
  review.

The daemon doesn't need to be perfect at this. The cost of getting
it wrong is "the construct ran on the wrong caliber for this task" —
the work still completes, just slower or more expensively than
optimal. The win when it gets it right is large; the loss when wrong
is small. Asymmetric upside, fits the spec's "good defaults beat
exhaustive policy" principle.

---

## Quota awareness — the dependency

The "ratchet down on low quota" leg requires quota signal the deck
doesn't have today. Build plan item 13 ("quota-aware throttling")
covers this: a Claude Code status-line script reads
`rate_limits.{five_hour,seven_day}.used_percentage` and writes them
to `<deck>/.cyberdeck/quota.json`; the deck reads that file when
making spawn decisions.

This design *requires* item 13 to land first or alongside. Without
quota signal, the daemon can pick model+effort by task properties
but can't optimize for "we're running out." That's still useful —
the speed/complexity axis alone is most of the value — but the
quota leg is what unlocks the "stretch the budget under pressure"
behavior.

Phasing: ship model+effort selection without quota awareness first
(daemon picks based on task only); wire quota in as a second slice
once item 13's signal exists. The first slice is shippable on its
own.

---

## Daemon's own caliber

The daemon is itself a Claude Code subprocess. Same model + effort +
fast-mode applies. Currently the daemon runs at the deck-wide
default; we add explicit per-daemon-session caliber.

Default daemon caliber: **opus + high** (or whatever the netrunner
prefers). The daemon's job is decomposition + dispatch — it benefits
from strong reasoning, but not necessarily max-effort reasoning.

The netrunner can override:

- **Per-launch flag.** `python tui.py --daemon-model opus --daemon-effort xhigh`
- **In-session via the `T` daemon chat.** "Switch to opus xhigh for
  this goal." The daemon-chat handler parses this kind of directive
  and applies it as a session-mid caliber change for the next
  outcome turn (and forward).
- **Future: a dedicated modal.** When `T` directives prove unwieldy,
  graduate to a modal. Defer until the chat parser shape is
  uncomfortable.

The daemon's current caliber surfaces visibly somewhere — the
sidebar `bin: claude` line could grow `bin: claude opus·high` or
similar. Or a separate sidebar line. UI placement is an open
question; not blocking the design.

---

## Override semantics

Three override paths:

1. **CLI at startup.** `--daemon-model`, `--daemon-effort`,
   `--pool-model`, `--pool-effort`, `--default-construct-model`,
   `--default-construct-effort`. Set the deck's caliber configuration;
   per-spawn daemon decisions still happen on top.
2. **Limits modal.** The `l` Limits modal (post the recent rework)
   grows fields for pool caliber and daemon caliber. Same shape as
   the existing pool_size / max_concurrent inputs — read current,
   show defaults, accept new values, apply on save.
3. **Daemon chat (`T`).** Free-form. The netrunner says "use opus
   for the rest of this goal" or "drop to haiku for the next few
   spawns" and the daemon honors it. Implementation: a small
   instruction in the daemon system prompt teaches the daemon to
   recognize caliber-shift directives and apply them; the daemon
   action loop tracks an in-flight caliber preference until the
   goal closes or another directive supersedes.

The override hierarchy:

- **Netrunner directive (chat) > daemon's per-spawn pick.** The
  daemon respects netrunner overrides until they're explicitly lifted.
- **Daemon's per-spawn pick > deck-wide default.** The daemon can
  go off-default whenever it judges fit.
- **Deck-wide default > Claude Code default.** The deck explicitly
  passes its configured caliber on every spawn so behavior is
  predictable across versions.

---

## UI surfaces

The deck visibly displays current caliber in three places:

1. **Sidebar.** New line: `daemon: opus · high` (or similar). Updates
   on every caliber change. Symmetric with the existing
   `bin:` / `mode:` lines.
2. **Construct pane header.** Each construct's pane shows its
   spawning caliber as a small suffix: `cx-abc123 [DONE] · sonnet`.
   Helps the netrunner trace "which constructs ran on which caliber"
   when reviewing fleet activity.
3. **Limits modal.** Pool caliber + daemon caliber as adjustable
   fields alongside max_concurrent / max_total_spawns / pool_size.

The chatlog renders caliber changes as marker lines:

```
[yellow]daemon caliber: sonnet·high → opus·xhigh[/yellow]
       [dim](netrunner directive)[/dim]
```

Or for daemon-driven changes:

```
[dim]daemon picked haiku·low for cx-abc123 (recon)[/dim]
```

Markers are bus events (`caliber.daemon_change`, `caliber.spawn_pick`)
so the watchdog Q&A context picks them up automatically.

---

## Implementation slices

Five phases, each shippable independently. The first three are the
core; 4 and 5 are quota-dependent and UX polish respectively.

### Phase 1: Caliber primitive + per-spawn plumbing

- New `caliber.py` module: `Caliber` dataclass, validation
  (allowed model+effort combinations, fast-mode constraints),
  CLI-flag formatter (`Caliber.to_claude_args() → ["--model",
  "sonnet", "--effort", "high"]`).
- Thread `caliber: Optional[Caliber]` through `Construct.__init__`,
  `Fleet.spawn(...)`, `daemon_session._execute_action(...)`. None
  = "use deck default."
- Construct's claude subprocess command builder appends the caliber
  args.
- Daemon's spawn action JSON shape grows `model`, `effort`,
  `fast_mode` (all optional). Daemon system prompt adds a brief
  CALIBER SELECTION paragraph (decision tree, not yet quota-aware).
- Verified end-to-end: real-deck spawn with `--effort low` runs at
  low effort (visible in cost/timing).

### Phase 2: Pool caliber + warm-pool reuse

- `SessionPool.warm_caliber: Caliber` — the caliber the pool warms
  with. Default sonnet+high. Configurable via Limits modal.
- `pull(requested_caliber)` matches against `warm_caliber`; mismatch
  returns None (caller spawns fresh). Same shape as profile gating.
- Per-spawn settings.json (already exists for brake_hook) grows an
  `effortLevel` field so per-construct effort travels with the spawn
  even when not via CLI flag.

### Phase 3: Daemon caliber + override

- App.daemon_caliber field; CLI flags `--daemon-model`,
  `--daemon-effort`, `--daemon-fast-mode`.
- Daemon subprocess command builder applies caliber args.
- Daemon-chat (`T`) parser recognizes caliber-shift directives:
  regex match on "switch to <model>", "drop to <effort>", "use
  <caliber> for the rest", etc. Bus event published; sidebar +
  chatlog update.
- Help modal (`?`) grows a CALIBER section listing the model
  aliases and effort levels.

### Phase 4: Quota-aware fallback (BLOCKED on build-plan item 13)

- Read `<deck>/.cyberdeck/quota.json` (per item 13's design).
- Add a quota-aware band to the daemon system prompt: at
  >75% used, prefer one tier down; at >90%, refuse non-essential
  spawns with the reason surfaced in the outcome turn.
- Verify with a synthetic high-quota scenario.

### Phase 5: UI polish + introspection

- Construct pane header shows caliber suffix.
- Sidebar daemon-caliber line.
- Watchdog Q&A system prompt grows CALIBER AWARENESS so questions
  like "which constructs ran on opus today?" / "why did the daemon
  pick haiku for that task?" answer correctly from the chatlog
  markers and the `caliber.*` bus events.

Total: ~3 sessions for phases 1-3 (the core), 1 for phase 4 once
quota lands, 1 for phase 5. Could compress to 2-3 sessions if each
phase is tight.

---

## How this composes with the existing roadmap

### Mechanic v0 (next priority)

**Independent.** Mechanic v0 is supervisor-only (subprocess
janitor); doesn't care about caliber. Caliber design composes
naturally with Mechanic v1 (LLM session) when that lands —
Mechanic v1 itself has a caliber (probably sonnet+medium for
diagnose-only work; cheap and fast).

### Spine phase 8 (cleanup)

**Independent.** Caliber events flow through the bus from day one;
no changes needed when add_listener shims retire.

### Quota-aware throttling (build plan item 13)

**Hard prerequisite for phase 4.** Without quota signal, the
daemon's "ratchet down on low budget" leg has no input. Phase 1-3
don't need it; phase 4 does.

### Local model substrate (D1)

**Future composition.** Once D1 lands, the model axis grows local
options: `local:llama-70b` or whatever. Same caliber primitive;
just a new value in the model enum and a different command-builder
path. The watchdog substrate swap (cloud → local) becomes a caliber
change for one specific subsystem.

### Profile system

**Composes.** Profiles are prescriptive templates; calibers are
runtime cost/capability decisions. Profiles can *suggest* a default
caliber via a new TOML field (`recommended_caliber = "haiku+low"`),
which the daemon respects unless task properties indicate otherwise.

### Brake / brake hook

**Independent.** Brake gates *which tools* a construct can use;
caliber gates *which model and how much it thinks*. Different axes,
different enforcement. Both flow through the same per-construct
spawn settings JSON though, so plumbing is shared.

### The morgue (deck history)

**Naturally records caliber.** Every finalized session record gains
a `caliber` field showing what the construct ran with. Useful for
"which calibers actually pay off for which tasks?" retrospective
analysis.

### Universal list-names

**Independent.** Caliber is metadata, not a listable object. Doesn't
need a list-name.

---

## Open questions

1. **Default pool caliber.** Sonnet+high is the safe everyday
   default, but the netrunner runs Opus+xhigh in their personal
   Claude.ai sessions per the screenshot. Pool-warming Opus is
   expensive (warm cost ~10x Sonnet). Probably sonnet+high for the
   pool; the netrunner can override via the Limits modal. Verify
   the cost profile on real-deck before committing.

2. **Effort fallback transparency.** When the daemon picks `max` on
   Sonnet (which clamps to `high` per Anthropic's runtime), should
   the deck surface the clamp? Probably yes — a chatlog dim marker
   so the netrunner sees "max effort requested but model
   doesn't support; using high." Same shape as other deck-side
   transparency markers.

3. **Caliber persistence for resumed sessions.** When a construct
   resumes a warm-pool session (sonnet+high), can the daemon
   request a different effort for that resume turn? Anthropic's
   API allows per-request effort; verify Claude Code passes per-turn
   effort through `--resume`. If not, caliber is locked at session
   start and the daemon's per-spawn caliber pick only affects
   non-pool fresh spawns.

4. **Fast mode interaction with brake.** Fast mode bypasses some
   internal Claude Code safety checks per the docs. Does the brake
   hook still fire? It should — the hook is at the PreToolUse
   layer, not internal Claude logic. Verify on real-deck before
   trusting fast-mode spawns at default brake.

5. **Daemon-chat directive grammar.** "Switch to opus" is easy;
   "use opus for the next 3 spawns then drop back to sonnet" is
   harder. Phase 3 starts with simple immediate directives; richer
   syntax can land later if the netrunner reaches for it. Avoid
   building a full DSL before the use case justifies it.

6. **Pool caliber change mid-session.** If the netrunner changes the
   pool caliber from sonnet+high to opus+xhigh mid-session via the
   Limits modal, what happens to the existing warm sessions? Two
   options: (a) drain them naturally as constructs consume them, top
   up at new caliber; (b) actively kill warm sonnet sessions, warm
   fresh at opus. Lean (a) — same "lowering pool_size doesn't
   actively shrink" pattern just landed.

7. **The Limits modal getting crowded.** Three caliber fields
   (pool, daemon, default-construct) plus the existing four
   (max_concurrent, max_total_spawns, pool_size, plus current-state
   readouts) plus the cost line — the modal grows. May warrant
   splitting into LimitsScreen + CaliberScreen, with a separate
   keybind for the latter. Defer until phase 5; if the modal feels
   crowded at that point, split.

8. **Watchdog and authoring substrate.** Watchdog Q&A and tripwire
   authoring run on cloud Claude today. Should they have their own
   caliber knobs, or inherit the daemon's? Lean: separate. The
   watchdog has a clear cost profile (cheap Q&A, occasional
   authoring) and benefits from low+haiku for routine answers and
   medium+sonnet for tripwire authoring. Phase 5 territory.

---

## Things to NOT do

- **Don't model the effort token-budget mechanics ourselves.** Effort
  is a behavioral signal; let Claude Code interpret it. The deck
  passes the level and trusts the runtime.
- **Don't build a per-caliber pool tree.** One pool, one warm
  caliber. Caliber mismatch falls through to fresh spawn. Per-caliber
  pools are an optimization that adds complexity without clear win
  given the warming-cost asymmetry.
- **Don't put the daemon's caliber decision in the hot path.**
  System-prompt-level decision is fine; runtime classifier per
  spawn is overkill. The daemon already classifies tasks via
  profile selection; caliber is the same shape of decision and
  belongs in the same prompting layer.
- **Don't override Claude Code's effort clamp.** When `max` on
  Sonnet falls back to `high`, that's the runtime's correct
  behavior. The deck doesn't try to be smarter; it just surfaces
  the clamp transparently.
- **Don't conflate caliber with profile.** Profiles are about
  *what work the construct does* (read-only review, recon,
  general-purpose); calibers are about *which model and how hard
  it thinks*. Different concerns, different files, different
  decision trees. They compose; they don't subsume.
- **Don't auto-escalate caliber on retry.** When a construct fails,
  the temptation is "rerun with bigger model." Reject this — the
  daemon already gets to pick caliber on its retry spawn. Adding
  automatic escalation gives constructs a quiet escape hatch from
  the cost-control logic.

---

## Cyberpunk vocabulary alignment

The combined model + effort + fast-mode bundle is the construct's
"caliber" — the capability/cost grade it deploys at. Lands lightly
in the existing vocabulary:

- *Brake* gates *capability tiers* (paranoid/default/yolo).
- *Profile* is a *prescriptive template* (recon_specialist, etc.).
- *Caliber* is the *capability grade* (haiku+low through opus+max).

Three different decisions about the same construct: what work it can
do, what role it's playing, how much horsepower it brings. The deck
already routes the first two cleanly through the spawn pipeline;
caliber is the third and lands the same way.

The word itself fits the deck's aesthetic — a netrunner picks
caliber the same way they'd pick a weapon's grade. Doesn't collide
with anything existing. Short, single-word, glanceable.

---

## Why this earns its keep

The real win isn't "the daemon can pick opus when needed." It's
**the deck stops wasting capacity in both directions**:

- Today, every recon construct runs at sonnet+high when haiku+low
  would work. That's 10-30x overspend on a workload that doesn't
  need the headroom.
- Today, every synthesis construct runs at sonnet+high when
  opus+xhigh would produce materially better output. That's
  under-delivering on the workload where capability matters.
- Today, the deck has no lever when the netrunner's quota is at
  85% and four hours remain. Caliber + quota awareness is the
  lever.

The deck's value-add over "just run claude in a terminal" is that
the daemon makes routing decisions the netrunner doesn't have to.
Caliber is the next routing decision in line.

---

*Filed 2026-04-30. Implementation deferred behind Mechanic v0 per
build-plan priority order. When picking this up, start with phase
1 (caliber primitive + per-spawn plumbing) — it's small, isolated,
and the rest depend on it. Phase 4 unlocks once quota awareness
(build plan item 13) lands; phases 1-3 + 5 are independent.*
