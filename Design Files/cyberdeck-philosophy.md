# Cyberdeck — Philosophy

*The north star. Why this exists, what it's for, how to think about
design choices when the spec doesn't directly answer them. This doc
should age slowly; revise only when the underlying convictions
change.*

*Companion to `cyberdeck-spec.md` (the what), `cyberdeck-build-plan.md`
(the when), and `cyberdeck-state.md` (the now).*

---

## What is the cyberdeck?

A local orchestration client for coordinating multiple AI agents
through a keyboard-first, one-hand-operable interface. Runs on modest
hardware. Delegates heavy lifting to remote model APIs. Grows
capability by adding interface plugins, saved scripts, and reusable
construct profiles.

In one sentence: **a personal AI cockpit that makes one human's
attention go further than it should.**

In one image: the netrunner from *Edgerunners*, minus the brain
interface, plus a TOML config file.

---

## Why does it exist?

Because the bottleneck of multi-agent AI work is not compute, not
model capability, not API latency. **The bottleneck is supervision
bandwidth.**

One human can spawn twenty agents trivially. One human cannot watch
twenty agents trivially. The cyberdeck's job is to close that gap —
not by making humans more attentive, but by making attention itself
more leverageable: better tooling, better defaults, better visibility,
better ways to express intent and delegate mechanics.

This is a personal project. Adoption is not a concern. Defaults can be
opinionated. Configuration can be "edit the TOML and if it breaks
that's a you problem." The deck is made for one user, not many — and
that's a feature.

---

## Three load-bearing theses

### 1. Express intent, delegate mechanics.

The deck has excellent UI for expressing what you want. Mechanics —
finding the API, writing the script, executing the call — are the
agents' problem.

**Implication for design:** every keypress, every modal, every
default is evaluated by *how cleanly it lets the netrunner say what
they mean.* If something forces them to think about implementation
plumbing instead of intent, it's broken.

**Implication for prompting:** when the deck builds prompts for
constructs and the daemon, it carries the netrunner's intent
forward. Framing matters more than information — the model can read
its own session history; what it can't read is the human's *attitude*
toward that history. That's what good prompts encode.

### 2. Capability accumulates.

Anything the netrunner does well once should become a saved tool the
deck can do cheaply forever. Plugins, scripts, profiles — all are
addressable, all are persistent, all can be invoked by future agents.

**Implication:** the deck is a *personal capability library that grows
with use.* It is never finished. It grows arms, not features.

**Implication for tooling:** the file system is the database. The
folder tree is the menu. Prefer human-readable, human-editable
formats (TOML, markdown, plain text) over opaque ones. The netrunner
should be able to crack open the deck's files with `vim` or
`notepad` and understand what they're looking at.

### 3. Supervision is structural, not aspirational.

The deck does not assume the netrunner is paying attention. It
assumes attention is finite, intermittent, and hard to recover.
Therefore:

- Risk is gated by **action class**, not by model confidence (models
  are most confident exactly when they're wrong).
- Tripwires are **deterministic matchers**, not LLM judgments
  (auditable, sub-millisecond, no hallucination risk).
- Roles are **separated by concern** (the entity noticing problems
  must not be graded on whether its plans work — daemon plans,
  watchdog observes).
- Every escalating action has **deliberate consent** — held keys,
  confirmation modals, hard limits.

The netrunner is in the loop, but the deck is structured so that
*not being in the loop* fails safely.

---

## What the deck is not

Naming non-goals explicitly because they keep showing up as "wouldn't
it be cool if..." — and they keep deserving "no, actually, here's
why."

### Not a multi-user system.
No accounts, no permissions, no shared state. One netrunner, one
deck. The threat model is "the netrunner trusts their cloud provider
with their own work" — anything else (compliance mode, engagement
contracts) is a deferred extension, not a base requirement.

