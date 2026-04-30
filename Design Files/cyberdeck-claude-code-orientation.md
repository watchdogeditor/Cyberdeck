# Cyberdeck — Claude Code Orientation

*Read this BEFORE doing anything else in a fresh Claude Code session
on this codebase. It captures the institutional knowledge that
matters most for getting work done quickly without re-introducing
bugs we've already filed.*

*Pair with `cyberdeck-state.md` (what's shipped + design decisions),
`cyberdeck-build-plan.md` (what's next), `cyberdeck-spec.md` (the
canonical architecture), and `cyberdeck-philosophy.md` (the why).*

---

## What this codebase is

A Textual TUI that orchestrates Claude Code subprocesses — a
"daemon" coordinator AI dispatches "construct" workers to do tasks
in parallel, while a "watchdog" oracle answers questions about fleet
activity. The user (the "netrunner") supervises through the UI. ~12k
LOC, 13 Python modules, real subprocess management, real terminals,
real Windows quirks.

The deck is in active production use by the user (the operator known
as Watchdog). They run it on Windows. They will catch bugs you don't.
Real-deck testing has caught more issues this project than mock
tests ever did. Trust their reports — the failure mode they describe
is the failure mode that's actually happening.

---

## Hard-won rules — read these first

### Real-claude testing > mock testing
Mocks miss subprocess streaming behavior, Windows path quirks, claude
CLI argument parsing edge cases (`-p` vs trailing positionals, stdin
vs argv prompt delivery), shutdown noise, ProactorEventLoop quirks,
and the wedge-recovery cycle. **When in doubt, test against real
claude before declaring a feature done.** Especially for anything
involving subprocess lifecycle, streaming, or Windows-specific
behavior.

### One milestone at a time
A session that ends mid-refactor leaves landmines. The
`_process_streaming` stub disaster — where the worker called the
wrong method name and watchdog questions all failed silently —
happened because a previous session stopped halfway. **Always close
the loop on the current method or feature before starting the next.**
If you have to stop mid-refactor, file a TODO that's impossible to
miss.

### Filed gotchas are sacred
The gotchas list in `cyberdeck-state.md` is *cumulative*. Every entry
on that list is a real bug we hit, diagnosed, and fixed. **Re-introducing
one wastes a session.** Read the list. When designing a fix that touches
subprocess lifecycle, file paths, modal screens, or async cleanup —
check the gotchas first.

### Push back when the user is wrong
The user appreciates being told "actually, that won't work because
X" or "I'd argue Y is the better approach." They've course-corrected
the AI multiple times when it was about to do the wrong thing
(MCP-vs-stdout-markers, manual-UI-vs-programmatic, etc.) Push back
politely, with reasoning. They'll do the same to you.

### Bias toward small, sharp, opinionated changes
The codebase rewards opinionated design decisions explained in
comments. Avoid "let's add a flag for both behaviors" — pick the one
that's right and document why. The flag-soup direction makes the
codebase harder to reason about and the comments thinner.

---

## Architectural concepts

### The four runtime entities
1. **The deck** (the TUI itself). Renders panels, dispatches actions
   on key presses, mounts/unmounts modals. Contains the App, all
   panes, and the action handlers.
2. **The fleet** (`fleet.py`). N concurrent Construct subprocesses
   managed by a `Fleet` object. Emits events on a queue; the App
   listens and updates panes/chatlog.
3. **The daemon** (`daemon.py` + `daemon_session.py`). A persistent
   Claude Code subprocess that decomposes goals into actions
   (spawn / kill / etc.) and dispatches via JSON. Has both one-shot
   and streaming backends; streaming is the default.
4. **The watchdog** (`watchdog.py`). An async question-queue oracle
   that answers human questions about fleet activity. Independent
   of the daemon; runs its own claude subprocess. Streaming default.

### Profiles (prescriptive templates)
A profile is a TOML file in `<home>/profiles/` defining `name`,
`category`, `description`, `default_daemon_addendum`,
`default_construct_addendum`, and `recommended_tools`. Profiles do
NOT narrow what a construct can do — they're prescriptive
templates that steer behavior (addendums) and suggest tools
(`recommended_tools` surfaces in the system-prompt addendum as a
soft hint). The daemon picks profiles per-spawn from a JSON field.
Profile registry hot-reloads from disk. The default profile
auto-seeds on first run; netrunner edits to it are sacred.

Historical note: profiles used to carry a `brake_profile` field and
an `allowed_tools` field that hard-narrowed `--allowedTools` at
spawn. Both were dropped during the brake refactor. Brake is now
deck-global (see below); tool narrowing is delegated to the brake
hook layer (also below).

### Brake state (deck-global, runtime enforcement via hook)
Three levels: paranoid / default / yolo. Set by the netrunner via
the `b` modal (paranoid is single-press; yolo requires a 3-second
held-key confirmation, mirroring the EJECT deliberate-consent
gesture). Persists at `<home>/.cyberdeck/state.json`. Sidebar
indicator next to connection state.

Enforcement is via Claude Code's `PreToolUse` hooks. Each new spawn
gets a per-construct `--settings` JSON pointing at `brake_hook.py`
(in the deck source dir) with the current brake passed via argv.
The hook is a self-contained ~150-LOC Python script that reads the
proposed tool call from stdin, applies hand-curated patterns, exits
0 (allow) or 2 (deny). Stderr text becomes the model-visible denial
reason; the construct sees it as a `tool_result.is_error=True` with
content like `"PreToolUse:Write hook error: [...]: <stderr>"`.

Per-brake behavior:
- **paranoid:** denies Write, Edit, Bash, WebFetch, NotebookEdit
  wholesale. Read-only investigation mode. The construct can still
  use Read/Glob/Grep/WebSearch/TodoWrite freely.
- **default:** allows broadly; denies destructive bash regex
  matches (rm -rf on system roots, format, dd of=/dev/, mkfs, fork
  bombs, shutdown) and Write/Edit to OS-root paths or the deck
  source directory.
- **yolo:** no hook installed at all. Constructs run unrestricted.

Mid-flight propagation is deferred — brake is captured at spawn
and baked into that construct's lifetime. New spawns see new
values. Watchdog observes denials via the `permission_denials`
field on result events; chatlog renders a yellow `· brake blocked:
Write×2, Bash×1` suffix on finalized lines.

### The dispatcher protocol
Constructs talk back to the deck via a one-way stdout marker protocol:
`__CYBERDECK::v1::ACTION::PAYLOAD__`. The marker scanner runs in
`_handle_event` BEFORE formatters. `dispatcher.py` is the construct-
side helper script bootstrapped to `<home>/tools/deck/cyberdeck.py`
and surfaced in the Tools panel like any other script. Constructs
invoke `cyberdeck files add <path>` etc. via Bash; the dispatcher
emits markers; the deck parses them.

### The spawn provenance system
`fleet.spawn(..., origin=...)` carries who initiated each spawn —
`daemon` / `netrunner` / `inject`. Threaded into the `spawned` meta
event payload. Renders as cyan `[you]` / `[↳you]` badges in the
chatlog. Watchdog's system prompt has the badge legend so it doesn't
have to reverse-engineer attribution from log timing (we caught it
doing this once — beautiful but expensive reasoning).

### Goal updates and netrunner messages
Both flow through `DaemonSession` setters that stash content for the
next outcome turn. `_format_outcomes` prepends preambles
(`⚠ GOAL UPDATE` / `≫ NETRUNNER MESSAGE`) to the daemon's input.
A wake event keeps idle sessions responsive. Goal updates overwrite
(latest wording wins); netrunner messages stack (FIFO). Force-push
(interrupt in-flight turn) is M5+ — today's deferred-to-next-break
delivery is good enough.

### The connection monitor
`connection_monitor.py` heartbeats `api.anthropic.com:443` every 30s
and emits state transitions: Online (●green) / Degraded (◐yellow) /
Offline (●red). Sidebar indicator + chatlog announcements update on
each transition. DNS failure skips Degraded → Offline directly.
Threshold-based: 2 failures → Degraded, 1 success → Online.
Spec'd consequences (spawn-blocking, daemon parking, recovery flow)
are NOT yet wired — only detection. That's the next M5+ slice.

### The session pool
`session_manager.py` keeps warm Claude Code sessions ready for
constructs to resume into. Saves cold-start cost. Always warms with
the **default** profile only — non-default profiles always spawn
fresh. We considered per-profile pools; the warming cost wasn't
worth the complexity.

---

## File-by-file orientation

### `tui.py` (~8.2k LOC, the heart)
- App class, BINDINGS, action handlers
- All modal screens (NewConstructScreen, AskWatchdogScreen,
  TalkDaemonScreen, GoalSetScreen, LimitsScreen, EjectScreen,
  ExpandModal, LaunchScreen, etc.)
- ConstructPane (focusable, expandable), DaemonPane, WatchdogPane,
  GoalPane, PoolMeter
- All list-item classes: FileListItem, ProfileListItem, ScriptListItem
- Section navigation: `_focus_section`, `_list_walk`,
  `_fall_through_to_neighbor`, `_jump_section`, `_cycle_in_section`
- The `__main__` block at the bottom with argparse + the
  `sys.unraisablehook` filter

This file is huge but well-organized. When adding a feature, first
grep for existing similar work — the pattern is almost always there.

### `watchdog.py` (1075 LOC)
- `Watchdog` class with `streaming_mode` switch (Q&A oracle half)
- One-shot path: `_process_oneshot` (claude `-p` per question, stdin)
- Streaming path: `_process_streaming` + `_spawn_streaming` +
  `_drain_streaming_question` + `_kill_streaming_proc` +
  `_shutdown_streaming`
- Wedge recovery: timeout → kill → respawn-on-next-question
- System prompt with badge legend + brake awareness + blacklist
  awareness + tripwire awareness paragraphs
- `Blacklist` + `BlacklistEntry` + `_fingerprint` — session-scoped
  registry of forbidden task patterns. Owned by Watchdog (per spec
  "persistent memory of what's forbidden") but consumed by
  DaemonSession (spawn refusal) and the TUI (Shift+K population +
  in-flight match scan). Tripwire authoring (slice 2) will read
  entries' rich context to author sharper rules than the current
  first-80-fingerprint matcher.
- `WatchdogHistory` + `WatchdogHistoryEntry` — persistent JSONL log
  of resolved Q&A at `<home>/.cyberdeck/watchdog.jsonl`. Watchdog
  appends in `_safe_callback` before firing the listener; TUI
  replays last N into WatchdogPane on mount. Per-entry `kind` field
  futureproofs for tripwire / blacklist records. First slice of the
  watchdog-log initiative; tripwire/blacklist kinds and a dedicated
  history-browse tab still deferred.
- Owns the `TripwireEngine` (constructed at __init__, default
  tripwires installed automatically) — the deterministic-enforces
  half of the spec's tripwire architecture. See `tripwires.py`.
- **`Watchdog.author_tripwires(...)` (slice 2)** — orchestrates one
  LLM-authored tripwire pass. Spawns a fresh `claude -p` one-shot;
  uses `--resume <session_id>` (rung 1) when a session has been
  captured from the streaming Q&A subprocess, fresh otherwise (rung
  2). Clears prior `Origin.LLM_AUTHORED` entries before registering
  new ones. Returns `TripwireAuthoringResult` for the TUI to render
  to the chatlog. Fire-and-forget at the call site
  (`run_worker(...)`); never blocks goal-set/update flow.
- **`_session_id`** captured from the streaming subprocess's first
  `system`/`init` event in `_drain_streaming_question`. Cleared on
  any subprocess-death path so a respawned subprocess captures a
  fresh id rather than handing out a stale one.

### `event_bus.py` — the spine (Phase 1 shipped 2026-04-30)
- `DeckEvent` dataclass — kind (dotted-namespace), source, timestamp,
  construct_id, severity, optional pre-rendered text, arbitrary
  payload. Stable shape across all event sources.
- `EventBus` — synchronous publish/subscribe on a single event loop
  (matches Textual single-loop). Bounded ring-buffer history (default
  10000) for late subscribers + snapshot consumers. Per-callback
  exception isolation; errors recorded in a bounded `errors` deque.
- `Subscription` — opaque handle returned by `subscribe()`; call
  `unsubscribe()` when done. Idempotent.
- Filter shape: None (everything), iterable of fnmatch patterns
  (`["fleet.*", "tripwire.fire"]`), or predicate callable.
- `Severity` constants (debug/info/warning/error/critical).
- `Kind` namespace: dotted-string constants for every migrated
  event source (`fleet.spawn`, `daemon.thinking`, `tripwire.fire`,
  `brake.change`, `chatlog.direct`, etc.).
- Phases 2-6 migrated every event source onto the bus; Phase 7a's
  DeckLogger is the first non-TUI subscriber; Phase 8 cleanup
  (retire `add_listener` / `on_*` shims) is queued.

### `logger.py` — per-launch file logger (Phase 7a shipped 2026-04-30)
- `DeckLogger` — bus subscriber that writes one NDJSON line per
  matching event to `<deck source>/logs/cyberdeck-YYYY-MM-DD-HHMMSS.log`.
  Severity threshold filter (default INFO; CRITICAL bypasses).
- `latest.log` pointer alongside — symlink on Linux/macOS, hard-copy
  on Windows. Best-effort.
- Self-describing header: deck version, argv, env (CYBERDECK_*+CLAUDE_*),
  brake, home, python, OS, log level, log path. Triagable in isolation.
- `close(reason=...)` writes a footer (`shutdown` / `eject` / `crash`).
  Mechanic supervisor will read this to distinguish clean from
  unclean exit. Idempotent.
- `_serialize_payload` walks dataclass-shaped payloads (FleetEvent,
  DaemonEvent, BlacklistEntry, etc.) recursively via `__dict__`,
  bounded depth 4, repr() fallback.
- Best-effort throughout — disk failures degrade gracefully.

### `tripwires.py` — slices 1 + 2 shipped 2026-04-29
- `Tripwire` dataclass — small DSL: pattern_type (regex today),
  pattern, event_kinds (which EventKinds to scan), field selector
  (`tool_use_command`, `tool_result_content`, `thinking_text`,
  `assistant_text`, `tool_use_input`, `user_text`, `any`), severity,
  scope (deck_global / per_construct), origin, authored_at.
- `TripwireEngine` — register/unregister/scan registry plus
  `clear_by_origin(origin)` for slice 2's "drop all LLM_AUTHORED
  before re-author" lifecycle. `register` returns `bool` (True on
  success, False on bad regex / unknown pattern_type) so authoring
  can tell which entries actually landed. scan() does scope +
  event_kind gating before text extraction; fires dispatch after
  the scan loop completes (listeners can mutate the registry without
  iterator invalidation).
