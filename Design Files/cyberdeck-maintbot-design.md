# Cyberdeck — Maintbot / Mechanic Design

*Architecture for the deck's supervisor / repair process. Separate
from the deck itself; outlives crashes; hooks into the deck's UI
when both are running. Captures decisions made 2026-04-29 so the
eventual implementation has a starting point.*

*Pair with `cyberdeck-spec.md` (the deck's runtime architecture),
`cyberdeck-philosophy.md` (separation-of-concerns reasoning),
`cyberdeck-state.md` (current status), and
`cyberdeck-build-plan.md` (where this slots in the priority order).*

**Naming note (2026-04-30):** netrunner has been calling this "the
Mechanic" in conversation; "maintbot" was the original placeholder.
File name keeps `maintbot` for now to avoid a touch-storm rename;
new content uses "Mechanic" as the canonical name. Resolve fully at
implementation time.

**Architectural shift (2026-04-30):** the design originally framed
this as a single LLM-backed process that fires on demand. Real-deck
testing of Phase 7's file logger surfaced a concrete need —
subprocess cleanup when the deck dies — that pointed at a
two-tier structure where a lightweight always-on **supervisor**
half does PID tracking + heartbeat + cleanup-on-death, and a
heavy on-demand **LLM session** half handles diagnosis. See "Two-tier
structure" below.

---

## Why a separate process

The deck has four runtime roles: netrunner, daemon, construct,
watchdog. None of them operates on **deck infrastructure itself** —
they all operate on the world the deck is reasoning about. When the
Python interpreter crashes, when an unhandled exception unwinds the
TUI, when the OS kills the process, when something in the wiring is
wedged in a way that the deck can't introspect from inside its own
event loop — none of the existing roles can help, because they're
running *inside* the thing that broke.

The maintbot is the missing role: an LLM-backed supervisor that
operates **on the deck**, not on the world. It reads logs, knows the
deck's source tree, understands its config files, can diagnose Python
tracebacks and propose fixes. Its access pattern is exactly inverted
from constructs (which can write the world freely under brake but
cannot touch deck source) — the maintbot can read deck source and
state but does not touch the world.

It is **not a deck role**. It is a separate process. The deck has no
idea it exists unless the deck itself launched it. This matters
because when the deck dies, the maintbot is unaffected — it is the
*outside* of the deck, by design.

---

## Two-tier structure: supervisor + LLM session

The Mechanic is one process with two responsibilities at very
different cost profiles. The architectural insight (filed
2026-04-30 from real-deck Ctrl+C debugging): split them and the
process can be always-on without burning context.

### Supervisor half — always-on, no LLM, ~no cost

Lightweight Python: watches the deck's PID, maintains a list of
the deck's child subprocess PIDs, performs cleanup when the deck
dies. Doesn't talk to claude. Doesn't read logs (yet). Memory
footprint is the Python interpreter's own ~30MB resident; CPU is
near-zero between heartbeat ticks.

Concrete responsibilities:

- **PID tracking.** The deck publishes its child claude subprocess
  PIDs (constructs, daemon, watchdog Q&A subprocess, watchdog
  authoring one-shots, pool warming subprocesses) to a place the
  supervisor can read. See "PID publish channel" below.
- **Heartbeat.** Polls the deck's PID at a slow cadence (every few
  seconds is plenty). When the PID disappears or its exit code is
  non-zero / missing, the supervisor knows the deck is dead.
- **Cleanup.** On detected deck death, kills every tracked
  subprocess via cross-platform Python (`os.kill(pid, signal.SIGTERM)`
  with a short grace, then `signal.SIGKILL`; or `psutil.Process(pid).
  kill()` if psutil is on hand). Any platform that runs Python runs
  this. No CREATE_NEW_PROCESS_GROUP, no Job Objects, no
  PR_SET_PDEATHSIG — just "read PIDs, send signals, walk away."
- **Optional auto-relaunch (v3+).** Deferred indefinitely; the
  supervisor does NOT relaunch the deck in v0/v1/v2. It cleans up
  and either stays running (interactive: waiting for the next
  manual deck launch) or fires the LLM session for triage
  (heartbeat-fired path).

