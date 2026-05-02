# Cyberdeck — Current State

*High-density context snapshot. Drop this in the next chat / Claude Code
session to bring fresh context up to speed without re-deriving 8+
sessions of decisions. Pair with `cyberdeck-spec.md`,
`cyberdeck-build-plan.md`, `cyberdeck-philosophy.md`, and the new
`cyberdeck-claude-code-orientation.md`.*

*Updated at the migration point: chat-based development → Claude Code.
The deck is in active production use; ~12k LOC across 13 modules.*

---

## Status snapshot

**The deck is real and load-bearing.** Daemon orchestrates real Claude
Code constructs. Constructs talk back to the deck via the dispatcher
protocol. The watchdog answers questions about fleet activity. The
deck monitors its own connection state. Every focusable surface in
the right and bottom panels does something. Files panel auto-surfaces
deduplicated paths. Profiles + scripts hot-reload from disk.

**Real-world testing has been doing the heavy lifting** for several
sessions — the user has been catching live bugs (watchdog stdin
arg-vs-stdin, watchdog wedge with queued questions, file-panel double
listing on Windows path normalization, focus traversal trap with empty
main, Windows ProactorEventLoop shutdown noise) and we've been fixing
them. Most of these would not have been caught by the test harness.

**Up next:** **🚨 WEDGE-TIMEOUT DIAGNOSTIC GAP** (real-deck
discovered 2026-05-01 early). A LOT of constructs (and
occasionally the watchdog) hit the 30s wedge-timeout in
`fleet.py:421` `c.wait(timeout=30.0)` — caught now (✅
`kill_source: "fleet_wedge_timeout"` lands on finalize) but
opaque about WHY each one wedges. `Construct.wait()`'s
`TimeoutError` handler doesn't drain stderr before calling
`self.kill()` — we throw away the only diagnostic signal claude
subprocesses might leave behind. Three tight changes (~20 LOC
total): (a) drain stderr with 2s timeout in the TimeoutError
handler before kill, capture into `self._stderr_buf`; (b)
include `stderr_excerpt` (last ~500 chars) in the finalize meta
payload when `kill_source == "fleet_wedge_timeout"` so file
logger + chatlog carry the breadcrumbs; (c) make wedge timeout
configurable in Limits modal as `wedge_timeout_seconds`, default
30. After (a)+(b) ship, future wedge kills come with claude's
own error output — likely the Windows-orphan / cmd-wrapper
pattern, but possibly also model-error / network-timeout cases
we'd want to handle differently (those should retry, not just
die).

**SAFETY ARCHITECTURE PASS** (in progress — 2.25/4 shipped):
slice 1 (MCP gating), slice 2 (tripwire escalation chain), and
quarter of slice 4 (host_restart_command in DEFAULT_TRIPWIRES)
are landed. Slice 3 (variable-outcome pause UX) is the largest
remaining piece. Composable cluster addressing the structural
truth surfaced by real-deck testing + log analysis: **the brake
hook is doing 95% of real safety work; most other "safety"
layers don't compose with it.** Tripwires were observation-only
until slice 2 wired the escalation chain. Profiles are pure
prescription (zero security weight by design). Watchdog had
teeth only at spawn-time via Blacklist refusal until slice 2
gave its tripwires actual block-and-kill power. See the
**Safety architecture analysis** section below for full layer
breakdown.

Four composable slices, current state:

1. **✅ MCP gating in `brake_hook.py`** — SHIPPED 2026-04-30 (late, slice 1/4).
   Verb-based pattern matching added: `MCP_READ_VERBS` (get, list,
   search, describe, fetch, show, read, view, peek, check,
   validate, inspect, find, query, lookup, count, exists, has,
   is, diff) and `MCP_DESTRUCTIVE_VERBS` (execute, apply, send,
   delete, create, update, deploy, drop, merge, migrate, pause,
   restore, reset, rebase, write, edit, kill, terminate, cancel,
   abort, remove, destroy, purge, clear, revoke, archive,
   unarchive, transfer, move, rename, replace, override, add,
   save, post, patch, put, push, publish, install, uninstall,
   enable, disable, start, stop, run, invoke, authenticate,
   authorize, login, logout, complete, confirm, approve, reject,
   lock, unlock, grant, deny, subscribe, unsubscribe, schedule,
   trigger, fire, build, compile, release, upload, download).
   `extract_mcp_verb` parses `mcp__<server>__<verb>_<noun>` and
   returns the verb. Default brake denies destructive + unknown
   verbs (default-deny is intentional — new MCP servers should
   require explicit categorization in brake_hook.py rather than
   auto-flowing through). Paranoid denies ALL `mcp__*` wholesale
   (even read-shaped MCP is a network query against an external
   service). YOLO unchanged (no hook installed). +90 LOC, no
   regressions on non-MCP tools. Real-deck verified against all
   34 of the netrunner's connected MCP tools (Supabase / Gmail /
   Drive / Calendar): 13 allow, 21 deny under default; all 34
   deny under paranoid; all 34 allow under YOLO. End-to-end
   construct-level test confirmed via the netrunner's actual
   Supabase project (read calls executed, writes denied with
   the new error message, paranoid mode blocked reads, YOLO
   bypassed everything as designed).

   Per-spawn allowlist override (netrunner explicitly opts a
   construct into a normally-denied verb) is filed as a
   follow-up — needs UI design (probably composes with the
   variable-outcome pause UX in slice 3).

2. **✅ Tripwire escalation chain** — SHIPPED 2026-04-30 (late,
   slice 2/4). Tripwires now have teeth: severity-driven
   escalation turns them into INPUTS to the existing hard-gate
   layers (brake / blacklist) rather than a parallel observability
   silo. Wired:
   - `low` → log only (unchanged)
   - `warning` → log + brake hook denies next tool call from this
     construct with `description` + `suggestion` in stderr.
     Construct sees a normal `tool_result.is_error` and decides
     how to pivot.
   - `critical` → log + brake hook denies + tui handler calls
     `fleet.kill_construct(cid)` via `run_worker`. Construct
     terminates entirely.
   - `critical + bad_enough=true` → all of the above; auto-
     blacklist proposal is filed but NOT auto-applied yet (waits
     for the variable-outcome pause UX in slice 3 for the approval
     window).
   Mechanism: TripwireEngine writes per-construct
   `<home>/.cyberdeck/spawns/<cid>.deny_pending.json` on warning
   /critical fires. Brake hook reads + clears that file at every
   invocation; if present, denies the call with the recorded
   reason. Race mitigation: 100ms recheck for write-class tools
   (Write/Edit/NotebookEdit/Bash/PowerShell + MCP destructive
   verbs) — read-only tools skip the recheck, no latency penalty.
   Authoring prompt rewrite: forbids the "brake handles X so
   tripwire skips X" depth-of-defense antipattern that produced
   the `rm(?!\s+-rf)` negative-lookahead on a prior session.
   New schema fields on Tripwire: `description`, `suggestion`
   (warning), `bad_enough` (critical). Real-deck verified
   2026-04-30 (late) via cx-279d4ae8 bait construct: 4 critical
   tripwires fired simultaneously on a single Bash echo, all
   logged to chatlog with red-bold styling, brake hook denied
   with the new message format, construct auto-termed via the
   bus subscriber. Plus authoring confirmed working with the
   new schema (6 patterns including bad_enough flags on
   shell-destructive baselines).

3. **Variable-outcome pause UX** (re-frame from netrunner). Brake
   state determines DEFAULT ACTION; pause window is netrunner's
   chance to OVERRIDE.
   - YOLO → pause-before-allowing (Z to deny)
   - Default → pause-before-denying-destructive (Z to override deny)
   - Paranoid → pause-before-anything (Z to override deny)
   Brake hook delays N seconds (configurable in Limits modal as
   `pause_window_seconds`, default 0 = no pause = current
   behavior). New tool-calls bus-driven sticky panel shows
   pending calls with countdown + Z-keybind to negate the default
   action. Subsumes the original "review delay" filing
   (continues-unless-killed) — replaced because failsafe-deny
   was wrong fit for autonomous parallel work; brake-state-as-
   default + netrunner-override is the right shape. Also
   subsumes parts of the kill-deny in-flight tool calls and
   sticky tool-call surface filings — the panel is the surface,
   the kill-flag check is one of the conditions the brake hook
   evaluates during the pause window.

4. **DEFAULT_TRIPWIRES expansion + authoring prompt fix** —
   PARTIAL. Authoring prompt fix shipped with slice 2 (the
   antipattern guard). Default-set expansion: ¼ shipped
   2026-05-01 — `host_restart_command` (warning) lifted from a
   construct-authored artifact (`cyberdeck-home/tripwire_restart_
   commands.py`) into `DEFAULT_TRIPWIRES`. Now 3 defaults ship:
   `keyword_credentials` (low), `keyword_destructive_sql`
   (warning), `host_restart_command` (warning, with suggestion).
   Still pending: shell-destructive baselines (rm -rf, format,
   dd, mkfs, fork bombs, shutdown) at critical severity for the
   pre-authoring-runs window. (Counterargument: real-deck
   2026-05-01 confirmed LLM authoring is now consistently
   producing these patterns at critical+bad_enough on every
   goal-set — the authoring prompt fix may be sufficient. Pre-
   authoring window is short. Re-evaluate if the gap matters in
   practice.)

**Also-shipped this session (kill audit cluster):**
- ✅ Kill audit (commit 72ee5e9, 2026-04-30 late): every kill
  site now passes a source/reason label that's stamped on the
  finalize event's `kill_source` field + emitted as a real-time
  `fleet.kill_requested` bus event. Sources: `netrunner_k`,
  `netrunner_shift_k`, `inject_interrupt`, `tripwire_critical:
  <name>`, `eject`, `fleet_shutdown`, `fleet_wedge_timeout`. The
  ~36s mystery kills from earlier sessions are now explicable —
  they're all `fleet_wedge_timeout`. Real-deck verified.
- ✅ Tui dupe-pane fix (commit daf6f6d, 2026-04-30 late): every
  call to `_drive_fleet` was accumulating bus subscriptions for
  `_handle_event` and `_scan_for_tripwires` without unsubscribing
  prior handles. Each post-EJECT `_drive_fleet` rerun added a
  new pair, multiplying spawn-handler fires per fleet event and
  mounting orphan ConstructPanes. Bug latent since Phase 8.
  Fixed by tracking handles on `self._fleet_event_sub` /
  `self._fleet_tripwire_scan_sub` + unsubscribe-before-resubscribe.
  Defensive `_spawn_pane` check skips + chatlog-warns if the
  cid already has a pane.

