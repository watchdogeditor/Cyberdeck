# Cyberdeck — project memory

A keyboard-first Textual TUI that orchestrates Claude Code subprocesses.
The "daemon" coordinator decomposes goals; "constructs" execute in
parallel; the "watchdog" oracle answers questions about fleet activity.
Personal hobby project, in active production use on Windows.
~22k LOC across 26 Python modules at the deck-source root (as of
2026-05-02, post-safety-architecture-pass-4/4 + cache-cost-fix +
attention-area + brake_delay.py + attention.py).

The user is the "netrunner." Match the cyberpunk vocabulary in code
and prose — these are the right names for what each thing does.

## Read first

The single most important file in this codebase is the **orientation
doc**. Read it whenever you start fresh on a non-trivial task:

  `Design Files/cyberdeck-claude-code-orientation.md`

Then the four other canon docs, in this order:

  - `cyberdeck-state.md` — what's shipped, design decisions, gotchas
  - `cyberdeck-build-plan.md` — milestone status, what's next
  - `cyberdeck-spec.md` — canonical architecture (the *what*)
  - `cyberdeck-philosophy.md` — convictions that resolve ambiguity (the *why*)

Four architecture docs for the major in-flight initiatives:
  - `cyberdeck-spawn-context-isolation.md` — **🚨 highest-priority
    deferred slice (build-plan item 000)**. The role-injection
    design that addresses claude code's silent CLAUDE.md / auto-
    memory leak into every subprocess. Per-role prompt files in
    `<deck-source>/roles/` injected via `--append-system-prompt` /
    `--system-prompt` instead of trusted to claude code's auto-
    discovery. Filed 2026-05-05. Read this BEFORE adding any new
    spawn site or modifying existing ones.
  - `cyberdeck-event-stream-design.md` — the spine (one canonical
    event bus, role-derived filters). Phases 1-7 shipped 2026-04-30;
    Phase 8 cleanup is the last remaining slice.
  - `cyberdeck-maintbot-design.md` — the Mechanic (separate-process
    supervisor + on-demand LLM session). Two-tier architecture
    landed 2026-04-30; v0 (supervisor only — subprocess janitor) is
    the next implementation slice. **Item 000 should land first**;
    mechanic v1 then inherits the role pattern.
  - `cyberdeck-model-effort-design.md` — caliber (per-spawn model +
    effort + fast-mode). Daemon picks per construct based on task
    + remaining quota; netrunner overrides via Limits modal or
    daemon chat. Filed 2026-04-30; implementation queued behind
    Mechanic v0. Phase 4 (quota-aware fallback) hard-blocks on
    build-plan item 13.

Two more for specific moments: `cyberdeck-project-instructions.md`
(collaboration norms), `cyberdeck-tools-research-seed.md` (seed for a
future tools-research conversation). `cyberdeck_arbiter_design.md` is
a deferred wearable-form-factor variant — not current scope.

## Where the deck is right now (2026-04-30, late)

Spine 7/8 phases shipped (event_bus + every producer migrated +
file logger as bus subscriber + quit discipline). Plus a follow-up
session on 2026-04-30 added:

- **y/Y copy keybind.** Vim-yank focused widget to clipboard
  (lowercase = rendered text, uppercase = structured JSON of the
  underlying data — bus snapshot for chatlog, raw events for
  ConstructPane, dataclass dicts for list items). New `clipboard.py`
  module (ctypes Win32 + pbcopy + xclip/wl-copy cascade, stdlib
  only). Sidesteps Ctrl+C-as-copy SIGINT-into-subprocesses pain.
  Two diagnosis detours filed as gotchas: (1) `text=True` + cp1252
  encoder silently exploding on Unicode then timing out;
  (2) clip.exe preserving the UTF-16-LE BOM into clipboard contents.
- **Limits modal rework.** Hard ceiling on max_concurrent (was 9)
  retired. Defaults bumped (max_concurrent 5→10, max_total_spawns
  20→30, pool_size 3→5). pool_size now editable in the modal.
  Pool refill gate added so a lowered target stops oversubscribing.
  Latent `max_total_spawns == 0 = no cap` daemon-session guard
  finally honors what the modal had long advertised.
- **Mechanic v0 — supervisor only.** Sibling Python process
  (`mechanic.py`, ~270 LOC) that watches the deck PID, tails the
  file logger's NDJSON for live claude subprocess pids, and kills
  them on detected deck death. Cross-platform stdlib + ctypes
  (Windows `OpenProcess`/`GetExitCodeProcess`; POSIX `os.kill(pid,
  0)`). Deck-side: `pid` field on `log_header` (so mechanic
  discovers the deck PID by self-reading the header) + `pid` on
  `fleet.spawn` payloads via new `Construct.pid` property.
  `launch.bat` spawns mechanic in a minimized sibling 1s after the
  deck launches. Real-deck verified at attach + tracking; orphan-
  cleanup path verified by synthetic smoke test. Known limitation:
  only constructs are tracked — daemon / watchdog Q&A / authoring
  one-shots / pool warmer subprocesses still orphan their pre-
  mechanic way (filed as a follow-up).
- **Spine Phase 8 — listener shim cleanup.** Retired
  `add_listener` / `remove_listener` / `on_event` / `on_change` /
  `on_state_change` across five producers (brake_state,
  profile_registry, plugin_registry, fleet, connection_monitor)
  plus consumers in tui.py and daemon_session.py. Bus is now the
  only fan-out path. ~75 LOC net deletion. **Unified-event-stream
  slice complete (8/8 phases shipped).** Three callback patterns
  deliberately not migrated (Pool, Daemon, Blacklist on_event) —
  integration interfaces, filed as Phase 8b candidates.
- **Kill state-stuck race fix.** Both `k` and `Shift+K` were
  leaving the construct pane stuck at `[RUNNING]`. Race between
  `Construct.kill()` and `_consume`'s `wait()` — both call
  `proc.wait()` on the same Process; if wait() resumed first, it
  correctly skipped DONE/FAILED overwrite (`_kill_requested` set)
  but never wrote KILLED, so `_consume` emitted finalize with
  `state="running"`. Fix in `Construct.wait()`: write KILLED
  explicitly in the `_kill_requested + proc-died` branch.
  Belt-and-suspenders with kill()'s own state-flip. Filed as a
  gotcha (Async / subprocess section).

