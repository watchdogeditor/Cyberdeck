# Cyberdeck — Per-Run Workspaces Design

> **STATUS: SHIPPED 2026-05-08 (v5 final form; pending real-deck verification).**
> Filed mid-day 2026-05-07, iterated through five revisions over
> 18 hours. The iteration history (§4) is the value of this doc —
> preserve when moving to `archive/shipped/`. The lessons span
> Textual modal hygiene, Python attribute shadowing, Claude Code's
> per-project session storage model, and auto-retry loop hazards.

---

## 1. Shipped behavior (v5)

A run is the deck process's lifetime: ONE per launch, minted at
`App.on_mount`, kept until shutdown / eject.

- **Run dir** at `<home>/runs/run-<YYYY-MM-DD-HHMMSS>-<4hex>/`.
  Mirrors the per-launch log file (`logs/cyberdeck-<TS>.log`).
- **Constructs cwd at `<home>`** — pool warming, fleet default,
  manual + daemon spawns, all of them. cwd is STABLE across the
  deck's lifetime AND across launches. This is non-negotiable
  because Claude Code's per-project session storage
  (`~/.claude/projects/<sanitized-cwd-path>/`) keys on cwd path,
  and cross-cwd `--resume` returns "No conversation found." See
  state.md gotcha (cwd-as-project-key).
- **Run dir is a prompt-level convention, not a sandbox.** The
  deck addendum tells every construct: *"OUTPUT DIRECTORY: write
  your files to `<absolute_run_dir>/`."* The daemon system prompt
  has the absolute path baked in for task-string composition.
  Constructs honor it because the prompt asks them to. Files
  outside the run dir end up flat in `<home>/` and don't get
  cleaned up.
- **Run dirs persist on disk indefinitely.** No auto-purge ever
  (in EXPLICIT NON-GOALS). Cleanup is manual; bulk gesture
  deferred.
- **Bus events** `run.opened` (on mount), `run.closed` (on
  shutdown/eject) are observability-only — DeckLogger captures
  them in NDJSON, no in-deck subscriber acts on them.

Code surface (~150 LOC net): new `runs.py` (Run dataclass +
`mint_run`), small additions to `tui.py` (mint at on_mount, run
dir interpolated into deck addendum + daemon system prompt,
publish run.opened/closed, crash hooks), `daemon.py` (PER-RUN
OUTPUT DIRECTORY section in system prompt), `event_bus.py` (Kind
constants).

## 2. Bonus fixes that survived all iterations

- **Crash-traceback capture.** `sys.excepthook` (main thread) +
  asyncio loop exception handler write `deck.crash` bus events
  with the full traceback. NDJSON gets the trace; mechanic +
  triage can read it. Without this, an uncaught exception that
  escapes Textual prints to terminal stderr — `wt --fullscreen`
  dies on CTD, taking the traceback with it.
- **Stale-resume detect-and-retry.** `Fleet._handle_stale_resume`
  watches result events for `"No conversation found"`, evicts the
  bad session_id from the manifest, publishes
  `pool.stale_resume`, and marks the construct for auto-retry.
  Finalize path synthesizes a fresh respawn with `force_fresh=True`
  to bypass the pool (otherwise the retry pulls another stale
  entry — fork-bomb). Defensive backstop; with stable cwd it
  shouldn't fire often, but session retention is server-controlled.

## 3. Why this design works

- The deck is short-lived (a session) and the netrunner is always
  present. No need for in-deck curation tooling — the netrunner
  asks the daemon / a construct to migrate files, same as any
  other file op.
- The run dir doesn't gate anything. Constructs can read/write
  absolute paths anywhere allowed by the brake hook. The run dir
  is a hint where their KEEPERS go, not a sandbox.
- Stable cwd preserves Claude Code's per-project session storage
  (~/.claude/projects/) AND its cache locality. Cross-launch pool
  reuse works because session storage finds the right project.
- Run dir mirrors per-launch log file structure. Future tooling
  (archive utilities, cross-launch search) gets parallel
  scaffolding for free.

---

## 4. Iteration history

Filed for posterity. The path from v1 to v5 produced five filed
gotchas in `cyberdeck-state.md`. Read this before re-architecting
anything in this area.

### v1 — per-goal-set, daemon-gated, end-of-run modal

**Idea:** one run per daemon goal-set, auto-close on daemon idle.
Modal pops with leave / promote tools / promote files / delete.
Constructs cwd into the run dir.

**Two catastrophic bugs surfaced on first real-deck launch:**

1. **Modal duplicate-IDs CTD.** `EndOfRunScreen.compose` reused
   `id="eor_meta"` and `id="eor_options"` across multiple Labels.
   Textual rejects duplicate IDs at mount → render-pipeline crash
   → entire TUI dies. Lesson filed: don't reuse IDs; CSS classes
   for shared styling.
2. **`self.run` shadowed `async def run()` method on
   DaemonSession.** Naming the new per-run-workspace attribute
   `self.run` shadowed the existing method. `await
   self.session.run(self.goal)` resolved `run` on the instance
   first, found the Run dataclass, tried to call it: `TypeError(
   "'Run' object is not callable")`. Fired synchronously before
   any await; the daemon "completed" in 562µs. Lesson filed:
   check class methods before naming attributes.