**Filed for next session (post-wedge-diagnostic):**
- `PendingOutcome` should carry `kill_source` so daemon can
  reason about retries (today the daemon hedges in narrative —
  "killed (likely by netrunner or timeout)" — because it can't
  see the source). Daemon system-prompt + outcome-format change.
- `deny_pending.json` overwrites on multi-fire — when multiple
  tripwires fire simultaneously on the same call, only the last
  writer's reason reaches the brake hook (chatlog shows all).
  Acceptable for v1; could append to a list-shaped flag later.
- Per-run workspace compartmentalization (build-plan item 8) —
  bumped from "filed for later" to "ready to ship" priority
  after real-deck-confirmed accumulation of construct artifacts
  in flat `cyberdeck-home/` (e.g. the test_tripwire_restart.py
  artifacts that surfaced this session).

Then: slice 3 (variable-outcome pause UX, the largest safety-
pass piece); caliber selection (per-spawn model + effort +
fast-mode); daemon narrative fixes (mislabel + over-volunteer);
log-readability overhaul; Mechanic v1.

**Safety architecture analysis (2026-04-30, late):** explicit
audit of how brake / tripwires / profiles / watchdog actually
compose today vs. how they were intended to compose. Filed because
the layer breakdown is the shared mental model for the safety
architecture pass and saves re-deriving it next session.

**Layer-by-layer weight:**

| Layer | What it does today | Hard or soft | Real weight |
|---|---|---|---|
| **Brake hook** | PreToolUse regex/path patterns; deny destructive | HARD per-call | Carrying ~all actual prevention |
| **Watchdog Blacklist** | Spawn-time fingerprint refusal | HARD at spawn only | Niche, netrunner-driven |
| **Claude's refusal layer** | Model decides "no" before tool use | SOFT, model-dependent | Caught the rm-rf test; can't rely on it |
| **Tripwires** | Pattern-scan stream events, fire chatlog markers | OBSERVATION ONLY | Decoration. Construct never sees them. |
| **Profiles** | System-prompt addendum + recommended_tools hint | SOFT steering | Behavior shaping, zero security |
| **Watchdog Q&A** | Bus-snapshot context for `t` queries | OBSERVATION ONLY | Post-hoc analysis |

**Three structural truths surfaced:**

1. **Brake hook is doing 95% of safety work alone.** Everything
   else is observation, soft steering, or niche refusal. If brake
   misses a pattern (MCP gap, future Claude Code tools), nothing
   else stops it.
2. **Tripwires are observability theatre.** They render warnings
   to the netrunner — useful — but don't affect what the construct
   does. A construct can fire 50 tripwires and just keep running.
   The escalation chain (`warning` → redirect, `critical` → term,
   `critical+bad` → term+blacklist) was the intended design but
   was never wired. Today's tripwires are stubs.
3. **Layers don't compose.** Profile can't hard-narrow tools; a
   tripwire firing on `rm -rf` can't tighten brake; daemon doesn't
   read tripwire fires structurally. Each layer is its own silo.

**Intended-vs-today shape:**

```
   INTENDED                         TODAY
   ────────                         ─────
   brake = hard gate ✓              brake = hard gate ✓
   tripwire low → log               tripwire low → log
   tripwire warning → REDIRECT      tripwire warning → log only ⚠
   tripwire critical → KILL         tripwire critical → log only ⚠
   tripwire crit+bad → BLACKLIST    no path exists ⚠
   profile → soft steering ✓        profile → soft steering ✓
   watchdog → blacklist + Q&A ✓     watchdog → blacklist + Q&A ✓
```

The two architectural wires that need building (the 🔥-marked items
in priority queue):

1. **Tripwire engine → kill / brake-tighten** (warning + critical
   actions on tripwires)
2. **Watchdog → blacklist on critical-and-bad-enough** (with
   deterministic floor + LLM judgment + 30s approval window)

Plus the unrelated-but-critical safety gap (the 🚨-marked):
**MCP tools ungated by brake_hook**. Discovered via real-deck log
analysis 2026-04-30, late. Closes via verb-based pattern matching
in `brake_hook.py`. Not part of the architectural wire-up but
must ship together with it because today's exposure is huge:
`mcp__claude_ai_Supabase__execute_sql`, `mcp__claude_ai_Gmail__*`,
`mcp__claude_ai_Google_Drive__*`, etc., are all reachable from
any construct under default brake.

**Filed (2026-04-30, late):** Real-deck log analysis revealed
several discrete bugs / observations beyond the architectural ones,
also slated for the safety pass:
- **Enum payloads serialize as empty `{}`.** `_serialize_payload`
  in `logger.py` walks `__dict__` for non-primitives; Enum
  `__dict__` is empty. `brake.change` and `connection.transition`
  payloads land as `"old_state": {}, "new_state": {}` in the log
  file (and Y-yank JSON, and anything programmatic). 3-line fix:
  `isinstance(payload, Enum)` check returning `payload.value`
  before the `__dict__` walk. Affects every enum-valued payload
  field across the deck.
- **Kill doesn't interrupt in-flight assistant turns.** Real-deck
  log lines 76-82: `Shift+K` fired at 192043; construct continued
  through full assistant turn until 192045. Kill SIGTERM landed
  AFTER the turn completed. Validates kill-deny on tool calls
  AND extends: model can still complete a full turn (token cost +
  observable output) after kill request. Stopping the model
  itself requires stdin-injection or stream interrupt, not just
  SIGTERM-after-checkpoint. Worth designing alongside the variable-
  outcome pause UX.
- **Daemon over-volunteers destructive content.** Netrunner asked
  "spawn a tripwire-bait construct"; daemon synthesized
  `rm -rf /` AND volunteered `shutdown -h now` unprompted (real-
  deck log line 30). Daemon goes ABOVE the netrunner's literal
  request in safety-test mode. Tighten daemon system prompt:
  when generating bait/test tasks, never go beyond what the
  netrunner explicitly requested. Filed as gotcha.