- Field-extractor functions — pull text out of stream-json events
  by structural type. The field selector is what makes tripwires
  precise — won't false-fire on incidental text mentions of
  dangerous-looking tokens.
- `DEFAULT_TRIPWIRES` — two ship-with-the-deck patterns
  (keyword_credentials low, keyword_destructive_sql warning).
  `install_default_tripwires(engine)` is idempotent.
- **Slice 2 authoring layer:** `TRIPWIRE_AUTHORING_SYSTEM_PROMPT`
  (separate from Q&A), `build_authoring_user_prompt(...)` (formats
  goal + brake + defaults + blacklist for the LLM), and
  `parse_authoring_response(raw)` (tolerant: strict JSON →
  markdown-fence extract → balanced-brace fallback → giveup;
  per-entry validation with rejection reasons).
  `TripwireAuthoringResult` dataclass carries the outcome to the
  caller (success, registered, rejected, used_resume, error,
  elapsed_s, raw_response). The orchestration that ties prompt +
  subprocess + engine mutation lives on the Watchdog.
- Severity / Field / Origin / Scope class-as-namespace constants
  (same pattern as construct.EventKind).

### `daemon.py` (685 LOC)
- `Daemon` class with both backends
- One-shot: spawns fresh `claude -p` per turn with `--resume <session>`
- Streaming: persistent `claude --input-format stream-json` subprocess
- Same shutdown pattern (close stdin → wait_closed → terminate → kill)

