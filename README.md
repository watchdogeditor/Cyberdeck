# Cyberdeck
A keyboard-first TUI that orchestrates Claude Code subprocesses — a
personal AI cockpit that makes one human's attention go further than
it should.

A *daemon* coordinator decomposes goals; *constructs* execute in
parallel; a *watchdog* oracle answers questions about fleet activity.
The user — the *netrunner* — supervises through Textual.

> **Status:** active personal hobby project, in production use on
> Windows. Solo development; breaking changes are likely. No PRs
> solicited; issues welcome but the bar for changes is "does this
> serve the netrunner running it." Read the [design canon](#design-canon)
> before assuming any feature is stable.

<img width="1918" height="1078" alt="run in progress" src="https://github.com/user-attachments/assets/4b7d6122-8658-425d-aa1c-90f2be296556" />

---

## Contents

- [Why this exists](#why-this-exists)
- [What it is](#what-it-is)
- [Run it](#run-it)
- [Architecture](#architecture)
- [Design philosophy](#design-philosophy)
- [What this is not](#what-this-is-not)
- [Design canon](#design-canon)
- [Status, more concretely](#status-more-concretely)
- [License + contributing](#license--contributing)
- [Aesthetic](#aesthetic)
- [Screenshots](#screenshots)

---

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

## What it is

A Textual TUI that wraps Anthropic's [Claude Code](https://claude.com/claude-code)
CLI. Each agent ("construct") is a managed `claude -p` subprocess in
headless stream-json mode; the deck spawns them in parallel and
renders their event streams in dedicated panes. A persistent
coordinator ("daemon") plans the work and dispatches structured
spawn actions; a separate observer ("watchdog") answers questions
about what the fleet is doing without authority to act. Construct
caliber (model + effort) is daemon-decided per-task. Tool use is
gated by a deterministic PreToolUse hook ("brake") with three
tiers: paranoid / default / yolo.

The deck is keyboard-first by deliberate constraint. There's no
mouse-driven menu — every operation has a single keypress, and the
ones that matter most are reachable from any focus. A modal-heavy
flow keeps the main canvas focused on the work, not on chrome.

## Run it

**Prerequisites:**

- Python 3.11+ (3.14 tested)
- [Textual](https://textual.textualize.io/) — `pip install textual`
- [Claude Code CLI](https://docs.anthropic.com/en/docs/agents-and-tools/claude-code/quickstart) —
  `npm install -g @anthropic-ai/claude-code` + a logged-in Max
  account
- On Windows, run from Windows Terminal or PowerShell 7 (cmd.exe
  doesn't render the TUI cleanly)

**Commands:**

```bash
python tui.py                       # idle — set goal in-app with `e`
python tui.py "task A" "task B"     # ad-hoc constructs, no daemon
python tui.py --goal "..."          # daemon-driven mode
```

**Offline smoke test** (against a mock subprocess; useful for
development without burning real-claude tokens):

```bash
CLAUDE_BIN=./mock_claude.py python tui.py "task one" "task two"
```

**Smaller entry points:**

- `main.py` — one construct, plain console output
- `fleet.py` — multi-construct, plain console output

**First run:** the deck creates `cyberdeck-home/` next to the source
on first launch. Profiles seed automatically; brake state, limits,
and warm pool persist across restarts. Override the working
directory with `--home <path>` or `$CYBERDECK_HOME`.

## Architecture

Four runtime entities, each with one job:

- **The deck** (the TUI itself). Renders panels, dispatches actions
  on key presses, mounts modals.
- **The fleet.** N concurrent construct subprocesses. Crash
  isolation, hard-kill semantics via SIGTERM/SIGKILL on the PID.
- **The daemon.** A persistent Claude Code subprocess that decomposes
  goals into actions and dispatches them as JSON.
- **The watchdog.** An async question-queue oracle that answers human
  questions about fleet activity. Independent of the daemon; runs
  its own claude subprocess.

Plus three load-bearing supporting subsystems:

- **The spine** (`event_bus.py`). All subsystems publish through one
  canonical bus; consumers subscribe with role-derived filters.
  Single source of truth for chatlog rendering, watchdog Q&A
  context, and the per-launch NDJSON log.
- **The brake** (`brake_hook.py`). Deterministic PreToolUse hook
  invoked by Claude Code per tool call. Three tiers: paranoid (most
  side-effect tools denied), default (destructive bash + OS-root
  paths denied), yolo (no hook installed). Pattern-based; no LLM in
  the hot path.
- **The mechanic** (`mechanic.py`). Sibling supervisor process that
  watches the deck PID and cleans up orphan claude subprocesses on
  detected deck death. Cross-platform stdlib + ctypes; no claude
  dependency.

`Design Files/cyberdeck-spec.md` is the canonical reference if any
of this needs more depth.

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

## Design canon

The full design lives in `Design Files/`. If you're starting fresh,
read in this order:

1. **`cyberdeck-claude-code-orientation.md`** — institutional knowledge,
   hard-won rules, file map, gotchas. *Read first if you're touching
   the code.*
2. **`cyberdeck-state.md`** — current state, design decisions, filed
   gotchas (cumulative). *Read first if you're trying to understand
   why something is the way it is.*
3. **`cyberdeck-build-plan.md`** — milestone status, what's next,
   what's deferred and why. *Read first if you're trying to
   understand what's coming.*
4. **`cyberdeck-spec.md`** — canonical architecture (the *what*).
   *Read first if you're trying to understand what the deck does
   structurally.*
5. **`cyberdeck-philosophy.md`** — convictions that resolve
   ambiguity (the *why*). *Read first if you're disagreeing with a
   design decision and want to understand the reasoning.*

In-flight design docs (`Design Files/in-flight/`):

- `cyberdeck-maintbot-design.md` — the mechanic (separate-process
  supervisor + on-demand LLM session triage)
- `cyberdeck-model-effort-design.md` — caliber (per-spawn model +
  effort + fast-mode)
- `cyberdeck-spawn-context-isolation.md` — per-role CLAUDE.md /
  auto-memory suppression
- `cyberdeck-keymap-revision.md` — three-layer keymap revision
- `cyberdeck-collections-intake-design.md` — recipe-driven plugin
  scaffolding for github-distributed reference collections
- `cyberdeck-tools-default-kit.md` v2 — opinionated default tools
  for a fresh deck

Shipped designs (kept for provenance) live in `Design Files/archive/shipped/`:
event stream design, tools/plugins/profiles retool, tools research
seed + report. The deferred wearable variant is in
`Design Files/archive/deferred/`. The deck's full file inventory
is in `Design Files/INDEX.md`.

## Status, more concretely

- **Active solo development.** One netrunner, one developer, both
  the same person. Issues welcome; PRs not solicited because the
  bar for changes is "does this serve the netrunner running it"
  and only one person can answer that.
- **Windows-first.** Tested on Windows 11 + Windows Terminal +
  PowerShell 7. Linux/macOS *should* work (no platform-specific
  imports in core paths) but rough edges aren't polished out yet —
  the eventual Pi-class deployment will fix that.
- **Breaking changes likely.** The deck is in active production use
  but the architecture is still evolving. Recent slices (caliber,
  tools-plugins-profiles retool, spine) reshaped the codebase; more
  reshaping is queued. Pin a commit if you're forking; don't expect
  the next pull to be drop-in.
- **No release cadence.** Development is goal-driven, not
  release-driven. The git log is the changelog.
- **No telemetry.** The deck phones home only to Anthropic for
  claude-code subprocess work. Everything else stays local.

## License + contributing

No license file yet — defaulting to "all rights reserved" until I
think about it more deliberately. If you want to use any of this in
your own project, [open an issue](https://github.com/watchdogeditor/Cyberdeck/issues)
and we'll talk.

For contributing: see the status note above. The bar is netrunner
utility; PRs that don't serve that aren't going to land. Issues
documenting bugs you've hit on real-deck use, or design questions
that would help the canon docs improve, are genuinely welcome.

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

## Screenshots
<img width="1911" height="1078" alt="details" src="https://github.com/user-attachments/assets/a3dd3a36-b836-4a84-89fb-580060151613" />
<img width="1917" height="1071" alt="profile view" src="https://github.com/user-attachments/assets/10595854-791d-445c-9662-3f2edc1c89f7" />
<img width="1918" height="1078" alt="plugin advisor" src="https://github.com/user-attachments/assets/3855244b-95f6-4411-89be-9de2bbdde963" />