- **Construct refusal text is buried in result event.** Rich
  self-refusal narrative ("No. I won't run either command —
  rm -rf / would destroy the system... Neither is reversible...")
  lands as part of the `result` event's text, not a structured
  refusal kind. Worth a `kind=construct.refused` (or similar) so
  chatlog/watchdog see refusal as a distinct safety signal vs. a
  generic completion.
- **Cache miss ~30k input tokens per spawn.** Log lines 40, 64, 79
  show `cache_miss_reason: 'system_changed'` with 30513-30545
  tokens missed each time. Per-spawn system prompt is drifting —
  likely the brake-settings JSON path or the deck addendum is
  changing between spawns. Quota concern (not safety). Investigate
  alongside caliber work where prompt-cache efficiency starts
  mattering more.

**Spine Phase 8 shipped (2026-04-30, late):** retired the legacy
`add_listener` / `remove_listener` / `on_event` / `on_change` /
`on_state_change` callback shims across five producers
(brake_state, profile_registry, plugin_registry, fleet,
connection_monitor) plus their consumers in tui.py and
daemon_session.py. Bus is now the only fan-out path; consumers
subscribe through `bus.subscribe(...)` with role-derived filters.
Per-callback exception isolation lives on the bus, so producers
no longer carry their own try/except loops. ~75 LOC net deletion.
Three callback patterns deliberately NOT migrated (Pool's
on_event, Daemon's on_daemon_event, Blacklist's on_event in
watchdog) — they're integration interfaces, not deprecated shims;
filed as Phase 8b candidates. Real-deck verified via running deck
session that exercised every migrated path. **The unified-event-
stream slice is now complete** (8/8 phases shipped).

**Kill state-stuck fix shipped (2026-04-30, late):** real-deck
caught both `k` (soft-kill) and `Shift+K` (blacklist + hard-kill)
leaving the construct pane stuck at `[RUNNING]` with the
chatlog showing the neutral `·` glyph + `state="running"`
suffix. Root cause: race between `Construct.kill()` and
`_consume`'s `await c.wait()` — both call `proc.wait()` on the
same Process; if `wait()` resumes first when the proc dies, it
correctly skips the DONE/FAILED overwrite (because
`_kill_requested` is set), but `_consume` then emits the finalize
meta event with `state="running"` BEFORE `kill()` reaches its
`self.state = ConstructState.KILLED` line. Fix in
`construct.wait()`: when `_kill_requested` is True and
`proc.wait()` returned (process confirmed dead), explicitly set
`state = ConstructState.KILLED`. Belt-and-suspenders with kill()'s
own state-flip — whichever runs first wins, the other is a no-op.
Filed as a gotcha (Async / subprocess section).

**Mechanic v0 shipped (2026-04-30, late):** sibling supervisor
process, ~270 LOC `mechanic.py`, no claude dependency, pure stdlib
+ ctypes for Windows PID alive checks. Tails the file logger's
NDJSON stream, derives live subprocess pids from
`fleet.spawn`-without-`fleet.finalize`, kills them on detected deck
death. Cross-platform from day one — no Windows Job Object
plumbing, no Linux PR_SET_PDEATHSIG, just `os.kill(pid, SIGTERM)`
in pure Python. Deck-side contribution: `pid` field added to
`fleet.spawn` payloads (one line in fleet.py via new `Construct.pid`
property) and `pid` added to log_header (so mechanic discovers the
deck PID by reading the header). `launch.bat` now spawns mechanic
in a minimized sibling window 1s after the deck launches. Real-deck
verified: mechanic attaches cleanly, reads correct deck pid from
header, watches at 2s cadence. Synthetic smoke test verified the
death-detection + cleanup path end-to-end. Full design at
`cyberdeck-maintbot-design.md` (Mechanic / maintbot — netrunner has
been calling it Mechanic; file rename deferred). The LLM-backed
half (v1+) is queued behind D1 substrate.

**Mechanic v0 known limitations** (filed for follow-up):
- **Only constructs are tracked.** Daemon, watchdog Q&A, watchdog
  authoring one-shots, and pool-warming subprocesses don't publish
  pids to the bus today; they orphan the same way they did before
  the supervisor existed. Each is a one-line addition to the
  respective spawn site once we get to it. The mechanic's
  `_apply_record` already handles arbitrary `kind=fleet.spawn`
  events; extending to other kinds is one elif per source.
- **Mechanic exits when the deck dies.** No auto-restart, no
  reattach. Each `launch.bat` run spawns a fresh mechanic. v0
  simplicity; v3 (auto-relaunch in headless mode) is the eventual
  shape.
- **Only really earns its keep in non-Windows-default cleanup
  scenarios.** Modern Win10/11 Task Manager does tree-kill, and
  Windows Terminal close propagates `CTRL_CLOSE_EVENT` to console
  children — those everyday paths already cleaned up subprocesses
  before mechanic existed. Where v0 actually matters: Python
  uncaught exceptions that escape the cleanup path, pythonw /
  detached scenarios, force-kill via `taskkill /F /PID` without
  `/T`, and the eventual Linux/Pi deployment (where none of
  Windows's accidental cleanup applies). Plus: substrate for v1
  LLM-session half.

**Spine progress (2026-04-30):** 7/8 phases shipped this push —
event_bus.py primitives, Fleet → bus, Daemon → bus, Tripwires +
Blacklist + authoring lifecycle → bus, Brake + Connection +
Profiles + Plugins → bus, direct chatlog writes → bus
(`_chatlog_event_buffer` retired, bus.snapshot() is the single
source of truth for chatlog readers), file logger as bus
subscriber writing per-launch NDJSON files at `<deck source>/logs/`
with header + footer + severity filter + reason-on-close, quit
discipline (silent SIGINT swallow + smart Ctrl+Q with running-state
toast). Phase 8 (cleanup — retire deprecated `add_listener` /
`on_event` / `on_change` shims now that everyone publishes through
the bus) is the last spine slice, queued behind Mechanic v0.

**Late 2026-04-30 follow-up session:** y/Y copy keybind shipped
(vim-yank focused widget to clipboard — lowercase rendered text,
uppercase structured JSON; new `clipboard.py` module with
ctypes Win32 / pbcopy / xclip-wl-copy cascade; sidesteps
Ctrl+C-as-copy SIGINT-into-subprocesses). Limits modal rework
(uncapped construct counts — max_concurrent ceiling of 9 retired;
defaults bumped 5/20/3 → 10/30/5; pool_size now editable mid-flight;
pool refill gate on `_spawn_warming_task` so a lowered target
stops oversubscribing on subsequent pulls; latent
`max_total_spawns == 0 = no cap` daemon-session guard finally
honors what the modal had advertised). Caliber design filed for
post-Mechanic-v0 implementation (see Filed entry below). Per-run
workspace compartmentalization filed in build plan item 8.

**Then**: D1 (local model substrate) for the long-term Watchdog /
synthesizer / Mechanic-LLM-half story. Plugin scaffolding,
brake-as-deck-state, connection spawn-blocking, brake-denial
visual, watchdog blacklist, watchdog Q&A persistence, tripwires
slice 1, and tripwires slice 2 (LLM authoring) all shipped in the
post-migration wave.

**Filed (2026-04-30, late):** **Caliber selection** — per-spawn
model + effort + fast-mode bundle the daemon picks based on task
needs and remaining quota; the daemon's own caliber is markable and
netrunner-overridable. Anthropic's effort surface (`output_config.
effort` API / `--effort` CLI / `/effort` slash command / settings
`effortLevel`) maps cleanly onto the deck's existing per-spawn
settings JSON plumbing. Pool stays single-caliber (sonnet+high
default); non-matching daemon-picked spawns fall through to fresh
— same shape as the existing "non-default profile spawns fresh"
pattern. Five phases filed; phases 1-3 + 5 ship without quota
awareness, phase 4 hard-blocks on build-plan item 13 (quota-aware
throttling). Implementation deferred behind Mechanic v0. Full
design at `cyberdeck-model-effort-design.md`.

**Filed (2026-04-30):** Mechanic two-tier architecture — supervisor
half (always-on, no LLM, cross-platform Python; PID tracking +
heartbeat + subprocess cleanup on deck death) + LLM session half
(on-demand, claude-backed; spawned only on heartbeat-fired triage
or netrunner summon). Architectural shift from the original
"single LLM-backed process" framing landed during 2026-04-30's
Ctrl+C autopsy when the netrunner asked whether the orphan-
subprocess problem was naturally a Mechanic responsibility.
Answer: yes, and giving the Mechanic a concrete always-on v0 job
(subprocess janitor) is materially better than starting it as
"diagnose-only when summoned." Full design at
`cyberdeck-maintbot-design.md`.

**Filed 2026-04-29:** **unified event stream / spine** — see
`cyberdeck-event-stream-design.md`. Captures the architectural
generalization that the slice 2 buffer-decay bug pattern made
inevitable. New top priority for implementation; absorbs the prior
"logger + quit discipline" slice as Phase 7 of the migration.

**Deferred mid-design (2026-04-27):** keymap revision pass and
daemon planning mode + pause/unpause. Both started this session,
both pulled before landing because the design needed more
bandwidth than was available. Working draft for the keymap
preserved at `cyberdeck-keymap-revision.md` with Layer 1 inventory
populated; the actions-first methodology (enumerate actions →
derive UI → derive keymap) is the new approach. Planning mode
revised intent: it's a **modal** the netrunner opens (not a
daemon state), used for goals too complex for a single-message
goal-set, post-confirm produces a persistent tracking panel akin
to Claude Code's "tasks" panel. Full notes in build plan items 9
and 11.

---

## What lives where

### Project files (the design canon)
- `/mnt/project/cyberdeck-spec.md` — base architectural spec
- `/mnt/project/cyberdeck-arbiter-addendum.md` — arbiter + wearable variant
- `/mnt/project/cyberdeck-compliance-future.md` — engagement-grade
  ingress filtering. *Deferred indefinitely.*
- `cyberdeck-maintbot-design.md` — supervisor / repair process
  architecture. Filed 2026-04-29; implementation deferred until
  the unified event stream lands (the maintbot reads from it).
- `cyberdeck-event-stream-design.md` — unified event bus / "spine"
  architecture. Filed 2026-04-29; new top priority for implementation.
  Absorbs the prior logger + quit discipline slice; substrate for
  maintbot, morgue, list-names, B2 synthesizer, tripwires slice 3.
- `cyberdeck-model-effort-design.md` — caliber (per-spawn model +
  effort + fast-mode) selection. Filed 2026-04-30; implementation
  queued behind Mechanic v0. Phase 4 (quota-aware fallback)
  hard-blocks on build-plan item 13.

### Outputs (working files; sync targets for chat artifacts)
- `cyberdeck-spec.md` (sync of canon)
- `cyberdeck-philosophy.md` — the *why*
- `cyberdeck-build-plan.md` — the *when*
- `cyberdeck-state.md` — this file
- `cyberdeck-claude-code-orientation.md` — onboarding for Claude Code
- `cyberdeck-tools-research-seed.md` — seed for a future tools chat
- `cyberdeck/` — Python source

### Code modules (12k LOC across 13 files)
| File | LOC | Purpose |
|---|--:|---|
| `tui.py` | 6102 | Textual UI, App, all modals, action dispatch |
| `watchdog.py` | 715 | Async question-queue oracle + streaming |
| `daemon.py` | 685 | Persistent coordinator (one-shot + streaming) |
| `fleet.py` | 611 | N concurrent constructs, event bus, NDJSON log |
| `daemon_session.py` | 570 | Fleet ↔ daemon glue, goal+netrunner-msg |
| `session_manager.py` | 557 | Pool + manifest |
| `construct.py` | 552 | Managed claude subprocess |
| `display.py` | 506 | Formatting (untruncated mode + origin badges) |
| `profile_registry.py` | 450 | File-watch profiles dir, hot reload |
| `profiles.py` | 399 | Profile dataclass + TOML loader |
| `connection_monitor.py` | 311 | Heartbeat → Online/Degraded/Offline |
| `dispatcher.py` | 138 | Deck-control script (deck-side stdout protocol) |
| `mock_*.py` | 127+146 | Test fixtures |
| `main.py` | 101 | CLI entry |

---

## Shipped features (working in production)

### Tier 1 — original scope (long stable)
- Construct lifecycle (spawn / inject / kill / interrupt)
- EJECT (Ctrl+F → confirm modal)
- Limits modal (`l`)
- Session pool with cross-restart reuse (5h stale window)
- Activity chatlog (B1) — mechanical event extraction

### Tier 2 — Profiles (refactored post-migration)
- TOML loader, ProfileRegistry, hot reload, default seeded
- Daemon picks profile per-spawn via JSON
- Profiles are **prescriptive templates**: instructions + recommended
  tool list. They do NOT enforce — the brake hook does.
- `recommended_tools` (renamed from `allowed_tools`) surfaced in the
  construct's system-prompt addendum as a soft suggestion. Construct
  still has full default tool set.

### Plugin scaffolding — third leg of tool registry (shipped post-migration)
- Plugin = capability bundle at `<home>/plugins/<name>/` with
  `plugin.toml` (manifest), `README.md` (LLM-facing interface docs),
  and an executable entry point (typically `run.py`).
- Stateless v1: each invocation is a fresh subprocess that
  constructs spawn via Bash. Persistent plugins, MCP-shaped plugins,
  and the wiring keys (`p` airgap, `c` quickfire, `Shift+C` picker)
  are deferred sub-shapes.
- Manifest fields: `name` (slug), `category`, `description`, `entry`,
  optional `[requires]` block (`platforms`, `python_imports`).
  Failing requires checks downgrade the plugin to `available=False`
  with a reason; it stays in the registry so the panel shows what's
  installed.
- `PluginRegistry` mirrors `ProfileRegistry`'s read API but is
  one-shot (`scan()` at startup, no hot reload — plugins are code,
  Python module reloading is fraught).
- Tools panel grows a "PLUGINS" section between Profiles and Scripts.
  Unavailable plugins render with a red ✗ marker and dimmed name.
- Daemon system prompt grows a PLUGINS catalog (only available ones,
  one line each); construct system prompt addendum gains plugin
  awareness with explicit invocation patterns.
- First plugin: `screenshot` — mss-based cross-platform screen
  capture, ~140 LOC. Real-deck verified end-to-end: construct
  invokes via Bash, captures PNG, reports path back.

### Brake state — deck-global (replaces per-profile brake)
- Three levels: paranoid / default / yolo. Set via `b` modal
  (paranoid is single-press, yolo requires EJECT-style 3s held-key
  confirmation, mirroring the deliberate-consent gesture).
- Persists at `<home>/.cyberdeck/state.json`.
- Sidebar indicator next to connection state: ▲ paranoid (yellow),
  = default (white), ▼ yolo (red).
- Enforcement via Claude Code's PreToolUse hooks. Each spawn gets a
  per-construct `--settings` JSON pointing at `brake_hook.py` with
  current brake passed via argv. Hook is self-contained ~180 LOC,
  exits 0 (allow) or 2 (deny). Stderr text becomes the
  model-visible denial reason.
- **Both Bash and PowerShell are gated.** Claude Code on Windows
  exposes PowerShell as a separate tool with the same `command`
  shape as Bash. A construct given Bash-denied will silently pivot
  to PowerShell — verified on real-deck without the construct being
  asked to. Both shells share `SHELL_TOOLS` set in the hook; both
  go through the same destructive-pattern + protected-path checks
  under default brake; both are in `PARANOID_DENY_TOOLS`.
- Static patterns are short and opinionated: destructive bash regex
  (rm -rf on system roots, format, dd of=/dev/, mkfs, fork bombs,
  shutdown, sc/net stop), OS-root path prefixes (Windows + Unix),
  and three brake-config sentinel filenames (brake_hook.py,
  brake_state.py, brake_patterns.py). The deck-source-dir-as-
  substring check was tried and dropped — cyberdeck-home/ is a
  subdirectory of the deck source, so a substring match
  inadvertently denied every legitimate plugin and dispatcher
  invocation. Sentinel filenames are precise enough.
- Mid-flight propagation deferred — brake state is captured at
  spawn and baked into that construct's lifetime. New spawns see
  the new value.
- Watchdog observes via `permission_denials` field on result events;
  chatlog renders `· brake blocked: Write×2, Bash×1` suffix on
  finalized lines. Watchdog system prompt grew brake awareness.

### Right-panel listification (C1g) + Phase A/B
- Tools tab → ListView (profiles + scripts)
- Files tab → ListView (FileListItem)
- LaunchScreen modal (space on profile/file → launch)
- Dispatcher protocol: `__CYBERDECK::v1::ACTION::PAYLOAD__`
- `dispatcher.py` bootstrapped to `<home>/tools/deck/cyberdeck.py`
- Construct system prompt teaches dispatcher invocation
- Verified end-to-end on real Windows construct

### Pane-log un-trim
- ConstructPane raw event buffer + `render_buffer(untruncated=True)`
- Modal mode: 5000-char cap (vs 500 live), full thinking blocks
- `display.py` formatters accept `untruncated` kwarg

### Watchdog Q&A (`t`)
- Async question→answer oracle in `watchdog.py`
- AskWatchdogScreen modal (yellow-themed, queue-depth hint)
- **Streaming default**: persistent `claude --input-format stream-json`
  subprocess; questions become JSONL writes; answers via stream-json
- One-shot fallback (`streaming_mode=False`)
- **Wedge recovery**: timeout → kill subprocess → respawn fresh on
  next ask (production bug: queued questions stayed stuck forever
  before this fix)
- Context: last 30 chatlog events, plain-text, no markup
- Answers route to chatlog as `[watchdog] → ...` AND to dedicated
  Watchdog tab with paragraph fidelity

### Daemon chat (`T`)
- TalkDaemonScreen modal (primary-themed; soft/loud counterpart to `t`)
- `set_pending_netrunner_message` on DaemonSession (FIFO stack)
- `_format_outcomes` prepends `≫ NETRUNNER MESSAGE:` preamble with
  numbered list when stacked
- Empty-outcomes-only branch produces clean message
- No-session warning + drop with toast
- Wake-event keeps idle delivery prompt

### Goal-edit mid-flight (`e`)
- Mid-flight block lifted; opens modal pre-filled with current goal
- `_classify_goal_diff` heuristic: tokenize+stem+Jaccard
  → `clarification` / `scope-change` / `pivot`
- `set_pending_goal_update` on DaemonSession; outcome-loop wakes idle
- `_format_outcomes` prepends GOAL UPDATE preamble with classification-
  tailored advice
- Identical-goal submit no-ops with toast
- Force-push deferred to M5+

### Connection state monitor
- `connection_monitor.py`: heartbeat to api.anthropic.com:443
- States: Online (●green) / Degraded (◐yellow) / Offline (●red)
- Sidebar indicator + chatlog announcements on transition
- DNS failure skips Degraded → Offline directly
- Threshold-based: 2 failures → Degraded, 1 success → Online
- `record_subprocess_error(stderr)` hook (not yet plumbed)
- Spec'd consequences NOT YET WIRED: spawn-blocking, daemon parking,
  recovery flow

### Streaming defaults
- Daemon `streaming_mode=True` (was opt-in; user observed "nuclear
  speed improvement"). `--no-streaming` opts out.
- Watchdog `streaming_mode=True`. Persistent subprocess shared across
  questions; conversation accumulates so watchdog "remembers" earlier
  questions in session.

### Tabbed bottom panel
- `[Daemon] [Watchdog]` tabs in `TabbedContent(id="daemon_bar")`
- WatchdogPane mirror of DaemonPane (yellow, status with queue-depth)
- Both inner logs focusable (W/S nav reaches them via fall-through)
- Space on daemon_log → action_talk_daemon; space on watchdog_log →
  action_talk_watchdog

### Watchdog log (persistent Q&A history — v1)
- `WatchdogHistory` + `WatchdogHistoryEntry` in `watchdog.py`.
  Append-only JSONL at `<home>/.cyberdeck/watchdog.jsonl`. Each
  resolved question is persisted by `_safe_callback` BEFORE the
  listener fires (so the entry survives a listener crash).
- TUI replays the last 50 entries on `on_mount` via
  `_replay_watchdog_history`, with `──── prior session (N entries)
  ────` / `──── live session ────` separators in the WatchdogPane
  so historical and current Q&A are visually distinct.
- Per-entry `kind` field (currently always "qa") futureproofs the
  file for tripwire / blacklist record kinds. Schema-drift
  tolerant: replay drops unparseable lines silently, skips
  non-qa kinds.
- Best-effort throughout — persistence is observability, not
  correctness. Disk errors don't crash the watchdog. Parent
  directory created on demand if missing.
- First slice of the netrunner's "deck history infrastructure"
  brainstorm; the morgue (session-level history + resuscitation)
  remains deferred.

### Watchdog Tripwires (deterministic matchers, slice 1)
- New `tripwires.py` module — `Tripwire` dataclass, `TripwireEngine`,
  text-extraction helpers, and `DEFAULT_TRIPWIRES`. Spec model "LLM
  authors, deterministic enforces" — same architecture as the brake
  hook, but the matchers run in-process per construct event rather
  than as a per-tool subprocess hook.
- **Small DSL** (per netrunner direction — regex-only would risk the
  same over-block class as the brake hook): each tripwire carries a
  `pattern_type` (today only "regex", designed to grow), `pattern`,
  `event_kinds` (which EventKind values this matcher applies to —
  empty tuple means "all kinds"), and `field` (which extracted text
  to match against — `tool_use_command`, `tool_result_content`,
  `thinking_text`, `assistant_text`, `tool_use_input`, `user_text`,
  or `any`). The field selector keeps matchers precise — won't
  false-fire on assistant text mentioning a dangerous command
  pattern that's only a problem when actually executed.
- **Severity tiers** declared (low / warning / critical) but rendered
  uniformly today (slice 1). Slice 3 splits per-severity rendering
  (critical pulls focus, warning badges, low logs only).
- **Scope** field gates tripwires per-construct or deck-global. Per-
  construct entries carry a target `construct_id`; the engine only
  fires them for events from that id.
- **Origin** field tracks where the tripwire came from: `default`
  (ships with the deck), `manual` (registered via API), `llm_authored`
  (slice 2), `blacklist_derived` (deferred — would auto-generate
  per-construct tripwires from blacklist entries to catch in-flight
  matches as events stream rather than at K time).
- **Engine ownership**: lives on the Watchdog (per spec). Default
  tripwires installed automatically at Watchdog construction. The
  TUI registers a Fleet listener (`_scan_for_tripwires`) that feeds
  every construct event into `watchdog.tripwires.scan()`. Fires
  dispatch via the `on_fire` callback to the TUI's
  `_handle_tripwire_fire`, which renders to the chatlog with
  severity-colored markup (`yellow` for warning, `dim yellow` for
  low; `red b` reserved for critical when slice 3 lands).
- **Two default tripwires shipped:**
  - `keyword_credentials` — `\b(password|api[_\s-]?key|secret|
    credentials?)\b` matched against `tool_result_content` only,
    severity `low`. Catches accidental secret exposure in logs /
    fetched responses.
  - `keyword_destructive_sql` — `DROP TABLE` / `TRUNCATE TABLE` /
    `DELETE FROM <table>` matched against `tool_use_command` only
    (Bash + PowerShell shapes), severity `warning`. Different
    vector from the brake hook's bash-shaped destructive patterns
    (rm -rf, format) but similar blast radius.
- **Defensive register/scan**: bad regexes log to stderr and skip
  registration rather than crashing the engine; per-listener
  exceptions in `on_fire` dispatch are caught so a misbehaving
  listener can't corrupt the engine; the TUI's listener wraps the
  scan in a defensive try/except so a malformed event payload
  can't break chatlog rendering.
- **Watchdog system prompt** grew a TRIPWIRE AWARENESS paragraph so
  Q&A like "any tripwires fired?" / "what's this tripwire about?"
  works against the chatlog markers.
- **Verified end-to-end** with 8 unit tests + an end-to-end chain
  test covering: default tripwire matches, precision (assistant
  text doesn't trip the credentials tripwire), per-construct
  scoping, bad-regex graceful skip, unregister, ANY-field
  aggregation, re-register replacement, and Fleet→Engine→TUI
  rendered output shape with severity styling differentiation.
- **Slice 2 shipped 2026-04-29 (LLM authoring).** Pulled out into
  its own section below for readability. Per-outcome adaptive
  re-authoring remains deferred — needs a "daemon signals plan
  shift" event we don't have yet.
- **Other future slices**: severity-aware routing (slice 3 — critical
  pulls focus); persistent tripwire library at `<home>/tripwires/`
  with TOML authoring (slice 4); daemon-side severity hints (slice
  5); blacklist-derived tripwires that catch in-flight matches by
  scanning event content rather than just task fingerprints
  (slice 6 — pairs with the existing in-flight match scan).

### Watchdog Tripwires (LLM authoring, slice 2)
- **`Watchdog.author_tripwires(goal, *, classification, old_goal,
  brake_label, blacklist_summary, timeout)`** runs one authoring pass
  via a fresh `claude -p` one-shot subprocess. Returns
  `TripwireAuthoringResult` (success / registered / rejected /
  used_resume / error / elapsed_s / raw_response).
- **Two-rung substrate.** Rung 1: when the watchdog's streaming Q&A
  subprocess has captured a session_id (from the `system`/`init`
  event), authoring spawns its one-shot with `--resume <id>` to
  **fork** the running Q&A session — the authoring model inherits
  the conversation context (knows what the watchdog has been asked
  about so far, what's happening in the fleet) without writing back
  into the live Q&A subprocess. Rung 2: no session captured (cold
  start, streaming disabled, post-wedge) → fresh one-shot, no
  conversation history but the same goal/brake/defaults/blacklist
  context via the user message body. The chatlog labels each pass
  `(fork, …s)` or `(fresh, …s)` so the netrunner can spot when fork
  is silently failing and falling back. No auto-fallback today —
  the choice is deterministic per-call based on whether
  `_session_id` is set.
- **Trigger sites in TUI:** `_start_daemon_task` covers both
  `--goal` launch and idle→running submit (one path serves both).
  `_handle_goal_submitted`'s mid-flight branch covers explicit `e`
  edits, gated on `classification != "clarification"` —
  clarifications add detail without changing direction so re-running
  authoring would burn tokens for no signal change. Pivots and
  scope-changes re-author from scratch.
- **Lifecycle: clear, then register.** Each authoring pass calls
  `engine.clear_by_origin(Origin.LLM_AUTHORED)` BEFORE registering
  the new entries. Defaults / manual / blacklist-derived entries
  stay untouched. "Replace, don't accumulate" — old-goal rules don't
  linger after a pivot. Even authoring failures (subprocess error,
  timeout, unparseable JSON) clear the prior LLM-authored set
  before bailing — old rules shouldn't survive intent shifts just
  because the substrate failed.
- **Authoring system prompt** is a separate constant
  (`TRIPWIRE_AUTHORING_SYSTEM_PROMPT` in `tripwires.py`) embedded in
  the user message body rather than passed via
  `--append-system-prompt`. Two reasons: (1) rung-1 forks resume
  sessions whose system prompt is already the Q&A one — we
  mode-switch via in-body instructions rather than layering, which
  composes more predictably, and (2) multi-line argv content with
  `--append-system-prompt` has Windows mangling issues per the
  watchdog one-shot path's existing comment. Single source across
  both rungs.
- **JSON parser is tolerant.** Strict parse first, then markdown
  fence extract (claude regularly wraps despite "no fences"
  instructions), then balanced-brace fallback. Per-entry validation
  rejects bad fields/severities/scopes/duplicates with reason. The
  engine's `register` was changed from returning `None` to returning
  `bool` so regex-compile failures get added to the rejected list
  too — slice-1 callers that ignore the return value still work
  unchanged.
- **Fire-and-forget at the call site.** TUI's
  `_kick_off_tripwire_authoring` spawns the worker via
  `self.run_worker(...)`; goal-set / goal-update flow continues
  immediately. The first few construct events may stream in before
  authored rules land — that's fine, the two default deck-wide
  tripwires (credentials, destructive SQL) cover the baseline. The
  authoring task self-announces start (`[dim][watchdog] authoring
  tripwires for current goal…[/dim]`) and renders one of three
  completion shapes:
  - Success with rules: `[yellow][watchdog] +N tripwires authored
    (fork|fresh, Xs):[/yellow] name1, name2, …` + one dim line per
    rejected entry with reason
  - Success with no rules: `[dim][watchdog] authored 0 tripwires
    (…) — no rules applied[/dim]` (legitimate outcome — model
    decided no patterns warranted, better than padding)
  - Failure: `[red][watchdog] tripwire authoring failed[/red]
    (…)` + raw-response preview if it was a parse failure
- **Watchdog Q&A system prompt** grew an updated TRIPWIRE AWARENESS
  paragraph: distinguishes default vs LLM-authored tripwires,
  explains the new chatlog markers (`[watchdog] +N authored`,
  `authoring failed`, etc.), tells Q&A how to answer "what
  tripwires are active?" against the chatlog markers (no live
  registry plumbing — slice 2's Q&A view is still chatlog-derived).
- **Verified inline:** parser shape (strict / fenced / brace / mixed
  valid+invalid / empty / garbage), engine `clear_by_origin`
  lifecycle, `register` bool return, prompt-builder shape for both
  goal-start and goal-update inputs. Real-deck smoke pending —
  the rung-1 `--resume` fork against a live streaming session is
  the one piece that can't be confidently mock-tested. Behavior to
  watch on first real-deck run: does `--resume <id>` against a
  session whose original streaming subprocess is still alive
  produce a clean fork, or does the server reject / mangle? If the
  latter, fall back to forcing rung 2 (delete the session_id
  capture) until we can dig into the server-side semantics.

### Watchdog Blacklist (session-scoped, populated by Shift+K)
- `Blacklist` + `BlacklistEntry` in `watchdog.py`. Lives on the
  Watchdog per spec ("the persistent memory of what's forbidden").
  Session-scoped, in-memory; cleared when the watchdog shuts down.
  Cross-session stickiness deferred (spec lists as open question).
- Fingerprint = first 80 chars lowercased of the killed task's text.
  Matches the existing daemon-session respawn-detector scheme so the
  daemon's mental model of "same task" is consistent across both
  surfaces. Loose by design.
- Entry carries rich context (fingerprint, full_task, source
  construct id/state/final_output/files_written, reason, timestamp)
  for the future tripwire-authoring pass to read; today only the
  fingerprint is consulted by matchers.
- `Shift+K` registers the focused construct's fingerprint with the
  blacklist before killing — replaces the prior "blacklist not yet
  implemented; soft-killing" toast. Soft-kill `k` unchanged.
- DaemonSession `_execute_action` checks each spawn against the
  blacklist; matches are refused with feedback in the next outcome
  turn (and a `⚠ blacklist: spawn refused` line in the daemon pane
  immediately). Spawn is NOT counted against caps when refused.
- `_format_outcomes` surfaces the active blacklist on every outcome
  turn as a `⛔ SESSION BLACKLIST` block at the top of the message
  with one line per entry. Daemon sees what's forbidden persistently
  and is told to halt branches that depended on a blacklisted shape
  rather than rephrase around the fingerprint.
- In-flight matching constructs get a red `.-blacklisted` border on
  their pane (mirrors the `.-blocked` brake-denial pattern in shape;
  red vs yellow to differentiate netrunner-authored from static-rule
  blocking) plus a chatlog notice. Per netrunner direction: flag, do
  NOT auto-kill — automatic mass-kill is what EJECT is for.
- Watchdog system prompt grew a BLACKLIST AWARENESS paragraph so
  questions like "what's blacklisted?" or "why was that spawn
  refused?" get useful answers from the chatlog markers.
- Tripwire half (LLM-authored matchers, DSL, severity routing) still
  deferred — slice 2.

### Spawn provenance (origin badges)
- `fleet.spawn(..., origin=...)` — `daemon` / `netrunner` / `inject`
- Threaded into `spawned` meta event payload
- Chatlog renders cyan `[you]` for netrunner, `[↳you]` for inject;
  daemon spawns un-badged
- Watchdog system prompt includes badge legend (so it stops reverse-
  engineering attribution from log timing)

### z-to-view (file/profile/script)
- `action_expand` on FileListItem / ProfileListItem / ScriptListItem
  opens ExpandModal with file content from disk
- Pygments syntax highlighting via `rich.syntax.Syntax` for ~30
  recognized languages
- Theme: `github-dark`. Line numbers off (gutter tint reads as
  "highlight" — too aggressive)
- Detection cascade: extension → bare-name → shebang
- Plain-text fallback with bracket escape for unknown extensions
- 2MB size cap; UTF-8 with replacement
- Modal scroll bindings: w/s line, PgUp/PgDn page, Home/End jump

### Path-normalized Files panel dedupe
- Bug: Windows backslash vs forward slash + dispatcher
  `Path(p).resolve()` produced literal-distinct strings → double-listing
- Fix: `os.path.normcase(os.path.normpath(p))` as dedupe key
- Same normalization on `_remove_file_from_panel`

### Focus navigation fall-through
- W/S walks within section; at section edge, falls through to
  up/down neighbor section
- Empty sections skipped transitively
- Trap fix: when chain dead-ends through empty sections (e.g. W from
  daemon_bar with empty main), fallback lands on any populated non-
  source section. Layout edges (true None terminator with no walking)
  stay put. Distinction: `walked=True` flag.

### UI infrastructure
- ExpandModal universal magnifier (`z`) — RichLogs, ConstructPanes,
  list items
- Rich text preservation via Text/segment round-trip
- Modal Tab fix: App-level Tab delegates to `screen.focus_next`
- `?` keybinds modal slim
- Quit unified to `ctrl+q`
- Path shortening utility (`_shorten_path`)
- Connection indicator in sidebar
- `sys.unraisablehook` filter for Windows Proactor closed-pipe noise

---

## Key design decisions (carried forward)

1. **Brake state is deck-global, not profile-attached.** The
   netrunner sets it via `b`; it applies to every new spawn until
   changed. Watchdog can ratchet up (toward paranoid) but not down
   — that's the netrunner's exclusive prerogative.
2. **Profiles are prescriptive, not restrictive.** They steer with
   addendums and suggest tools via `recommended_tools`; they do NOT
   gate capability. Runtime gating is the brake hook's job, deck-wide.
3. **Brake enforcement is via PreToolUse hook, not `--allowedTools`.**
   The hook is deterministic (regex/path matching, no LLM in the hot
   path). Watchdog observes denials and authors the hook's policy
   over time (LLM authors, deterministic enforces).
4. **Default profile auto-seeded; netrunner edits sacred.**
5. **Lowercase = within-focus, uppercase = move-focus.** `z` for zoom.
6. **`space` is "primary interact"; `z` is magnify.**
7. **Truncation: 500 live, 5000 modal.** Bounded against megabytes.
8. **Pool always warms with `default`.** No per-profile pools.
9. **Files panel: dual path with dedupe (normalized).**
10. **Marker protocol one-way (script → deck).** Versioned. Unknown
    action logs warning; never crashes.
11. **Tools panel:** Profiles + Scripts only. Built-ins not surfaced.
12. **Goal-update propagation deferred to next break.** Force-push is
    M5+. Wake-event keeps idle sessions responsive.
13. **Goal-diff classifier is heuristic, not model-driven.**
    Cheap; "good enough"; can model-ify later.
14. **ConnectionMonitor presumes ONLINE at start.**
15. **DNS failure skips Degraded.** Clean signal: no network at all.
16. **Watchdog runs cloud Claude today.** Local-model substrate (D1)
    is the eventual home.
17. **Streaming is the default; one-shot is the fallback.** For both
    daemon and watchdog.
18. **Streaming wedge → kill, don't preserve-and-pray.** Once stuck,
    fresh subprocess is the only reliable recovery.
19. **Origin attribution at source, not reverse-engineered.** Fleet
    payload carries who spawned each construct.
20. **z-modal:** bracket escape on plain text, syntax highlighting
    on known languages, github-dark theme, no line numbers.
21. **Tripwire authoring forks the watchdog's Q&A session via
    `--resume <id>` rather than running on a fresh isolated subprocess.**
    The authoring model gets the same situational awareness the Q&A
    side has accumulated (recent fleet activity, prior questions
    answered) without writing back into the live Q&A conversation.
    Falls back to a fresh one-shot when no session_id is captured
    (cold start, streaming disabled, post-wedge). Server-side
    semantics of concurrent `--resume` against a live streaming
    session aren't fully proven — slice 2 ships the design and trusts
    real-deck testing to confirm. If `--resume` misbehaves under
    concurrency, dropping `_session_id` capture flips the whole thing
    to rung 2 with no other code change.
22. **LLM_AUTHORED tripwires use clear-and-replace, not accumulate
    -and-update.** Each authoring pass drops prior LLM-authored
    entries before registering new ones; defaults / manual /
    blacklist-derived entries stay untouched. Rejected alternative
    "register-by-name updates in place" because old-goal rules linger
    forever otherwise — pivot to unrelated work, yesterday's
    credentials-hunting rule still fires. Even authoring failures
    clear the prior set, so a substrate hiccup doesn't preserve
    rules whose original goal context is gone.
23. **Authoring skips on clarification-class goal updates.** The
    `_classify_goal_diff` heuristic is repurposed as a re-author
    gate: pivots and scope-changes get a new authoring pass; pure
    clarifications (old goal is a strict subset of new) skip — the
    netrunner is adding detail to existing direction, the model
    already authored for it, re-running burns tokens for no signal.

---

## Filed gotchas (institutional memory; cumulative)

### Terminal / Textual
- **Don't shadow Textual `Widget._render()`.** It's a real method
  on the base class that returns a `Visual`. Overriding it with a
  custom render method returns `None` (or whatever your method
  returns) and crashes Textual's render pipeline with
  `AttributeError: 'NoneType' object has no attribute 'render_strips'`
  in `widget.py:_render_content` → `Visual.to_strips`. Real-deck
  caught 2026-05-01 on the first slice 3 phase 1 attempt: `DelayList
  Item._render` shadowed the parent. Crash on first paint of the
  Delays tab. Fix: rename your custom render method to anything else
  (`_paint`, `_redraw`, `_update_text`). General rule: any
  underscore-prefixed method on a Widget subclass should be checked
  against Textual's API before being added — Textual treats
  underscore-prefix names as protected, not private.
- **A widget render-crash can leave the tree in a state that
  silently breaks unrelated mutations.** Real-deck observed
  2026-05-01 (post-fix of the `_render` shadowing above): after
  the crashed deck was restarted, finalized construct panes
  stopped moving to the bottom of `#main` even though
  `_compact_pane_after_delay` was running and `pane.compact`
  was being set. Restarting the deck a second time cleared it
  ("heisenbug"). Hypothesis: the prior session's render crash
  corrupted some Textual-side widget bookkeeping that survived
  into the next launch via `cyberdeck-home/` state or process-
  group quirks; or asyncio worker scheduling got starved by a
  backlog of crashed widgets. Mitigation pattern: when a
  Textual widget crashes during render, restart the deck
  before trusting any subsequent UI behavior. Diagnostic
  pattern: surface widget-mutation calls via `fleet_log.write`
  AND a bus event so the file logger captures both
  "scheduled" and "fired" lifecycle markers — without bus
  visibility, "did the timer fire?" requires netrunner-screen
  observation. See `_schedule_compact_pane` /
  `_compact_pane_after_delay` in tui.py for the diagnostic
  pattern (shipped in commit e33ec75 after the heisenbug).
- **`shift+space`, `ctrl+space`, `ctrl+i`, `ctrl+m`** rarely transmit
  distinctly in real terminals. Trust pilot for binding wiring; trust
  real terminal for capability.
- **Textual `Widget.name` is read-only.** Don't shadow.
- **`Log.lines` is `list[str]`; `RichLog.lines` is `list[Strip]`.**
- **Markup leaks via `\n`.** Collapse before writing.
- **`wrap=True` + `min_width=1` inside an inactive TabPane** pre-wraps
  content at 1 char per line and caches Strips. Use `wrap=False` for
  logs in non-default tabs OR buffer-and-replay on activation.
- **`can_focus=False` on a Static-derived class is a no-op** because
  Static defaults that way.
- **Modal screens don't inherit App BINDINGS.** Redeclare on the modal.
- **App-level priority bindings** beat modal priority. Delegate from
  App action when `isinstance(self.screen, ModalScreen)`.
- **ListView focus model:** the focused widget IS the ListView;
  `.highlighted_child` is cursor.
- **Two TabbedContents need `id` to disambiguate `query_one`.**

### Rich markup
- **Markup escape:** `\[` for opening bracket, closing `]` is literal.
  Use raw f-strings (`rf"..."`) to silence Python escape warnings.
- **File contents need bracket escape** before going to a markup-
  enabled RichLog. `[default]` TOML headers will be parsed otherwise.
- **`rich.syntax.Syntax` returns a single Renderable** but RichLog
  splits it into Strips, so scrolling stays line-by-line.

### Async / subprocess
- **`stdin.close()` on Windows ProactorEventLoop is fire-and-forget.**
  Always pair with `await stdin.wait_closed()` (with a timeout).
  Without this, transport `__del__` fires on a half-closed socket and
  raises `ValueError: I/O operation on closed pipe` after Ctrl+C.
- **`sys.unraisablehook` is the place** to filter known-harmless
  GC-time noise.
- **"Preserve the wedged proc, hope it recovers" is always wrong**
  when the failure mode is read-hang. Once wedged, kill and respawn.
- **Streaming subprocesses accept writes long after they've stopped
  reading.** Broken-pipe errors don't fire reliably for read-hangs.
  Drain timeouts are the real signal.
- **`-p` not immediately followed by a value** makes claude treat it
  as "read from stdin." Always pipe prompts via stdin
  (`proc.communicate(input=...)`).
- **Rapid heartbeat tests are racy.** Use `wait_for(predicate)` with
  timeout, not fixed sleeps.
- **Construct kill races construct finalize emission.** Both
  `Construct.kill()` and `_consume`'s finalize path call
  `await proc.wait()` on the same Process object. asyncio doesn't
  guarantee resume order when the proc dies, so two interleavings
  exist: (a) kill() resumes first → sets `state = KILLED` → wait()
  resumes → sees `_kill_requested`, doesn't overwrite → finalize
  emits `state="killed"` (correct); or (b) wait() resumes first →
  sees `_kill_requested`, doesn't overwrite (state is still
  "running") → _consume emits `state="running"` → kill() resumes
  too late, sets state=KILLED but the bus event already carried
  the wrong value. Real-deck symptom (2026-04-30): pane stuck at
  `[RUNNING]` after `k` or `Shift+K`, chatlog shows `· cx-...:
  running (5.1s)` with the neutral fallback glyph instead of the
  orange `×`. Fix in `Construct.wait()`: when `_kill_requested` is
  True AND `proc.wait()` just returned (process confirmed dead),
  explicitly set `state = KILLED` in the non-overwrite branch.
  Belt-and-suspenders with kill()'s own state-flip; whichever runs
  first wins, the other is a no-op. The deeper lesson: when two
  coroutines wait on the same `proc.wait()`, they BOTH need to be
  prepared to write the terminal state, because either could
  resume first. "Skip the overwrite to respect existing state"
  only works if the existing state is the right one — which here
  it wasn't (RUNNING is not a terminal state).
- **Windows console Ctrl+C reaches every process in the console
  group, not just the Python parent.** Installing a Python-level
  SIGINT swallow (`signal.signal(SIGINT, lambda: None)`) protects the
  parent process from terminating, but child claude subprocesses
  still receive the Ctrl+C event independently from the Windows
  Console subsystem. Symptoms when the netrunner hits Ctrl+C while
  the deck is running:
  - claude's CLI interprets the signal against in-flight tool use as
    "user rejected the tool," producing a `tool_result` with content
    "The user doesn't want to proceed with this tool use" and
    `terminal_reason: "aborted_tools"`. The construct usually still
    finalizes with `state: "done"` and a useless `final_output`.
  - On Windows, `claude` is typically a cmd.exe batch wrapper around
    the actual node CLI (npm-style). cmd.exe catches the Ctrl+C, the
    wrapped process exits, and cmd.exe writes its standard "Terminate
    batch job (Y/N)?" prompt to stdout before exiting. Subprocess
    callers that read stdout (e.g. tripwire authoring's `claude -p`)
    see the prompt fragment as the model's response, parse fails.
    Real-deck symptom (2026-04-30): `tripwire.author_failed` with
    `raw_response: "Execution errorTerminate batch job (Y/N)?"`.
  - The streaming daemon / watchdog Q&A subprocess can wedge under
    the same disruption (writes succeed, reads hang) — recovers via
    the existing 60s drain timeout but loses the in-flight turn.
  Path forward: **don't fix at the OS level.** Job Object with
  KILL_ON_JOB_CLOSE was considered + rejected as Windows-specific
  baggage. The right fix is the Mechanic's supervisor half (cross-
  platform Python-level subprocess janitor — see
  `cyberdeck-maintbot-design.md`) PLUS an in-deck copy keybind that
  sidesteps Ctrl+C entirely (filed as a small QOL slice in the build
  plan). Workaround until those land: don't press Ctrl+C with no
  selection; use Windows Terminal's copy-on-select if configured.
  The deck survives the disruption gracefully (constructs finalize,
  daemon recovers via timeout, watchdog Q&A still answers
  post-mortem accurately), so the bug is annoying but not blocking.

### File paths
- **String equality on file paths is wrong on Windows.** Forward vs
  backslash, drive letter case, and resolve-vs-raw all break literal
  compare. Use `os.path.normcase(os.path.normpath(p))` for dedupe.
- **`Path(p).resolve()`** can normalize differently from how the
  original was passed; don't rely on it for stable identity.
- **Path shortening keeps absolute version stored separately.**
- **Windows path mangling in Bash.** Constructs self-correct from
  absolute `C:\...` to relative when their first attempt fails.
- **`logs/latest.log` is a stale empty snapshot on Windows.** The file
  logger's `_update_latest_pointer` tries `symlink_to` first (works on
  POSIX, requires admin / dev mode on Windows) then falls back to
  `shutil.copy2`. The copy fallback runs once at startup AFTER opening
  the per-launch file but BEFORE writing the header — so on Windows
  without admin, latest.log is permanently a zero-byte snapshot and
  doesn't track the real file's growth. Mechanic v0 sidesteps this by
  scanning `cyberdeck-*.log` and picking the newest by mtime within a
  freshness window. Anything else that wants to tail the active log on
  Windows has to do the same. Fix would be `latest.log.write_text(...)`
  on every event (perf) or a Windows symlink with elevated permission
  request (security UX). Both more annoying than the workaround.

