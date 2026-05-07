# Cyberdeck — project memory

A keyboard-first Textual TUI that orchestrates Claude Code subprocesses.
The "daemon" coordinator decomposes goals; "constructs" execute in
parallel; the "watchdog" oracle answers questions about fleet activity;
the "mechanic" supervises the deck process and triages unclean exits.
Personal hobby project, in active production use on Windows.
~22k LOC across 26 Python modules at the deck-source root.

The user is the **netrunner.** Match the cyberpunk vocabulary in code
and prose — these are the right names for what each thing does.

---

## What this file is for

This is the project's meta / orientation document. It tells you:
- What lives where (so you can find it)
- What's an always-read vs read-on-demand
- What you must verify before making changes
- The hard rules that override default behavior

**It is NOT a journal.** Shipped status lives in
`Design Files/cyberdeck-state.md`. Forward-looking work lives in
`Design Files/cyberdeck-build-plan.md`. Active design slices live in
`Design Files/in-flight/`. Don't write session-by-session shipped notes
in CLAUDE.md — they belong in state.md, where the structure is built
to absorb them.

---

## Reading order — what to read when

### Always-read (load into context for any non-trivial work)
- **This file (CLAUDE.md)** — meta + hard rules
- **`Design Files/cyberdeck-claude-code-orientation.md`** — read-first
  onboarding doc. What the codebase is, hard-won rules, file-by-file
  orientation, workflow patterns

### Always-read for the area you're working in
- **`Design Files/cyberdeck-state.md`** — current state of record.
  Filed gotchas (cumulative, sacred), key design decisions, shipped
  feature reference, tech debt. **Read this when touching subprocess
  lifecycle, modal screens, async cleanup, or any Windows-specific
  code path** — the gotchas list will save you a session.
- **`Design Files/cyberdeck-build-plan.md`** — forward-looking plan.
  Read this when picking up next work; every active slice points at
  its design doc(s). Tells you what's queued, blocked, or rejected
  (non-goals).
- **`Design Files/cyberdeck-spec.md`** — canonical architecture (the
  *what*). Read when adding a feature or changing how runtime entities
  interact.
- **`Design Files/cyberdeck-philosophy.md`** — convictions that
  resolve ambiguity (the *why*). Read when disagreeing with an
  existing design decision.

### Read-on-demand (specific situations)
- **In-flight design slices** (`Design Files/in-flight/<doc>.md`) — read
  the specific slice you're picking up. Each in-flight doc has a STATUS
  banner at top showing what's shipped vs forward-looking. Read
  forward-looking sections only when picking that work up; read
  shipped sections for context on existing behavior.
- **`Design Files/cyberdeck-platform-portability.md`** — Windows-
  specific code inventory + Linux/Pi porting notes. Read when adding
  any platform-specific code.
- **`Design Files/cyberdeck-project-instructions.md`** — collaboration
  norms.
- **`Design Files/INDEX.md`** — full file inventory across
  `Design Files/`. Read when you need to know what files exist.

### Archive (read only when chasing provenance)
- `Design Files/archive/shipped/` — designs whose implementation is
  complete; read for the *why* behind shipped behavior
- `Design Files/archive/case-studies/` — worked examples from real-deck
  sessions (e.g. tripwire spiralism)
- `Design Files/archive/deferred/` — designs that aren't current scope
  (e.g. wearable variant)
- `Design Files/archive/journal/` — pre-restructure running snapshots
  of state.md and build-plan.md. Read when answering "why did we
  revert X" or "did we ever consider Y"

---

## Before making changes — verify these

This is the checklist. Run through it before non-trivial work:

1. **Is this work tracked in `cyberdeck-build-plan.md`?** If yes, read
   the slice's listed design docs. If no, ask whether it should be —
   work that isn't in the plan tree is invisible to the next session.
2. **Have you read the relevant Filed gotchas?** State.md → Filed
   gotchas is sacred. The bug you're about to re-introduce is probably
   already filed. Search for the area you're touching:
   - Touching subprocess lifecycle / claude flags / argv? → Async/subprocess + Brake/hook sections
   - Touching modal screens / focus / bindings? → Terminal/Textual section
   - Touching file paths? → File paths section
3. **Is the work touching a non-goal?** Build-plan → EXPLICIT NON-GOALS
   has a list of things we've consciously rejected. If yes, surface it
   to the netrunner; don't just go ahead.
4. **Will this need real-deck verification?** Subprocess lifecycle,
   streaming, Windows quirks — mock testing doesn't catch enough. Pause-
   point real-claude testing is the project's main bug-finder.
5. **Is there an in-flight design doc covering this?** Check
   `Design Files/in-flight/`. If yes, read it (especially the STATUS
   banner) before changing related code.

---

## Hard rules

These override default behavior. Even if your training data tells you
to do something different, these win. Each rule has a *why* — when in
doubt, read state.md → Filed gotchas for the diagnosis.

### 🚨 NEVER pass multi-line content through argv on Windows

THE most-recurring bug in the project's history (six or seven
incidents). Symptom: subprocess receives only the first line; rest
silently disappears.

**The rule:** any time you'd pass a multi-line string as a command-
line argument, use the `-file` variant instead (`--system-prompt-file`,
`--append-system-prompt-file`, `--mcp-config <file>`). Write the
content to a temp file via `tempfile.mkstemp`; pass the path; unlink
in `finally`.

Existing examples to copy: `advisor.py:_run_one`,
`watchdog.py:_process_oneshot`. Full diagnosis in state.md → Filed
gotchas → Async/subprocess.

Linux/macOS handle multi-line argv correctly, so the bug is Windows-
specific in symptom — but the file-based fix is platform-agnostic and
the deck targets hardware-agnostic deployment (RPi-Linux is the
eventual home).