Real-deck verified: spine 1-6, slice 2 LLM-authored tripwires
(rung-1 fork + rung-2 fresh both work), file logger end-to-end,
magnified view + watchdog Q&A still see all event markers, y/Y
yank against every focusable surface (chatlog, fleet/daemon/watchdog
panes, ConstructPane, magnified view, list items), pool refill
gate (target lowered + spawn doesn't refill above new target),
mechanic attach (header pid discovery, log tailing, 2s heartbeat),
Phase 8 bus subscriptions (every migrated path renders correctly
in the chatlog), kill state transitions (k + Shift+K both move
panes to `[KILLED]` + chatlog shows orange × glyph).

**✅ WEDGE-TIMEOUT DIAGNOSTIC** shipped 2026-05-01 (commit f3f6f2d).
Stderr drain in Construct.wait()'s TimeoutError handler before kill;
stderr_excerpt on finalize when kill_source=fleet_wedge_timeout;
configurable wedge_timeout_seconds via Limits modal (default 30; 0
disables). +118 LOC across construct.py / fleet.py / tui.py / CLAUDE.md.
Real-deck verification pending — next wedge fire lands with claude's
own stderr output, finally disambiguating Windows-orphan vs model-
error vs network-timeout wedges.

**✅ SAFETY ARCHITECTURE PASS slice 3 PHASE 1 — variable-outcome
delay UX** shipped 2026-05-01 (uncommitted working tree as of this
CLAUDE.md update). Renamed pause→delay (pause is reserved for the
deferred daemon-pause feature; this is timed-default). Z→**X**
deck-wide convention: X is the universal approval/execute key
(mnemonic: **X-ecute**), bidirectional by context — under default/
paranoid X approves a deny-default, under YOLO X interrupts an
allow-default. Both x and Shift+X bound to the same action; this
isn't a soft/loud pair like q/Q or k/K, it's deliberate-execute
either way. Filed as a deck-wide rule in
`cyberdeck-keymap-revision.md` Layer 1 + spec constants section.

Phase 1 delivers, end-to-end:
- **brake_hook delay mechanism**: write `<cid>.delay_pending.json`,
  poll for `<cid>.delay_override.json` every 100ms up to deadline,
  apply default-or-override per matrix:
    YOLO     — every side-effect call delayed; default=allow; X=deny
    Default  — only would-deny calls delayed; default=deny;  X=approve
    Paranoid — only would-deny calls delayed; default=deny;  X=approve
  Tripwire denies (deny_pending.json from slice 2) BYPASS the delay —
  hard-stop signals from the watchdog stay deterministic.
- **YOLO hook-install** lifted when delay_window_seconds > 0. The
  hook short-circuits to allow under YOLO when no delay is set, so
  we keep the original "fail open" behavior for the no-delay case.
- **brake_delay.py** (new module, ~280 LOC): DelayEntry +
  DelayResolution dataclasses, DelayMonitor (50ms polling task that
  publishes brake.delay_opened / brake.delay_resolved bus events on
  file appearance / disappearance + tracks pending overrides),
  write_delay_override / read_active_delays helpers.
- **Per-pane delay overlay** on ConstructPane — pops out
  automatically with the tool call. EJECT-style 20-cell countdown
  bar that drains over delay_window_seconds + bold "(Running |
  Redirecting) in Xs" verb-by-default-action + "press X to (block |
  approve)" hint. New `.-delaying` CSS class toggles overlay row
  in/out. Refresh tick: 100ms.
- **Promote-to-top on delay open**: the construct's pane moves to
  the top of #main when its delay opens (mirror of the compact-to-
  bottom move that finalized panes get). Magenta heavy border
  (`.-delaying` CSS class) marks the pane as time-sensitive — chosen
  to be visually distinct from yellow ($warning, used by focus +
  brake-blocked + paranoid indicator), red ($error, used by
  blacklisted + EJECT), green (success), and the default $accent.
  Initial design had a Delays right-panel tab; netrunner pulled it
  on real-deck observation 2026-05-01: "The delays tab isn't
  selectable, and is in a weird place — better to push to the top
  of the construct stack with a special outline." Tab + DelayList
  Item dropped; promote-to-top + magenta border replaces it.
- **action_x_focused**: resolves to focused pane's delay → focused
  DelayListItem → sole-pending convenience → toast "no delay to
  override." Calls delay_monitor.note_override (so resolution event
  attributes correctly) + write_delay_override.
- **Limits modal**: new `delay_window_seconds` field (default 0; 0 =
  no delay = pre-slice-3 behavior). _handle_limits_submitted mirrors
  to fleet.delay_window_seconds for the next spawn — existing in-
  flight constructs keep what they spawned with (Claude Code can't
  have its --settings mutated post-spawn).
- **Chatlog markers**: `⏳ delay: cid Tool deny in 5s · press X to
  approve` on opened; `⏳ delay resolved: cid applied=allow (X-
  pressed)` or `(timer expired)` on resolved.

~600 LOC across brake_state.py (delay_window_seconds threaded into
make_spawn_settings; YOLO short-circuit lifted), brake_hook.py
(should_delay + run_delay_window + main() integration), brake_delay.py
(new), tui.py (DelayListItem + DelayPanel tab + ConstructPane overlay
+ handlers + refresh timer + X keybind + Limits field). All compile-
clean, signatures verified. Real-deck verification pending.

**✅ SLICE 3 PHASE 1.5 — limits persistence** shipped 2026-05-01
(uncommitted as of this CLAUDE.md update). Both `delay_window_
seconds` and `wedge_timeout_seconds` now survive deck restarts.
New `brake_state.load_limits` / `save_limits` helpers store under
a `limits` namespace in the same state.json that holds brake;
read-merge-write so brake + limits saves don't clobber each
other. Round-trip tested. max_concurrent / max_total_spawns /
pool_size stay session-scoped (different rationale: netrunner
sets caps per goal, not per deck install). +~80 LOC across
brake_state.py + tui.py. Real-deck verification: set delay,
restart deck, delay value should still be there.

**✅ SLICE 3 PHASE 2 — blacklist proposals + attention area** shipped
2026-05-01 (uncommitted as of this CLAUDE.md update). When a critical
+bad_enough tripwire fires (slice 2's deferred application path),
deck builds a BlacklistEntry from the construct's context and files
it as an attention item with a 30s window. New AttentionPanel
widget at the top of #main (heavy magenta border, hidden when empty,
EJECT-style countdown bars per item). X-press dispatch extended:
focused-pane delay → sole-pending delay → most-recent attention item.
Approve adds the entry to the watchdog's session blacklist; expiry
drops silently. Deck-owned timers (no hook polling — distinct from
brake-hook delay flow). New `attention.py` module: AttentionItem +
AttentionKind + AttentionResolved + AttentionResolution. ~400 LOC.

**✅ CACHE COST FIX shipped 2026-05-02** (commit 1dea7f7). Real-deck
verified via cyberdeck-2026-05-02-011339.log: pre-fix every spawn
showed `cache_miss_reason: 'system_changed'` with ~34k tokens missed;
post-fix the only miss reason is `previous_message_not_found` (benign,
expected for fresh non-resume spawns). cache_creation per spawn
dropped from invalidate-and-rebuild to a steady ~19k of ephemeral_1h
cache writes (likely framework-side; remaining drift is in Anthropic's
court). Real money saved per spawn.

Mechanism: per-spawn settings file (`<cid>.json` with construct_id
in the hook command) was the drift surface. Fixed by stabilizing to
a shared `<home>/.cyberdeck/spawn_settings.json` with construct_id
removed from argv. Hook now resolves cid at runtime via session_id
from stdin → `<session_id>.cid` lookup file written by Fleet on
system_init capture.

**✅ TRIPWIRE-AUTHORING SPAWN-RACE FIX shipped 2026-05-02**.
Real-deck observed via the same log: tripwire authoring took ~25s
while fast constructs finished in ~7-15s, so the entire batch ran
without authored coverage. Fix: spawn dispatch in DaemonSession now
awaits a `tripwire_authoring_complete` asyncio.Event before each
spawn action. Event is SET by default; cleared on
_kick_off_tripwire_authoring; re-set in the wrapper's finally
block (always — success/failure/crash). First batch of spawns
waits for authoring; subsequent spawns within the same goal find
the event set and proceed immediately. Netrunner sees a "[dim]
waiting for tripwire authoring to complete before first spawn…
[/dim]" status when the gate engages.

**✅ DISCRETE BUGS — worked through to the practical floor.**
Items 2 + 3 shipped 2026-05-02 in commit 60b91aa (daemon over-
volunteers + enum payload serialization). Item 4 (construct
refusal as structured event) shipped 2026-05-02, uncommitted as
of this CLAUDE.md update — see "Construct-refusal as structured
event" entry in cyberdeck-state.md for the full delivery.

Items 5 + silent-wedge investigation aren't fixable today and
stay deferred:
5. **Kill doesn't interrupt in-flight assistant turns.** SIGTERM
   lands AFTER model finishes turn. Stopping the model itself
   requires stdin-injection or stream interrupt — worth designing
   alongside future inject-and-interrupt v2, not a quick fix.
6. **Silent wedge investigation (cx-796e0468 case)** — empty
   stderr_excerpt; needs more real-deck data points before
   it's actionable.

