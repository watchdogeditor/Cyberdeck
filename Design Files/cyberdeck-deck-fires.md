# Cyberdeck — Deck Fires

*Concentrated, scannable registry of patterns that **brick the deck**
on contact. Read this BEFORE adding new spawn-site code, new modal
code, new settings-JSON entries, or anything that touches subprocess
spawn argv on Windows.*

*Updated 2026-05-14.*

This is the short-form, action-oriented surface over the long-form
"Filed gotchas" section in `cyberdeck-state.md`. Each entry here:

- **TRIGGER** — the code pattern that lights the fire
- **SYMPTOM** — how the failure presents on real deck
- **FIX** — the canonical avoidance
- **CASE STUDY** — pointer to the filed gotcha for the full story

When a new "the whole deck went down" bug gets diagnosed, file the
gotcha in `cyberdeck-state.md` for the full story, then add a
two-line entry here pointing back at it. Keep this doc **brutally
scannable** — if it grows past ~30 entries, split by category.

---

## Category 1 — Subprocess / argv (Windows)

The deck spawns lots of `claude` subprocesses. Windows argv handling
is full of landmines that don't trigger on POSIX systems. The whole
project targets Pi-Linux eventually, but until then Windows is the
load-bearing platform and these matter.

### 🔥 Multi-line content as argv

- **TRIGGER**: passing a multi-line string as a `--system-prompt`
  / `--append-system-prompt` / `--mcp-config` argv value on Windows
- **SYMPTOM**: subprocess receives only the first line; rest silently
  disappears. No error, no warning. Symptom often manifests as the
  model honestly reporting that it doesn't know something you know
  you sent. Has bit the project 6-7 times in different forms.
- **FIX**: use the `-file` variant of the flag (`--system-prompt-file`,
  `--append-system-prompt-file`, `--mcp-config <file>`). Write content
  to a tempfile via `tempfile.mkstemp`, pass the path, unlink in
  `finally`. Copy from `advisor.py:_run_one` (Family A — full replace)
  or `watchdog.py:_process_oneshot` (Family B — append).
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "MULTI-LINE ARGV ON WINDOWS — TRUNCATES AT FIRST NEWLINE"
- **PROMOTED**: this is a CLAUDE.md top-level Hard Rule.

### 🔥 Long Windows-backslash paths in `--append-system-prompt` argv

- **TRIGGER**: interpolating `self.home_dir`, `self._active_run.dir_path`,
  or any raw `C:\...\.claude\worktrees\<slug>\...` Windows path into
  a long `--append-system-prompt <text>` argv (text gets newline-
  collapsed then list2cmdline-encoded for CreateProcess)
- **SYMPTOM**: every construct spawn from the deck exits code 1 in
  ~30ms, stderr `"The system cannot find the file specified.\r\n"`.
  Reproduces from real-deck only — standalone Python tests with
  identical-looking `cmd[]` lists succeed. Heisenbug — a working
  worktree on the same machine can spawn fine while a "broken" one
  fails consistently.
- **FIX**: same family as the multi-line rule — when addendum content
  contains long Windows paths, use `--append-system-prompt-file` instead
  of `--append-system-prompt`. The construct's spawn site is the
  remaining offender; see filed follow-on. The deck-side workaround:
  don't interpolate `_active_run.dir_path` or `self.home_dir` into the
  static deck addendum; communicate the run dir through the daemon's
  per-spawn task framing instead.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "Long Windows-path-containing `--append-system-prompt` argv breaks
  `claude.CMD` spawn"

### 🔥 `--bare` flag on Claude Max OAuth

- **TRIGGER**: passing `--bare` to `claude -p` on a deck authenticated
  via Claude Max (OAuth/keychain).
- **SYMPTOM**: every spawn exits 1 with NO stderr. Looks like the deck
  is silently broken. Per `claude --help`: "`--bare` ... Anthropic
  auth is **strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings
  (OAuth and keychain are never read)**."
- **FIX**: NEVER use `--bare` on this deck. Use env vars +
  `--disable-slash-commands` for the suppression `--bare` provides
  (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `_DISABLE_AUTO_MEMORY=1`,
  `_DISABLE_GIT_INSTRUCTIONS=1`).
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "`--bare` breaks Claude Max OAuth auth"
- **PROMOTED**: noted as EXPLICIT NON-GOAL in build-plan.

### 🔥 `statusLine` block in `--settings` JSON for `-p` mode

- **TRIGGER**: adding a `statusLine` block to the per-spawn settings
  JSON for `claude -p` (headless) invocations on Windows.