### `fleet.py` (611 LOC)
- `Fleet` class manages N constructs concurrently with a semaphore
- `spawn(task, ..., origin=...)` is the main entry point
- Events flow on an asyncio.Queue; consumers (the App) subscribe
- `parent_id`, `resumed_from`, `profile_name`, `origin` in the
  spawned payload

### `daemon_session.py` (570 LOC)
- `DaemonSession` glues the daemon to the fleet
- `_format_outcomes` is the daemon's input message builder; respects
  goal_update + netrunner_messages + respawn_warnings + outcomes
- `_outcome_event` wakes the loop on inputs from idle states
- `set_pending_goal_update`, `set_pending_netrunner_message`

### `display.py` (506 LOC)
- `summarize`, `render_block`, `fmt_input` — formatters with
  `untruncated=False` kwarg
- `chatlog_format_fleet` — chatlog spawn line renderer with origin
  badge logic

### `profile_registry.py` + `profiles.py`
- TOML loader, frozen Profile dataclass with `source_path`
- File-watch + hot reload, default seeded
- Profiles are pure prescription post-refactor: `recommended_tools`
  surfaces as a soft suggestion in the system-prompt addendum;
  brake_profile field and is_privesc check are gone.

### `brake_state.py`
- BrakeState enum (paranoid/default/yolo) + persistent store +
  listener pattern (mirrors ConnectionMonitor)