Why this is the right shape: the netrunner's earlier framing —
"when idle, the bot's not running or spun up — it only creates a
session on demand" — was specifically about the **LLM session**,
not the supervisor process. The supervisor is what's always-on; the
LLM session is the lazy half.

### LLM session half — on-demand, claude-backed, real cost per fire

Spins up only on:
- **Heartbeat-fired triage**: deck died uncleanly → supervisor
  cleaned up subprocesses → LLM session reads the log file +
  Python traceback, summarizes what happened, surfaces it in a new
  wt window
- **Deliberate summon**: netrunner opened the Mechanic from the
  deck's UI to ask "what's going on" or "clean up old logs"

Same tooling profile as before (read deck source, read logs, no
write to the world). Cost profile: a few thousand tokens per fire
on cloud Claude; potentially free on D1 substrate. Once the session
ends, the supervisor half stays running and the LLM context closes.

### Why this matters for cross-platform reach

The OS-level mechanisms for "clean up child processes when parent
dies" are platform-specific:

- Windows: Job Objects with KILL_ON_JOB_CLOSE flag, ~80 LOC ctypes
- Linux: `prctl(PR_SET_PDEATHSIG, SIGKILL)` per child, OR process
  group leader pattern with signal forwarding
- macOS: similar to Linux

Routing this through the **supervisor** instead of the OS kernel
means one Python implementation works on every platform Python
runs on. The deck's spawn sites need only one tiny change:
publish the spawned PID to the supervisor's tracking channel.
No platform-conditional `creationflags` parameter, no `pywin32`
dependency, no `prctl` ctypes calls.

This composes cleanly with the "Pi-class deployment" eventual
target: the supervisor's Python binary runs unchanged from
Windows-laptop to ARM-Linux-board.

### PID publish channel

The deck has to communicate "here's a new child PID" / "this PID
finalized" to the supervisor. Two reasonable options:

- **Option A: enrich the file log.** Add a `pid` field to the
  `fleet.spawn` event payload (one-line change at each spawn
  site). The supervisor reads `<deck>/logs/latest.log` (or tails
  it), walks forward, builds a list of "spawned but not finalized"
  PIDs. No new infrastructure — the file logger already exists
  (Phase 7a) and the supervisor will read it for triage anyway.
- **Option B: dedicated pidfile.** Deck writes
  `<deck>/.cyberdeck/active-pids-<deck_pid>.json` listing live
  child PIDs, updates on every spawn/finalize. Smaller surface,
  no log parsing required.

Lean toward Option A — the file logger is the canonical "what's
happening on the deck" surface; deriving live PIDs from spawn-
without-matching-finalize keeps everything in one place. Adding
the `pid` field to FleetEvent is small and additive.

---

## Process topology

Two modes, depending on how the deck got launched.

### Headless / wall-mount mode

Maintbot is the top-level process. Deck runs as its child. This is
the eventual deployment shape — a Pi-class deck running scheduled
work unattended, with the maintbot as its supervisor.

```
maintbot (top level, supervisor, owns terminal)
  │
  └── deck (child process)
        ├── claude subprocesses (constructs)
        ├── claude subprocesses (daemon)
        └── claude subprocesses (watchdog)
```

The maintbot owns the terminal; the deck inherits it (or runs
detached and writes its UI to a separate display surface, depending
on the form factor). When the deck crashes or exits uncleanly, the
maintbot is still running and can:

- Read the deck's last log file directly
- Read whatever Python wrote to its stderr
- Inspect the deck's exit code
- Decide whether to relaunch, triage, or wait for the netrunner

### Interactive / desk mode

Deck is the top-level process (current behavior, what `launch.bat`
runs today). The Mechanic supervisor runs alongside as a separate
sibling process — replaces the original "heartbeat sensor sidecar"
framing. Same single process throughout the deck's lifetime:

```
deck (TUI, top level)
  ├── claude subprocesses (constructs / daemon / watchdog)
  └── (siblings — launched by launch.bat or supervisor itself)

mechanic (sibling supervisor process — always on)
  ├── PID tracking + heartbeat (always running)
  └── LLM session (spawned on-demand: heartbeat-fired or summoned)
```