- **SYMPTOM**: tried during item-13 quota work; constructs failed in
  the same way as the path-in-argv bug above. Whether the statusLine
  block has independent issues OR was a confounder of the path-in-argv
  bug isn't fully isolated — both were investigated together. Kept
  DISABLED in `brake_state.make_spawn_settings` until separately
  re-tested in isolation.
- **FIX**: don't emit `statusLine` blocks for `-p` spawns until
  Anthropic clarifies the intended behavior in headless mode. Quota
  capture works through other channels (parse `rate_limit_event`
  from stream-json; deferring until local-substrate landed).
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  the path-in-argv entry mentions statusLine as a confounder.

### 🔥 Per-spawn settings.json content drift

- **TRIGGER**: writing different per-spawn settings.json files
  (different paths OR different content) for each construct.
- **SYMPTOM**: ~30k cache_creation tokens burned per spawn with
  `cache_miss_reason: system_changed`. Anthropic's prompt cache
  invalidates because `--settings <path>` was part of the cached
  key and changed per spawn.
- **FIX**: shared settings.json path + content STABLE across spawns
  under the same `(brake, delay)` config. Per-construct state
  (kill-pending flags, tripwire deny-pending) lives in SIDECAR
  files keyed by session_id, resolved at hook runtime via a
  `<home>/.cyberdeck/spawns/<session_id>.cid` lookup. See
  `brake_state.make_spawn_settings`.
- **CASE STUDY**: state.md → Cache cost fix (2026-05-02).

---

## Category 2 — Textual / TUI

The deck's UI runs on Textual. A few Textual primitives have surprising
behavior; touching them wrong takes the deck down.

### 🔥 Shadowing `Widget._render()`

- **TRIGGER**: defining a method named `_render` on a Textual `Widget`
  subclass thinking it's a custom render hook.
- **SYMPTOM**: `AttributeError: 'NoneType' object has no attribute
  'render_strips'` on first paint of the offending widget. Crashes the
  entire render pipeline; takes the deck down.
- **FIX**: rename to anything that's NOT a Textual API method
  (`_paint`, `_redraw`, `_update_text`). Underscore-prefix on Widget
  subclasses is Textual's protected-method convention, not yours.
- **CASE STUDY**: state.md → Filed gotchas → Terminal/Textual →
  "Don't shadow Textual `Widget._render()`"

### 🔥 Duplicate widget IDs in a single mount

- **TRIGGER**: a modal screen mounts two widgets with the same
  `id="..."` (e.g. accidentally reusing a hardcoded id for multiple
  ListItems).
- **SYMPTOM**: Textual raises on mount; modal CTD; deck process exits.
  No graceful fallback — modal-mount crashes kill the App.
- **FIX**: use unique IDs per widget. For dynamic lists, derive IDs
  from data (e.g. `f"item-{i}"`). When in doubt, omit `id=` entirely.
- **CASE STUDY**: per-run-workspaces v1 (2026-05-07); the modal duplicate-
  IDs bug is mentioned in build-plan SHIPPED → per-run workspaces.

### 🔥 `priority=True` bindings + typed `Input`

- **TRIGGER**: `Binding(..., priority=True)` on a modal screen where
  the focused widget is an `Input(type="integer")` or similar typed
  input.
- **SYMPTOM**: the binding silently doesn't fire when input is focused.
  Keyboard ergonomics break in the modal; netrunner can't escape via
  the supposedly-priority key.
- **FIX**: add a focusable `Label` (or similar Static-derived widget)
  as the modal's initial focus target. Static widgets don't filter
  letter keys; priority bindings fire from there. Add a `:focus`
  CSS rule for visual indication.
- **CASE STUDY**: state.md → Filed gotchas → Terminal/Textual →
  "priority=True bindings DON'T reliably fire when focus is on an
  Input with type=integer"

---

## Category 3 — Async lifecycle / subprocess teardown

### 🔥 Preserving wedged subprocesses

- **TRIGGER**: subprocess timeout fires; code preserves the proc
  "in case it recovers."
- **SYMPTOM**: subsequent turns write to wedged stdin; writes silently
  buffer; outputs appear later in confusing bursts. Net token cost is
  real; deck appears hung.
- **FIX**: on subprocess timeout, KILL the subprocess + null the
  reference. Next turn respawns fresh. See `daemon._drain_streaming_turn`
  and `watchdog._drain_streaming_question`.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "Daemon streaming subprocess silent wedge after `_drain_streaming_turn`
  TimeoutError"

### 🔥 Async cleanup vs `App.exit()`

- **TRIGGER**: relying on a finally-block inside an async coroutine
  to run before `App.exit()` terminates the process.