- Per-spawn settings file generation at
  `<home>/.cyberdeck/spawns/<construct_id>.json`
- `cleanup_spawn_settings` runs on construct finalization

### `brake_hook.py`
- Self-contained PreToolUse hook script invoked by claude per tool
  call. Reads JSON from stdin, brake state from argv, exits 0/2
  with stderr denial reason
- ~180 LOC; depends only on stdlib (json, re, os, sys, pathlib)
- Patterns are hand-curated and short — destructive bash regex
  and OS-root path prefixes
- **Both Bash and PowerShell are gated** (`SHELL_TOOLS` set +
  `PARANOID_DENY_TOOLS` includes both). Claude Code on Windows
  exposes PowerShell as a separate tool with the same `command`
  shape; an LLM denied Bash will pivot to PowerShell automatically
  if PowerShell isn't gated equivalently
- Path-aware shell check: any Bash/PowerShell command mentioning
  one of three sentinel deck filenames (brake_hook.py,
  brake_state.py, brake_patterns.py) is denied — closes the
  redirect bypass route to the brake config itself
- Deck-source-dir-as-substring matching was deliberately dropped:
  cyberdeck-home/ sits inside the deck source dir, so a substring
  match denies every legitimate plugin and dispatcher invocation
- Future: watchdog will eventually author additional goal-scoped
  patterns; this script's pattern lists are the always-on baseline