Note the heartbeat sensor isn't a separate process anymore — it's
the supervisor half doing its always-on job. When the supervisor
detects deck death, it cleans up subprocesses synchronously (no
LLM call) and then optionally fires the LLM session in a new wt
window for triage. When the netrunner deliberately summons the
Mechanic for "what's going on" / "clean up old logs," the
supervisor's LLM session activates against the same process.

Launch sequence options:

- **Side-by-side**: `launch.bat` starts the deck and the Mechanic
  supervisor as parallel processes. Simplest; both die / both
  restart together.
- **Supervisor-first**: the Mechanic supervisor launches the deck
  as its child. Closer to headless mode but interactive deck has
  a TUI so the supervisor doesn't own the terminal — it would have
  to spawn the deck detached. Deferred; side-by-side is simpler.

### Activation paths

Three paths, but only the **LLM session** has an "activation" — the
supervisor process is always running:

| Path | Trigger | Activates | Deck state |
|---|---|---|---|
| **Headless** | Mechanic launches deck | Supervisor + LLM both up | Wrapped subprocess |
| **Heartbeat-fired** | Deck unclean exit | Supervisor cleans up, fires LLM in new wt window | Already dead, subprocesses killed |
| **Deliberate summon** | Netrunner menu / keybind | LLM only | Healthy, may keep running |

In all three, the **supervisor** is always running (or always
running once the supervisor first comes up). Cleanup happens
synchronously without any LLM cost. The LLM session is the
expensive part and stays lazy.

---

## What the maintbot can see

The maintbot's access is inverted from constructs:

| Resource | Constructs | Maintbot |
|---|---|---|
| The world (network, filesystem, plugins) | Yes (under brake) | No (out of scope) |
| Deck source tree (`*.py`) | Read denied via brake hook | Read allowed |
| Deck logs (`<deck>/logs/*.log`) | Read denied via brake hook | Read allowed |
| Deck state files (`<home>/.cyberdeck/*`) | Read denied via brake hook | Read allowed |
| Python tracebacks (post-mortem) | N/A — they're dead by then | Yes |
| Live deck process (when deck is alive) | N/A | Indirect — via shared logs only |

The asymmetry is the whole point. The maintbot is *for* deck
infrastructure; constructs are *for* the world. Neither needs the
other's access.

The logs directory at `<deck source>/logs/` is the **shared
interface**. It lives outside `cyberdeck-home/` deliberately — that
keeps it outside what constructs can touch (brake hook protects deck
source) and inside what the maintbot can read by default. Per-launch
log files with timestamps + a `latest.log` pointer mean the maintbot
can locate the relevant file in O(1): when the heartbeat fires, the
maintbot reads `latest.log`, parses the header for context, scans
for ERROR/CRITICAL lines, and pulls the last N INFO lines before
the death point — all *before* spending tokens on the LLM call.

---

## What the maintbot can do

### v0 — supervisor only (no LLM session yet)

Concrete, narrow, ships first. The Mechanic process exists, runs
the supervisor half, does no LLM work at all. Specifically:

- Reads `<deck>/logs/latest.log` (or polls a small `active-pids`
  file — see PID publish channel above) to track live subprocess
  PIDs
- Polls the deck's PID at low cadence
- On deck death (any cause: clean exit, EJECT, crash, OOM, kill -9
  from Task Manager, blue screen): kills every tracked subprocess
  via cross-platform Python signals
- That's it. No diagnostics, no LLM, no UI. Headless, reliable,
  fast.

This v0 alone solves the concrete problem real-deck testing
surfaced: when the deck dies, claude subprocesses currently orphan
on Windows. The supervisor cleans them up. Cross-platform from
day one, no Job Object plumbing in the deck itself.

Implementation cost: ~150 LOC of Python (PID tracking, signal
loop, cross-platform signal dispatch). No claude dependency. Can
ship without D1, without the file logger being feature-complete,
without anything else queued.

**This is the right v0 because the netrunner-immediate value is
real (no orphaned claude subprocesses → no zombie Anthropic
sessions → no surprise quota burn) and the implementation is
small + isolated + testable. The LLM-backed half can land later
when the cost profile is better understood.**

### v1 — diagnose-only LLM session

The first LLM-backed version is read-only. Given a crashed deck
(or a healthy one, when summoned deliberately), it can:

- Summarize the crash: what was the deck doing, what blew up,
  what's the Python traceback or exit code