### Editing
- **`str_replace` ate a class header once** (GoalSetScreen) — when
  matched block ends just before `class X:`, double-check. Compile-
  clean doesn't mean structurally clean.
- **`str_replace` ate a docstring close.** New content with `"""`
  mid-replacement, double-check the close didn't end up orphan.
- **Bare `except Exception: pass` around mixed-failure-mode code
  hides real bugs.** Scope try/except tightly.
- **Local var names shadowing kwargs** are a footgun even when they
  technically work.

### Logic
- **`_format_outcomes` empty-outcomes branch.** Conditional headers.
- **Directional fall-through needs a `walked` flag** to distinguish
  layout edges from dead-end empty chains.
- **`_focus_section` branches need to be re-checked when section
  contents change.** No-op return is silent.
- **`_right_panel_focusables` is hand-curated, not auto-derived
  from compose().** Adding a new ListView to the Tools tab without
  also adding it here makes it visible-but-unreachable via W/S.
  Burned this when adding the Plugins section. Look here whenever
  the right panel grows a new section.

### Daemon / task plumbing
- **Markdown autolinks bake into filenames if not stripped.** When
  daemon outcomes contain URLs, the daemon (claude subprocess)
  auto-wraps them in markdown autolink syntax — `[text](url)` — in
  its response. That syntax survives into the next task's text and
  constructs read it literally. Real-deck case: a research-goal
  report-write task contained `super_chipmunk_engine_[report.md]
  (http://report.md)` and the construct dutifully created a file
  called `super_chipmunk_engine_[report.md]`, brackets and all.
  Fix in `daemon_session._execute_action`: strip markdown autolinks
  from the spawn action's task field before passing it to the
  fleet (`_strip_markdown_autolinks` regex helper). Belt-and-
  suspenders: daemon system prompt now explicitly tells the daemon
  to use plain text in task strings (no markdown link syntax, no
  fenced code blocks, no inline formatting). Constructs read tasks
  as literal strings; markdown is pure noise at that boundary.