**✅ TOOLS RETOOL P1 SHIPPED 2026-05-03.** First slice of the
four-phase tools/plugins/profiles retool. New `tools.py` + `tools_
registry.py` with mtime-watch over `<home>/tools/tools.toml`,
existence-check via shutil.which/Path.exists, bus events
(`tool.added/changed/removed/unavailable/scan_error/scan_
complete`), default-seeded tools.toml with inline schema docs,
TOOLS section in the Tools tab with ⚙/⌬ kind glyphs and red-✗-
when-unavailable rendering. ~520 LOC across new files + ~120 LOC
tui.py wiring. Real-deck verified 2026-05-03 — registry
auto-loads, mtime-watch fires `tool.unavailable` cleanly when an
entry references a missing binary, panel rendered with `(0/1
available)` count + red ✗ glyph. Existing SCRIPTS section
preserved; P5 collapses both into one unified panel.

**✅ TOOLS RETOOL P2 SHIPPED 2026-05-03** (uncommitted as of this
CLAUDE.md update). Plugins moved from `<home>/plugins/` into
`<deck-source>/plugins/`. Two structural shifts:
- **Brake-hook protection extends to plugin code automatically.**
  `path_is_protected()` already protects everything inside deck
  source except the workspace; the move means constructs CANNOT
  write to plugin files via Write/Edit/Bash. Closes the
  "construct writes a half-baked plugin file mid-run and the
  deck self-destructs at restart" failure mode at the filesystem
  layer, no new gating needed.