- Quote relevant chatlog markers (the chatlog buffer goes to the log
  per the logger overhaul) so the netrunner sees what they would
  have seen if they'd been watching
- Propose plausible causes: "this looks like the wedge from the
  filed gotchas list under <topic>"
- Suggest next steps: "try relaunching with `--no-streaming` and
  reproduce" or "the watchdog session_id capture didn't fire on
  startup; the streaming subprocess may have died early"

The value here is "you don't have to copy the log to another
machine and ask the AI for help." The maintbot reads what's there
and tells you. No autonomy, no automatic fixes, no surprises.

### v2 — guided correction

Once v1 is trusted, the maintbot can be allowed to **propose** fixes
the netrunner approves with one keystroke. Examples:

- "I see your tripwire registry got corrupted; want me to clear it?"
- "Old logs are eating disk; want me to compress / delete entries
  older than 30 days?"
- "Your default profile has a syntax error; want me to revert to
  the auto-seeded version?"

Every action is netrunner-approved. The maintbot does not act on
its own.

### v3 — autonomous correction (deferred indefinitely)

In headless mode, the maintbot needs the option of relaunching the
deck after a crash without netrunner intervention. This is real
autonomy, and it requires careful policy: how many crashes before we
stop relaunching? What if the crash is in a config the maintbot
itself wrote? Defer this until v1 + v2 have given us enough trust
data to make that policy real.

---

## Exit-code → "is this unclean?" decision tree

The heartbeat sensor needs a deterministic answer to "did the deck
die or did it quit?" After the **logger + quit discipline** slice
lands (paired follow-up to slice 2), the answer is:

| Signal | Classification |
|---|---|
| Exit code `0` | **Clean** — normal quit (Ctrl+Q from idle, or program terminated normally) |
| Exit code `0` + `latest-eject-*.json` snapshot present | **Clean (deliberate)** — netrunner EJECTed; snapshot is the autopsy artifact |
| Any other exit code | **Unclean** — fire maintbot |
| PID disappears with no exit code | **Unclean** — fire maintbot (process killed externally / OS killed it / segfault) |
| Exit code `130` (SIGINT) | **Unclean** — should be impossible after Ctrl+C swallow lands; if seen, our SIGINT handler failed |

The Ctrl+C swallow is part of the **deck quit discipline** slice.
Until that lands, exit code 130 has to be tolerated as
possibly-clean (the netrunner might have hit Ctrl+C reflexively
trying to copy text); after, it becomes a clear unclean signal.

EJECT specifically writes a flag into the snapshot file
(`{"reason": "eject", ...}`) so the maintbot can distinguish
"netrunner punched out deliberately" from "deck crashed and happened
to leave a snapshot." Different triage path: deliberate-eject reads
the snapshot for autopsy on demand; crash auto-fires the maintbot
even though the snapshot exists.

---

## Substrate

Same trajectory as the watchdog: cloud Claude in v1, D1 (local model
runtime) eventually. The cost profile is fundamentally different
from the daemon though — the maintbot fires roughly **once per
crash**, not per turn. A few thousand tokens per fire is fine on
cloud quota even for heavy users.

Where the maintbot becomes expensive is headless mode running 24/7:
even an idle process that spawns a session on every interesting
log entry can chew through quota fast. Hence:

- v1 ships with a **manual-summon-only** mode for headless. Every
  diagnostic session is netrunner-initiated.
- v2 with auto-fire on crash is rate-limited to N crashes per hour
  (default 3) — beyond that, queue and notify, don't fire.
- D1 substrate makes auto-fire cheap enough to run unrestricted.

The local-model substrate is also what unlocks **always-watching**
mode — a maintbot that scans the live log stream as events arrive
and surfaces anomalies in real time, not just on death. This is far
out, but the architecture above accommodates it: a separate process
already reading `latest.log` is already 80% of always-watching.

---

## What the maintbot is NOT

Naming non-goals so they don't creep:

- **Not a watchdog replacement.** The Watchdog (cyberdeck role)
  observes the daemon-construct loop and authors tripwires. The
  maintbot operates on deck infrastructure. Different access,
  different role, different runtime. Both can exist; neither
  subsumes the other.