- **SYMPTOM**: cleanup never runs; log_footer never written; mechanic
  fires triage on every clean Ctrl+Q.
- **FIX**: do cleanup SYNCHRONOUSLY in the action handler BEFORE
  calling `self.exit()`. Async finally blocks under cancellation are
  not guaranteed to fire.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "Async-task teardown isn't guaranteed to run before process exit"

### 🔥 ctypes Windows handles without argtypes/restype

- **TRIGGER**: calling `kernel32` / `user32` / `advapi32` functions
  via `ctypes.windll.<lib>.<func>(...)` without setting argtypes
  and restype.
- **SYMPTOM**: HANDLEs get truncated to 32 bits on 64-bit Windows
  (default `c_int`). Sometimes the truncated handle is non-zero
  (passes `if not h:` checks) but corrupt — calls using it fail
  silently in confusing ways. Mechanic v0 reported "deck died"
  immediately after launch this way.
- **FIX**: ALWAYS declare full argtypes + restype using `ctypes.wintypes`:
  ```python
  OpenProcess.argtypes = [DWORD, BOOL, DWORD]
  OpenProcess.restype = HANDLE
  ```
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "ctypes Windows-handle truncation"

---

## Category 4 — Lookups / file selection / state races

### 🔥 mtime-only file selection across launches

- **TRIGGER**: selecting "the current launch's log file" or similar
  by `glob` + newest-mtime, without validating the file's content
  identifies the right session.
- **SYMPTOM**: on quick deck restart, the previous launch's log is
  picked up by the mechanic; triage fires against a stale pid.
- **FIX**: validate via content. The mechanic now reads the candidate
  log's first line (`log_header.pid`) and confirms the pid is
  currently alive via `pid_alive(pid)`. Skip the file if not.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess →
  "mechanic.py log-file-selection race on quick deck restart"

### 🔥 cwd-based session resume

- **TRIGGER**: spawning constructs with cwd different from where
  the pool warmed their session.
- **SYMPTOM**: every pool-served `--resume <id>` returns "No
  conversation found." Claude Code keys session storage by sanitized
  cwd; cross-cwd resume fails the same way as server-side eviction.
- **FIX**: keep cwd CONSTANT across session warmup + resume. Pool +
  fleet + every construct cwd at `<home>`. Run dirs are a prompt-
  level convention, NOT a cwd change.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess
  (continued) → "Claude Code's per-project session storage means cwd
  MUST match"

### 🔥 Retry path re-entering the trigger

- **TRIGGER**: an auto-retry mechanism re-enters the same code path
  that originally failed.
- **SYMPTOM**: retry hits the same failure → retries again → infinite
  loop / fork bomb. Real-deck observed 8+ failed spawns in a row of
  the same task before noticing.
- **FIX**: retries must take a DIFFERENT code path or carry a flag
  preventing re-trigger. `Fleet._handle_stale_resume` passes
  `force_fresh=True` so the retry skips the pool entirely; the pool
  was the source of the stale entries.
- **CASE STUDY**: state.md → Filed gotchas → Async/subprocess
  (continued) → "Pool stale-resume detect-and-retry — fork-bomb if
  retry doesn't bypass the pool"

---

## Category 5 — Editing / refactoring

### 🔥 Class-method-name shadowing via instance attribute

- **TRIGGER**: assigning `self.<name> = <value>` where `<name>` is
  already a method on the class (e.g. `self.run = ...` on a class
  with `async def run(self)`).
- **SYMPTOM**: later `self.run(...)` finds the attribute first,
  tries to call it as a function, raises `TypeError: '<X>' object
  is not callable`. Bug surfaces in unrelated code; root cause is
  hard to find.
- **FIX**: before naming a new instance attribute, check the class's
  existing methods. Avoid `run`, `start`, `stop`, `update`, `close`,
  `init`, `wait` as attribute names.
- **CASE STUDY**: state.md → Filed gotchas → Editing →
  "Don't shadow class methods with instance attributes"

---

## When you find a new fire

1. Diagnose the bug (real-deck verification preferred).
2. File the full story in `cyberdeck-state.md` → Filed gotchas (the
   long-form record).
3. Add a 5-7 line entry HERE pointing at the gotcha.
4. If the pattern is severe enough that EVERY future spawn-site or
   modal author must read it: promote to a CLAUDE.md Hard Rule.

**Keep this doc scannable.** If an entry needs paragraphs, the
paragraphs go in state.md; the entry here stays a brief pointer.
The whole point is that someone about to add new code can read this
in 2 minutes.