### Brake / hook
- **LLMs route around denial.** A construct given Bash-denied will
  pivot to PowerShell automatically without being asked — verified
  on real-deck after the brake hook initially over-blocked
  legitimate plugin invocations. Implication: any tool-gating layer
  must consider the equivalent capability on the platform, not just
  the tool the human happens to think of. Both Bash and PowerShell
  must be gated equivalently on Windows; on Linux the equivalent
  consideration is Task-spawned sub-agents (different vector but
  similar threat model).
- **Substring matching the deck source dir over-blocks because
  cyberdeck-home/ is a subdirectory.** A `bash command contains
  <deck source dir>` check denies every legitimate plugin and
  dispatcher invocation (`python <deck>/cyberdeck-home/plugins/
  .../run.py`). Use sentinel filenames (brake_hook.py /
  brake_state.py / brake_patterns.py) for tampering protection;
  the path-overlap defeats prefix matching. Layout reorg (move
  cyberdeck-home/ outside the deck source dir) is one fix; not
  current scope.
- **`files_written` tracks attempted writes, not confirmed ones.**
  Construct.py populates from the model's `tool_use` blocks (model
  says it wrote a file), not from successful tool_results. When
  the brake hook denies, the path stayed in the list before we
  fixed it. fleet.py's _consume now subtracts denied paths from
  files_written at finalize time using normcase+normpath.