- **Not a daemon replacement.** The daemon plans the netrunner's
  goals. The maintbot does not plan world-work; it diagnoses deck
  state. If the netrunner wants something done in the world, that's
  a daemon goal, not a maintbot ask.
- **Not always-on (LLM session).** The supervisor process is
  always-on by design (cheap, no LLM cost) but the LLM session is
  lazy: spins up on heartbeat-fired triage or netrunner summon,
  tears down when closed. Cost discipline applies to the LLM half;
  the supervisor half is essentially free.
- **Not autonomous in v1.** All actions land via netrunner approval.
  Relaunching after crash is a v3 feature once v1 + v2 have built
  trust.
- **Not the chatlog UI overhaul.** That's a separate slice (in-deck
  observability, not file-log surfacing).

---

## Implementation sketch

When the time comes to build:

### Pieces

- **`mechanic.py`** (or `mechanic/` package) — the supervisor +
  LLM-session combined process. Argparse entry:
  `python mechanic.py [--watch-deck-pid <pid>] [--summoned] [--launch-deck]`.
  v0 ships with the supervisor half only; --summoned activates the
  LLM session in v1+.
- **Supervisor half** (v0):
  - Polls `<deck_pid>` at low cadence (every 2-5s)
  - Reads `<deck>/logs/latest.log` to track live subprocess PIDs
    via spawn-without-matching-finalize derivation, OR reads a
    dedicated `<deck>/.cyberdeck/active-pids-<deck_pid>.json` file
  - On deck death: kills every tracked subprocess via
    cross-platform Python (`os.kill` with SIGTERM, short grace,
    then SIGKILL; `psutil` if we eventually add it as a dep)
  - Synchronous; no LLM call at this stage
- **LLM session half** (v1+):
  - Has the "preprocessor" that reads `latest.log`, parses the
    header, extracts the post-mortem context before any LLM call
  - Mechanic system prompt — analogous to the Watchdog's, but
    oriented around deck infrastructure rather than fleet
    observation. Knows about gotchas, knows the file layout, knows
    the spec doc exists and can be read.
- **`launch.bat` updates**: spawn the Mechanic supervisor as a
  sibling alongside the deck. Something like:
  ```
  start /B python mechanic.py --watch-deck-pid %DECK_PID%
  wt --fullscreen python tui.py
  ```
  When the deck dies, the supervisor stays running (Mechanic
  process is independent) and either: cleans up subprocesses
  silently then exits, or fires the LLM session into a new wt
  window for triage.
- **A "summon mechanic" surface in the deck UI** (v1+) — menu
  entry or keybind. Sends a request to the already-running
  supervisor process to activate the LLM session. Spawns the LLM
  session UI in a new wt window. Doesn't kill the deck.
- **Deck-side change**: `fleet.spawn` event payload grows a `pid`
  field so the supervisor can derive live PIDs from
  `<deck>/logs/latest.log` (Option A from PID publish channel
  above). One-line change at each spawn site in `fleet.py`. The
  bus already carries the event; the file logger already writes
  it; no new infrastructure.

### Cost, in rough hours

- v1 (diagnose-only, heartbeat-fired + manual-summon): ~1 session
  to land, a few smaller sessions to harden against weird exit shapes
- v2 (guided correction): another 1-2 sessions
- v3 (auto-relaunch): not now

### Sequencing

- **Blocked by:** logger + quit discipline slice (logs need to be
  per-launch with structured levels for the preprocessor to work;
  exit-code semantics need to be clean for the heartbeat to be
  reliable).
- **Unblocks:** the morgue and other deck-history surfaces (the
  maintbot is the natural consumer of that data).
- **Substrate:** cloud Claude for v1. Migration to D1 happens
  whenever D1 lands — no architectural change required.

---

## Open questions

Captured for when we're closer to building, not for solving now:

1. **Naming.** "Maintbot" was the original placeholder; netrunner
   has been calling it "the Mechanic" in conversation since
   2026-04-30 and that's stuck. Open question is just whether the
   file name and code symbols should rename now (touch-storm
   across this doc, state.md, build-plan, eventual `mechanic.py`)
   or land at implementation time as one focused commit. Lean
   toward the latter.

