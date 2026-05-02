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

Three architecture docs for the major in-flight initiatives:
  - `cyberdeck-event-stream-design.md` — the spine (one canonical
    event bus, role-derived filters). Phases 1-7 shipped 2026-04-30;
    Phase 8 cleanup is the last remaining slice.
  - `cyberdeck-maintbot-design.md` — the Mechanic (separate-process
    supervisor + on-demand LLM session). Two-tier architecture
    landed 2026-04-30; v0 (supervisor only — subprocess janitor) is
    the next implementation slice.
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

**Next session picks up at: BUILD-PLAN PIVOT.** With discrete-
bugs at the practical floor, queue is back to design-led work.
Top of the priority queue: caliber selection (per-spawn model +
effort + fast-mode — see `cyberdeck-model-effort-design.md`).
Mechanic v0 follow-ups (track non-construct subprocess sources)
and Phase 8b (Pool/Daemon callback cleanup) on deck. Tools/
plugins/profiles retool design also waiting at phase 1 (tools
registry + hot-reload + missing-tool grey-out).

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
