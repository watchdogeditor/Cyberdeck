# Cyberdeck

A keyboard-first TUI that orchestrates Claude Code subprocesses.

A *daemon* coordinator decomposes goals; *constructs* execute in
parallel; a *watchdog* oracle answers questions about fleet activity.
The user — the *netrunner* — supervises through Textual.

Personal hobby project. Currently in active production use on Windows.
~12k LOC across 13 modules. Targets Pi-class hardware eventually.

## Contents

- [Why this exists](#why-this-exists)
- [Design philosophy](#design-philosophy)
- [What this is not](#what-this-is-not)
- [Architecture, briefly](#architecture-briefly)
- [Status](#status)
- [Running](#running)
- [Documentation](#documentation)
- [Aesthetic](#aesthetic)

## Why this exists

The netrunner fantasy from *Edgerunners* — multiple windows, each
making progress on something, a human steering the whole thing — is
achievable now, minus the brain interface. What's missing isn't
compute or model capability. **It's supervision bandwidth.**

One human can spawn twenty agents trivially. One human cannot *watch*
twenty agents trivially. Chat interfaces are excellent for one-on-one
collaboration but break down when work fans out, when several
conversations are happening at once, when the human's attention has
to triage rather than dwell. The cyberdeck's job is to close that
gap — not by making the human more attentive, but by making attention
itself more leverageable.

In one sentence: **a personal AI cockpit that makes one human's
attention go further than it should.**

## Design philosophy

The deck rests on three load-bearing theses.

**Express intent, delegate mechanics.** The UI is excellent at
capturing what the netrunner wants. Mechanics — finding the API,
writing the script, executing the call — are the agents' problem.
Every keypress, every modal, every default is judged by how cleanly
it lets the netrunner say what they mean. If something forces the
human to think about implementation plumbing instead of intent, it's
broken.

**Capability accumulates.** Anything the netrunner does well once
should become a saved tool the deck can do cheaply forever. Plugins,
scripts, profiles — all addressable, all persistent, all invocable by
future agents. The deck is a *personal capability library that grows
with use.* It is never finished; it grows arms.

**Supervision is structural, not aspirational.** The deck does not
assume the netrunner is paying attention. It assumes attention is
finite, intermittent, and hard to recover. Risk is gated by action
class, not model confidence (models are most confident exactly when
they're wrong). Tripwires are deterministic matchers, not LLM
judgments (auditable, sub-millisecond, no hallucination risk). Roles
are separated by concern: the entity noticing problems must not be
graded on whether its plans work — the daemon plans, the watchdog
observes. The netrunner is in the loop, but the deck is structured so
that *not being in the loop* fails safely.

## What this is not

- **Not a multi-user system.** One netrunner, one deck. No accounts,
  no permissions, no shared state.
- **Not a chat interface.** The deck contains a chat interface (the
  daemon pane), but the deck itself is not a chat. Goal-setting is
  one keypress; injection is one keypress; the rest is happening in
  parallel without conversational turn-taking.
- **Not autonomous.** Even when constructs work in parallel, the
  daemon plans, the netrunner steers, and the watchdog observes. The
  deck is a force-multiplier on a human, not a replacement.
- **Not a product.** Personal hobby project. Polish where it matters
  for the netrunner's experience; skip polish where it would only
  matter for adoption.

## Architecture, briefly

Four runtime entities, each with one job:

- **The deck** (the TUI itself). Renders panels, dispatches actions
  on key presses, mounts modals.
- **The fleet.** N concurrent construct subprocesses. Crash
  isolation, hard-kill semantics via SIGKILL on the PID.
- **The daemon.** A persistent Claude Code subprocess that decomposes
  goals into actions and dispatches them as JSON.
- **The watchdog.** An async question-queue oracle that answers human
  questions about fleet activity. Independent of the daemon; runs its
  own claude subprocess.

Each construct is a managed `claude -p` subprocess in headless
stream-json mode, scoped via `--allowedTools` and `--permission-mode`.
The daemon picks a profile (TOML config defining tools, system prompt
addendum, brake tier) at spawn time. The watchdog reads the same
event stream the netrunner does and reasons over it without authority
to act.

`Design Files/cyberdeck-spec.md` is the canonical reference if any of
this needs more depth.

## Status

Tier 1 (constructs, fleet, daemon-driven goals, EJECT, limits) and
Tier 2 (profiles, brake tiers, hot-reload registry) are shipped.
Watchdog Q&A oracle, daemon chat, mid-flight goal edits, connection
monitor, and spawn-origin badges are all in production. Up next:
plugin scaffolding, connection consequences, local-model substrate.

See `Design Files/cyberdeck-state.md` for the full state snapshot and
`Design Files/cyberdeck-build-plan.md` for what's next.

## Running

```bash
python tui.py                    # idle — set goal in-app with `e`
python tui.py "task A" "task B"  # ad-hoc constructs, no daemon
python tui.py --goal "..."       # daemon-driven mode
```

For offline smoke testing against a mock subprocess:

```bash
CLAUDE_BIN=./mock_claude.py python tui.py "task one" "task two"
```

Real runs require `npm install -g @anthropic-ai/claude-code` and a
logged-in Max account. On Windows, run from Windows Terminal or
PowerShell 7 — old cmd.exe doesn't render the TUI cleanly.

Smaller entry points: `main.py` (one construct, console),
`fleet.py` (multi-construct, console output).

## Documentation

The design canon lives in `Design Files/`. Read in this order if
starting fresh:

1. `cyberdeck-claude-code-orientation.md` — institutional knowledge,
   hard-won rules, file map, gotchas
2. `cyberdeck-state.md` — current state, design decisions, filed
   gotchas (cumulative)
3. `cyberdeck-build-plan.md` — milestone status, what's next, what's
   deferred and why
4. `cyberdeck-spec.md` — canonical architecture (the *what*)
5. `cyberdeck-philosophy.md` — convictions that resolve ambiguity
   (the *why*)

`cyberdeck-project-instructions.md` covers collaboration norms;
`cyberdeck-tools-research-seed.md` is a stub for a future
tools-research conversation; `cyberdeck_arbiter_design.md` is a
deferred wearable-form-factor variant.

## Aesthetic

The deck is cyberpunk in vocabulary, professional in execution.
*Netrunner*, *daemon*, *construct*, *watchdog*, *brake profile*,
*quickfire*, *airgap* — these are not jokes. They are the right names
for what these things do, and they happen to come from a fictional
aesthetic that genuinely captures the *feel* of operating a
multi-agent system under time pressure.

But the implementation isn't a costume. The deck doesn't bleep at
you, doesn't have neon Comic Sans, doesn't pretend to be from 2077.
It's a quiet TUI that does serious work and uses good names for what
it's doing. The vibe is "your hacker friend's actual workstation,"
not "the lobby of a Y2K-era cybercafe."