### `plugins.py` + `plugin_registry.py`
- Plugin dataclass + manifest loader; one-shot registry that scans
  `<home>/plugins/` at deck startup
- Plugin folders contain `plugin.toml` (name, category, description,
  entry, optional requires block), `README.md` (LLM-facing interface
  docs), and an executable entry point
- `requires` checks (platforms + python_imports) gate availability;
  unavailable plugins still appear in the registry but the daemon
  prompt doesn't suggest them
- No hot reload: plugins are code, not data. Add or edit a plugin →
  restart the deck. Profiles got hot reload because TOMLs are pure
  data; plugins have arbitrary native dependency state

### `connection_monitor.py` (311 LOC)
- ConnectionMonitor with heartbeat loop
- State machine, transition events, callback
- `record_subprocess_error(stderr)` hook

### `dispatcher.py` (138 LOC)
- The construct-side helper script bootstrapped to
  `<home>/tools/deck/cyberdeck.py`
- `cyberdeck files add/remove`, future actions
- Marker emission only — never reads from the deck

### `construct.py` (552 LOC)
- The managed claude subprocess
- Stream-event parsing, `_files_written` tracking, `_handle_event`
  marker scanner

### `session_manager.py` (557 LOC)
- Pool with manifest, cross-restart reuse, 5h stale window
- Warms with `default` profile only

### `clipboard.py` — cross-platform clipboard write (shipped 2026-04-30)
- Stdlib-only — no third-party dependency.
- `copy(text) -> (ok, err)` — single entry point.
- Per-platform branches: Windows uses ctypes against the Win32
  clipboard API (CF_UNICODETEXT direct), macOS uses `pbcopy`,
  Linux tries `wl-copy` (Wayland) → `xclip -selection clipboard`
  (X11). All paths pass explicit byte encoding (UTF-16-LE on
  Windows, UTF-8 elsewhere) — `text=True` was tried first and
  blew up on Unicode via cp1252 default encoder.
- ctypes path on Windows is what `clip.exe` should have been:
  no encoding round-trip, no BOM injection, no subprocess
  timeout. clip.exe was tried twice (once with `text=True`, once
  with explicit UTF-16-LE+BOM bytes) — both had real bugs that
  ctypes sidesteps. Filed gotchas in cyberdeck-state.md.