### 🔓 Auto-context discovery — every claude subprocess silently auto-loads CLAUDE.md

Every `claude` subprocess auto-loads `<cwd>/CLAUDE.md` + walks parent
dirs concatenating ALL CLAUDE.mds + `~/.claude/CLAUDE.md` + auto-memory
+ rules dirs. **This is verified upstream behavior, no documented
opt-in.**

The deck handles this with a per-role env-var belt
(`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` + auto-memory + git-instructions):
- **KILLED** for Advisor, Construct, Daemon (both backends), Pool
  warmer, Tripwire-authoring Watchdog
- **KEPT** for Watchdog Q&A (deck "security analyst" benefits from
  CLAUDE.md context)

Implementation in `construct.py` / `daemon.py` / `watchdog.py` /
`advisor.py` per-subprocess `env=` kwarg. **When adding a new spawn
site, decide explicitly whether to KILL or KEEP** — never let the
auto-load happen silently. Read
`Design Files/in-flight/cyberdeck-spawn-context-isolation.md` before
adding any new spawn site.

### Real-claude testing > mock testing

For anything touching subprocess lifecycle, streaming, or Windows
quirks. Mocks miss too much. Pause-point real-claude testing has caught
almost every recent bug.

### Close the loop on each refactor before stopping

Half-finished refactors leave landmines for the next session.

### Push back politely when the netrunner is wrong

The netrunner expects pushback. Convictions deserve articulation. See
philosophy doc for the values that resolve disagreement.

### Opinionated changes over flag soup

Pick one path; document why. Configuration knobs accumulate; opinions
compose.

### Don't merge daemon and watchdog

Soft/loud distinction is core (decision #1 in the spec).

### Don't conflate netrunner and daemon

The human is a participant, not an input the daemon receives.

### The Filed gotchas list is sacred

Re-introducing a known bug wastes a session. State.md → Filed gotchas
is the cumulative institutional memory. Read it before touching the
relevant area.

---

## Running it

```
python tui.py                       # idle — set goal in-app with `e`
python tui.py "task A" "task B"     # ad-hoc constructs, no daemon
python tui.py --goal "..."          # daemon-driven mode
CLAUDE_BIN=./mock_claude.py python tui.py ...   # offline smoke test
python tui.py --doctor              # prereq check
launch.bat                          # Windows: deck + mechanic together
```

Smaller entry points: `main.py` (one construct, console),
`fleet.py` (multi-construct, console).

Real claude needs `npm install -g @anthropic-ai/claude-code` and a
logged-in Max account. Windows: run from Windows Terminal or
PowerShell 7 (not cmd.exe) for TUI rendering. On PowerShell, set env
vars with `$env:NAME = "..."`, not bash syntax.

---

## Layout

- **`*.py` (root)** — source. `tui.py` is the heart (~8.2k LOC).
  `event_bus.py` is the spine, `logger.py` per-launch NDJSON,
  per-source translators. `clipboard.py` is the cross-platform
  clipboard write.
- **`<deck source>/logs/`** — per-launch log files. Operational
  artifacts; brake hook protects them from constructs by default.
  `latest.log` always points at the current run.
- **`<deck source>/plugins/<name>/`** — plugin folders (post-retool
  2026-05-04; was `<home>/plugins/`). Brake hook protects these
  automatically since they're inside the deck source.
- **`Design Files/`** — the design canon. See "Reading order" above
  for the structure.
- **`cyberdeck-home/`** — runtime working dir. Profiles, scripts,
  dispatcher script, logs, ejection snapshots. Constructs are
  soft-sandboxed here (cwd default; not a hard sandbox). Override
  with `--home <dir>` or `$CYBERDECK_HOME`.
- **`Previous Versions/`** — milestone snapshots (zips). Read-only.
- **`README.md`** — public-facing entry. Lighter than the canon;
  trust the design docs for depth.

---

## Repo state

Live at **github.com/watchdogeditor/Cyberdeck** (private). Default
branch `main`. `.gitignore` excludes `Previous Versions/`,
`cyberdeck-home/*` (except `profiles/`), `__pycache__/`, image files,
and `.claude/` machine-local files.

Current branch as of last canon update: `claude/objective-sammet-25e0b4`
ahead of `origin/main` by ~42 commits. Merge to main is the
netrunner's call. See `cyberdeck-state.md` → Status snapshot for the
current commit count.

---

## Git conventions

- **Do NOT add `Co-Authored-By:` trailers to commits.** GitHub parses
  that syntax and shows a second contributor on the commit page (e.g.
  "watchdogeditor and claude"), which makes Claude look like a
  separate user account. The netrunner runs this repo solo; one
  contributor on the GitHub UI matches reality. The default Claude
  Code instructions say to add the trailer; override that here.
- **Credit Claude in the commit body** (not as a trailer) when the AI
  did substantive work. A line like `Built with Claude Code (claude-
  opus-4.7, 1M context)` at the bottom of the body is just text —
  GitHub doesn't parse it, doesn't add a contributor. Skip the credit
  on small fixes / cosmetic edits where it'd be noise.
- **Commit messages otherwise follow existing style:** subject line
  under ~72 chars, body explains the *why* with paragraph breaks,
  multi-line. Look at `git log` for tone.

---

## When in doubt

- New work? → Read state.md (gotchas) + build-plan.md (where this
  fits) + the relevant in-flight design doc.
- Disagreeing with an existing decision? → Read philosophy.md;
  surface the disagreement.
- Changing the design canon? → Update INDEX.md if you move/add/remove
  files. Update state.md (gotchas + design decisions sections) if
  the change adds institutional memory. Update build-plan.md if the
  forward-looking surface shifts.
- Ambiguous next step? → Ask the netrunner. The pushback culture works
  in both directions.