2. **EJECT-then-summon gesture.** EJECT writes a snapshot but
   doesn't fire the maintbot — it's a deliberate halt with autopsy
   artifact. There may be value in "EJECT, then summon maintbot
   to read what just blew up" as a chained gesture (Ctrl+F held
   into Ctrl+M? Ctrl+F twice?). Defer; the manual-summon path
   covers the use case adequately for v1.

3. **Interactive-mode UI placement.** When deck is alive and the
   maintbot is summoned deliberately, does it open in a new window
   (consistent with the heartbeat-fire path) or hook into the deck's
   existing UI somehow? Hooking is nicer UX but may require non-
   trivial Windows-side IPC. New window for v1; revisit if the
   netrunner finds the context-switch annoying.

4. **Headless display surface.** When the maintbot wraps the deck
   and the deck would normally render a TUI, where does the TUI go?
   Options: a virtual terminal the maintbot can render to, a
   web-served version of the UI, or "headless-mode just doesn't have
   a UI; everything goes through the maintbot's text interface."
   Far enough out that it's premature to decide.

5. **Read-vs-write boundary on deck source.** v1 is read-only —
   maintbot can read `*.py` files but cannot edit them. v2 may want
   limited write access (for example: "the auto-seeded default
   profile got corrupted, want me to restore it?"). Where exactly
   does that line go? Probably: read-anything, write-only-to-files-
   the-maintbot-itself-created-or-explicitly-listed-as-restorable.
   Defer decision until v2 design starts.

6. **Cyberdeck-home naming.** Filed earlier in `cyberdeck-state.md`
   as a known confusion: `cyberdeck-home/` lives inside the deck
   source tree, which causes both naming and protection-pattern
   confusion. The maintbot reinforces the question — its access
   model is "read deck source + read-deck-state, deny world-work"
   but `cyberdeck-home/` blends "deck state" (profiles, plugins,
   .cyberdeck/) with "world-work artifacts" (whatever constructs
   write). Resolving the naming + layout would clarify what the
   maintbot is allowed to read and what it isn't. Not blocking; the
   maintbot can carry the messy boundary in v1 and we tighten the
   layout when we're ready.

7. **Clean-vs-unclean exit signaling for the supervisor.** The
   supervisor needs to distinguish "deck shut down deliberately"
   (subprocess cleanup may be redundant — the deck already killed
   them) from "deck crashed" (subprocesses likely orphaned, kill
   them now). Two signals available:
   - File logger writes a `log_footer` with `reason: "shutdown"` /
     `"eject"` on clean exit. Supervisor reads that to confirm
     deliberate halt.
   - Deck PID disappears with non-zero exit code or no exit code
     at all → unclean.
   Combine: if footer present AND PID gone → clean (still walk
   active-PIDs and kill any stragglers, but no triage). If no
   footer → unclean (kill everything + fire LLM session). Probably
   the right shape, but the timing is fiddly — footer write happens
   before the Python process actually exits, so there's a small
   window where the footer is in the file but the deck is still
   technically alive. Defer until implementation time; the
   tolerable failure mode is "supervisor occasionally fires LLM
   session for a clean exit," which costs a few thousand tokens
   one time.

---

## Why this earns its keep

The deck is in active production use on Windows. Things break.
Today, when something breaks, the recovery loop is:

1. Deck dies (or wedges)
2. Netrunner copies `cyberdeck.log` (which contains every session
   ever) to another device
3. Netrunner pastes it into a chat with an AI
4. AI reads it and proposes a fix
5. Netrunner applies the fix on the deck

The maintbot collapses 1-4 into "deck dies; new window pops with
the AI already triaging." Step 5 stays — the netrunner approves
fixes before they happen — but everything else is automatic. That's
the value: lower-friction recovery from breakage, in-place, on the
deck itself, with the AI that has the entire deck source tree in
context already.

The fact that it's a separate process is what makes this work. If
the maintbot were an in-deck role, it would die with the deck.
Outside the deck, it survives whatever the deck does — including
the deck not existing yet.

---

*Filed 2026-04-29. Architecture set; implementation deferred until
the logger + quit discipline slice lands and probably until D1
makes the cost profile sustainable. When picking this up, start by
re-reading this doc + the gotchas list in `cyberdeck-state.md` —
many of the patterns the maintbot will diagnose are already
catalogued there.*