The modal CTD masked the attribute-shadow bug — the deck died
before the TypeError could surface in the daemon pane. Process
lesson: fix the catastrophic bug first, THEN you can see the
underlying functional bugs.

### v2 — bugs fixed, same shape

After fixing the TypeError + duplicate IDs, the deck stayed alive
but the netrunner observed UX problems with the lifecycle model:

> 1. The daemon shouldn't mark the session over until the netrunner says so.
> 2. The end-of-run modal shouldn't auto-dismiss on a single action.
> 3. Manual constructs need a way to attach to runs that doesn't depend on a daemon being active.

### v3 — netrunner-gated, persistent active run, per-file curation

**Idea:**
- ONE active run, persists across daemon idle / done / error.
- `Shift+R` (`action_close_run`) gesture to wrap the run.
- `EndOfRunScreen` rebuilt as per-file curation: file list, up/down
  nav, per-file LEAVE / TOOLIFY / PRESERVE / DELETE marks,
  Space-commit / Esc-cancel.
- Dispatcher `cyberdeck mark plugin/script/tool <path>` subcommands
  let constructs pre-seed TOOLIFY marks via `marks.json`.

**Result:** worked functionally. The netrunner had a meta-realization:

> *"I'm honestly starting to think this may have been a mistake...
> The 'runs' system shouldn't be this involved."*

### v4 — simplified to launch-scoped

**Idea:** run = deck process's lifetime. No netrunner-gated close,
no curation modal, no dispatcher mark-as-*. Files stay where they
land; manual migration via normal spawn actions.

**Result:** ~500 LOC removed. Behavior matched the netrunner's
mental model. **But the cwd plumbing was still in place** — every
construct cwd'd into the run dir.

### v5 — cwd plumbing reverted (final)

**Trigger:** real-deck testing showed every pool-served manual
spawn failing with `"No conversation found with session ID:
<uuid>"`. Three rounds of debugging assumed Anthropic-side
session retention and tweaked `boot_recovery` heuristics (5h →
1h → drop-all-cross-restart-reuse). Each fix made the symptom
worse or weirder.

**Diagnostic:** the netrunner pushed back hard:

> *"I think this is some sort of issue where the naming convention
> changed for warmed constructs or something."*

`ls ~/.claude/projects/` revealed the truth. Claude Code stores
session metadata per-project where "project" = sanitized cwd
path. Pool warmed at `<home>` → sessions stored under
`...-cyberdeck-home/`. Manual spawns cwd'd into the run dir →
Claude Code looked for sessions under
`...-cyberdeck-home-runs-<run_id>/`. Different projects.
Sessions not found locally. Reported as "No conversation found"
— same string Anthropic uses for server-side eviction, which is
why three rounds of debugging chased the wrong cause.

**Fix:** cwd plumbing reverted entirely. ~80 more LOC of
`Fleet.spawn(cwd=)` kwarg + `DaemonSession(active_run=)` param +
manual spawn `cwd=` args + `_active_run_cwd` helper all gone.
Pool, fleet default, every construct cwd back to `<home>`. Run
dir survives as the prompt-level convention described in §1.
`boot_recovery` reverted to the original 5h `is_stale` heuristic.
Cross-launch session reuse works again.

**Lessons filed in state.md:**
- **cwd-as-project-key**: Claude Code's per-project session storage
  is part of session identity. cwd-changing code paths must keep
  cwd consistent between session creation and resume.
- **Process lesson**: when the netrunner says "this used to work,
  something changed in our code today," lean hard on that. Three
  rounds of theorizing about Anthropic's retention window when
  the actual diff was sitting in our own commit.
- **Fork-bomb sub-gotcha**: the auto-retry needed `force_fresh=True`
  to bypass the pool. Without it, retry hit another stale entry,
  triggered another retry, looped. Each retry was a NEW Construct
  instance with `_retry_attempted=False`, so the original's loop
  guard didn't help. Lesson: any auto-retry that re-enters its own
  caller path needs a flag that prevents the retry from triggering
  another retry.

---

## 5. Real-deck verification (pending)

- `run.opened` fires once on launch; `run.closed` fires on quit + eject.
- Daemon goal-set composes the absolute run dir into spawn task strings.
- Manual `n` / file-launch / tool-launch / inject all spawn fast (pool reuse works).
- Cross-launch pool reuse works (no stale-resume failures on second launch within 5h).
- Constructs honor the OUTPUT DIRECTORY convention (real-deck observation: do they actually write files into the run dir, or do they ignore the prompt and write flat in `<home>/`?).

---

## 6. What this is NOT

- **Not a sandbox.** The run dir is a hint, not a fence. Constructs
  read/write absolute paths anywhere allowed by the brake hook.
- **Not a session-recovery mechanism.** The morgue (deferred)
  handles that — session_id-level resume across deck launches
  via `--resume`.
- **Not auto-cleanup.** No auto-purge ever per netrunner direction.
- **Not a curation surface.** Files stay where they land; the
  netrunner asks for what they want via normal spawn actions.