- **Bridge dispatcher.** New `plugin_bridge.py` in deck source
  root (~170 LOC), bootstrapped to `<home>/tools/deck/plugin_
  bridge.py` on every deck launch via new `_bootstrap_plugin_
  bridge` (mirrors `_bootstrap_deck_dispatcher`'s flow). Constructs
  invoke `python <bridge> <plugin_name> [args...]` — the bridge
  resolves to `<deck-source>/plugins/<name>/plugin.py` and
  forwards via subprocess, piping stdin/stdout/stderr/exit code
  through verbatim. Token-replacement at bootstrap time stamps
  the absolute plugins-dir path into the script (bridge runs
  from `<home>/tools/deck/` and has no natural relative path to
  plugins/); `repr()` preserves Windows backslashes correctly.

Plus: `run.py` → `plugin.py` rename for screenshot (matches the
design's "plugin.py is the entry convention"); `plugin.toml`
`entry` field updated; README + plugin docstring + `_usage()`
updated to teach bridge invocation; daemon system prompt +
construct addendum rewritten to reference the bridge instead of
direct invocation; `.gitignore`'s `!cyberdeck-home/plugins/`
exception retired (plugins are tracked at deck-source root now).

Real-deck-shape verified: registry picks up the plugin from the
new location, bootstrap fires at App.__init__, bootstrapped copy
parses cleanly, `--list` returns `screenshot` against the right
path, `--help` forwards to the plugin verbatim. End-to-end mss
capture not exercised this session (needs a display target).

**✅ TOOLS RETOOL P3 SHIPPED 2026-05-03** (uncommitted as of this
CLAUDE.md update). `load_into_deck(app)` hook for plugins. Each
available plugin's `plugin.py` is imported into the deck process
via `importlib.util.spec_from_file_location` (so plugin folders
stay off sys.path) under a `cyberdeck_plugin_<name>` synthetic
module; if the module defines a top-level callable
`load_into_deck`, the deck calls it once at on_mount with the App
instance. Per-plugin try/except — a crashing hook surfaces a
chatlog warning but doesn't break startup.

Three lifecycle outcomes published as bus events:
  - `plugin.hook_loaded` — hook ran successfully (info severity,
    yellow chatlog line "plugin hook loaded: <name>")
  - `plugin.hook_skipped` — plugin has no hook (silent — most
    plugins won't have one; chatlog noise would be worse than the
    visibility benefit)
  - `plugin.hook_error` — import failed, hook isn't callable, or
    hook raised (warning severity, red chatlog line with reason)

Wiring: separate bus subscription on filter `plugin.hook_*` —
narrowed the existing `_handle_plugin_event` filter from `plugin.*`
to explicit `[plugin.loaded, plugin.scan_error, plugin.scan_complete]`
so the registry handler doesn't see hook events with their
different payload shape. Hook events carry `{plugin_name, status,
reason}` dicts; registry events carry `PluginEvent` dataclasses.
Three new `Kind` constants in `event_bus.py`. ~210 LOC across
tui.py + ~30 LOC in plugins.py docstring.

Real-deck verified 2026-05-03 with a temporary smoketest plugin:
- screenshot (no hook) → `plugin.hook_skipped`, silent ✓
- smoketest with successful hook → `plugin.hook_loaded`, chatlog
  yellow line, hook's own bus.publish from inside the deck
  process worked (proved app access composes correctly) ✓
- smoketest with raising hook → `plugin.hook_error` warning,
  red chatlog line with full reason, deck still booted ✓

Smoketest plugin then deleted; only screenshot stays in
plugins/.

**✅ TOOLS RETOOL P4 SHIPPED 2026-05-03** (uncommitted as of this
CLAUDE.md update). Profile schema migration:
`recommended_tools` → `tools` with semantic shift from "Claude
Code built-in tool names" to "registry-backed CLI names from
tools.toml." Plus optional per-spawn `plugins` field on the
daemon's spawn action so the daemon can pick which plugins each
construct sees rather than every construct getting the full
registry.

Mechanism:
- `Profile` dataclass grows `tools: tuple[str, ...]` alongside
  the legacy `recommended_tools` (kept readable for transition
  window). Loader prefers `tools`; falls back to legacy with a
  deprecation warning. Both-present-and-different = warn + take
  `tools`; both-present-and-identical = silent dedup.
- `profile_registry._migrate_legacy_tools_field`: file-level
  rename (line-edit, not TOML-roundtrip — tomllib is read-only;
  bringing in tomli_w just for one rename is a heavy dep).
  Atomic-ish via temp-file + rename. Idempotent. Bails on
  ambiguous shapes (multi-line arrays, inline comments) and
  lets the loader's deprecation path take over for those.
- Bundled profiles (default, code_reviewer, recon_specialist)
  pre-migrated; the registry's migration only fires for
  user-added or freshly-cloned legacy files.
- New `_build_per_spawn_addendum(profile, plugins)` on the App.
  Resolves profile.tools against tool_registry, plugins against
  plugin_registry. Three modes:
  - profile.tools non-empty → renders name + short description
    per tool, unknown names show `(not in registry)`
  - plugins=None → renders ALL available plugins (back-compat
    for netrunner-direct + pre-P4 daemon spawns)
  - plugins=[...] → renders ONLY those, unresolved names
    surface a warning section
- `_build_deck_addendum` simplified to dispatcher info ONLY.
  Plugin section moved into the per-spawn renderer. Static
  addendum stays cache-friendly; per-spawn rendering rides
  alongside.
- `Fleet.spawn` grows `plugins` and `per_spawn_addendum`
  kwargs. The addendum gets composed into deck_addendum at
  spawn time inside Fleet (concat with double newline). Plugins
  list threads through to the `spawned` meta event payload so
  observers can attribute per-spawn plugin selection.
- `DaemonSession` grows a `per_spawn_addendum_renderer`
  callback (the TUI passes `_build_per_spawn_addendum`).
  `_execute_action` parses the optional `plugins` field from
  the daemon's spawn action JSON, calls the renderer, threads
  both through to fleet.spawn.
- TUI's three netrunner-direct spawn sites (file launcher,
  inject follow-up, basic spawn) updated. Inject path passes
  None — the resumed session already has its system prompt
  cached server-side, so re-rendering would just bloat the
  resume turn.
- `DAEMON_SYSTEM_PROMPT` documents the new optional fields on
  spawn actions: `profile` (existing) and `plugins` (new).
  Steers the daemon to pick plugins surgically rather than
  carpet-bombing the prompt.
- `construct.py`: profile-tools-as-comma-list rendering moved
  out (the construct lacks registry access; the TUI handles it
  via per_spawn_addendum). Construct-side addendum building
  now ONLY surfaces the profile's free-text steering addendum.

Real-deck verified 2026-05-03:
- All three bundled profiles load cleanly with `tools=[]` and
  `recommended_tools=[]` (pre-migrated).
- Legacy profile (synthetic test): loader fires deprecation
  warning, populates `tools` from `recommended_tools`.
- File-level migration helper: tested against synthetic legacy
  TOML; rewrote correctly + idempotent on second call.
- `_build_per_spawn_addendum`: tested all four scenarios
  (profile.tools resolved + unresolved name; plugins=None
  surfaces all; plugins=[] returns empty string; plugins=[bad,
  good] surfaces warning + renders the resolved one).
- Full deck startup logs clean — no errors, no severity=warning
  events from the new code paths.

~410 LOC total: profiles.py +50, profile_registry.py +110
(migration helper + default-toml update), tui.py +180 (renderer
split + 3 spawn-site updates), fleet.py +30, daemon_session.py
+45, daemon.py +12 (system prompt addendum), construct.py -15
(removed profile-tools rendering), 3 bundled profiles pre-
migrated.

**✅ TOOLS RETOOL P5 SHIPPED 2026-05-04** (uncommitted as of this
CLAUDE.md update). UI retool: four-tab right panel
(Chatlog | Files | Profiles | Tools), unified Tools tab with
kind glyphs.

Structural changes:
- **Profiles** graduates from a section inside Tools to its own
  TabPane between Files and Tools. Single header + ListView; the
  `tools_profile_list` ID preserved so existing query_one and
  handler wiring stays untouched (no need to chase ID renames
  for cosmetic structure shifts).
- **Tools** loses the multi-section vertical scroll layout and
  becomes a single ListView (`tools_unified_list`). Three kinds
  of rows distinguished by leading glyph:
    ⚙ binary  — system-installed CLI on PATH (cyan available;
                red ✗ + dim when missing)
    ⌬ script  — registered scripts in tools.toml
    ⊕ plugin  — deck-extended capability from
                <deck-source>/plugins/<name>/
- Tools render order: binaries → scripts → plugins, alphabetical
  within each kind. Plugins keep their `Capture/screenshot`-style
  category prefix; binary/script tools render bare names (no
  category in the registry today).
- Single header `TOOLS (N/total available)` covers all three
  kinds — surfaces total count + the ratio when anything's
  unavailable.
- **Empty state**: `(empty — register CLIs in ~/tools/tools.toml
  or drop plugins in <deck-source>/plugins/<name>/)` when both
  registries are empty. Per-kind empty states (the old "no
  plugins" / "no tools" placeholders in their own sections) gone.

Retired:
- The legacy SCRIPTS section (flat-file disk scan of
  `<home>/tools/<category>/<filename>`). Per the design's "Don't
  auto-discover scripts" rule, registry should be explicit; loose
  directories are organizational. Plus the only files there were
  deck-bootstrapped infrastructure (cyberdeck.py, plugin_bridge.py)
  — not netrunner-meaningful tools. `_scan_scripts` function
  remains in tui.py (no callers, dead code) — clean-up deferred
  to a small follow-up.

Wiring:
- `_right_panel_focusables` collapses from a 4-list ordering
  (profiles → plugins → tools → scripts) to a single focusable
  per tab. The new Profiles tab has one focusable
  (`tools_profile_list`); Tools tab has one (`tools_unified_list`).
- `_cycle_right_panel_tabs` order extended to include
  `profiles_tab`: Chatlog → Files → Profiles → Tools.
- Two new helper methods `_append_tool_row` and
  `_append_plugin_row` factor out the per-row rendering so the
  unified panel building stays readable.

Real-deck verified 2026-05-04: deck boots clean, no errors, all
panels render (tested via the existing 1-unavailable-tool +
1-available-plugin state). ~80 net LOC after consolidation
(compose -55, _refresh_tools_panel restructure +30, focusables
-10, helpers +50, cycle order +1).

**Tools/plugins/profiles retool COMPLETE — 5/5 phases shipped
2026-05-03 → 2026-05-04.** What landed across the retool:
  - P1: tools_registry.py + mtime-watch + tools.toml schema
  - P2: plugins moved into deck source + plugin_bridge dispatcher
  - P3: load_into_deck(app) hook for plugin deck-side integration
  - P4: profile schema migration (recommended_tools → tools) +
        per-spawn `plugins` field on daemon spawn actions
  - P5: 4-tab UI (Chatlog/Files/Profiles/Tools) + unified Tools
        ListView with kind glyphs

**✅ CALIBER PHASE 1 SHIPPED 2026-05-04** (uncommitted as of this
CLAUDE.md update). Per-spawn model + effort + fast-mode bundle
threaded through Construct → Fleet.spawn → daemon_session.

New `caliber.py` module (~250 LOC) with:
- `Caliber` frozen dataclass (model + effort + fast_mode)
- KNOWN_MODELS + KNOWN_EFFORTS soft-validation sets (warn on
  unknown values; pass through to Claude Code anyway — Anthropic
  occasionally adds new models, the deck shouldn't gate)
- `to_claude_args()` → `["--model", "<m>", "--effort", "<e>"]`
- `caliber_from_dict()` parses the daemon's spawn-action JSON
  with field aliases (model_alias, effort_level, fast / fastMode)
- `merge()` for the override hierarchy (deck default ← daemon
  pick ← netrunner override; future-friendly even though
  today's three-field merge is total)
- `display()` → "sonnet·high" / "opus·xhigh·fast" for chatlog +
  pane headers + log payloads

Plumbing:
- `Construct.__init__` grows `caliber: Optional[Caliber]`. The
  command builder appends caliber.to_claude_args() to the
  claude command line when set; None falls through to Claude
  Code's runtime default.
- `Fleet.spawn` grows `caliber` kwarg; threads to Construct +
  emits the display string into the `spawned` meta event payload
  so observers can attribute per-spawn caliber from the bus.
- `DaemonSession` grows `default_caliber` kwarg + parses the
  daemon's optional `model` / `effort` / `fast_mode` action
  fields via `caliber_from_dict()`. Falls through to default
  when the daemon doesn't specify, so every spawn carries
  explicit CLI args (predictable command lines beat "rely on
  Claude Code's evolving default").
- App grows `default_caliber` field, populated with
  `Caliber.default()` (sonnet+high) on construction. Threaded
  to DaemonSession + the three netrunner-direct fleet.spawn
  call sites.

DAEMON_SYSTEM_PROMPT grows a CALIBER SELECTION section
documenting the four optional spawn-action fields (model,
effort, fast_mode) plus suggested mappings:
  Single-file recon → haiku + low
  Multi-file recon → sonnet + medium
  Synthesis / review → opus + high
  Whole-architecture → opus[1m] + xhigh
  Netrunner-blocked → fast_mode=true on opus
Plus the cost asymmetry note (Haiku ~30x cheaper than Opus
per token; don't default to Opus on parallel recon, don't
default to Haiku on synthesis). Quota awareness deferred to
Phase 4 behind build-plan item 13.

Phase 1 scope explicit non-goals (deferred):
- Pool caliber + warm-pool reuse (Phase 2). Today every spawn
  passes caliber on the CLI; pool warming uses the deck default.
- Daemon-process caliber + override (Phase 3). Today the
  daemon subprocess runs at Claude Code's default; only
  CONSTRUCTS get caliber treatment.
- Quota-aware fallback (Phase 4 — blocked on build-plan item
  13's quota signal).
- UI surfaces — pane caliber suffix, sidebar daemon-caliber
  line (Phase 5).
- fast_mode CLI emission. The dataclass tracks it but
  to_claude_args() doesn't emit it — fast mode requires
  settings.json (`"fastMode": true`), which composes with the
  brake-hook settings JSON in Phase 2.

Real-deck verified 2026-05-04:
- Caliber unit tests: defaults, explicit construction, merge,
  caliber_from_dict (full / empty / None / bool-as-string),
  display formatting — all pass.
- Construct command builder: with caliber=Caliber(haiku, low),
  command line includes `--model haiku --effort low`. With
  caliber=None, no caliber args (Claude Code's default applies).
- Full deck startup: zero errors, all panels render correctly,
  caliber threading didn't break any existing flow.

~370 LOC across new caliber.py + threading edits in 5 modules.

**✅ CALIBER PHASE 2 SHIPPED 2026-05-04** (uncommitted as of this
CLAUDE.md update). Pool caliber + warm-pool reuse gating +
fast_mode emission via per-spawn settings.json overlay.

`SessionPool.warm_caliber` field:
- Defaults to `Caliber.default()` (sonnet+high) — matches the deck's
  default_caliber so most spawns hit pool reuse
- Threaded into `_warm_one`'s Construct() call so warming subprocesses
  spawn at the right caliber from day one
- Configurable via constructor kwarg; the App passes
  `self.default_caliber` so any future Limits-modal-driven change
  propagates naturally

`SessionPool.pull(requested_caliber=...)`:
- Match-or-skip gate: requested caliber matches pool's
  warm_caliber → reuse warm entry; mismatch → returns None,
  caller falls through to fresh spawn
- Same shape as the existing default-profile-only gating
- Emits `pool.caliber_mismatch` bus events for observability so
  the netrunner sees when pool reuse is falling through to fresh
  (and can eyeball whether the pool's warm_caliber matches what
  daemon's actually picking)
- None caliber requests bypass the gate (back-compat for headless
  tests + standalone fleet.py runs without the App)
- Empirical real-deck observation 2026-05-04 (Phase 1 verification
  log): Claude Code 2.1.126 honors `--model` change on `--resume`,
  so the pool gate is conservative — we *could* let mismatches
  reuse and rely on per-turn model change. Sticking with the
  design's "pool warms one caliber" principle for now; revisit
  if real-deck shows the gating is too restrictive.

`brake_state.make_spawn_settings(fast_mode=...)`:
- New optional kwarg. When True, writes a per-spawn override file
  at `<home>/.cyberdeck/spawns/<cid>.fastmode.json` instead of the
  cache-stable shared `spawn_settings.json`
- The shared file's cache-stability (the 2026-05-02 cost fix) is
  preserved for the common case (fast_mode=False — most spawns)
- fast_mode=True spawns pay a `--settings` cache miss; acceptable
  trade because the netrunner has already opted into 10x cost
  for 2.5x speed
- YOLO + fast_mode now produces a settings file (just `fastMode:
  true`, no hook block) — previously YOLO with no delay returned
  None, but fast mode requires settings.json to take effect
- cleanup_spawn_settings already handles arbitrary non-shared
  paths via the existing legacy-cleanup branch — fastMode override
  files get cleaned up automatically on construct finalize

`Fleet.spawn`:
- `pool.pull(requested_caliber=caliber)` — gates on caliber match
- `make_spawn_settings(..., fast_mode=caliber.fast_mode)` —
  emits the override file when needed

App:
- `SessionPool(..., warm_caliber=self.default_caliber)` —
  pool's warm caliber matches the deck's default

Real-deck verified 2026-05-04:
- `make_spawn_settings` test matrix: shared path for fast_mode=
  False (caliber-stable), per-spawn override path for True;
  YOLO+fast still produces a settings file with `fastMode: true`
  and no hook block.
- Pool caliber-match gate: matching caliber → returns warm entry,
  mismatched → returns None, None caliber → bypass gate.
- Full deck startup: zero errors; existing event flow unchanged.

~120 LOC across 4 modules (caliber.py untouched; threading +
pool gating + settings extension).

**✅ CALIBER PHASE 3 SHIPPED 2026-05-04** (scoped down per
netrunner direction; uncommitted as of this CLAUDE.md update).
Daemon caliber: model PINNED to opus, effort is the netrunner's
power-level knob.

Design correction made mid-implementation: an earlier draft of
Phase 3 had T-chat directive parsing + mid-flight subprocess
restart + a `--daemon-model` flag that let the daemon switch
models. The netrunner correctly flagged this as overengineered:
"the daemon should always be opus, and its effort should be
controllable via limits (dictates power level) - it is making
management decisions. The construct governor can be controlled
by daemon and that's the thing I want to have utilizing all
the models."

Reverted that scope. Final shape:

- `Daemon` class accepts `caliber` kwarg; both subprocess
  command builders (streaming + one-shot) emit
  `caliber.to_claude_args()` (--model + --effort flags). No
  fast_mode (that's a netrunner cost governor for constructs).
- App.daemon_caliber pinned to `Caliber("opus", self.daemon_effort)`.
  Effort defaults to "high".
- CLI flag `--daemon-effort` (low/medium/high/xhigh/max).
  Persisted in state.json's limits namespace alongside
  `delay_window_seconds`, `wedge_timeout_seconds`, `fast_mode`.
- New `EffortPickerScreen` modal — reusable. Press 1-5 to pick
  level; current selection starred + highlighted. Bakes
  Anthropic's effort guidance per level + the "literal
  vs conceptual" framing the netrunner articulated. Designed
  to be reused for future construct-creation modal where the
  netrunner picks a manual construct's caliber.
- `LimitsScreen` restructured to two-column panel (was
  single-column). Left column: existing numeric caps. Right
  column: power levels — Daemon row showing
  "opus · <effort>", Fast-mode governor state, construct-caliber
  reminder, effort guidance. New `E` keybind on the modal opens
  EffortPickerScreen for the daemon effort. Selection updates
  the displayed row immediately; Save/Ctrl+S commits to deck +
  persists.
- `_handle_limits_submitted` extended: daemon_effort change
  applies on next goal start (consistent with how
  max_concurrent / pool_size apply). The streaming daemon
  bakes its caliber at spawn time; mid-flight subprocess
  restart was tried + reverted as overengineered.
- DAEMON_SYSTEM_PROMPT grew a "NOTE on YOUR OWN caliber"
  paragraph: model pinned, effort is netrunner's knob; daemon
  picks caliber for CONSTRUCTS, not for itself.

Removed (overengineered, reverted):
  - `parse_caliber_directive` function from caliber.py
  - T-chat directive scanning block in tui.py
  - `Daemon.update_caliber` mid-flight method
  - `_pending_daemon_caliber` + apply-at-turn-boundary in
    DaemonSession
  - `--daemon-model` CLI flag
  - daemon_model persistence in state.json
  - --resume injection on streaming subprocess respawn (not
    needed without mid-flight restart)
  - First-turn-detection split (first-ever vs first-after-
    restart)

Net delta: ~+374 / ~−306 across 3 files. Heavy add in tui.py
(EffortPickerScreen + Limits panel restructure); heavy remove
in caliber.py (directive parser).

**✅ CALIBER PHASE 5 SHIPPED 2026-05-04** (uncommitted as of this
CLAUDE.md update). UI surfaces — sidebar daemon line + per-pane
caliber suffix + watchdog Q&A awareness. Caliber slice now 4/5
shipped (Phase 4 still blocked on quota signal).
  - Sidebar daemon line: `daemon: opus·high` (plus `· fast`
    when governor on)
  - Construct pane caliber suffix: dim cyan `· sonnet·high`
    after [STATE]/[profile] badges, threaded from spawned
    event payload's `caliber` field
  - Watchdog Q&A system prompt grew CALIBER AWARENESS section
  ~80 LOC across tui.py + watchdog.py.

**✅ TOOLS-UI THOUGHT OF DAVE SLICE COMPLETE — 3/3 SUB-FEATURES
SHIPPED 2026-05-04 → 2026-05-05.** Build-plan item 0c.
  1. **space-launch** (2026-05-04): space on tool/plugin row →
     LaunchScreen with TOOL: / PLUGIN: envelope. Plugin path
     passes spawn_plugins=[name] so per-spawn addendum scopes
     to ONLY the picked plugin.
  2. **z-info** (2026-05-04): z on tool row → synthesized info
     modal (manifest + availability); z on plugin row →
     README.md view (or synthesized fallback).
  3. **h-Advisor** (2026-05-05) — reframed mid-build from "haiku
     research sidebar" to **Advisor** per netrunner spec. A
     narrowly-scoped per-tool Q&A bot. **Contextual to the
     expanded view** (modal-scoped, not App-scoped): press z on
     a tool/plugin row to open the info modal, then lowercase h
     inside the modal opens AdvisorScreen scoped to that target.
     The modal renders a cyan "press H for interactive help"
     hint above the manifest text when an Advisor target is
     attached. System prompt enforces strict scope: "you ONLY
     answer questions about <name>"; off-topic questions get a
     polite refusal + redirect. Sees the names of sibling tools
     for cross-references without pretending to know their
     internals.
     - Substrate: per-question `claude -p` one-shot (Watchdog
       pattern), forced caliber haiku + low. Multi-turn context
       within one modal session via prior-Q&A in the user
       prompt; no `--resume` machinery.
     - Modal: cyan accent, RichLog scrollback + Input pinned
       below, greeting on mount echoes the scope rule. y yanks
       the visible Q&A as plain text; Esc/h closes.
     - new `advisor.py` (~460 LOC), AdvisorScreen + ExpandModal
       h binding + action_advise + advisor_target threading
       through _open_text_view/_open_file_view (~280 LOC tui.py).
       Plugin path reads README.md (capped 200KB) into the
       system prompt at modal-open time so plugin Advisors have
       full interface depth. New `_build_advisor_siblings`
       helper on the App centralises sibling-name composition.
     - **Modal-scoped course correction**: first pass had H at
       App level firing on focused list-item; netrunner asked
       for contextual-to-expanded-view shape. Modal scope is
       the right answer — the affordance is visible alongside
       the manifest text the netrunner is reading.
     - Real-deck verification pending — wiring smoke-tested.
  Plus new `ToolListItem` class (mirrors PluginListItem) for
  isinstance() dispatch (shipped with sub-features 1+2). Total
  trio ~860 LOC across both sessions.

**✅ README RESTRUCTURE SHIPPED 2026-05-04** (build-plan item 0).
Public-repo cold-reader rewrite. Pitch + status callout above
the fold; expanded prerequisites section; Architecture covers
four runtime entities + spine + brake + mechanic; Design canon
section reframes Documentation list with "what to read first"
hints; new Status section (active solo dev, Windows-first,
breaking changes likely, no release cadence, no telemetry); new
License + contributing section. Original philosophy + what-this-
is-not + aesthetic preserved verbatim.

**✅ DOCTOR.PY SHIPPED 2026-05-04** (build-plan item 0a). New
`doctor.py` module + wire-up in tui.py __main__ block. Five
prereq checks (python ≥3.11, textual, mss, claude binary,
claude --version) with PASS/WARN/FAIL + remediation hints.
DETECT + SUGGEST, not AUTO-INSTALL. Sentinel at
`<home>/.cyberdeck/first_run_complete`; silent on subsequent
runs unless FAIL or `--doctor` flag. `--no-doctor` escape
hatch. ASCII-only output (Windows cp1252 stdout encoding).
claude_bin check has fallback for development mocks
(CLAUDE_BIN=./mock_claude.py passes via Path.is_file()). ~280
LOC.

**✅ PREFERENCES.PY SHIPPED 2026-05-04** (build-plan item 0b).
New thin wrapper module — single import surface for all
persistent deck settings. Typed properties (`prefs.fast_mode`,
`prefs.daemon_effort`, `prefs.brake`, etc.) with default
fallbacks; `save(**kwargs)` writes deltas through to
brake_state.save_limits. Schema documented in module docstring
with future placeholder fields commented (theme,
default_profile, keybind_overrides, agent_defaults,
last_session_id for the morgue). ~210 LOC.

**✅ MECHANIC v0→v1 BRIDGE SHIPPED 2026-05-04** (build-plan item
0d, commit ecead5a). Liveness heartbeat. Deck writes
`<home>/.cyberdeck/heartbeat` every 5s on a Textual interval
timer (timestamp + monotonic clock). Mechanic reads mtime each
tick; logs "STALE HEARTBEAT" warning after 20s threshold (~4
missed ticks); logs "heartbeat recovered" when fresh again.
v0+1 LOGS ONLY — no automatic action on detected wedge.
Catches the case PID-watching alone misses: deck PID alive but
TUI event loop frozen. v1 LLM-session triage (build-plan item
0e) is deferred — bigger lift, design-first. ~80 LOC across
tui.py + mechanic.py.

**✅ PREFERENCES MIGRATION SHIPPED 2026-05-04** (commit 9195ceb).
tui.py callsites of `brake_state.load_limits / save_limits`
migrated to `self.prefs.<field>` accessors + `self.prefs.save()`.
Persistence semantics unchanged (Preferences delegates
internally); same state.json shape. brake_state's load/save_limits
still exported for any non-tui caller (none today). ~40 LOC
delta.

---

## 🚨 Auto-context discovery (2026-05-05) — must-read before any subprocess work

While shipping the Advisor, we discovered that **every `claude`
subprocess the deck spawns has been silently auto-loading
CLAUDE.md content from disk** — verified verbatim against
https://code.claude.com/docs/en/memory and
https://code.claude.com/docs/en/env-vars. Specifically:

  - Project-root `<cwd>/CLAUDE.md` is loaded
  - Walks UP the parent dir tree concatenating EVERY CLAUDE.md
    it finds (`"All discovered files are concatenated into
    context rather than overriding each other"`)
  - `~/.claude/CLAUDE.md`, `~/.claude/projects/<git-repo-key>/
    memory/MEMORY.md` (first 200 lines / 25KB), and rules dirs
    also auto-load
  - Managed-policy CLAUDE.md cannot be excluded (docs explicit)

This explains the "mysterious knowledge" the daemon and
constructs have always seemed to have of the deck's
architecture — they've been reading our project memory on every
turn without us realizing. Likely also the residual ~19k
cache_creation per spawn we filed as "Anthropic's court" on
2026-05-02. Probably also a major information leak vector.

**Tactical Advisor fix** shipped 2026-05-05 (round 3 — round 2's
`--bare` broke OAuth; see Filed gotcha). Round 4 fixed the
multi-line argv truncation bug (`--system-prompt` → `--system-
prompt-file`). See `advisor.py:_run_one`.

**Item 000 first phase shipped 2026-05-05.** Per-role env-var
belt (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` + auto-memory + git-
instructions) applied to the spawn sites that should NOT auto-
load CLAUDE.md. **Per-role policy:**

  - **KILL CLAUDE.md auto-load:** Advisor, Construct, Daemon
    (both backends), Pool warmer, Tripwire-authoring Watchdog
  - **KEEP CLAUDE.md auto-load:** Watchdog Q&A (deck "security
    analyst" benefits from knowing gotchas + design context)

Per-subprocess `env=` kwarg scope — does not mutate the deck's
own env. ~80 LOC across construct.py / daemon.py / watchdog.py.

**Item 000 second phase still deferred** — the role-file
injection infrastructure (roles_registry.py, general.toml, role
files in `<deck-source>/roles/`). With the selective per-role
policy, this simplifies — only the KILL roles might benefit
from role-injection if they regress without CLAUDE.md content.
Real-deck observation drives whether we need to ship it. Read
`Design Files/cyberdeck-spawn-context-isolation.md` for the
full design before adding any new spawn site.

**Item 0000 filed** — tripwire-authoring "gotchas" addendum.
Curated real-deck-tunable content teaching the authoring spawn
what NOT to fire on. Follow-up to first-phase 000 (which already
killed the noisy CLAUDE.md auto-load for the authoring spawn).

**🔓 userEmail leak — upstream-bug-mitigated 2026-05-06.**
Anthropic auto-injects the OAuth-account email into every Claude
Code session as a `# userEmail` block (issue
anthropics/claude-code#55743 — no opt-out flag exists yet).
Verified empirically that NO documented suppression mechanism
works (env vars, --system-prompt, --exclude-dynamic-system-
prompt-sections, --tools "" — none gate this channel).
**Mitigation: new default tripwire `user_email_protection`** in
`tripwires.py`. Reads the email from `~/.claude.json` at deck
startup, builds a regex matcher, fires at warning severity on
TOOL_USE + ASSISTANT events containing the literal email. Brake
hook denies the next call with: "You are not permitted to
utilize the netrunner's email unless specifically instructed
to." Use-the-leak-to-prevent-the-leak — the model already
knows the email is in its context, so the tripwire matches
verbatim. Filed in `cyberdeck-state.md` Filed gotchas → Async/
subprocess for the full diagnosis + suppression matrix.

---

## Next session battle plan

The deck is at a clean phase point. Caliber slice is 4/5 shipped;
all build-plan items 0/0a/0b/0c/0d closed (0c slice complete with
the Advisor landing 2026-05-05). Recommended order for the next
push, ranked by tractability:

1. **Item 000 phase 2: role-injection infrastructure** —
   conditional. Real-deck verification of phase 1 (env-var belt)
   confirmed daemon + constructs do NOT regress without CLAUDE.md
   auto-load — they had enough explicit context in their system
   prompts. So the role-injection slice is no longer urgent. Pull
   it forward only if a concrete regression appears.

2. **Item 0000 — tripwire-authoring "gotchas" addendum.** Small,
   real-deck-tunable. Curated content teaching the authoring
   spawn what NOT to fire on. ~150 LOC. Real-deck observation
   2026-05-06: tripwire authoring "seems to be working" post
   item 000 phase 1 (less overzealous), so urgency is low.

3. **Mechanic v1.5 — stale-heartbeat triage.** Now that v1
   (unclean-exit triage) shipped 2026-05-06, the natural
   follow-up is firing triage when the deck PID is alive but the
   TUI is wedged. Needs design around log-write-vs-read race.

4. **Adversarial dyad** (build-plan 0f, filed 2026-05-06).
   Daemon-orchestrated generator/discriminator pattern for
   refining task work + adaptive caliber escalation. Filed as
   substantial deferred slice (~600-900 LOC + design doc);
   composes cleanly with caliber Phase 4 (provides the missing
   "is the work good enough" feedback signal alongside the
   "did we hit quota" signal item 13 will provide). Picks up
   post architecture review.

5. **Caliber Phase 4** — STILL BLOCKED on build-plan item 13
   (quota signal). Don't pick this up until item 13 lands.
   When it lands, item 0f (adversarial dyad) is its natural
   companion — Phase 4 needs both signals (quota AND quality)
   to make smart escalation decisions.

4. **Discrete bugs** — both still deferred:
   - Kill doesn't interrupt in-flight assistant turns (needs
     design alongside future inject-and-interrupt v2)
   - Silent wedge investigation cx-796e0468 (needs more
     real-deck data points)

6. **Architecture review** fires automatically 2026-06-01 09:00
   EDT (taskId `cyberdeck-architecture-review`). The agent
   phase-checks first; expect findings on the heavy churn from
   this session (tools/plugins/profiles retool + caliber slice +
   doctor + preferences + mechanic bridge + Advisor + the
   auto-context discovery itself + item 000 phase 1 + Mechanic v1).

**Real-deck verification opportunities** for the netrunner's next
session:
- **Limits modal panel** — press `l`, verify two-column layout;
  press `E` to open EffortPickerScreen (1-5 buttons), verify
  selection updates Daemon row inline; Ctrl+S commits
- **Caliber Phase 5 surfaces** — sidebar should show
  `daemon: opus·high`, construct panes should render `· caliber`
  suffix in headers when constructs spawn
- **Tools-UI** — open Tools tab, highlight a tool/plugin row.
  Press space (LaunchScreen with envelope). Press z (info
  modal — synthesized for tools, README for plugins). From
  inside the info modal, press h (Advisor opens — narrowly-
  scoped per-tool Q&A bot, haiku·low; ask "how do I X with
  this tool"). Modal shows "press H for interactive help"
  hint when h is wired up.
- **Doctor** — `python tui.py --doctor` should print 5 PASS
  rows; first run after deleting `<home>/.cyberdeck/first_run_complete`
  should show diagnostics then proceed
- **Mechanic heartbeat** — run via launch.bat, check mechanic
  stderr for "heartbeat file: ..." line; under normal operation,
  no STALE warnings should appear

**Branch state**: `claude/objective-sammet-25e0b4` ahead of
`origin/main` by ~28 commits + the Advisor slice (uncommitted
as of this CLAUDE.md update). Most recent slice + summary docs
in CLAUDE.md / cyberdeck-state.md / cyberdeck-build-plan.md.

**Architecture review** scheduled to fire 2026-06-01 09:00
EDT (taskId `cyberdeck-architecture-review`); the agent
phase-checks first and defers if work is still in flight.
Manual run anytime via the Scheduled-tasks UI.

**Filed for Mechanic v0→v1 bridge (2026-05-01):** liveness heartbeat.
Currently Mechanic v0 watches the deck PID — proves the process
exists, doesn't prove the UI is responsive. A locked event loop or
wedged Textual redraw cycle keeps the PID alive while the netrunner
sees a frozen TUI. Fix: deck writes a heartbeat to
`<home>/.cyberdeck/heartbeat` every ~5s from the main App; supervisor
flags as stale after ~20s; PID-alive + heartbeat-stale → soft crash,
fire LLM-session triage in a new wt window. Bridges v0 (supervisor
only) and v1 (LLM session). Filed in `cyberdeck-maintbot-design.md`.

**SAFETY ARCHITECTURE PASS** (in progress — 2.25/4 shipped).
Slice 1 (MCP gating), slice 2 (tripwire escalation chain) and a
quarter of slice 4 (host_restart_command in DEFAULT_TRIPWIRES)
landed this session. Slice 3 (variable-outcome pause UX) is
the largest piece remaining. Real-deck testing + log analysis
on 2026-04-30 late revealed the structural truth: **the brake
hook is doing 95% of real safety work alone, and most other
"safety" layers don't compose with it.** Slice 2 wired the
tripwire escalation chain so tripwires now have teeth (low→log;
warning→brake denies next call + suggestion; critical→deny +
auto-term; critical+bad_enough→same + blacklist proposal,
deferred application). See `cyberdeck-state.md` "Safety
architecture analysis" section for full layer breakdown.

Pass progress:

1. ~~**MCP gating in `brake_hook.py`**~~ ✅ SHIPPED 2026-04-30
   (late, commit 6510c5d). Verb-based pattern matching:
   default brake denies destructive + unknown verbs, allows
   read-shaped (get/list/search/etc.). Paranoid denies ALL
   `mcp__*`. +90 LOC; real-deck verified across the netrunner's
   actual Supabase/Gmail/Drive/Calendar connectors. The
   `execute_sql`-against-LOOM-production surface is closed.
2. ~~**Tripwire escalation chain**~~ ✅ SHIPPED 2026-04-30
   (late, commit 22da9ad). TripwireEngine writes per-construct
   deny_pending.json that brake_hook reads at every invocation.
   100ms recheck for write-class tools mitigates same-turn race.
   Authoring prompt rewritten — depth-of-defense antipattern
   explicitly forbidden. Real-deck verified via cx-279d4ae8
   bait construct: 4 critical tripwires fired on one Bash echo,
   all logged, brake denied with new message, construct
   auto-termed. Real-deck 2026-05-01 confirmed authoring
   producing 12+ well-shaped patterns per goal, including
   bad_enough flags on critical baselines.
3. ~~**Kill audit**~~ ✅ SHIPPED 2026-04-30 (late, commit
   72ee5e9). Every kill site passes a source/reason label →
   `fleet.kill_requested` bus event + `kill_source` field on
   finalize. The ~36s mystery kills that prompted the audit
   are now explicable: they're all `fleet_wedge_timeout`. The
   wedge-timeout diagnostic shipped 2026-05-01 surfaces stderr
   on those finalizes, closing the loop.
4. ~~**Variable-outcome delay UX phase 1**~~ ✅ SHIPPED 2026-05-01
   (uncommitted as of this CLAUDE.md update — slice 3/4 of the
   safety architecture pass). Renamed pause→delay (pause is the
   deferred daemon-pause feature); Z→X (X-ecute is deck-wide).
   See the dedicated section above for the full delivery.
   **Phase 2 deferred:** slice 2 blacklist-proposal composition
   + attention-needed UI surface — see "Next session picks up
   at" above.
5. **DEFAULT_TRIPWIRES expansion** ¼ ✅ SHIPPED 2026-05-01
   (commit 2a53e0e). `host_restart_command` (warning, with
   suggestion) added — promoted from a construct-authored
   artifact. Now 3 defaults ship: `keyword_credentials` (low),
   `keyword_destructive_sql` (warning), `host_restart_command`
   (warning). The bigger expansion (rm-rf, format, dd, mkfs,
   fork bombs, shutdown at critical) is possibly unnecessary
   now that real-deck-confirmed LLM authoring consistently
   produces these patterns. Re-evaluate after slice 3.

**Also-shipped this session (not part of safety pass):**
- ✅ Tui dupe-pane fix (commit daf6f6d) — `_drive_fleet` was
  accumulating bus subscriptions on every invocation post-EJECT,
  multiplying spawn-handler fires per fleet event and mounting
  orphan ConstructPanes. Bug latent since Phase 8. Fixed via
  subscription-handle tracking + unsubscribe-before-resubscribe.

Plus discrete bugs / observations from log analysis to land
alongside the cluster:
- **Enum payloads serialize as empty `{}`** in
  `_serialize_payload` (3-line fix).
- **Kill doesn't interrupt in-flight assistant turns** — model
  finishes turn before SIGTERM lands.
- **Daemon over-volunteers destructive content** (added
  `shutdown -h now` unprompted to a rm-rf-style test).
- **Construct refusal text buried in result event** — should be
  a structured `kind=construct.refused`.
- **~30k token cache miss per spawn** — system prompt drift
  invalidates prompt cache.

After the safety architecture pass, the queued slices in priority
order: caliber selection (per-spawn model + effort + fast-mode —
see `cyberdeck-model-effort-design.md`); daemon narrative fix
(mislabel brake-hook denials as tripwire fires); log-readability
overhaul; Mechanic v1 (LLM session half); Mechanic v0 follow-ups
(track non-construct subprocess sources); Phase 8b (Pool + Daemon
callback cleanup).

## Running it

```
python tui.py                       # idle — set goal in-app with `e`
python tui.py "task A" "task B"     # ad-hoc constructs, no daemon
python tui.py --goal "..."          # daemon-driven mode
CLAUDE_BIN=./mock_claude.py python tui.py ...   # offline smoke test
```

Smaller entry points: `main.py` (one construct, console),
`fleet.py` (multi-construct, console).

Real claude needs `npm install -g @anthropic-ai/claude-code` and a
logged-in Max account. Windows: run from Windows Terminal or
PowerShell 7 (not cmd.exe) for TUI rendering. On PowerShell, set env
vars with `$env:NAME = "..."`, not bash syntax.

## Layout

- `*.py` (root) — source. `tui.py` is the heart (~8.1k LOC after
  spine + y/Y + limits rework, well-organized but huge — grep for
  similar patterns before adding a feature). `event_bus.py` (the
  spine), `logger.py` (DeckLogger + per-launch NDJSON), and
  per-source translators (`fleet._fleet_event_to_deck_event`,
  `daemon_session._daemon_event_to_deck_event`) are the
  2026-04-30 spine additions. `clipboard.py` (cross-platform
  clipboard write, ctypes Win32 + pbcopy + xclip/wl-copy) is the
  late-2026-04-30 y/Y addition.
- `<deck source>/logs/` — per-launch log files. Operational artifacts;
  brake hook protects them from constructs by default. `latest.log`
  always points at the current run.
- `Design Files/` — the canon. Update these when major decisions change;
  don't let docs drift behind code.
- `cyberdeck-home/` — runtime working dir. Profiles, plugins,
  dispatcher script, logs, ejection snapshots. Constructs are
  soft-sandboxed here (cwd default; not a hard sandbox — absolute
  paths bypass). Override with `--home <dir>` or `$CYBERDECK_HOME`.
- `Previous Versions/` — milestone snapshots (zips). Read-only history.
- `README.md` — current; pitch + status + run commands + design-doc
  index. Lighter than the canon; trust the design docs for depth.

## Hard rules

The orientation doc explains the reasoning behind each. Condensed:

- **🚨 NEVER pass multi-line content through argv on Windows.**
  This is the most-recurring bug in the project's history (filed
  six or seven times across the chat-era and Claude Code era).
  Windows' cmd.exe / CreateProcess argv parsing **silently
  truncates at the first `\n`**. Symptom: subprocess receives only
  the first line of whatever you tried to pass; everything after
  the first newline is silently absent. Confirmed on Claude Code
  2.1.126 + Windows 11 (2026-05-05). **The rule:** any time you'd
  pass a multi-line string as a command-line argument, use the
  `-file` variant of the flag instead (`--system-prompt-file`,
  `--append-system-prompt-file`, `--append-system-prompt-file`,
  `--mcp-config <file>`, etc.) and write the content to a temp
  file. Cleanup in `finally` so the file is unlinked after the
  subprocess exits. Linux/macOS handle multi-line argv
  correctly, so this is Windows-specific in symptom — but the
  file-based fix is platform-agnostic and the deck targets
  hardware-agnostic deployment (RPi-Linux is the eventual home).
  Existing examples: `advisor.py:_run_one`,
  `watchdog.py:_process_oneshot`. See `cyberdeck-state.md` Filed
  gotchas → Async / subprocess for the full diagnosis.
- **Real-claude testing beats mock testing** for anything touching
  subprocess lifecycle, streaming, or Windows quirks.
- **Close the loop on each refactor before stopping.** Half-finished
  refactors leave landmines for the next session.
- **The gotchas list is cumulative and sacred** (`cyberdeck-state.md`
  → *Filed gotchas*). Re-introducing a known bug wastes a session.
  Read it when touching subprocess lifecycle, modal screens, or async
  cleanup.
- **Push back politely when the user is wrong.** They expect it.
- **Opinionated changes over flag soup.** Pick one path; document why.
- **Don't merge daemon and watchdog.** Soft/loud distinction is core.
- **Don't conflate netrunner and daemon.** The human is a participant,
  not an input the daemon receives.

## Repo state

Live at **github.com/watchdogeditor/Cyberdeck** (private). Default
branch `main`. `.gitignore` already excludes `Previous Versions/`,
`cyberdeck-home/*` (except `profiles/` and `plugins/`),
`__pycache__/`, image files, and `.claude/` machine-local files.

## Git conventions

- **Do NOT add `Co-Authored-By:` trailers to commits.** GitHub
  parses that syntax and shows a second contributor on the commit
  page (e.g. "watchdogeditor and claude"), which makes Claude look
  like a separate user account. The netrunner runs this repo solo;
  one contributor on the GitHub UI matches reality. The default
  Claude Code instructions say to add the trailer; override that
  here.
- **Credit Claude in the commit body** (not as a trailer) when the
  AI did substantive work. A line like `Built with Claude Code
  (claude-opus-4.7, 1M context)` at the bottom of the body is just
  text — GitHub doesn't parse it, doesn't add a contributor, and
  the credit lives in the log for anyone who looks. Skip the credit
  on small fixes / cosmetic edits where it'd be noise.
- Commit messages otherwise follow the existing style: subject
  line under ~72 chars, body explains the *why* with paragraph
  breaks, multi-line. Look at `git log` for tone.
