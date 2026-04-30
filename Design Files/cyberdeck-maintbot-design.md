# Cyberdeck — Maintbot Design

*Architecture for the deck's supervisor / repair process. Separate
from the deck itself; outlives crashes; hooks into the deck's UI
when both are running. Captures decisions made 2026-04-29 so the
eventual implementation has a starting point.*

*Pair with `cyberdeck-spec.md` (the deck's runtime architecture),
`cyberdeck-philosophy.md` (separation-of-concerns reasoning),
`cyberdeck-state.md` (current status), and
`cyberdeck-build-plan.md` (where this slots in the priority order).*

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
runs today). Maintbot is **optional**:

```
deck (TUI, top level)
  ├── claude subprocesses
  └── heartbeat sensor (sidecar, ~50 LOC PID watcher)
        │
        └── on unclean deck exit: spawns maintbot in new wt window
```

The heartbeat sensor is a tiny separate process launched alongside
the deck (probably from `launch.bat`). It does one thing: watch the
deck's PID + final exit code. When the deck dies cleanly (exit 0,
EJECT-flagged-snapshot present), the heartbeat exits silently. When
the deck dies uncleanly, the heartbeat spawns the maintbot in a new
Windows Terminal window with a "deck died, here's what we know"
context payload.

The maintbot can also be summoned **deliberately** — a menu entry
in the deck (or a future keybind) that asks "open the maintbot?"
This path is for when the deck is healthy but the netrunner wants
to clean up old logs, audit the brake hook's pattern list, or
investigate something in the deck's recent operational history.

### Three activation paths

Summarized:

| Path | Trigger | Maintbot UI | Deck state |
|---|---|---|---|
| **Headless** | Maintbot launches deck | Maintbot's existing terminal | Wrapped subprocess |
| **Heartbeat-fired** | Deck unclean exit | New `wt` window | Already dead |
| **Deliberate** | Netrunner menu / keybind | New `wt` window | Healthy, may keep running |

In all three, the maintbot is **lazy by default**. The process may
be running but its claude session is not spun up until a
diagnose / chat request actually fires. Holding open an LLM session
on the off chance of a crash is wasted context budget.

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

### v1 — diagnose-only

The first version is read-only. Given a crashed deck (or a healthy
one, when summoned deliberately), it can:

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
- **Not always-on.** Sessions spin up on demand and tear down when
  closed. Cost discipline.
- **Not autonomous in v1.** All actions land via netrunner approval.
  Relaunching after crash is a v3 feature once v1 + v2 have built
  trust.
- **Not the chatlog UI overhaul.** That's a separate slice (in-deck
  observability, not file-log surfacing).

---

## Implementation sketch

When the time comes to build:

### Pieces

- **`maintbot.py`** (or `maintbot/` package) — the bot itself.
  Argparse entry: `python maintbot.py [--summoned-by heartbeat | --interactive | --headless --launch-deck]`.
  Has a small "preprocessor" that reads `latest.log`, parses the
  header, extracts the post-mortem context before any LLM call.
- **`heartbeat.py`** — the PID watcher. Tiny. Argparse: `python heartbeat.py <deck_pid>`. Polls
  the PID, reads exit code on death, classifies clean vs unclean,
  spawns maintbot if unclean.
- **`launch.bat`** updates: spawn the heartbeat alongside the deck.
  Something like `start /B python heartbeat.py %!%` after the deck
  launch. Or — cleaner — let the deck spawn the heartbeat itself
  before going into TUI mode, with the heartbeat inheriting the
  deck's PID via env.
- **Maintbot system prompt** — analogous to the Watchdog's, but
  oriented around deck infrastructure rather than fleet observation.
  Knows about gotchas, knows the file layout, knows the spec doc
  exists and can be read.
- **A "summon maintbot" surface in the deck UI** — menu entry or
  keybind. Spawns the maintbot in a new wt window. Doesn't kill the
  deck.

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

1. **Naming.** "Maintbot" is the working name and it's fine for
   internal use. Cyberpunk-pro options: *mechanic* (verb-aligned
   with diagnose-and-fix), *sysop* (BBS-era authority over
   infrastructure), *custodian* (bureaucratic; not great), *surgeon*
   (in-flight correction implication; v2-shaped). My current lean
   is **mechanic**. Resolve when the doc gets canonized.

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