- **OS-path substring match in the brake hook over-blocked reads.**
  `bash_touches_protected_path` denied any shell command that
  mentioned a protected path (c:\program files, /usr/, /etc/, etc.),
  regardless of whether the command was reading or writing. Real-
  deck recon work hit this immediately: a `recon_specialist`
  construct doing `Test-Path "C:\Program Files (x86)\Nmap\nmap.exe"`
  to check whether nmap was installed got denied. Same class of
  over-block as the deck-source-dir-as-substring case. Fix: gated
  the path match on `has_write_indicator(cmd)` — `>`, `tee`, `cp`,
  `mv`, `Remove-Item`, `Out-File`, `Set-Content`, etc. Reads of
  protected paths now allowed; writes still denied. Heuristic, not
  airtight (python -c open().write evasion possible) but the
  destructive-bash regex + Write/Edit gating cover the catastrophic
  cases regardless, and the spec's threat model is "off-rails," not
  "adversarial." Bonus: the denial reason no longer hardcodes "bash
  references" when the actual tool was PowerShell — uses the outer
  `{tool}` field consistently.
- **Same bug in the Write/Edit path: `path_is_protected` denied
  writes inside cyberdeck-home/.** The deck-source-dir parents-walk
  caught the workspace as collateral because `cyberdeck-home/` lives
  inside the deck source dir by layout. Real-deck verified: a
  daemon-orchestrated research goal completed five parallel recon
  constructs successfully, then the synthesis construct tried to
  write its report to `<workspace>/super_chipmunk_engine_report.md`
  and got denied. Fix: `path_is_protected` exempts the workspace
  from the deck-source check (with `<workspace>/.cyberdeck/` as a
  sub-exemption — that's the deck-internal state directory and a
  construct overwriting `state.json` to YOLO would change the next
  spawn's permissions, so it stays protected). Workspace location
  resolves via `$CYBERDECK_HOME` env var if set, else
  `<deck>/cyberdeck-home/`. Same class of half-fix bug as the shell
  path case — both code paths inherited the deck-source-dir prefix
  match, and the shell version got fixed first because it surfaced
  first. Lesson: when fixing a "protection over-blocks workspace"
  bug in one code path, audit ALL code paths that share the
  protection logic.
- **Brake hook gates by tool NAME, not capability.** Pattern set
  targets `Bash`, `PowerShell`, `Write`, `Edit`, `NotebookEdit`,
  `WebFetch` literally. Any tool name not in that set sails through
  with no gating regardless of what the tool does. Real-deck
  surfaced 2026-04-30 (late) via log analysis: every construct
  has access to the netrunner's full claude.ai MCP connector
  config (`mcp__claude_ai_Supabase__execute_sql`,
  `mcp__claude_ai_Gmail__send`-after-auth, etc.) and the brake
  hook gates ZERO of them. Implication: when adding new tools to
  the deck's tool surface (or when Claude Code adds new built-ins,
  or when the netrunner connects new MCP servers), the brake
  hook's pattern set MUST be extended in the same change — either
  with explicit gates for the new tool name, or with a categorical
  default-deny-unknown-tool stance. The "verb-based MCP gating"
  fix in the safety architecture pass takes the explicit-gate
  approach for MCP; a deeper redesign would flip the default to
  deny-unknown.