### Not a chat interface.
The deck *contains* a chat interface (the daemon pane), but the deck
itself is not a chat. Goal-setting is one keypress; injection is one
keypress; the rest is happening in parallel without conversational
turn-taking. If you want to chat with Claude, use claude.ai. The deck
is for orchestrating *work*, not having dialogues.

### Not a load-bearing inter-agent comm bus.
Constructs talk through the daemon by default. Wirings allow direct
channels between specific construct pairs but are explicit, limited,
and turn-budgeted. We deliberately avoid the architecture where every
agent can chatter at every other agent — that explodes context
multiplicatively and reduces signal-to-noise. The chatlog (when it
ships) is *for the netrunner*, not for the agents to talk to each
other through.

### Not autonomous.
Even when constructs work in parallel, the daemon plans, the
netrunner steers, and the watchdog observes. The deck is a
force-multiplier on a human, not a replacement. EJECT, hard-kill,
brake profiles, deliberate-consent gestures — all of these exist
because the human stays in the loop.

### Not a product.
This is a hobby deck. It will probably never have users beyond the
person building it. Polish where it matters for *the netrunner's
experience*; skip polish where it would matter only for adoption.
"Documentation for new users" is not on the roadmap. "Documentation
for future-me when I forget how this works" is.

---

## Philosophy in one diagram

```
NETRUNNER                       ← human, finite attention
   │
   │ expresses intent (keypress, modal, goal text)
   ▼
DAEMON                          ← persistent coordinator
   │                              decomposes intent into subgoals
   │                              spawns constructs
   │                              integrates outcomes
   ▼
CONSTRUCTS                      ← task-scoped workers
   │                              hermetic by default
   │                              report back as outcomes
   │
   ▼
TOOLS / SCRIPTS / PROFILES      ← capability library
                                  grows with use
                                  human-readable on disk

                                ↑
                                │
                                │ observes, alerts, remembers
                                │
WATCHDOG                        ← read-only oversight
                                  authors deterministic tripwires
                                  Q&A oracle for "what's happening?"
                                  blacklist memory
```

Each role has one job. Crossing job boundaries is a smell.

- Daemon plans → daemon does not also observe its own plans
- Watchdog observes → watchdog does not also plan
- Constructs execute → constructs do not also coordinate
- Netrunner steers → netrunner does not also implement

Violations of this separation are how systems become incomprehensible
to their own builders. If the daemon starts grading itself, it loses
the property that the watchdog is supposed to provide. If the
watchdog starts intervening in plans, it loses the property that
asking it questions can never derail execution.

---

## Design principles (in priority order)

When two principles conflict, the higher one wins. This list is
brutal on purpose — design choices that try to optimize for
everything end up optimizing for nothing.

### 1. Sub-50ms input latency.
No model in the hot path of any keypress. The deck must feel
responsive even when half the agents are stalled on slow APIs.
Models are for what models are for; key handling is not it.

### 2. Stopping is a feature; resume is best-effort.
EJECT works always. Hard-kill works always. Soft-kill, queue-inject,
graceful shutdown — all of these can fail or be incomplete.
Recovery is opportunistic. The user can always *stop*; whether they
can pick up where they left off is a nice-to-have.

### 3. Truth beats polish.
A boring-but-accurate status line beats a fluent-but-fallible one.
Mechanical event extraction beats LLM narration when both are
available. The chatlog is mechanical for this reason; if we ever add
a synthesizer, it goes *alongside* the mechanical log, not on top of
it.

### 4. Defaults are opinionated.
"It should work for everyone" is not a goal. "It should work for the
netrunner" is. Defaults reflect the netrunner's actual workflow:
keyboard-first, one-handed, terminal-comfortable, hostile to
mouse-required UI, friendly to TOML editing.

### 5. Capability accumulates, complexity does not.
Adding a plugin = good. Adding a new abstraction layer that all
plugins must inherit from = bad unless overwhelmingly justified.
Surface area grows; conceptual core stays small.

### 6. The escape hatch is always one key away.
EJECT, Esc-to-up-one-level, Ctrl+Q. The netrunner should never feel
stuck. If a UI state can be entered, exiting it should be trivially
discoverable.

