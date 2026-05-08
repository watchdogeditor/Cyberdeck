# Cyberdeck — Build Plan

*Forward-looking plan tree for the deck. Companion to `cyberdeck-spec.md`
(architecture) and `cyberdeck-state.md` (shipped state of record).*

*Last lean rebuild 2026-05-07. Every active line item points at its
design doc(s). The shipped section is a high-level index — full slice
chronology lives in `cyberdeck-state.md`. Full session-by-session
journal of the previous build plan lives at
`archive/journal/cyberdeck-build-plan-journal-pre-2026-05-07.md`.*

---

## How to read this

- **SHIPPED** is the executive index of what's done — one line per slice,
  pointing at `cyberdeck-state.md` for detail.
- **CURRENT FRONTIER** is what to pick up next, ranked by tractability.
- **NEAR / MID / LONG FUTURE** is queued work, dependency-aware.
- **NON-GOALS** is what we've consciously rejected — check before
  proposing a slice that touches one of these areas.

Every active item names the design doc(s) that scope it. If you're going to
work a slice, read its docs first.

---

## SHIPPED

For the full chronology — dates, commits, real-deck verification notes —
see `cyberdeck-state.md`. This is a one-line index.

### Tier 1 (foundational, pre-Claude-Code-migration)
- M0–M5 lifecycle + fleet + TUI + keyboard-driven cyberdeck + daemon-driven goals + session pool
- Phase A Step 1–3 (EJECT, q/Q construct injection, `l` Limits modal)
- B1 Activity chatlog (mechanical event extraction, color-coded)

### Tier 2 (extensibility)
- C1 Profiles (TOML loader, frozen `Profile`, `ProfileRegistry` hot-reload, daemon-picked per-spawn)
- C3 Tool registry — three legs: profiles, scripts, plugins
- Phase A deck-control protocol (dispatcher script, marker protocol)
- Profile/brake refactor + brake-state deck-global + `PreToolUse` hook enforcement

### Watchdog tripwires (slices 1+2)
- Deterministic matcher engine + DSL + severity tiers + LLM-authored tripwires (rung-1 fork / rung-2 fresh)
- *Design:* tripwires shipped before designs were extracted; case study at `archive/case-studies/cyberdeck-tripwire-case-spiralism.md`

### Unified event stream (the spine) — 8/8 phases
- Per-launch NDJSON + bus-as-canonical-fan-out + listener-shim cleanup
- *Design:* `archive/shipped/cyberdeck-event-stream-design.md`

### Late 2026-04-30 cluster
- y/Y copy keybind (`clipboard.py`)
- Limits modal rework (uncapped construct counts, pool refill gate)
- Mechanic v0 supervisor (`mechanic.py`)
- Kill state-stuck race fix
- Tui dupe-pane fix

### Safety architecture pass — 4/4 slices
- Slice 1 — MCP gating in `brake_hook.py`
- Slice 2 — Tripwire escalation chain (low → log; warning → deny+suggest; critical → deny+auto-term; critical+bad_enough → blacklist proposal)
- Slice 3 — Variable-outcome delay UX (Phase 1, Phase 1.5, Phase 2). X-deck-wide rule (X-ecute) established
- Slice 4 (¼) — `host_restart_command` warning tripwire promoted into defaults
- Kill audit — `kill_source` field on every finalize

### Diagnostics + cost (2026-05-01 → 2026-05-02)
- Wedge-timeout diagnostic (stderr drain on TimeoutError)
- Cache cost fix (stable shared `spawn_settings.json`)
- Tripwire-authoring spawn-race fix (DaemonSession awaits authoring complete)
- Discrete bugs cluster (daemon over-volunteers + enum payload serialization)
- Construct-refusal as structured event

### Tools / Plugins / Profiles retool — 5/5 phases (2026-05-03 → 2026-05-04)
- *Design:* `archive/shipped/cyberdeck-tools-plugins-profiles-retool.md`

### Caliber — 4/5 phases shipped (2026-05-04)
- Phases 1, 2, 3, 5 shipped. Phase 4 (quota-aware fallback) blocked on item 13
- *Design:* `in-flight/cyberdeck-model-effort-design.md`

### Tools-UI "Thought of Dave" slice — 3/3
- space-launch (TOOL: / PLUGIN: envelopes)
- z-info (synthesized info / README view)
- h-Advisor (modal-scoped per-tool Q&A bot, sonnet+medium — bumped from haiku+low 2026-05-05 for scope-following reliability)

### Public-repo readiness (2026-05-04)
- Item 0 — README restructure
- Item 0a — `doctor.py`
- Item 0b — `preferences.py`
- Item 0d — Mechanic v0→v1 bridge: liveness heartbeat