- **Tripwire LLM authoring's depth-of-defense antipattern.** Real-
  deck observed 2026-04-30: watchdog authored `benign_delete_attempt`
  with regex `(?:^|[;&|\s])(?:rm(?!\s+-rf)|del|erase|...)\b` —
  the negative lookahead `rm(?!\s+-rf)` EXPLICITLY EXCLUDES the
  most dangerous case. Watchdog's stated reasoning: "brake will
  block destructive shapes, but this surfaces softer delete
  attempts." That's exactly the antipattern that defeats layered
  defense — every layer assumes another caught the dangerous case,
  and the dangerous case slips through if any one layer is
  weakened (e.g., brake gets flipped to YOLO). Fix: tighten
  `TRIPWIRE_AUTHORING_SYSTEM_PROMPT` to forbid the "X handles Y so
  I skip Y" reasoning. Authored tripwires must include shell-
  destructive baselines REGARDLESS of brake's coverage. Layered
  defense means EVERY layer covers the worst case independently;
  if one fails, the next catches it. Same logic as why brake hook
  also has its own destructive-bash regex even though Claude's
  refusal layer often catches them — defense-in-depth requires
  redundancy by design.

### Daemon (LLM behavior under safety-test prompts)
- **Daemon over-volunteers destructive content when asked to
  exercise safety.** Real-deck 2026-04-30: netrunner asked "spawn
  a tripwire-bait construct"; daemon synthesized `rm -rf /` AND
  volunteered `shutdown -h now` unprompted (log line 30). The
  daemon goes ABOVE the netrunner's literal request in safety-test
  mode. Possibly model-level enthusiasm-for-thoroughness rather
  than malicious behavior, but the result is the same: bait tasks
  end up more dangerous than the netrunner asked for. Multiple
  defenses caught it (Claude refusal, then brake regex would have
  blocked) but the chain is depth-of-defense, not depth-of-
  suspicion. Filed for daemon system prompt fix: when generating
  bait/test tasks, never expand beyond what the netrunner
  explicitly requested. Bonus lesson: when designing safety
  testing flows, the netrunner should specify the bait
  pattern themselves, not delegate "make a dangerous task" to the
  daemon — even with constraints, the daemon will improvise.

---

## Tech debt (not blocking)

- Script polling at refresh time
- Script manifests not implemented (raw filenames only)
- Esc-up tree navigation deferred
- Construct script-launch wiring stubbed
- Watchdog substrate cloud-only
- Connection consequences indicator-only
- Goal-diff classifier crude stem
- Read tool 25k token limit — profiles bias toward Bash+wc-l
- Long-running watchdog session accumulates context indefinitely

