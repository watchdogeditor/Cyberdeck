# Cyberdeck — Project Instructions

These are instructions for Claude working on the Cyberdeck project.
Pair these with `cyberdeck-spec.md` (the current design state) and
the source files. The spec tells you *what the system is*; these
instructions tell you *how to think about it* and *how to work with me*.

---

## What we're building

Cyberdeck is a personal, hobbyist, one-human-one-user multi-agent
orchestration client, inspired by netrunner interfaces in Cyberpunk
Edgerunners. The thesis: **the "multiple windows with stuff happening"
netrunner fantasy is achievable now, minus the brain interface. What's
missing isn't compute or model capability; it's *supervision
bandwidth*.** One human can spawn twenty agents trivially. One human
cannot watch twenty agents trivially. The deck's job is to make human
attention go further than it should.

Single-line pitch: **express intent, delegate mechanics.** The UI is
excellent at capturing what the user wants. Agents figure out how.

It is explicitly *not* a product. Not for sale, not for adoption. May
land on GitHub if I feel like it. Opinionated defaults are fine.
Configuration via TOML/code-edit is fine. If something breaks because
I configured it weirdly, that's on me.

Windows development, Linux deployment target (eventually a Pi-class
SBC in some portable chassis — the "deck" part of the cyberdeck).
Software first, hardware later.

---

## Vocabulary (use this consistently)

- **Netrunner** — the human operator (me).
- **Daemon** — the always-running coordinator. Takes the user's goal,
  decomposes it into subgoals, spawns constructs, routes messages,
  holds canonical intent. Named per cyberpunk canon ("slightly
  self-aware software that runs other code in response to conditions").
- **Construct** — a task-focused worker with its own context window
  and scoped tool access. One subgoal per construct. Hermetic by
  default; explicit wiring between specific constructs allowed.
- **Watchdog** — observational role. Reads all construct and daemon
  streams, authors tripwires at spawn/intent-shift time, fires alerts
  on matches. Does not act directly; routes events to the daemon or
  surfaces them to the user.
- **Tripwire** — deterministic rule authored by a model, executed by
  a deterministic matcher (regex, grammar, timers). LLM authors the
  rules; no LLM in the hot path of detection.
- **Wiring** — explicit directed channel between two constructs
  (default: they can't see each other's work).
- **Brake profile** — named risk/confidence policy: Paranoid /
  Default / YOLO. Maps to Claude Code `--permission-mode` values:
  `default` / `acceptEdits` / `bypassPermissions`.
- **Inject** — user input delivered to a construct. Two modes:
  `inject-and-interrupt` (kill current work, steer, resume with
  summarized context) and `queue-inject` (deliver at next break,
  notify daemon).
- **Fleet** — the M1 abstraction for "N constructs running side by
  side." Eventually becomes part of the daemon's construct-management
  layer. For now it's a thin orchestration class.

---

## Architecture, fast

- **Subprocess per construct.** OS-level isolation. Crash of one
  doesn't take down the deck. Hard-kill actually works (SIGKILL the
  PID). Per-PID resource accounting via `/proc`.
- **Claude Code as the primary construct backend.** Headless mode
  (`claude -p <task> --output-format stream-json --allowedTools ... 
  --permission-mode ...`). Uses my Max subscription — no API spend. 
  The Agent SDK gives us the full agentic loop with file ops, Bash,
  and web search as built-in tools. Session resume supports
  inject-and-continue semantics.
- **Local model (Ollama, 7B-ish) is load-bearing, not optional.** Carries
  the ~70% of calls that don't need frontier capability: concierge,
  classification, summarization, tripwire authoring, watchdog prose
  fallback. Required for boot — the deck should work offline and
  escalate to remote only when tasks earn it.
- **OpenRouter** (optional, small credit balance) for non-Claude
  models when a specific task benefits.
- **NO unofficial programmatic access.** No session-token tricks, no
  scraped web endpoints. Claude Code SDK is the sanctioned path.
  Anything else is TOS violation and ban risk.
- **Python.** "The language that can do anything, badly." Plugin
  ergonomics beat everything else for a project that grows by
  accretion. The deck's capability ceiling is the union of its
  plugins.
- **asyncio + subprocess** today. Single-process orchestration. The
  spec says ZeroMQ for inter-role IPC; that gets introduced when the
  daemon and watchdog split into their own processes. Not yet.
- **Textual** for the TUI.
- **NDJSON append-only logs** for run records. Fields: `run_id, ts,
  construct_id, kind, payload`. `kind` ∈ `{event, meta}`. Foundation
  for annotated post-mortem inspection.

---

## Supervision model (the non-obvious parts)

- **Autonomy is a function of confidence × action risk, not confidence
  alone.** Models are frequently most confident exactly when wrong.
  A pure confidence threshold will eventually green-light a 0.97
  disaster. Use structural limits: per-action-type risk classes
  (read → auto, write → confirm, irreversible → always confirm,
  network/credential → always confirm).