- Used by `tui.action_copy_focused` (lowercase y) and
  `tui.action_copy_focused_json` (uppercase Y) plus the parallel
  ExpandModal `action_copy` / `action_copy_json` for in-modal
  yank. Surface map matches `action_expand` (z) — anything you
  can zoom, you can yank.

### `mock_claude.py`, `mock_daemon.py`
- Test fixtures for the chat-era development. Several streaming
  variants exist in `/tmp/mock_streaming_claude.py`,
  `/tmp/mock_wedging_claude.py` etc. when reproducing specific bugs.

---

## Workflow patterns

### Adding a feature
1. Read the relevant section of `cyberdeck-spec.md`.
2. Grep for similar existing patterns. The codebase is opinionated;
   match the prevailing style.
3. Mock-test if it's a new module; real-claude-test if it touches
   subprocess lifecycle.
4. Add comments explaining *why*, not just *what*. The codebase has
   a very high comment density and that has paid off repeatedly.
5. Update `cyberdeck-state.md` and `cyberdeck-build-plan.md` when a
   milestone closes. Don't let those drift.
6. If you fix a bug that's not in the gotchas list, add it.

### Refactoring
1. Use `str_replace`-equivalent edits sparingly on long files —
   read the file first, edit precisely. The chat era saw two cases
   where edit operations ate adjacent code (a class header, a
   docstring close).
2. Compile-clean ≠ structurally clean. After bulk edits, view the
   surrounding code to confirm the structure is intact.
3. Don't introduce `if config.flag: legacy_path() else: new_path()`
   — that's how flag soup starts. Pick one path, comment why.

### Debugging a real-deck bug
1. Believe the user. The screenshot/stack/description is the truth.
2. Reproduce in a mock if possible — but if mocks can't reproduce,
   trust the real-deck symptom.
3. Check the gotchas list FIRST. Most "weird bugs" map to a known
   gotcha pattern.
4. When fixing, file the gotcha. Even if you've fixed it before — if
   you forgot, the next session will too.

### Mid-session checkpoints
The chat era used "FILES TO REPLACE" blocks at the end of every
multi-file change so the user could `cp` artifacts to their working
tree. Claude Code doesn't need this — edits land in place. But the
discipline of "summarize what changed and why" at the end of a
substantive change is still useful. Keep it.

---

## Things to NOT do

- Don't introduce flag-soup ("paranoid mode flag, default mode flag,
  yolo mode flag"). The brake tier system is the right shape; if you
  feel the urge to add a fourth tier, redesign instead.
- Don't reach for inheritance when composition works. The codebase
  has very few inheritance hierarchies and that's deliberate.
- Don't add features that aren't in the build plan without
  discussing them. The user has clear priorities; freelancing burns
  trust.
- Don't write extensive tests for things real-claude testing covers
  better. A 200-line mock test that misses the subprocess-streaming
  edge case isn't worth the maintenance burden.
- Don't over-engineer the watchdog. The watchdog is the deck's
  security analyst — it authors policy (tripwires, when they ship)
  and observes the deterministic enforcement layer (hook denials,
  fleet events). Q&A is what it does with leftover bandwidth.
  Adding multi-step reasoning, planning authority, or putting it in
  the hot path of any tool call defeats the spec's separation of
  concerns.
- Don't put an LLM in the brake-enforcement hot path. The hook is
  deterministic by design; the watchdog observes denials and
  authors patterns over time, but it does NOT gate per-call
  decisions. Spec language: "LLM authors, deterministic enforces."
- Don't reintroduce per-profile brake state or `--allowedTools`-
  based tool narrowing. Those got refactored out for good reasons:
  brake is deck-global and netrunner-controlled, profiles are
  prescriptive templates, and runtime gating happens via the brake
  hook regardless of which profile a construct spawned with.
- Don't gate one shell tool without the other. Bash and PowerShell
  are equivalent execution surfaces on Windows. An LLM whose Bash
  is denied will silently pivot to PowerShell (verified on real-
  deck — the screenshot construct did exactly this without being
  asked). Any tool-gating layer must consider equivalent
  capabilities, not just the tool the human happens to think of.