### Auto-context discovery + per-role isolation phase 1 (2026-05-05)
- Discovery + 4-line proof of CLAUDE.md auto-load via Anthropic docs
- Per-role env-var belt (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` etc.)
  - **KILLED** for Advisor, Construct, Daemon, Pool warmer, Tripwire-authoring Watchdog
  - **KEPT** for Watchdog Q&A
- Multi-line argv truncation fix (`--system-prompt-file` + `--append-system-prompt-file`) — promoted to top-level Hard Rule
- `user_email_protection` default tripwire (mitigation for `anthropics/claude-code#55743`)
- Watchdog Q&A PROJECT MEMORY AWARENESS section
- *Design:* `in-flight/cyberdeck-spawn-context-isolation.md` (Phase 1 only)

### Mechanic v1 + v1.5 cluster (2026-05-06)
- Item 0e — Mechanic v1 diagnose-only LLM-session triage (`mechanic_triage.py`, ~480 LOC)
- Item 0e2 — Mechanic v1.5 stale-heartbeat triage with interactive prompt + listens-for-recovery
- Adjacent fixes: Ctrl+Q triage skip; ctypes Windows-handle truncation in `_pid_alive_win`; log-selection race; live narration via `stream-json --verbose`; partial-recovery on timeout
- *Design:* `in-flight/cyberdeck-maintbot-design.md` (v0–v1.5 sections)
- Living inventory `cyberdeck-platform-portability.md` filed

### Construct/daemon pane chrome + tripwire overlay (2026-05-07)
Real-deck UX pass on the deck's primary read surfaces.

- **Construct pane log** — `display.summarize` / `render_block` rewritten to emit per-event-type chrome with preserved newlines instead of a single ` | `-joined line with `\n` collapsed to spaces. Each block gets a leading glyph (`▸` tool_use, `◂` tool_result, `▒` thinking, `▌` text) + optional header (e.g., `Bash`, `result (1247 chars)`) + indented body with newlines preserved. Bash commands keep their shape; tool results scan vertically; thinking blocks read as paragraphs. Errors flagged in red. Pane log widget switched from `Log` (no markup, single-line writes) to `RichLog` (markup + `wrap=False` + auto-split on `\n`). `add_event` formats the event-kind prefix on the first line and indents continuations 15 spaces so multi-line bodies hang under the prefix visually. `render_buffer` (ExpandModal feed) uses `Text.from_markup` so the modal renders the same chrome.
- **Daemon pane chrome** — mirrors WatchdogPane's Q/A shape. New methods on `DaemonPane`: `write_goal_set(goal)`, `write_goal_update(goal, classification, old_goal)`, `write_netrunner_message(text)`. Visual rule (`──────`) + `⚑ goal:` / `≫ netrunner:` chrome. Existing `write_thinking` / `write_chat` updated: `[dim italic]› thinking:[/dim italic]` and `[green b]▶ daemon:[/green b]` so the daemon's voice matches the Watchdog's `A:` framing. `_handle_goal_submitted` (idle + mid-flight branches) and the netrunner-message inject path wired to call the new methods.
- **Tripwire delay overlay** — when the delay was tripwire-driven (`DelayEntry.is_tripwire_driven`), the per-pane overlay renders a third+ row below the countdown bar showing `⚠ tripwire <name> [SEVERITY]` + description + suggestion (warning only). Severity-colored chrome (red for critical, yellow for warning). Pre-fix the netrunner pressed X without knowing WHICH rule they were overriding; now the rule is named on screen during the X-window. `pane_delay` Label CSS switched from `height: 2` to `height: auto` so the overlay grows for tripwire context.
- *Pending real-deck eyes:* construct pane chrome look in narrow panes (do glyphs render in cmd.exe's default font? do dim/cyan styles read clearly against the panel background?); daemon pane Q/A flow during a real goal cycle; tripwire-overlay text wrapping when description is long.

### Mechanic v1.6 — iterative triage (item 0g, 2026-05-07)
- Multi-pass deepening on top of v1's single-pass shape. Pass 1 fires the standard triage; mechanic then prompts on stderr "Keep delving? [y/N]"; on yes, fires a deepening pass via `claude -p --resume <session_id>` and appends a `## Deeper analysis (pass N)` section to the same report file
- Reuses v1.5's streaming + prompt-thread infrastructure. New helpers: `prompt_keep_delving()`, `run_iterative_triage()`, `_build_deepen_directive()`, `_append_pass_to_report()`. New `TriageResult.session_id` field captured from the first `system/init` stream event
- Mechanic CLI gains `--no-iterative` (collapse to single-pass behavior) and `--max-triage-passes` (default 4). Non-TTY stdin auto-skips the prompt so headless / wall-mount deployments get single-pass without hanging on `input()`
- *Design:* `in-flight/cyberdeck-maintbot-design.md` (now noted in STATUS banner)

### Stale pool session detect-and-retry (2026-05-07 → 2026-05-08)

Defensive backstop for the case where a pool-served `--resume <id>` fails with "No conversation found." The original symptom was a real-deck storm of failed manual spawns triggered by the per-run-workspaces cwd plumbing (Claude Code's per-project session storage broke cross-cwd resume). The cwd plumbing got reverted in v5; the detect-and-retry layer stays as defense in depth — server-side eviction or any other surprise gets caught and recovered without netrunner intervention.

- `Fleet._handle_stale_resume` watches result events for the "No conversation found" error string, evicts the bad session_id from the manifest, publishes `pool.stale_resume`, and marks the construct for auto-retry. The finalize path synthesizes a fresh respawn with the same params (sans `--resume`). Gated on `_retry_params` (only pool-resumed spawns retry; explicit-resume callers like inject opt out) and `_retry_attempted` (prevents loops).
- `Fleet.spawn(force_fresh=True)` kwarg lets the retry path skip the pool entirely (otherwise the retry pulls another stale entry — real-deck-observed fork-bomb).
- `DEFAULT_STALE_AFTER_SECS` stays at 5h (the original heuristic). It WAS tightened to 1h mid-iteration but reverted when the cwd revert removed the actual bug.
- New `Kind.POOL_STALE_RESUME` bus constant.
- Filed gotchas + design lessons in `cyberdeck-state.md` Filed gotchas → Async/subprocess (cwd-as-project-key + fork-bomb sub-gotcha).

### Per-run workspace compartmentalization (2026-05-07 → 2026-05-08)

Filed mid-day 2026-05-07, designed in five revisions over 18 hours, ended up much smaller than it started. Net code surface: `runs.py` (new, ~175 LOC), small additions to `tui.py`, `daemon.py`, `event_bus.py`. (`fleet.py`, `daemon_session.py`, `dispatcher.py`, `session_manager.py` were touched in mid-iterations and reverted.)

**Final shape (v5, 2026-05-08):**

- ONE run per deck launch. Minted at `App.on_mount`; kept for the process lifetime; closed at shutdown / eject.
- Run dir: `<home>/runs/run-<YYYY-MM-DD-HHMMSS>-<4hex>/`. Mirrors per-launch log file structure.
- Constructs (all paths) cwd at `<home>` — STABLE, not the run dir. Pool warms at `<home>`, fleet default at `<home>`, manual + daemon spawns all at `<home>`. Cross-launch session reuse works because cwd path determines Claude Code's per-project session storage.
- Run dir is communicated as a PROMPT-LEVEL convention: deck addendum says "write your outputs to `<absolute_run_dir>/`", daemon system prompt has the run dir baked in for task-string composition. Constructs honor it because the prompt asks them to, not because they're forced to.
- Bus events: `run.opened` (on mount), `run.closed` (on quit / eject).
- Run dirs persist on disk indefinitely. Cleanup is manual; auto-purge is in EXPLICIT NON-GOALS.

**Iteration history (preserved in design doc; the value of the slice is the lessons):**

- **v1**: per-goal-set runs, auto-close on daemon idle, end-of-run modal with 4 wholesale buttons. Surfaced two catastrophic bugs (modal duplicate-IDs CTD; `self.run` shadowed `async def run()`).
- **v2**: bugs fixed, same shape.
- **v3**: netrunner-gated lifecycle, Shift+R gesture, per-file curation modal (LEAVE/TOOLIFY/PRESERVE/DELETE marks), dispatcher `mark-as-*` protocol.
- **v4**: ripped curation back out per netrunner direction ("the runs system shouldn't be this involved"). Run = launch lifetime. ~500 LOC removed.
- **v5 (final)**: cwd plumbing reverted entirely after real-deck stale-resume storm exposed Claude Code's per-project session storage. Run dir survives as prompt-level convention only. ~80 more LOC of plumbing removed.

**Bonus fixes that survived all iterations (filed in state.md gotchas):**

- **Modal duplicate-IDs CTD** — Textual rejects duplicate IDs at mount; render-pipeline crash kills the whole TUI. Lesson filed; offending modal is gone.
- **`self.run` shadowing `async def run()`** — instance attr shadows class method. Lesson: check class method names before naming new attrs.
- **Crash-traceback capture** — `sys.excepthook` + asyncio loop exception handler write `deck.crash` events with full traceback. Future CTDs leave evidence.
- **Stale pool session detect-and-retry** — `Fleet._handle_stale_resume` watches result events for "No conversation found", evicts the bad session_id, marks construct for auto-retry; retry path passes `force_fresh=True` to bypass pool (avoids fork-bomb where retry hits another stale entry).
- **Claude Code per-project session storage gotcha** — `~/.claude/projects/<cwd-as-path>/` keys session storage on cwd. Cross-cwd `--resume` returns "No conversation found" (same string as server-side eviction). Diagnostic process lesson filed.

*Design:* `in-flight/cyberdeck-per-run-workspaces-design.md` (will rewrite to match v5 final form before moving to `archive/shipped/`).
*Pending real-deck verification:* manual constructs work fast via pool reuse; daemon composes run dir into spawn task strings; constructs honor the output-directory convention; cross-launch pool reuse works.

### Tripwires redesign — brake/tripwire unification (2026-05-07)

Major reshape of tripwire enforcement in response to a real-deck operational pain point ("most tasks failed because tripwires fired on benign content"). Lands as one slice; touches `brake_state.py`, `brake_hook.py`, `brake_delay.py`, `tripwires.py`, `tui.py`.

- **Item 0000** — Tripwire-authoring GOTCHAS addendum (field-selector intent-mapping, telegraphed-intent, research-goal framing) was the first piece; subsumed by the larger rewrite when scope expanded.
- **X is unidirectional now** — always means "allow this particular action to ignore the rules." Key design decision #24 revised; YOLO+delay→interrupt branch retired.
- **YOLO never installs the hook** — live-fast-and-die, no enforcement, no overrides. `make_spawn_settings` returns None (or fast-mode-only settings) under YOLO.
- **Tripwire fires route through the same delay window as brake denies** — engine writes `deny_pending.json` (now with `bad_enough`); hook reads it, opens standard delay window with tripwire context embedded; X-allow skips kill + skips blacklist proposal.
- **Critical-kill + bad_enough blacklist moved deck-side** — `_handle_tripwire_fire` renders only; consequences fire from `_handle_delay_resolved` gated on `severity=critical AND applied_action=deny`.
- **Authoring-prompt rewrite** — drop "DO author critical-severity shell-destructive baselines" (the contradiction at the root of overauthoring); add explicit DIVISION OF LABOR (brake = OS integrity; tripwires = goal-specific drift; no overlap); reframe to "how do I break this?" red-team; tighten cardinality 0-8 → 0-5; preserve GOTCHAS section.
- **DelayEntry / DelayResolution** carry tripwire context fields (`tripwire_name`, `tripwire_severity`, `tripwire_description`, `tripwire_suggestion`, `tripwire_excerpt`, `tripwire_bad_enough`).
- *Design:* spec'd inline + filed in `cyberdeck-state.md` Filed gotchas (overauthoring contradiction) + key design decisions #24, #27, #28.
- *Pending real-deck verification:* X-allow on critical fire skips kill; YOLO truly installs no settings file; tripwire fires under default brake produce visible delay overlays with tripwire context; authored tripwires for goal-specific drift land cleanly without OS-baseline duplicates.

---

## CURRENT FRONTIER

Branch `claude/objective-sammet-25e0b4` ahead of `origin/main`. Per-run workspaces shipped 2026-05-08 (v5 final form) — pending real-deck verification before the next slice picks up. Remaining candidates:

### 1. Item 000 phase 2 — Role-injection infrastructure
- Conditional. ~600-1000 LOC. Pull forward only on concrete regression
- Real-deck verification of phase-1 confirmed daemon + constructs do NOT regress without CLAUDE.md auto-load
- New `roles_registry.py`, `general.toml`, `<deck-source>/roles/*.md`, `--system-prompt`/`--append-system-prompt-file` injection
- *Design:* `in-flight/cyberdeck-spawn-context-isolation.md` (Phase 2 section)

### 2. Item 0f — Adversarial dyad
- Generator/discriminator paired-construct pattern. Daemon synthesizes both opinions; provides "is this work good enough" signal for caliber escalation
- ~600-900 LOC + new design doc `cyberdeck-adversarial-dyad-design.md` (TBC)
- Picks up post-architecture-review
- Companion to caliber Phase 4 (provides quality signal alongside item 13's quota signal)
- *Design:* doc to be filed; concept summary in this build plan + user auto-memory `project_prompt_shaping_design.md`

### 3. Item 0h — Mechanic repair authority for non-source issues
- Promotes maintbot v2. Diff-preview + per-fix approval for config files (state.json, profile TOML, tools.toml — NOT deck source)
- ~300-400 LOC; new `mechanic_repair.py`
- Composes with 0g (now SHIPPED) as a "third pass" trigger when iterative triage detects config issues
- *Design:* `in-flight/cyberdeck-maintbot-design.md` (v2 section)

### 4. Architecture review
- Scheduled to fire 2026-06-01 09:00 EDT (taskId `cyberdeck-architecture-review`)
- Read-only; outputs `Design Files/cyberdeck-review-<date>.md`
- Findings under (A) architecture coherence, (B) hard-rules compliance, (C) filed-gotcha re-introduction risk, (D) tech debt + TODOs
- Agent phase-checks first; defers if work is in flight

### Verification opportunities pending real-deck eyes
- Limits modal two-column panel + EffortPickerScreen
- Caliber Phase 5 surfaces (sidebar `daemon: opus·high`, per-pane caliber suffix)
- Tools-UI: space-launch, z-info, h-Advisor on real targets
- `python tui.py --doctor` first-run + happy-path
- Mechanic heartbeat under normal operation

---

## NEAR FUTURE (queued, well-specified, ready to pick up)

### Prompt-shaping pass
- Ethics-strip (single identity-line + single pre-authorization-line)
- Iterative plan document (daemon writes a real plan file)
- Opt-in validator (daemon-activated, Haiku-tier, couples with caliber)
- Skills stay Python-injected at launch — no dynamic mounting
- *Design:* `in-flight/cyberdeck-prompt-shaping-design.md` (filed 2026-05-07, content layer; sourced from transilience.ai heist). User auto-memory `project_prompt_shaping_design.md` is the commitment layer (the four directives in two paragraphs).

### Collections intake
- Recipe-driven plugin scaffolding for github-distributed reference collections (PATT, SecLists, nuclei-templates, GTFOBins, HackTricks, LOLBAS)
- Three pieces: intake recipe TOML + assembler script + generated plugin
- Implementation queued behind prompt-shaping pass and Mechanic v2
- *Design:* `in-flight/cyberdeck-collections-intake-design.md`

### Caliber Phase 4 — quota-aware fallback
- HARD-BLOCKED on item 13 below
- Read `<deck>/.cyberdeck/quota.json`; daemon system prompt grows quota-aware band
- *Design:* `in-flight/cyberdeck-model-effort-design.md` (Phase 4 section)

### Item 13 — Quota-aware throttling
- Daemon gates spawns on remaining Max quota (5h + weekly windows)
- Mechanism: Claude Code's status-line script receives rate-limit fields on stdin; writes JSON; daemon reads
- Cold-start caveat: rate-limit fields populate only after first model call
- Pi gotcha: tmpfs (`/dev/shm/cyberdeck-quota.json`) on OrangePi to avoid SD-card wear
- *Design:* spec'd inline here; touches status-line script + new quota reader module

### Per-spawn tool/plugin availability surface

Filed 2026-05-07. Profile-spawned constructs should know which tools on their list are actually installed/working so they don't waste turns invoking unavailable ones.

Current state (partial): tools-registry already tracks `available` + `unavailable_reason`; the per-spawn addendum (tui.py `_build_per_spawn_addendum`) renders `[unavailable: <reason>]` after the description for tools that fail their availability check. But:

- No explicit guidance to the construct on what to do when it sees `[unavailable]` — model behavior is implicit (most models will avoid, but it's not told to).
- Plugins are filtered through `plugin_registry.available()` BEFORE the addendum renders, so unavailable plugins are silently absent. A construct given a daemon-selected plugin that isn't loaded gets an error mid-task instead of a heads-up at spawn time.

Slice shape (~40-60 LOC):
- Tools: add a brief instruction in the tools header ("entries marked `[unavailable]` are listed for awareness; do not invoke — use the alternative or surface the gap to the netrunner").
- Plugins: render unavailable plugins in a separate dim section with their `[requires]` reason, instead of filtering them out.
- Header tweak: explicit "available now" framing so models that skim the list don't assume everything's ready.

*Design:* spec'd inline; touches `tui.py:_build_per_spawn_addendum` only.

### Discrete bugs (deferred but specified)
- **Kill doesn't interrupt in-flight assistant turns** — SIGTERM lands AFTER model finishes turn. Stopping mid-turn requires stdin-injection or stream interrupt; design alongside future inject-and-interrupt v2
- **Silent wedge investigation (cx-796e0468 case)** — empty `stderr_excerpt`; needs more real-deck data points
- **Daemon narrative fix** — daemon mislabels brake-hook denials as tripwire fires; tighten daemon system prompt to distinguish `permission_denials` from `tripwire.fire`
- **Verify Claude Code's fast-mode settings.json key** — current `{"fastMode": true}` may need to be `{"speed": "fast"}`; real-deck verify via `system_init` event's `fast_mode_state` field

### Q-inject (interrupt-inject) revisit — DEFERRED, may DROP
Filed 2026-05-07 evening from real-deck observation. Netrunner direction: postpone, possibly remove the feature entirely depending on what the redesign reveals.

Current behavior is impractical: pressing `Q` (interrupt-inject) sends SIGTERM to the construct's claude subprocess + queues a follow-up spawn, but per filed gotcha "Kill doesn't interrupt in-flight assistant turns," the kill lands AFTER the current model turn finishes. From the netrunner's perspective `Q` is functionally identical to `q` (queue-inject) plus an extra kill+respawn step — the construct keeps doing the wrong thing for 30-180s before the redirect takes effect, defeating the point of "interrupt."

Three redesign options sketched (dropped-feature is also on the table):

1. **Rename + tighten expectations** (~10 LOC). Drop the "interrupt" framing; both q and Q are queue-shape with different post-current-turn dispositions. Honest naming, doesn't fix the actual problem.
2. **Stdin-injection mid-turn** (medium ambition, likely doesn't deliver). Inject the netrunner's message into the construct's stream-json stdin while a turn is in-flight. Probably ignored by claude-code until current turn finishes; same wedge gotcha applies.
3. **Kill-and-respawn-fresh with abandoned-tool-call framing** (~150 LOC). SIGKILL the subprocess immediately, spawn a new construct with a "previous construct's work was abandoned mid-flight; netrunner's redirect: <message>" preamble. Optionally `--resume <session_id>` so the new construct inherits prior conversation context.
4. **Drop the feature.** If options 1-3 don't justify the binding's existence, remove `Q` and live with `q` (queue-inject) as the only inject path. The netrunner can `k` then `n` for a hard cut-and-replace if they want the interrupt UX.

Help modal updated 2026-05-07 to honestly note the disconnect: "Q intent: kill current work + redirect; today behaves as queue-inject due to Claude's mid-turn kill discipline — see filed gotcha." That keeps the binding from lying until we commit to a direction.

Pending decision when picking up: option 1 vs option 3 vs drop. Real-deck data on how often the netrunner reaches for `Q` (vs `q` + `k` separately) would help — feature usage might already be low enough that drop is the right call.

*Design:* spec'd inline above; touches `tui.py:action_interrupt_inject` + Construct kill discipline + (option 3) construct spawn-with-resume infrastructure.

### UI polish pass — partial (construct/daemon panes + tripwire overlay shipped 2026-05-07)
Filed 2026-05-07 from real-deck observation. Netrunner flagged several render surfaces as needing substantial improvement. First batch shipped same day; sidebar + advisor truncation still outstanding.

**Shipped 2026-05-07** (see SHIPPED → Construct/daemon pane chrome + tripwire overlay):
- Construct pane log: per-event-type chrome (▸ tool_use, ◂ tool_result, ▒ thinking, ▌ text), preserved newlines, multi-line bodies indented under headers. Switched pane_log widget from Log to RichLog so markup + multi-line writes work.
- Daemon pane: WatchdogPane-style chrome — `⚑ goal:` + separator on goal-set, `≫ netrunner:` + separator on inject, `▶ daemon:` for chat output (was just `chat:`).
- Tripwire delay overlay: extra row below the countdown showing tripwire name + severity badge + description (+ suggestion for warnings). Pre-fix the netrunner saw "Redirecting in Xs, press X to approve" with no clue WHICH rule fired.

**Still outstanding:**
- **Sidebar (left bar)** — fleet log + status indicators (brake / connection / caliber / pool / cost / spawns). Functional but cramped. Layout, hierarchy, density all on the table.
- **Chatlog (center activity stream)** — renders fleet events, daemon thinking, watchdog Q&A, tripwire fires, brake denials, attention items. Each event type has its own format and the cumulative result is dense. Concerns: visual hierarchy, event-type discoverability, scrollback navigation, search, density tuning. (The construct/daemon pane work above is a model for what the central chatlog could look like.)
- **Advisor modal scroll truncation** — concrete sub-bug, real-deck observed 2026-05-07: asking the Advisor "what do you know?" produced output that got truncated by the scrollbar. Investigate whether the scrollbar is consuming visible area or the content's height isn't being computed properly.
- *Design:* none yet — file an in-flight design doc when the pass picks up.

### Tripwires slice 3 — severity-aware rendering
- Critical pulls focus, warning badges, low logs only
- Severity tiers already in DSL; just visual routing
- Composes with safety pass slice 2 (already shipped) — once tripwires escalate, visual tier matters more

### Tripwires slices 4-6 (LLM-authoring follow-ons)
- **Slice 4** — persistent tripwire library at `<home>/tripwires/` with TOML authoring
- **Slice 5** — daemon-side severity hints
- **Slice 6** — blacklist-derived tripwires that fire on event content rather than just task fingerprints
- Per-outcome adaptive re-authoring still blocked on a "daemon signals plan shift" event
- *Design seed:* `archive/case-studies/cyberdeck-tripwire-case-spiralism.md`

### Mechanic v0 follow-ups
- Track non-construct subprocesses: daemon, watchdog Q&A, watchdog authoring one-shots, pool warmers
- One line per spawn site to add `pid` field; one elif per source in `mechanic._apply_record`
- *Design:* `in-flight/cyberdeck-maintbot-design.md` v0 section

### Spine Phase 8b — Pool + Daemon callback cleanup
- SessionPool's `on_event`, Daemon's `on_daemon_event`, Blacklist's `on_event` survived Phase 8 because they're integration interfaces
- Migrating first two to direct bus publishing completes the spine; Blacklist stays internal
- Low-priority cleanup
- *Design:* `archive/shipped/cyberdeck-event-stream-design.md` (Phase 8b section)

### Connection consequences round 2
- Daemon parking on connection-blocked spawns + recovery flow
- Detection shipped; consequences need hooks into spawn path + daemon lifecycle
- *Design:* spec'd inline; touches `daemon.py` lifecycle + `connection_monitor.py`

---

## MID FUTURE (designed but blocked or queued behind near-future)

### Universal list-names (spec-level rule)
- Every new listable object MUST carry `list_name`
- Two-tier: mechanical fallback instant + LLM-authored async overwrite (Haiku, ~$0.001/name)
- Lives next to canonical record (files_written, BlacklistEntry, WatchdogHistoryEntry, profile TOML, runtime objects in-memory)
- Open: how to display original text on demand; namespace conflict prevention
- Dovetails with morgue + keymap revision pass
- *Design:* user auto-memory `feedback_universal_list_names.md`

### Routing (`r`) — wire constructs together
- Coordination primitive AND **recovery primitive** (canonical: report-write-blocked-by-brake → route output to fresh construct)
- Strong argument for prioritizing sooner — see user auto-memory `feedback_wiring_as_recovery_primitive.md`
- *Design:* spec'd inline; touches `tui.py` (modal + binding) + `daemon_session.py` (route action)

### Daemon planning mode + pause/unpause (`E`) — deferred behind keymap revision
- Three workflow paths: (A) direct construct one-shot, (B) goal-to-daemon hot path, (C) planning mode for complex goals
- Planning mode is **input modality (modal), not daemon state**
- **Persistent tracking panel** (akin to Claude Code's "tasks" panel) — plan steps tick off as constructs finish
- Pause/unpause is the simpler half — can ship independently
- Composes with prompt-shaping iterative-plan-document directive
- *Design:* TBC; design doc to file when picked up

### Keymap revision pass (ON HOLD since 2026-04-27)
- Methodology: actions-first
- Three layers: (1) actions inventory **populated**, (2) themes synthesis **blank**, (3) joint keymap proposal **blank**
- **Blocks new global bindings until done** (planning mode `E` sits behind this)
- Spec impacts: orientation doc + CLAUDE.md + spec.md + tui.py
- *Design:* `in-flight/cyberdeck-keymap-revision.md`

### The morgue + watchdog log
- **Morgue:** append-only JSONL `<home>/.cyberdeck/sessions.jsonl`; one record per finalized construct
- UI: right-panel "Morgue" tab; `z` expand row; "resuscitate" action opens NewConstructScreen pre-populated with `--resume <session_id>`
- **Watchdog log v1 SHIPPED** — tripwire/blacklist record kinds + dedicated history tab still deferred
- Both follow "files on disk are the database" pattern
- Pairs with Routing (different recovery paths: morgue resumes session_id, wiring pipes output)
- *Design:* spec'd in `cyberdeck-state.md` Not Implemented section; TBC

### Plugin sub-features (deferred from C3 + retool)
- Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`)
- Persistent (stateful) mode for plugins needing long-lived service process (camera live preview, SSH session)
- MCP-as-metadata as v2 sub-shape (plugins routing through `--mcp-config`)
- Hierarchical Esc-up navigation
- Script manifests (declarative `(name, category, args, expected output shape)`)
- *Design:* `archive/shipped/cyberdeck-tools-plugins-profiles-retool.md` deferred-slices section

### Mechanic v3 — autonomous correction
- Headless-mode relaunch after crash without netrunner intervention
- Defer until v1 + v2 give enough trust data
- *Design:* `in-flight/cyberdeck-maintbot-design.md` v3 section

### Mechanic plugin-integrity scan
- Heartbeat-tick hash check on plugin files; fire if mid-run modification
- *Design:* `in-flight/cyberdeck-maintbot-design.md` follow-ups

### Tools-default-kit v2 implementation
- 2340-line design filed 2026-04-30; explicitly downstream of retool
- Kit-packs reconciliation, manifest schema, operational tempo / OPSEC / noise modulation
- *Design:* `in-flight/cyberdeck-tools-default-kit.md`

### Goal-edit force-push — apply-now interrupt of in-flight turn
- *Design:* spec'd in `cyberdeck-state.md` Not Implemented section

### Plugin permission gating + in-deck function exposure beyond `load_into_deck`
- Today: any loaded plugin callable by any construct via bridge
- Future: profile-restricted plugin visibility, or per-plugin brake-tier settings
- A plugin emits an MCP server constructs invoke through `--mcp-config`
- *Design:* `archive/shipped/cyberdeck-tools-plugins-profiles-retool.md` deferred slices

### Linux/Pi porting checklist (incrementally targeted)
- **Required for first Linux launch:** `launch.sh` sibling; verify clipboard.py wl-copy/xclip on deploy distro; PATH/shell expectations; real-deck Linux verification of every gotcha
- **Optional / form-factor-dependent:** headless mode (tmux/systemd); boot-time auto-launch; D1 substrate (local model)
- **Non-blocking:** Rich terminal feature detection; plugin platform gates
- *Design:* `cyberdeck-platform-portability.md` (living inventory)

---

## LONG FUTURE (designed for, no near-term path)

### Phase D — Local-model infrastructure (mostly hardware-blocked)
- **D1** — Local model runtime (Ollama-compatible API). Required for D2; ideal for B2; unblocks Watchdog substrate cloud→local swap
- **D2** — Arbiter pre-daemon classifier with TOML policy. Hardware-blocked for production (RK3588 NPU latency); could prototype on dev with measured latency
- **D3** — B2 fleet synthesizer on local substrate. Once D1 in. Substrate-blocked

### Wearable Cyberdeck Arbiter (deferred form-factor variant)
- Wrist unit + core unit (Radxa Rock 5C 16GB or OrangePi 5 Plus, RK3588 NPU)
- Software stack: Ubuntu arm64 → RKLLama → Qwen3 4B (w8a8 quant)
- Local-first dispatcher with cloud escalation; egress scrubber (presidio + detect-secrets); audit log SQLite + age-encrypted backups
- **NOT current scope** per CLAUDE.md
- *Design:* `archive/deferred/cyberdeck_arbiter_design.md`

### Phase E — Compliance mode (deferred indefinitely)
- Tokenization, secret store, watchdog blindfold
- "Personal use doesn't need it"
- *Design:* none filed; would file on demand

### Open spec questions (resolve as use cases land)
Sparklines vs phase dots · Concrete local model pick · Wired-pair construct UI · Plugin manifest format/sandboxing · Tripwire critical routing · Goal-edit diff classifier · Watchdog Q&A latency · Blacklist persistence scope · Tool registry v2 export/import · Active plugin discoverability · Connection monitor cadence · Recovery flow scope · Profile-addendum injection vs `--resume` · Thinking block content empty in Opus 4.7 stream-json
- *Source:* `cyberdeck-spec.md` Open questions

---

## EXPLICIT NON-GOALS (deferred indefinitely or actively rejected)

Check this list before proposing a slice that touches one of these areas. If you think we should reverse one of these, say so explicitly — they're decisions, not oversights.

- Inter-agent chatter as load-bearing communication (chatlog is observational; wirings stay limited)
- Real-time per-construct narration via small model (mechanical > model-narrated)
- Per-profile pool warming (pool always warms with default caliber)
- Built-in Claude Code tools surfaced as registry citizens (those are Claude Code's surface)
- Spawn-blocking on Online (consequences only kick in on Degraded/Offline)
- Merging daemon and watchdog (soft/loud distinction is core)
- Conflating netrunner and daemon (human is participant, not input)
- Auto-install in `doctor.py` (DETECT + SUGGEST only)
- Confirmation dialogs (deliberate-consent instead)
- Putting a model in latency-critical path (hotkeys, tripwire matching stay deterministic)
- Confidence-gated autonomy (self-reported confidence is a liar)
- Hot-reload of plugins (restart-required is the safe default)
- Listing plugins in profiles (plugin selection is daemon-per-spawn)
- Auto-discover scripts in `<home>/tools/<subdir>/` (registry is explicit)
- Merging tools and plugins into one type (different invocation models, lifecycles, trust boundaries)
- Putting plugins in `<home>/` (brake hook protects `<deck-source>/`)
- Dynamic skill mounting (skills stay Python-injected at launch)
- Ethics layering / per-skill permission preambles (hooks are the safety layer)
- Autonomous thrash bounds at the daemon level (runner's continuous-comms is the counter-thrash)
- `--bare` flag on Claude Max OAuth subprocesses (breaks OAuth/keychain auth)
- `--system-prompt` / `--append-system-prompt` for multi-line content on Windows (argv truncates at first `\n`; use `-file` variants)
- Auto-purge of run dirs (per-run workspaces, 2026-05-07). Runs persist until the netrunner explicitly deletes one (manual `rm`, or a future bulk "clean out all sessions" gesture). The cost of vaporizing wanted work dwarfs the cost of leaving stale dirs around
- Autonomous moves of files out of the run dir (per-run workspaces, 2026-05-08). The deck does not auto-promote, auto-archive, or auto-organize files generated during a run — the netrunner asks for what they want via normal spawn actions ("move foo.txt to keepers/")
- cwd-based per-run workspaces (per-run workspaces, 2026-05-08). Constructs cwd at `<home>` for cache locality + Claude Code's per-project session storage; the run dir is a prompt-level output convention, not a sandbox. cwd-per-run was real-deck-broken (cross-cwd `--resume` returns "No conversation found"); not retrying that approach

---

## Cross-cuts and dependency edges

- **Item 13 (quota signal)** unblocks **Caliber Phase 4** AND naturally pairs with **Item 0f (adversarial dyad)** — Phase 4 needs both quota AND quality signals to make smart escalation decisions
- **Item 0g (iterative triage)** + **Item 0h (repair authority)** compose — repair could be triggered as a "third pass" when iterative triage detects config issues
- **Mechanic v1.5 prompt-thread state machine** is the reusable substrate for items 0g/0h
- **Universal list-names** + **Per-run workspaces** + **Morgue** all dovetail (folder = `run-{run_id}-{list_name}/`; morgue session record gains `cwd` field)
- **Routing (`r`)** + **Morgue** = complementary recovery paths (wiring pipes output; morgue resumes session_id)
- **Keymap revision** blocks any new global keybinds — including planning-mode `E`
- **Item 000 phase 2** simplified by phase-1 selective policy (Watchdog Q&A keeps CLAUDE.md, so role files only needed for KILLED roles where regression appears)
- **Prompt-shaping pass** has overlap with **Item 000 phase 2** (both touch system prompt composition); coordinate when both pick up
- **B2 fleet synthesizer** is substrate-blocked on D1; spine completion gives it a clean substrate when D1 lands
- **Architecture review (2026-06-01)** is a soft phase-checkpoint — agent defers if work is in flight