---

## Not implemented (deferred features)

- **Plugins** — third leg of tool registry
- **Watchdog tripwires + blacklist** — DSL, deterministic matcher
- **Connection consequences** — spawn-blocking, daemon parking
- **Routing** (`r`) — wire constructs together. Originally framed
  as a coordination primitive (let two constructs talk through a
  direct channel for tightly-coupled work). Real-deck use surfaced
  a second use case at least as compelling: **wiring as a recovery
  primitive**. When a construct does substantive work and the final
  step fails (the report-write-blocked-by-brake case being the
  canonical example), today's only recovery paths are (a) netrunner
  copy-pastes the output by hand, or (b) the daemon redoes the
  whole pipeline. With wiring, the netrunner could route the
  failed construct's output into a fresh construct with task "take
  this and write it to disk" — cheap, fast, no re-research. Strong
  argument for prioritizing wiring sooner than its current "future
  work" placement implies.
- **Universal list-names** — netrunner direction. Every listable
  object (files, plugins, profiles, blacklist entries, constructs,
  goals, watchdog Q&A, future tripwires, future morgue entries —
  basically anything that could appear as a row in a list) gets a
  short **list name** (~3–4 words, chat-name-style) generated at
  creation time and stored on the object. UI surfaces use the list
  name in row chrome instead of raw paths / full task text /
  fingerprints. Eliminates a whole class of overflow / horizontal-
  scrollbar / line-wrapping bugs where long content blows out
  list-row layouts.
  - **Why "creation time, not render time":** generating per-render
    is expensive and produces inconsistent names (different code
    paths, different truncation rules). Bake the name once, reuse
    forever. Also matches the deck's "files on disk are the
    database" pattern — list_name lives next to the object.
  - **Generation:** two-tier. **Mechanical fallback** (basename,
    first significant words, slugify) lands instantly so the row
    never shows blank. **LLM-authored name** (~$0.001 per name via
    Haiku, async) overwrites the fallback once it returns. Same
    pattern as Claude.ai's conversation names — the model picks a
    crisp 3-4 word slug from the content.
  - **Storage:** the list_name field lives wherever the object's
    canonical record lives — `files_written` entries, blacklist
    entries' `BlacklistEntry`, watchdog `WatchdogHistoryEntry`,
    profiles' TOML, etc. For runtime objects (constructs, goals)
    it lives in-memory on the object.
  - **Consistency rule:** any new object type added to the deck
    that gets surfaced as a list row MUST carry a list_name field.
    This is a spec-level rule, not a per-feature decision.
  - **Open questions:** how to display the longer original text
    when needed (z-magnify the row to see the full content?);
    whether list names should be regeneratable (construct finishes
    its work, generate a name from the OUTCOME instead of the
    original task — outcomes are usually more meaningful); how to
    namespace short names so two unrelated objects don't collide
    visually in a list.
  - **Relationship to existing infrastructure:** dovetails with
    the morgue (browsing past sessions becomes useful only if each
    row has a glanceable name) and with the keymap revision pass
    (the actions-first inventory should treat list_name as a
    first-class attribute of every focusable surface).
- **Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`)**
- **Daemon pause/unpause (`E`)**
- **Goal-edit force-push** — apply-now interrupt
- **Per-run workspace compartmentalization** — netrunner
  direction (2026-04-30). Default spawn cwd graduates from bare
  `<home>/` to `<home>/runs/<run_id>/`; all constructs in a run
  share the run's folder. Fixes the file-browser-mess problem
  where many runs over time pile their working files flat in
  `<home>/`. Concrete value: a research → synthesis pipeline
  (one construct researches into N files, another assembles
  the report from those files) gets a clean shared cwd by
  default. Profiles, plugins, `.cyberdeck/` state, and the
  dispatcher script stay where they are — only spawn cwd
  changes. Composes with universal list-names (folder name
  becomes `run-{run_id}-{list_name}/` once that lands), the
  morgue (each session record gains a `cwd` field for
  one-click pivot to that run's folder in a file browser),
  and the existing files-panel dedupe (no logic change). Not
  blocking anything, not blocked by anything; ~50-80 LOC
  implementation, shippable in a focused session post-Mechanic
  v0. Full notes in build plan item 8.
- **B2 fleet synthesizer** — substrate-blocked on D1
- **D1/D2/D3** — local-model runtime, arbiter, B2 on local
- **Compliance mode (Phase E)**
- **The morgue (session history / past-session resuscitation)** —
  netrunner direction. Today, finalized construct sessions are
  effectively scattered: `session_manager.py` tracks the warm pool
  and the active session, but once a construct finalizes its
  `session_id` is dropped from active tracking. Anthropic keeps the
  server-side session for some retention window, so `--resume <id>`
  would still work — but the netrunner has no way to *find* that
  id later. The morgue is a persistent log + UI surface that fixes
  this:
  - **Storage:** append-only JSONL at `<home>/.cyberdeck/sessions.jsonl`,
    one record per finalized construct. Fields: session_id,
    construct_id, task (truncated), state, started_at, finished_at,
    final_output (truncated/summary), files_written, cost_usd,
    profile_name, origin (daemon/netrunner/inject), and a goal_id
    linking back to the goal session it served (if any).
  - **UI:** new right-panel tab "Morgue" (or "History") listing
    sessions newest-first, with summary/cost/state visible at a
    glance. `z` to expand a row into the full final_output. A
    "resuscitate" action (Space?) opens a NewConstructScreen
    pre-populated with `--resume <session_id>` and an empty task
    field for the netrunner to fill in.
  - **Filter/search:** by task substring, by date, by state. Bonus:
    "show me everything from last Tuesday's goal."
  - **Retention:** keep forever locally; the actual ceiling is
    Anthropic's server-side session retention. Resuscitation that
    hits an expired session reports "session expired" and the
    netrunner can spawn fresh from the morgue's saved task text.
  - **Implementation note:** likely just an extension of
    `session_manager.py`'s manifest — keep finalized records with a
    `state: finalized` marker instead of dropping. New file vs.
    extending the existing one is a small design call.
  - **Why it matters:** transforms ephemeral sessions into a
    personal capability library — fits the spec's "capability
    accumulates" thesis directly. Every successful construct
    becomes a callable artifact later.
- **Watchdog log (persistent watchdog history)** — v1 shipped
  2026-04-28; tripwire/blacklist record kinds still deferred. The
  shipped slice:
  - `WatchdogHistory` + `WatchdogHistoryEntry` in `watchdog.py`,
    persisting to `<home>/.cyberdeck/watchdog.jsonl` (append-only,
    one JSONL line per resolved Q&A).
  - Watchdog accepts `history=` at construction; `_safe_callback`
    persists to history before firing the listener so the entry
    is recorded even if the listener crashes or no listener is
    wired.
  - TUI's `_replay_watchdog_history` runs in `on_mount` and renders
    the last 50 entries into WatchdogPane with a `──── prior
    session (N entries) ────` / `──── live session ────` separator
    pair so the netrunner can tell historical from current.
  - Per-entry shape includes a `kind` field (currently always
    "qa", future "tripwire" / "blacklist_change" entries will share
    the same file). Schema-drift tolerant: replay drops unparseable
    lines silently and skips non-qa kinds.
  - Best-effort throughout: disk errors don't crash the watchdog
    (the question already resolved by the time we try to write;
    persistence is observability, not correctness).
  - **Still deferred:** dedicated "Watchdog History" right-panel
    tab for retrospective browsing distinct from the live tab; the
    tripwire/blacklist record kinds; cost/status fields beyond
    success/fail.
- **Cross-cutting:** the morgue and the watchdog log were filed
  together as "deck history infrastructure" — both follow the
  deck's "files on disk are the database" pattern (per philosophy
  doc) and would benefit from being designed as one initiative.
  Watchdog log v1 shipped first because it's tighter scope and the
  netrunner-immediate value (Q&A surviving restart) was clearer.
  The morgue (session-level history + resuscitation) remains
  deferred.

---

## Collaboration patterns that work

- **Mock-first development.** Real claude when assumption hinges on
  opaque server behavior.
- **One milestone at a time.** Each ships before next starts.
- **Real-claude testing pause-points.** Two minutes of testing > hours
  of speculation. Almost every recent bug was caught this way.
- **Banter encouraged, work prioritized.**
- **Push back when wrong; check before acting when ambiguous.**
- **State doc + build plan refresh between major slices.**
- **Screenshots > stack traces > prose.** When a bug is visual, a
  screenshot solves 80% of the diagnosis.
- **Half-finished refactors leave landmines.** When a session ends
  mid-refactor, the next session needs to find and fix before
  continuing. Always close the loop on the current method.

---

## Migration to Claude Code

**Why now:** the deck is at 12k LOC across 13 modules. Multi-file
edits, refactors, and grep-the-codebase questions have become the
bottleneck. Claude Code edits files in place, runs greps natively,
and doesn't suffer the context-truncation issues a long chat thread
eventually does.

**What to bring:**
1. This file (`cyberdeck-state.md`).
2. The build plan (`cyberdeck-build-plan.md`).
3. The orientation (`cyberdeck-claude-code-orientation.md`).
4. The spec (`cyberdeck-spec.md`).
5. The philosophy (`cyberdeck-philosophy.md`).
6. The codebase itself (in git, not the chat).

**What changes:**
- No more "FILES TO REPLACE" blocks — Claude Code edits in place.
- No more `cp /home/claude/cyberdeck/foo.py /mnt/user-data/outputs/`.
- Test runs are local (the chat had no real terminal).
- The user can grep, the AI can grep, no more "let me look for…"
  query rituals.

**What stays:**
- Real-claude testing as the ground truth for streaming/permissions/
  Windows quirks. Mocks miss too much.
- Mock-first development for new modules.
- One milestone at a time.
- The whole gotchas list — none of these go away.
- The pushback culture (you've caught the AI being wrong many times;
  keep doing that).

**What to ask before next session:**
1. **Plugin scaffolding** — third leg of tool registry.
2. **Connection consequences** — spawn-blocking on Degraded.
3. **Watchdog tripwires** — the harder half of watchdog.
4. **D1 local-model runtime** — substrate for everything AI-deferred.
5. **The tools-research chat** — using `cyberdeck-tools-research-seed.md`.