- Don't substring-match the deck source dir for protection.
  cyberdeck-home/ lives inside the deck source dir, so a prefix
  check denies every legitimate plugin/dispatcher invocation. Use
  sentinel filenames (brake_hook.py / brake_state.py /
  brake_patterns.py) for tampering protection — they survive the
  path overlap and are precise enough on their own.
- Don't try to make the daemon "smarter." It already is — it's
  Claude Code with a system prompt. The daemon doesn't need
  scaffolding; it needs clear inputs and clean propagation paths.
- Don't merge the daemon and watchdog. They're deliberately separate.
  Soft/loud distinction is core to the spec.
- Don't conflate netrunner and daemon. The user is a participant in
  the system, not an input the daemon receives. Goal updates,
  netrunner messages, and direct spawns are first-class.

---

## What "done" looks like for a feature

1. The feature works end-to-end against real claude (where applicable).
2. It compiles cleanly.
3. The relevant section in `cyberdeck-state.md` mentions it.
4. If it shipped a new module: the module's purpose is clear in its
   first docstring.
5. If it touched the gotchas list: any new lessons are filed.
6. The user has tested it on their real deck and confirmed.
7. Code is committed with a sensible message.

---

## Quick reference: where the bodies are buried

| Topic | Look at |
|---|---|
| App startup, sidebar layout | `tui.py` `compose` and `__init__` |
| Daemon's input format | `daemon_session.py` `_format_outcomes` |
| The dispatcher protocol | `dispatcher.py` + `tui.py` `_handle_event` marker scan |
| Streaming subprocess management | `daemon.py` and `watchdog.py` `_run_streaming_*` / `_process_streaming` |
| Event flow from constructs | `fleet.py` `_consume` + `tui.py` `_handle_*_event` handlers |
| Profile loading & hot reload | `profile_registry.py` |
| Why a particular comment exists | `git log` (every comment was load-bearing) |
| Modal navigation | `tui.py` `_focus_section`, `_list_walk`, `_cycle_*_panel_tabs` |
| Origin badges | `display.py` `chatlog_format_fleet` |
| Connection state | `connection_monitor.py` |
| Brake state (deck-global) | `brake_state.py` + `tui.py` `BrakeScreen`/`_handle_brake_change` |
| Brake hook (runtime enforcement) | `brake_hook.py` |
| Brake-hook spawn settings | `brake_state.make_spawn_settings` + `fleet.spawn` |
| permission_denials feed | `fleet.py` `_consume` (scrape) + `display.py` `chatlog_format_fleet` (render) + `watchdog.py` (system prompt) |
| Plugin registry / loader | `plugin_registry.py` + `plugins.py` |
| Plugin shape (manifest, README, entry) | `<home>/plugins/<name>/` |
| Plugin awareness in prompts | `tui.py` `_build_daemon_system_prompt` + `_build_deck_addendum` |
| Watchdog Blacklist | `watchdog.py` `Blacklist` / `BlacklistEntry` (data) + `tui.py` `action_hard_kill_focused` (populate) + `tui.py` `_handle_blacklist_event` (render + flag) + `daemon_session.py` `_execute_action` spawn branch (refusal) + `daemon_session._format_outcomes` (daemon-facing surface) |
| Watchdog Q&A persistence | `watchdog.py` `WatchdogHistory` / `WatchdogHistoryEntry` (data + replay) + `Watchdog._safe_callback` (write on resolve) + `tui.py` `_replay_watchdog_history` (mount-time read) + `WatchdogPane.write_history_separator` / `write_live_session_marker` (visual chrome) |
| Tripwires (slices 1 + 2) | `tripwires.py` (`Tripwire` data + `TripwireEngine` + field extractors + `DEFAULT_TRIPWIRES` + `TRIPWIRE_AUTHORING_SYSTEM_PROMPT` + `build_authoring_user_prompt` + `parse_authoring_response` + `TripwireAuthoringResult`) + `Watchdog.__init__` (engine ownership + default install + `_session_id` capture) + `Watchdog.author_tripwires` (slice 2 orchestrator: subprocess + parse + engine mutation) + `tui._scan_for_tripwires` (Fleet listener feeding events into engine) + `tui._handle_tripwire_fire` (chatlog rendering with severity-colored markup) + `tui._kick_off_tripwire_authoring` / `_author_tripwires_wrapper` / `_render_tripwire_authoring_result` (slice 2 trigger + worker + chatlog announcement) — trigger sites are `_start_daemon_task` (goal-start) and the mid-flight branch of `_handle_goal_submitted` (goal-update, gated on classification != "clarification") |
| The spine (event bus, phases 1-7 shipped) | `event_bus.py` (DeckEvent + EventBus + Subscription + Severity + Kind constants) + `tui.CyberdeckApp.bus` (instance) + producers wire bus= via constructors (Fleet, DaemonSession, Watchdog, Blacklist, TripwireEngine, BrakeStateStore, ConnectionMonitor, ProfileRegistry, PluginRegistry) + `tui._chatlog_write` publishes `chatlog.direct` + `tui._chatlog_format_bus_event` dispatches by event payload type (FleetEvent → format_fleet, DaemonEvent → format_daemon, chatlog.direct → event.text) + `tui._render_chatlog_buffer` (magnified view, reads `bus.snapshot()`) + `tui._build_watchdog_context` (Q&A snapshot, reads `bus.snapshot()`). Each producer module has its own translator (`_fleet_event_to_deck_event`, `_daemon_event_to_deck_event`, etc.) and adds bus publish AFTER the legacy callback fan-out — additive migration. See `cyberdeck-event-stream-design.md`. |
| File logger (Phase 7a) | `logger.py` (`DeckLogger` + `_serialize_payload` + `_SEVERITY_RANKS`) + `tui.CyberdeckApp.deck_logger` instance built after `brake_state_store.load()` + `attach_to_bus(self.bus)` subscription + close(reason="shutdown") in `_drive_fleet` teardown + close(reason="eject") in `_do_eject`. CLI: `--log-dir` / `--log-level` / env `CYBERDECK_LOG_DIR` / `CYBERDECK_LOG_LEVEL`. Default dir `<deck source>/logs/`; `latest.log` pointer alongside. Header on first line, footer on last. NDJSON. |
| Quit discipline (Phase 7b) | `signal.signal(SIGINT, lambda: None)` installed in `tui.py` `__main__` block before App construction + smart `CyberdeckApp.action_quit` (idle: clean exit; running: toast + block, lists daemon-running + live constructs). Ctrl+F (held) remains the only halt-now gesture. EJECT responsiveness via existing `asyncio.gather` parallelization in `_do_eject`; SIGTERM-grace-skip force-kill upgrade is queued separately. |
| Copy keybind (y/Y) | `clipboard.py` (cross-platform write) + `tui._extract_text_for_copy` / `tui._extract_json_for_copy` (duck-typed surface dispatch) + `tui._snapshot_lines_to_plain_text` (markup-strip helper) + `CyberdeckApp.action_copy_focused` / `action_copy_focused_json` (App bindings y / Y) + `ExpandModal.action_copy` / `action_copy_json` (modal-scoped y / Y; modals don't inherit App BINDINGS). JSON path reuses `logger._serialize_payload` so the yank shape matches the per-launch .log files exactly. |
| Limits modal (post-rework) | `LimitsScreen` (now takes `pool_size` alongside `max_concurrent` + `max_total_spawns`; lower-clamps only; the 9-construct hard ceiling retired 2026-04-30) + `CyberdeckApp.action_open_limits` + `_handle_limits_submitted` (applies pool_size live by setting `session_pool.target_size` + nudging `start()` to top up). Sidebar renders `spawn: N/∞` when max_total_spawns == 0. Defaults: max_concurrent=10, max_total_spawns=30, pool_size=5. |
| Pool refill gate | `SessionPool._spawn_warming_task` no-ops when `warm_count + len(_warming_tasks) >= target_size`. Bounds every caller (pull / start / future). Fixes the latent "lower target mid-session, pool keeps refilling toward old target" bug; same fix covers manual and daemon-driven spawns since both flow through `pool.pull()`. |

---

## A final note

This project exists because the user wanted a real cyberpunk-aesthetic
hacker's deck — a workshop, not a sandbox. The aesthetic is part of the
spec. Banter is welcome. Cyberpunk framing is welcome. But work
first; banter around the work, not in place of it.

The user has built something real here. Help them ship the next
piece, and keep the standards high.