- **Tripwires: LLM authors, deterministic engine enforces.** Watchdog
  uses model capability to *write* rules ("expect 'Tests passed',
  fire on `/error|FAIL|panic/i`, silent > 120s → alert") but never
  puts the LLM in the detection hot path. Sub-ms reaction, no
  hallucination risk, rules are inspectable artifacts that can be
  edited and versioned.
- **Daemon and watchdog are separate roles.** The entity noticing
  problems should not be the entity graded on whether its plans
  work. Can be collapsed into one process for cheap tasks where the
  tradeoff is acceptable; separate by default.
- **Severity hints flow from daemon → watchdog at delegation time.**
  Daemon marks a step "blocker-critical" or not, so watchdog has
  priors rather than inferring severity from symptoms alone.

---

## UI/UX principles (non-negotiable)

- **Keyboard-first. One-handed operable on a proper QWERTY.** No
  voice-as-primary. The chassis is arm-carry, so the input model
  must work with one hand.
- **Modal navigation (vi-style).** NAV mode: single unmodified keys
  do things (`1..9` jump to construct, `i` inject, `k` kill, `z`
  undo, `/` fuzzy-jump, `g/d/w` jump to goal/daemon/watchdog). INPUT
  mode: normal typing. Mode indicator always visible.
- **Sub-50ms input latency.** No model in the hot path of any
  keypress. Acks are synchronous; model work happens out of band.
- **Input history is sacred.** Up-arrow recall, fuzzy recall of prior
  intents. Re-issue is the single highest-leverage UX affordance.
- **No confirmation dialogs.** Use undo, or delayed-commit (visible
  cancel for ~1.5s), for anything reversible-ish. Confirmations
  train users to ignore them.
- **Two-layer goal pane.** Top: your goal (source of truth, what you
  typed). Bottom: current subgoal (read-only, daemon's current step
  in its decomposition). Glance check for drift.
- **Inject-and-interrupt vs queue-inject are DIFFERENT verbs.** Don't
  collapse them into one. "STOP, pivot" and "FYI for next turn" are
  genuinely different and deserve different affordances.
- **Kill is sacred.** SIGTERM → SIGKILL escalation. The big red
  button must always work. Never gate kill behind confirmation.

---

## Build state

This section used to track milestone status inline, but it drifted
behind reality every time the deck shipped a feature. Live state
lives in two docs that get updated alongside code:

- `cyberdeck-state.md` — what's shipped, design decisions carried
  forward, the cumulative gotchas list.
- `cyberdeck-build-plan.md` — milestone status, what's next,
  what's deferred and why.

Read those for the current snapshot. The orientation doc
(`cyberdeck-claude-code-orientation.md`) has the file-by-file map
of the codebase.

**Running state, briefly (specific to running the deck):**

- `python tui.py` is the entry point. `--goal "..."` for daemon mode,
  positional args for ad-hoc constructs, plain launch for idle. See
  the README and the orientation doc for details.
- `CLAUDE_BIN=./mock_claude.py python tui.py ...` for offline
  testing against the mock fixture.
- Real runs require `npm install -g @anthropic-ai/claude-code` and
  a logged-in Max account.

---

## Known tech debt (don't silently inherit)

From the first code-quality critique pass on `construct.py`:

- ✅ `events()` conflated streaming with lifecycle finalization —
  fixed (split into `events()` + `wait()`).
- ✅ `spawn()` slices `_build_command()[1:]` to splice in the resolved
  binary path — fixed (`_build_command(claude_bin)` takes the resolved
  path as a required arg).
- `kill()` sets `state = KILLED` *before* confirming termination.
  Should transition only after the process is confirmed dead.
- `tools` default hardcoded in `__init__` signature → should live in
  a module-level `DEFAULT_TOOLS`.
- `classify_event` kind values are bare strings across the codebase.
  Should be an enum/constants to prevent typos in downstream switch-
  style consumers (watchdog, tripwire DSL).

Open research items:

- **Thinking blocks render empty in real Opus 4.7 stream-json.**
  Classifier identifies the kind correctly but content extraction
  returns empty. Field name? Redaction? Missing flag? TBD.
- **Tripwire DSL design** is unblocked now that we have real
  stream-json event shapes in hand.

---

## How I like to work (collaboration notes)

- **I'm a writer and hacker.** Technological literacy is not a
  bottleneck. Skip the tutorial voice, assume I can read code.
- **Banter is welcome.** Cyberpunk framing is on-thesis. Don't
  hedge unnecessarily. Push back when architecturally correct.
- **Ship, break, fix, repeat.** Prefer running code over perfect
  architecture. A working thing with known debt beats a perfect
  thing that's still on a whiteboard.
- **Discipline matters too.** When I say "fix the dumb thing and
  keep moving," I mean *that* thing, not adjacent cleanups. Note
  the rest, don't scope-creep.
- **Don't flatten critique into politeness.** If an idea has holes,
  say so. I'd rather catch a design flaw now than six files later.
- **Concrete > abstract.** A five-line code sketch lands better
  than three paragraphs of theory.
- **Keep responses proportional.** Early-phase brainstorming wants
  long threads; mid-build wants tight operational answers. Read
  the moment.
- **Update the spec and these instructions when major decisions
  change.** Don't let design drift live only in chat history.

---

## Anti-patterns to watch for

- **Scope creep across milestones.** M2 is rendering only. M3 is
  keyboard. Don't slide daemon logic into M2 because it'd be "easy."
- **Premature abstraction.** We designed for "backend-swap via API
  call structure" but the first real implementation is specifically
  Claude Code headless. Don't pre-build a plugin registry before it
  has two plugins.
- **Learned when hand-edited works.** Router config is a TOML table,
  not an ML system. Tune by hand, log outcomes, only graduate to
  learned if data warrants.
- **Confidence-gated autonomy.** Always pair confidence with action
  risk. Self-reported confidence is a liar.
- **Over-trusting real-time output for classification.** Structured
  status channels beat regex-on-prose. If we need to know a
  construct's phase, have it emit phase explicitly.
- **Collapsing inject-and-interrupt with queue-inject.** They are
  different verbs.
- **Putting a model in a latency-critical path.** Hotkeys, tripwire
  matching, state updates are all deterministic.

---

## When in doubt

- Check `cyberdeck-spec.md` for the current design state.
- Check source files for current implementation reality.
- If spec and code disagree, code wins (and we update the spec).
- If a decision feels underdetermined, prefer the path that keeps
  local-first, hobbyist-scale, quota-aware, and operable with one
  hand.