### 7. Persistent over ephemeral.
Logs, manifests, profiles, scripts, snapshots — all of these are
files on disk by default. Memory-only state is a smell unless
there's a specific reason (secret store, ephemeral session warming).
You should be able to kill the deck, walk away, come back tomorrow,
and find the work where you left it.

### 8. Quota-aware, not quota-paranoid.
The deck does not pre-throttle. User-set caps are a separate "if
it happens anyway, here's what to do" layer. Pre-throttling means
making a guess about what the netrunner can afford — and being
wrong in either direction is worse than just running and pausing
on cap-hit.

---

## When to add a feature

A new feature earns its keep when it answers *yes* to most of these:

- Does it reduce supervision overhead, or does it add to it?
- Can the netrunner ignore it when not needed? (Modal-on-demand vs
  always-on visual noise.)
- Does it compose with existing primitives, or does it require its
  own special-case logic?
- Does it survive a chat reset / session break / power loss?
- Could a 4B local model do it instead of burning Claude quota?
- Is the failure mode "small wasted spawn," or is it "unsafe and
  irrecoverable"?

If most answers are bad, the feature wants more design before it
wants implementation.

---

## When to defer a feature

A feature that's good but not now is one of these:

- **Hardware-blocked.** Wearable form factor, multi-radio topology,
  RK3588 NPU acceleration. Real, valuable, deferred.
- **Substrate-blocked.** Anything that needs local-model
  infrastructure before it can land. Workable on dev with Ollama,
  production with RKLLama; just not this week.
- **Use-case-blocked.** Compliance mode is real if and only if the
  deck does client engagement work. It's not blocked on
  architecture — it's blocked on *whether the use case actually
  exists for this user*.
- **Composition-blocked.** Plugin system can't really exist before
  the tool registry is real. Brake profiles can't really exist
  before profiles are real. Sequence the foundations first.

The build plan tracks which is which. When unsure, defer — the deck
is a hobby project; we have time.

---

## When to throw something out

The hardest discipline. If a feature ships and doesn't earn its keep
within a few sessions of use, kill it. Carrying around half-working
features is how decks become unmaintainable.

Specific heuristics for "this should die":

- The netrunner forgets it exists. → Wasn't pulling its weight.
- The netrunner avoids it because it's flaky. → Better gone than
  half-broken.
- It composes badly with something else we want to add. → Sunk cost
  is not a reason to keep it.
- It costs tokens or attention proportional to its value, where
  "value" is "I notice when it's missing."

---

## The aesthetic

This isn't strictly philosophy, but it shapes design choices, so
it earns a place here.

The deck is **cyberpunk in vocabulary, professional in execution.**
Netrunner, daemon, construct, watchdog, brake profile, quickfire,
airgap — these are not jokes. They're the right names for what these
things do, and they happen to come from a fictional aesthetic that
also genuinely captures the *feel* of operating a multi-agent system
under time pressure.

But the implementation isn't a costume. The deck doesn't bleep at
you, doesn't have neon Comic Sans, doesn't pretend to be from 2077.
It's a quiet TUI that does serious work and uses good names for what
it's doing.

The vibe is "your hacker friend's actual workstation," not "the lobby
of a Y2K-era cybercafe." When in doubt, lean toward Linux-y
seriousness over RGB-keyboard theatrics.

---

## What to do when this doc and the spec disagree

The spec wins on *what*; this doc wins on *why*. If the spec says
"build X" and this doc says "X violates principle Y" — pause. One of
them is wrong. Figure out which before building.

Most often, the resolution is that the spec is correct and the
philosophy needs an addendum to clarify why an apparent violation
isn't one. Sometimes the spec is wrong and needs revision. Either
way, "build it because the spec says so" is not a reason; "build it
because it serves the netrunner's leverage" is.

---

*Last updated: M5.3e + Phase A Steps 1-2 complete.
Revise when convictions shift; otherwise leave alone.*
