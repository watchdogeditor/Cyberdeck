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

### Models catalog — item 13 follow-on (2026-05-11 late)
Bundled response to Anthropic's announced 2026-06-15 SDK-credit-pool change. The deck's local-substrate migration (long-deferred Phase D) is now on the critical path; the catalog primitive ships first to set up backend-adapter swap-ins. The daemon's caliber decisions become data-driven against a netrunner-edited catalog rather than hardcoded Anthropic-model knowledge.

- New `models_registry.py` (~530 LOC) loads `<deck-source>/roles/models.toml` — same folder as `general.toml` + role markdown files. Default-restore on missing / wiped file. Bundled defaults ship the three Anthropic entries (haiku/sonnet/opus); commented-out templates for ollama / openrouter / private — netrunner uncomments + tunes when backend adapters ship.
- Schema: per-model `name` + `power` (informational summary) + `provider` + `api_model` + `description` + `use_cases` (pipe-separated; daemon's primary relevance signal) + `cost_per_1m_input/output` + `network_required`. Subtables: `[model.requirements]` (hardware constraints for local providers) + `[model.effort.<level>]` (per-effort `power` rank + description + api_effort).
- **Power calibration**: each effort table has its own `power` value; daemon ranks (model, effort) pairs by `effort.power`. Encodes both base capability AND effort's contribution. Netrunner-tunable judgment.
- DAEMON_SYSTEM_PROMPT's CALIBER SELECTION rewritten: hardcoded mappings → catalog-driven decision procedure (filter by use_cases → rank by power → apply CONNECTION/QUOTA/RESOURCE constraints → tie-break local-over-cloud + cheaper-over-pricier). Default disposition flipped: "start at LOW end of power range; escalate only when task warrants."
- New CONNECTION AWARENESS + RESOURCE AWARENESS sections in daemon system prompt. CONNECTION wires in NOW (consumes existing ConnectionMonitor state via new DaemonSession.connection_state_provider); RESOURCE AWARENESS section ships forward-compatible (RESOURCES line wires in next slice when hardware_profile + resource_monitor ship).
- CONNECTION line injected into `_format_outcomes` alongside QUOTA — graceful degradation when provider is None (daemon's prompt handles missing as "assume online").
- TUI wiring: `self.models_registry` in App.__init__; catalog appended to daemon system prompt via `render_catalog_for_daemon`; `_connection_state_for_daemon` reads ConnectionMonitor → passed as DaemonSession kwarg.
- *Design:* schema + decision procedure documented inline in `models_registry.py` docstring + DAEMON_SYSTEM_PROMPT.
- *Pending real-deck verification:* models.toml seeds on first launch; netrunner edits survive restart; wiped file restores defaults; CONNECTION line appears in daemon prompts; daemon visibly picks lower-power calibers under new default disposition.

### Quota-aware throttling — item 13 + Caliber Phase 4 (2026-05-11)
- Claude Code's `statusLine` per-subprocess command wired via `quota_statusline.py` to atomically write `<home>/.cyberdeck/quota.json`. Every claude spawn (constructs + pool warmers) populates the file as a side effect of its first model call. `brake_state.make_spawn_settings` grows a `statusLine` block universally — even YOLO spawns get the file (quota tracking is observation, not enforcement).
- `quota_reader.py` deck-side: `QuotaSnapshot` + `QuotaWindow` dataclasses, `load(home_dir)` with stale detection (60-min default threshold), `format_for_daemon(snapshot)` rendering the `QUOTA: 5h=47% 7d=12%` line (with `(stale by N min)` suffix when over threshold). Tolerant parsing throughout.
- Caliber Phase 4 (Daemon awareness) landed simultaneously: `DAEMON_SYSTEM_PROMPT` grew `QUOTA AWARENESS` section with policy ratchet bands (<50% normal / 50-75% slight bias / 75-90% tier-down / >90% refuse non-essential). Per-turn QUOTA line injected via `daemon_session._format_outcomes` (new `quota_snapshot=` kwarg) between human-input/warning blocks and outcomes. DaemonSession's new `quota_provider` callable invoked per turn; TUI wires it as `lambda: quota_reader.load(self.home_dir)`.
- Cold-start covered by side effect: pool warmers populate quota.json before the daemon's first turn. Daemon system prompt explicitly handles "no QUOTA line at all" as cold-start scenario, proceeds without quota considerations.
- Pi tmpfs: `CYBERDECK_QUOTA_PATH` env-var handled symmetrically by writer + reader. Linux/Pi deployments point at `/dev/shm/cyberdeck-quota.json` without code changes.
- Files: new `quota_statusline.py` (~200 LOC), new `quota_reader.py` (~200 LOC). Edits: `brake_state.py` (statusLine universal; settings file always returned), `daemon.py` (QUOTA AWARENESS section), `daemon_session.py` (quota_provider param + _format_outcomes injection), `tui.py` (`_load_quota_snapshot` helper + DaemonSession wire-up).
- *Design:* `in-flight/cyberdeck-model-effort-design.md` (STATUS banner now lists Phase 4 as shipped; Phase 4 section documents the implementation).
- *Pending real-deck verification:* quota.json appears in `<home>/.cyberdeck/` after first claude spawn; QUOTA line appears in daemon prompts; stale flag fires correctly after >60 min idle; daemon visibly tiers down caliber when quota >75%; CYBERDECK_QUOTA_PATH redirects correctly.

### Role injection — item 000 phase 2 (2026-05-11)
- Per-role system prompts externalized to `<deck-source>/roles/*.md` behind `prefs.role_injection` flag (default OFF for first ship). Four role files: `daemon.md`, `watchdog-qa.md`, `watchdog-authoring.md`, `advisor.md`. Plus `general.toml` for netrunner identity (name + pronouns + free-text notes) prepended to every role-injected spawn.
- Scope narrowed from original design (netrunner direction 2026-05-11):
  - **Construct STAYS in code**: defense-in-depth content (brake awareness, dispatcher protocol, security-architecture prose) shouldn't be user-editable even on a single-netrunner deck.
  - **Mechanic v1 (triage) + v2 (repair) STAY in code**: recently verified, no ergonomic gain from externalizing.
  - **No hot reload**: load once at startup; restart picks up edits. Mid-flight role-prompt changes would produce confusing half-applied behavior.
  - **All configs in one folder**: `<deck-source>/roles/` holds the 4 `.md` files + `general.toml` (per "all configs accessible" netrunner direction).
- New modules: `roles_registry.py` (~280 LOC), `general_config.py` (~200 LOC), `roles/_defaults.py` (~180 LOC), `roles/__init__.py`.
- Existing modules touched: `advisor.py` (extracted ADVISOR_TEMPLATE constant + `template=` kwarg), `watchdog.py` (new `authoring_system_prompt` attribute), `preferences.py` (new `role_injection` property), `tui.py` (registry + general_config wiring in `App.__init__`, `_compose_role_text` + `_apply_role_injection_to_watchdog` helpers, daemon prompt builder consults flag, AdvisorScreen takes `template=`).
- Circular-import dodge: `_defaults.py` uses lazy imports inside per-role builder functions; registry calls them at `load()` time, not at module import.
- Flag-OFF behavior unchanged: every spawn site uses the in-code constant. Flag-ON reads from disk. The `.md` files + `general.toml` are gitignored — runtime artifacts seeded from canonical `_defaults.py` constants in code (same pattern as profile_registry seeding `default.toml`).
- *Design:* `in-flight/cyberdeck-spawn-context-isolation.md` (STATUS banner updated; design doc still in-flight as documentation reference for future role additions).
- *Pending real-deck verification:* flag-OFF default produces identical behavior to pre-phase-2; flipping flag-ON routes 4 roles through their `.md` files; editing a role file + restart picks up the change; saving a role file blank restores the bundled default on next launch; populated `general.toml` injects identity block.

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

### Mechanic v2 — config-file repair authority (item 0h, 2026-05-10)
- Scan-and-propose for the deck's three writable config-file shapes: `<home>/.cyberdeck/state.json`, `<home>/profiles/*.toml`, `<home>/tools/tools.toml`. NOT deck source — that boundary is hard.
- New `mechanic_repair.py` (~600 LOC) — Family A spawn shape mirroring triage. LLM is read-only (Read/Glob/Grep) and proposes via structured JSON output; deck applies via Python with hard path allowlist + per-proposal y/N/q approval + backup-before-write. Same separation philosophy as tripwires (LLM authors, deterministic engine enforces).
- Sanity-check semantics per netrunner direction: SHAPE check, not value check. Propose fixes for syntax errors / missing required fields / wrong types / enum violations / broken file references; do NOT propose for non-default values that are still type-valid. Non-defaults get acknowledged in a separate "Non-default values noticed" section — the netrunner sees the divergence without it becoming a fix proposal.
- Two activation paths: (a) **triage-coupled** — `mechanic_triage.py`'s system prompt got a new `## Repair recommendation` section ("Recommend repair: Y/N - reasoning"); v2 reads it via `parse_repair_recommendation` to set the post-triage prompt default ([Y/n] when triage recommended; [y/N] otherwise), runs as a fresh spawn (not --resume off triage). (b) **standalone summon** — `python mechanic.py --repair` skips the supervisor loop and fires a one-shot scan against the resolved home dir.
- New CLI flags: `--repair`, `--no-repair-prompt`, `--repair-timeout` (default 240s). Backup format: `<home>/.cyberdeck/repair-backups/<YYYY-MM-DD-HHMMSS>-<basename>` with `.N` suffix for sub-second collisions. Plain stderr only — no ANSI color.
- New symbols: `RepairRequest` / `RepairProposal` / `RepairResult` dataclasses; `is_writable_path` (deck-side allowlist enforcement); `parse_repair_response` (tolerant JSON extraction mirroring `tripwires.parse_authoring_response`); `parse_repair_recommendation` (triage-report Y/N parser with heading-section + keyword fallback); `present_proposal` (diff display + y/N/q prompt); `apply_proposal` (allowlist check + no-op detection + backup + write/delete); `run_repair` (orchestrator); `prompt_repair` (post-triage Y/n vs y/N picker).
- *Design:* `in-flight/cyberdeck-maintbot-design.md` (v2 section + STATUS banner updated)
- *Pending real-deck verification:* triage-coupled trigger fires at end of iterative triage; recommendation parser correctly picks default; per-proposal approval loop renders diffs cleanly on cmd.exe / Windows Terminal; allowlist rejects non-config paths; backup directory created on first apply; standalone --repair path works without a log file.

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

Branch `claude/awesome-wozniak-41d9f1` ahead of `origin/main`. Models catalog (item 13 follow-on) shipped 2026-05-11 late — first step of the accelerated local-substrate migration triggered by Anthropic's 2026-06-15 SDK-credit-pool change. Quota-aware throttling (item 13 + Caliber Phase 4) shipped 2026-05-11. Item 000 phase 2 (role injection) shipped 2026-05-11. Mechanic v2 (item 0h) shipped 2026-05-10. Per-run workspaces (v5, 2026-05-08) and tripwires redesign (2026-05-07) also pending real-deck eyes. **Highest-priority next slice: hardware_profile + resource_monitor + first backend adapter (Ollama)** — completes the local-substrate plumbing. Remaining candidates after that:

### 1. Item 0f — Adversarial dyad
- Generator/discriminator paired-construct pattern. Daemon synthesizes both opinions; provides "is this work good enough" signal for caliber escalation
- ~600-900 LOC + new design doc `cyberdeck-adversarial-dyad-design.md` (TBC)
- Picks up post-architecture-review
- Companion to caliber Phase 4 (provides quality signal alongside item 13's quota signal)
- *Design:* doc to be filed; concept summary in this build plan + user auto-memory `project_prompt_shaping_design.md`

### 2. Architecture review
- Scheduled to fire 2026-06-01 09:00 EDT (taskId `cyberdeck-architecture-review`)
- Read-only; outputs `Design Files/cyberdeck-review-<date>.md`
- Findings under (A) architecture coherence, (B) hard-rules compliance, (C) filed-gotcha re-introduction risk, (D) tech debt + TODOs
- Agent phase-checks first; defers if work is in flight

### Verification opportunities pending real-deck eyes
- Models catalog (models.toml seeds on first launch in `<deck-source>/roles/`; wipe-to-restore works; daemon prompt includes the rendered catalog block; CONNECTION line appears in daemon prompts; daemon's caliber picks visibly trend lower-power post-new-default-disposition)
- Quota-aware throttling (quota.json appears in `<home>/.cyberdeck/`; QUOTA: line appears in daemon prompts on second turn onward; stale flag fires after >60 min idle; daemon tiers down caliber at >75% quota; CYBERDECK_QUOTA_PATH env override redirects file)
- Role injection phase 2 (flag-OFF identical to pre-phase-2; flipping flag-ON routes 4 roles through `.md` files; save-blank-to-restore works; populated general.toml injects identity block)
- Per-run workspaces v5 (manual constructs work fast via pool reuse; daemon composes run dir into spawn task strings; cross-launch pool reuse works)
- Tripwires redesign (X-allow on critical fire skips kill; YOLO truly installs no settings file; tripwire fires under default brake produce visible delay overlays with tripwire context)
- Mechanic v2 repair authority (triage-coupled trigger fires; recommendation parser picks default; per-proposal approval renders cleanly; allowlist rejects non-config paths; backup dir created on first apply; standalone --repair works without a log)
- Construct/daemon pane chrome + tripwire overlay (look in narrow panes; daemon Q/A flow during real goal; tripwire-overlay text wrapping when description is long)
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

### Construct spawn: migrate to `--append-system-prompt-file` (filed 2026-05-14)
The deck's `construct.py` spawn path uses `--append-system-prompt <text>` directly with the deck addendum concatenated as a single newline-collapsed argv. This is the LAST major spawn site in the deck still passing addendum content through argv rather than through a `-file` variant. Real-deck-verified 2026-05-14: interpolating Windows backslash-paths into the addendum (the `_active_run.dir_path` interpolation) brought down every construct spawn with a CreateProcess "file not found" error. Reverting the path interpolation fixed the immediate fire but left the underlying class of bug intact — any future addition to the deck addendum that includes a Windows path or pathological string risks the same failure mode.

**Slice shape (~30-50 LOC)**:
- Mirror the pattern from `mechanic_triage.py` / `mechanic_repair.py` / `watchdog.py`: write composed addendum to a tempfile via `tempfile.mkstemp`, pass `--append-system-prompt-file <path>`, unlink in `finally`.
- Cleanup needs to be per-construct lifecycle aware — temp file lives only as long as the subprocess is reading its prompt at startup. Can unlink immediately after `proc.stdin` close OR after `proc.wait()` completes (depending on whether claude code reads the file lazily; verify on real deck).
- After this migration, the deck addendum can safely interpolate Windows paths again — `_active_run.dir_path` re-injection becomes safe.

Once this lands, the path-in-argv class of bug can't surface at the construct spawn site. Filed in `cyberdeck-deck-fires.md` Category 1.

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

### Mechanic launch-checksum / integrity scan (speculatively deferred, 2026-05-11)
Filed as a tentative v2 follow-on after the netrunner mused about it during v2 testing. **Status: not built; may not be needed.** Real-deck use of v2 will tell us whether there's a class of config corruption the LLM consistently misses.

The shape: deck computes a hash of each config file at known-good state (probably first-launch / post-seed / post-explicit-baseline), stores it. On launch / on crash, the mechanic compares against the stored hash to detect "this changed" without needing an LLM scan. Sub-millisecond, no tokens, no false negatives within its scope.

The tradeoff: it can't tell "netrunner edited their profile intentionally" from "file got corrupted." Every legitimate edit invalidates the checksum, asking for re-baselining. That ergonomic cost compounds — profiles are meant to be edited freely per the philosophy ("file system is the database, edit with vim").

Reasonable middle-ground if it lands: only baseline-checksum files that have a deck-canonical version (default.toml, the seeded tools.toml header). Don't checksum user-authored profiles (no canonical to compare against). But that's basically what v2's `restore_default` proposal kind already does — LLM reads the canonical seed in source, compares to disk, proposes restore if diverged. We may not need a separate hash mechanism.

When to pull this forward: if real-deck reports show v2's LLM consistently missing a specific corruption pattern that a checksum would catch reliably (e.g. "I edited state.json by hand and it broke, but v2 said it looked fine"). Until that signal, defer.

*Design:* spec'd inline above; touches a small new state-tracking module + integration into mechanic.py launch path. ~100-200 LOC if built.

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

- **Item 13 (quota signal) + Caliber Phase 4 shipped 2026-05-11** as a bundled slice (signal + consumer landed together). Item 0f (adversarial dyad) remains Phase 4's natural companion — Phase 4 now has quota signal + needs the quality signal 0f will provide for the most sophisticated escalation decisions.
- **Item 0g (iterative triage)** + **Item 0h (repair authority)** compose — both shipped 2026-05-07 and 2026-05-10 respectively. The composition shape: triage's `## Repair recommendation` section (Y/N + reasoning) sets the default for the post-triage repair prompt; v2 runs as a fresh spawn (not --resume off triage) for clean role separation
- **Mechanic v1.5 prompt-thread state machine** is the reusable substrate for items 0g/0h
- **Universal list-names** + **Per-run workspaces** + **Morgue** all dovetail (folder = `run-{run_id}-{list_name}/`; morgue session record gains `cwd` field)
- **Routing (`r`)** + **Morgue** = complementary recovery paths (wiring pipes output; morgue resumes session_id)
- **Keymap revision** blocks any new global keybinds — including planning-mode `E`
- **Item 000 phase 2 shipped 2026-05-11** — scope landed narrower than the original design (construct + mechanic stay in code per netrunner direction; 4 role files instead of 5). Phase 1 selective policy still applies (Watchdog Q&A keeps CLAUDE.md). The shipped form preserves flag-OFF as current behavior.
- **Prompt-shaping pass** has overlap with item 000 phase 2 (both touch system prompt composition). With phase 2 shipped, prompt-shaping iterations now have a clean substrate to land on — the netrunner edits `roles/*.md` between launches without code changes.
- **B2 fleet synthesizer** is substrate-blocked on D1; spine completion gives it a clean substrate when D1 lands
- **Architecture review (2026-06-01)** is a soft phase-checkpoint — agent defers if work is in flight
