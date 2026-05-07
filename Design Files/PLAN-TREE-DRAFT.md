# Cyberdeck — Plan Tree (start to finish)

*Draft assembled 2026-05-07 from across all 19 design files + CLAUDE.md battle plan + state.md graveyard. Today's anchor: branch `claude/objective-sammet-25e0b4` is ~42 commits ahead of `origin/main`. Working artifact for the Design Files restructure — not yet a canonical doc.*

---

## SHIPPED

### Tier 1 — Original scope (long stable, pre-Claude-Code-migration)
- M0 Construct (subprocess + lifecycle)
- M1 Fleet (N concurrent constructs, NDJSON logging, run accounting)
- M2 TUI skeleton over fleet
- M3 Keyboard-driven cyberdeck (focus, jump keys, spawn/kill modals, expand/collapse)
- M4a Daemon-driven goals (decomposition, max_concurrent gating, respawn-loop detection, files_written propagation)
- M5.1–M5.3e (keymap, idle lifecycle, focus polish, session pool with cross-restart reuse, RAM meter)
- Phase A Step 1–3 (EJECT, q/Q construct injection, `l` Limits modal)

### Tier 1+ Observability — B1
- Activity chatlog · mechanical event extraction · color-coded · deque-backed · ExpandModal-magnifiable

### Tier 2 — Profiles (C1)
- TOML loader, frozen `Profile` dataclass, `ProfileRegistry` with hot-reload
- `default_construct_addendum` / `default_daemon_addendum`
- Daemon picks profile per-spawn via JSON
- C1g listification (Tools/Files → ListView, LaunchScreen modal)

### Tier 2 — Tool registry C3 (all three legs)
- Profiles, Scripts, Plugins registries (folders with manifest + README + entry script; screenshot as first plugin)
- Phase A deck-control protocol (dispatcher script, marker protocol)
- Phase B Tools-panel restructure

### Profile/brake refactor + brake state
- Brake state deck-global, persisted at `<home>/.cyberdeck/state.json` via `b` modal
- Brake enforcement via Claude Code `PreToolUse` hooks (`brake_hook.py` ~150 LOC)
- Watchdog observes via `permission_denials`; chatlog `· brake blocked: Write×2` suffix
- Yellow-border + header-badge pane indicators
- C2 brake-profiles superseded by deck-global model

### Production-grade fixes (recent baseline, pre-2026-04-30)
- Pane-log un-trim, watchdog Q&A `t` streaming, daemon chat `T`, goal-edit `e` with diff classifier
- Connection state monitor, tabbed bottom panel, spawn provenance origin badges
- z-to-view, path-normalized Files panel, focus navigation fall-through, ProactorEventLoop shutdown noise filter
- Watchdog Blacklist primitive (`Shift+K`)
- Connection consequences round 1 — spawn-blocking on Degraded/Offline

### Watchdog tripwires + blacklist (slices 1+2)
- **Slice 1** (2026-04-29) — `tripwires.py` deterministic matcher, DSL, severity tiers, two default tripwires
- **Slice 2** (2026-04-29) — LLM-authored tripwires at goal-start; rung-1 fork (`claude -p --resume`); rung-2 fresh fallback. Real-deck verified 2026-04-30 (rung-1 fork at 4.2s vs 19.7s fresh)

### Unified event stream — 8/8 phases COMPLETE
- Phase 1 (`event_bus.py` + `DeckEvent`) → Phase 7 (file logger as bus subscriber + quit discipline) → Phase 8 (listener-shim cleanup, ~75 LOC net deletion)
- Per-launch NDJSON in `<deck-source>/logs/`, smart Ctrl+Q with running-state guard, parent SIGINT swallow

### Late 2026-04-30 cluster
- **y/Y copy keybind** — vim-yank focused widget; `clipboard.py` (ctypes Win32 + pbcopy + xclip/wl-copy)
- **Limits modal rework** — uncapped construct counts; defaults bumped (10/30/5); pool refill gate
- **Mechanic v0** (~270 LOC) — sibling Python process watches deck PID, kills orphan claudes
- **Spine Phase 8 listener-shim cleanup** — bus is sole fan-out path
- **Kill state-stuck race fix**, **Tui dupe-pane fix** (subscription-handle tracking)

### Safety architecture pass (4/4 slices shipped)
- **Slice 1: MCP gating in `brake_hook.py`** (commit 6510c5d) — verb-based pattern matching
- **Slice 2: Tripwire escalation chain** (commit 22da9ad) — low→log; warning→deny+suggest; critical→deny+auto-term; critical+bad_enough→same+blacklist proposal
- **Slice 3: Variable-outcome delay UX** (2026-05-01) — Phase 1 (delay rename, X→X-ecute, EJECT-style countdown overlay), Phase 1.5 (limits persistence), Phase 2 (blacklist proposals + AttentionPanel)
- **Slice 4 (¼)**: `host_restart_command` warning tripwire promoted into defaults
- **Kill audit** (commit 72ee5e9) — `kill_source` field on finalize

### Diagnostics + cost (2026-05-01 → 2026-05-02)
- **Wedge-timeout diagnostic** (commit f3f6f2d) — stderr drain on TimeoutError; `stderr_excerpt` on finalize
- **Cache cost fix** (commit 1dea7f7) — stable shared `spawn_settings.json`; `system_changed` cache misses gone
- **Tripwire-authoring spawn-race fix** (commit 8632b00) — DaemonSession awaits `tripwire_authoring_complete`
- **Discrete bugs cluster** (commit 60b91aa) — daemon over-volunteers + enum payload serialization
- **Construct-refusal as structured event** (`kind=construct.refused`)

### Tools / Plugins / Profiles retool — 5/5 phases COMPLETE (2026-05-03 → 2026-05-04)
- **P1** — `tools_registry.py` + mtime-watch + `tools.toml` + ⚙/⌬ kind glyphs
- **P2** — plugins moved into deck source; brake-hook protection extends to plugin code; `plugin_bridge.py` dispatcher (~170 LOC)
- **P3** — `load_into_deck(app)` hook; per-plugin try/except; bus events `plugin.hook_*`
- **P4** — Profile schema migration `recommended_tools` → `tools`; per-spawn `plugins` field on daemon spawn actions
- **P5** — Four-tab right panel (Chatlog | Files | Profiles | Tools); unified Tools ListView with kind glyphs

### Caliber slice — 4/5 phases shipped (2026-05-04)
- **Phase 1** — `caliber.py` (~250 LOC); plumbing through Construct / Fleet.spawn / DaemonSession
- **Phase 2** — `SessionPool.warm_caliber` + match-or-skip gate; `make_spawn_settings(fast_mode=...)`
- **Phase 3** (scoped down) — Daemon model PINNED to opus, effort = power-level knob; `EffortPickerScreen` modal; persisted `daemon_effort`
- **Phase 5** — Sidebar daemon line; per-pane caliber suffix; watchdog Q&A CALIBER AWARENESS section
- *Phase 4 NOT shipped — blocked on quota signal (build-plan item 13)*

### Tools-UI "Thought of Dave" slice — 3/3 sub-features shipped
- **space-launch** (2026-05-04) — TOOL: / PLUGIN: envelopes
- **z-info** (2026-05-04) — synthesized info modal for tools; README.md view for plugins
- **h-Advisor** (2026-05-05) — narrowly-scoped per-tool Q&A bot, modal-scoped, `advisor.py` (~460 LOC), haiku+low

### Public-repo readiness (build-plan items 0/0a/0b/0d, 2026-05-04)
- **Item 0** — README restructure (commit 1aa7564)
- **Item 0a** — `doctor.py` (commit cddae01); 5 prereq checks; `--doctor` / `--no-doctor`; first-run sentinel
- **Item 0b** — `preferences.py` (commits 213ae90, 9195ceb); typed properties + delta-save
- **Item 0d** — Mechanic v0→v1 bridge: liveness heartbeat (commit ecead5a); 5s deck heartbeat; mechanic logs STALE after 20s

### Auto-context discovery + per-role isolation phase 1 (2026-05-05)
- **🚨 Discovery** — every claude subprocess silently auto-loads `<cwd>/CLAUDE.md` + parent dir tree + `~/.claude/CLAUDE.md` + auto-memory
- **Phase 1: per-role env-var belt** (~80 LOC) — `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` etc.
  - **KILLED:** Advisor, Construct, Daemon, Pool warmer, Tripwire-authoring Watchdog
  - **KEPT:** Watchdog Q&A
- **Multi-line argv truncation fix** — switched to `--system-prompt-file` / `--append-system-prompt-file`. Promoted to top-level Hard Rule
- **`user_email_protection` default tripwire** — mitigation for upstream bug `anthropics/claude-code#55743`
- **Watchdog Q&A PROJECT MEMORY AWARENESS** in `WATCHDOG_SYSTEM_PROMPT`

### Mechanic v1/v1.5 cluster (2026-05-06, ~14 commits)
- **Item 0e — Mechanic v1: diagnose-only LLM-session triage** — `mechanic_triage.py` (~480 LOC); Family A clean-spawn recipe; structured Markdown to `<log>-triage.md`
- **Item 0e2 — Mechanic v1.5: stale-heartbeat triage** — interactive prompt + listens-for-recovery; `--auto-triage-on-stale` for headless; state machine
- **Adjacent fixes:** Ctrl+Q triage skip; ctypes Windows-handle truncation in `_pid_alive_win`; log-selection race; live narration via `stream-json --verbose`; partial-recovery on timeout; default timeout 180s→300s
- **`cyberdeck-platform-portability.md`** filed as living inventory

---

## CURRENT FRONTIER (where work is right now)

Branch `claude/objective-sammet-25e0b4` ~42 commits ahead of `origin/main`. Deck at clean phase point — most recent slice fully real-deck verified.

### Ranked candidates for next session pickup (per CLAUDE.md battle plan)
1. **Item 0000 — Tripwire-authoring "gotchas" addendum** (filed 2026-05-05, low urgency post phase-1 of item 000). ~150 LOC.
2. **Item 000 phase 2 — Role-injection infrastructure** (deferred, conditional). `roles_registry.py`, `general.toml`, `<deck-source>/roles/*.md`. ~600-1000 LOC. Pull forward only on concrete regression.
3. **Item 0f — Adversarial dyad** (filed 2026-05-06). Generator/discriminator paired-construct pattern. ~600-900 LOC + new design doc. Companion to caliber Phase 4 (provides quality signal alongside quota signal).
4. **Item 0g — Mechanic iterative triage** (filed 2026-05-06). Multi-pass deepening with stderr prompts between passes. ~200-300 LOC.
5. **Item 0h — Mechanic repair authority for non-source issues** (filed 2026-05-06; promotes maintbot v2). Diff-preview + per-fix approval for config files. ~300-400 LOC.
6. **Architecture review** scheduled 2026-06-01 09:00 EDT (taskId `cyberdeck-architecture-review`). Read-only; outputs `Design Files/cyberdeck-review-<date>.md`.

### Verification opportunities pending real-deck eyes
- Limits modal two-column panel + EffortPickerScreen
- Caliber Phase 5 surfaces (sidebar `daemon: opus·high`, per-pane caliber suffix)
- Tools-UI: space-launch, z-info, h-Advisor on real targets
- `python tui.py --doctor` first-run + happy-path
- Mechanic heartbeat under normal operation

---

## NEAR FUTURE (queued, well-specified, ready to pick up)

### Prompt-shaping pass (committed directives, queued for other branch)
- **Ethics-strip:** single identity-line + single pre-authorization-line; no per-skill ethics preambles
- **Iterative plan document:** daemon writes a real plan file (analogue of transilience's `attack-chain.md`)
- **Opt-in validator:** daemon-activated, not blanket; rides cheapest tier (Haiku); naturally couples with caliber
- **Skills stay Python-injected at launch — no dynamic mounting**
- *Source:* user auto-memory `project_prompt_shaping_design.md`

### Collections intake (filed 2026-05-06)
- Recipe-driven plugin scaffolding for github-distributed reference collections (PATT, SecLists, nuclei-templates, GTFOBins, HackTricks, LOLBAS)
- Three pieces: intake recipe TOML + assembler script + generated plugin
- Implementation queued behind prompt-shaping pass and Mechanic v2

### Caliber Phase 4 — Quota-aware fallback
- HARD-BLOCKED on build-plan **item 13** (quota signal)
- Read `<deck>/.cyberdeck/quota.json`; daemon system prompt grows quota-aware band

### Item 13 — Quota-aware throttling
- Daemon gates spawns on remaining Max quota (5h + weekly windows)
- Mechanism: Claude Code's status-line script receives rate-limit fields on stdin; writes JSON; daemon reads
- Pi gotcha: tmpfs (`/dev/shm/cyberdeck-quota.json`) on OrangePi to avoid SD-card wear

### Discrete bugs (deferred but specified)
- **Kill doesn't interrupt in-flight assistant turns** — SIGTERM lands AFTER model finishes turn
- **Silent wedge investigation (cx-796e0468 case)** — empty `stderr_excerpt`; needs more real-deck data
- **Daemon narrative fix** — daemon mislabels brake-hook denials as tripwire fires
- **Verify Claude Code's fast-mode settings.json key** — `{"fastMode": true}` may need to be `{"speed": "fast"}`

### Tripwires slice 3 — severity-aware rendering
- Critical pulls focus, warning badges, low logs only

### Tripwires slices 4-6 (LLM-authoring follow-ons)
- **Slice 4** — persistent tripwire library at `<home>/tripwires/` with TOML authoring
- **Slice 5** — daemon-side severity hints
- **Slice 6** — blacklist-derived tripwires that fire on event content

### Mechanic v0 follow-ups
- Track non-construct subprocesses: daemon, watchdog Q&A, watchdog authoring one-shots, pool warmers
- One line per spawn site to add `pid` field; one elif per source in `mechanic._apply_record`

### Spine Phase 8b — Pool + Daemon callback cleanup
- SessionPool's `on_event`, Daemon's `on_daemon_event`, Blacklist's `on_event` survived Phase 8 because they're integration interfaces

### Connection consequences round 2
- Daemon parking on connection-blocked spawns + recovery flow

---

## MID FUTURE (designed but blocked or queued behind near-future)

### Per-run workspace compartmentalization
- Default spawn cwd `<home>/` → `<home>/runs/<run_id>/`. ~50-80 LOC
- Composes with universal list-names, morgue, files-panel dedupe

### Universal list-names (spec-level rule)
- Every new listable object MUST carry `list_name`
- Two-tier: mechanical fallback instant + LLM-authored async overwrite (Haiku, ~$0.001/name)
- Lives next to canonical record

### Routing (`r`) — wire constructs together
- Coordination primitive AND **recovery primitive** (canonical: report-write-blocked-by-brake → route output to fresh construct)
- Strong argument for prioritizing sooner

### Daemon planning mode + pause/unpause (`E`) — deferred behind keymap revision
- Three workflow paths: (A) direct construct one-shot, (B) goal-to-daemon hot path, (C) planning mode
- Planning mode is **input modality (modal), not daemon state**
- **Persistent tracking panel** (akin to Claude Code's "tasks" panel) — plan steps tick off as constructs finish
- Pause/unpause is the simpler half — can ship independently if planning mode stalls

### Keymap revision pass (ON HOLD since 2026-04-27)
- Methodology: actions-first
- Three layers: (1) actions inventory **populated**, (2) themes synthesis **blank**, (3) joint keymap proposal **blank**
- **Blocks new global bindings until done** (planning mode `E` sits behind this)

### The morgue + watchdog log
- **Morgue:** append-only JSONL `<home>/.cyberdeck/sessions.jsonl`; one record per finalized construct
- UI: right-panel "Morgue" tab; `z` expand row; "resuscitate" action opens NewConstructScreen pre-populated with `--resume <session_id>`
- **Watchdog log v1 SHIPPED** — tripwire/blacklist record kinds + dedicated history tab still deferred
- Pairs with Routing (different recovery paths: morgue resumes, wiring pipes)

### Plugin sub-features (deferred from C3)
- Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`)
- Persistent (stateful) mode for plugins needing long-lived service process
- MCP-as-metadata as v2 sub-shape
- Hierarchical Esc-up navigation
- Script manifests; Construct script-launch wiring

### Mechanic v2 — guided correction (PROMOTED to item 0h)
- Limited write access: config files in `<home>` only; per-fix netrunner approval; backup before applying

### Mechanic plugin-integrity scan
- Heartbeat-tick hash check on plugin files; fire if mid-run modification

### Tools-default-kit v2 implementation
- 91KB design doc filed 2026-04-30; explicitly downstream of retool
- Kit-packs reconciliation, manifest schema, operational tempo / OPSEC / noise modulation

### Goal-edit force-push — apply-now interrupt of in-flight turn

### Plugin permission gating + in-deck function exposure beyond `load_into_deck`

### Linux/Pi porting checklist (incrementally targeted)
- **Required for first Linux launch:** `launch.sh` sibling; verify clipboard.py wl-copy/xclip; PATH/shell expectations
- **Optional:** headless mode (tmux/systemd); boot-time auto-launch; D1 substrate (local model)

---

## LONG FUTURE (designed for, no near-term path)

### Phase D — Local-model infrastructure (mostly hardware-blocked)
- **D1** — Local model runtime (Ollama-compatible API)
- **D2** — Arbiter pre-daemon classifier with TOML policy (RK3588 NPU latency-blocked)
- **D3** — B2 fleet synthesizer on local substrate

### Wearable Cyberdeck Arbiter (deferred form-factor variant)
- Wrist unit (display + input, ESP32-S3/RP2040, BLE/USB-C) + core unit (Radxa Rock 5C 16GB or OrangePi 5 Plus, RK3588 NPU)
- Software stack: Ubuntu arm64 → RKLLama → Qwen3 4B (w8a8 quant)
- Local-first dispatcher with cloud escalation; egress scrubber (presidio + detect-secrets); audit log SQLite + age-encrypted backups
- Build order: prototype on existing Pi → dispatcher state machine → scrubber → wire arbiter→dispatcher→scrubber→cloud → audit-log → commit to RK3588 → wrist hardware last
- **NOT current scope** per CLAUDE.md

### Phase E — Compliance mode (deferred indefinitely)
- Tokenization, secret store, watchdog blindfold; "personal use doesn't need it"

### Mechanic v3 — autonomous correction (deferred indefinitely per design)
- Headless-mode relaunch after crash without intervention
- Defer until v1 + v2 give enough trust data

### Open spec questions (architectural; resolve as use cases land)
Sparklines vs phase dots · Concrete local model pick · Wired-pair construct UI · Plugin manifest format/sandboxing · Tripwire critical routing · Goal-edit diff classifier · Watchdog Q&A latency · Blacklist persistence scope · Tool registry v2 export/import · Active plugin discoverability · Connection monitor cadence · Recovery flow scope · Profile-addendum injection vs `--resume` · Thinking block content empty in Opus 4.7 stream-json

---

## EXPLICIT NON-GOALS (deferred indefinitely or actively rejected)

- Inter-agent chatter as load-bearing communication
- Real-time per-construct narration via small model
- Per-profile pool warming
- Built-in Claude Code tools surfaced as registry citizens
- Spawn-blocking on Online
- Merging daemon and watchdog
- Conflating netrunner and daemon
- Auto-install in `doctor.py` (DETECT + SUGGEST only)
- Confirmation dialogs (deliberate-consent instead)
- Putting a model in latency-critical path
- Confidence-gated autonomy
- Hot-reload of plugins
- Listing plugins in profiles
- Auto-discover scripts in `<home>/tools/<subdir>/`
- Merging tools and plugins into one type
- Putting plugins in `<home>/`
- Dynamic skill mounting
- Ethics layering / per-skill permission preambles
- Autonomous thrash bounds at the daemon level
- `--bare` flag on Claude Max OAuth subprocesses
- `--system-prompt` / `--append-system-prompt` for multi-line content on Windows

---

## Doc-to-plan-tree mapping (which docs are still load-bearing)

**Fully consumed into shipped state — clean archive candidates:**
- `cyberdeck-event-stream-design.md` (only Phase 8b cleanup item remains)
- `cyberdeck-tools-plugins-profiles-retool.md` (5/5 shipped; deferred-slices section is the only forward-looking content)

**Partially load-bearing (some shipped, some forward-looking):**
- `cyberdeck-spawn-context-isolation.md` (phase 1 shipped; phase 2 conditional)
- `cyberdeck-maintbot-design.md` (v0/v0.5/v1/v1.5 shipped; v2 = item 0h; v3 deferred)
- `cyberdeck-model-effort-design.md` (Phases 1-3, 5 shipped; Phase 4 blocked on quota)
- `cyberdeck-keymap-revision.md` (ON HOLD; Layer 1 done, Layers 2-3 blank)

**Fully forward-looking:**
- `cyberdeck-collections-intake-design.md`
- `cyberdeck-tools-default-kit.md` v2
- `cyberdeck_arbiter_design.md`

**Canon (always load-bearing):**
- `cyberdeck-claude-code-orientation.md` · `cyberdeck-state.md` · `cyberdeck-build-plan.md` · `cyberdeck-spec.md` · `cyberdeck-philosophy.md`

**Living inventory:** `cyberdeck-platform-portability.md`

**Project-instructions / collaboration:** `cyberdeck-project-instructions.md`

**Historical / case study (input → shipped output):**
- `cyberdeck-tripwire-case-spiralism.md` (informs tripwire authoring)
- `cyberdeck-tools-research-seed.md` + `cyberdeck-tools-research-report.md` (consumed into v2 default-kit doc)

---

## Cross-cuts and dependency edges

- **Item 13 (quota signal)** unblocks **Caliber Phase 4** AND naturally pairs with **Item 0f (adversarial dyad)** — Phase 4 needs both quota AND quality signals
- **Item 0g (iterative triage)** + **Item 0h (repair authority)** compose — repair could be triggered as a "third pass" when iterative triage detects config issues
- **Mechanic v1.5 prompt-thread state machine** is the reusable substrate for items 0g/0h
- **Universal list-names** + **Per-run workspaces** + **Morgue** all dovetail (folder = `run-{run_id}-{list_name}/`; morgue session record gains `cwd` field)
- **Routing (`r`)** + **Morgue** = complementary recovery paths (wiring pipes output; morgue resumes session_id)
- **Keymap revision** blocks any new global keybinds — including planning-mode `E`
- **Item 000 phase 2** simplified by phase-1 selective policy (Watchdog Q&A keeps CLAUDE.md, so role files only needed for KILLED roles where regression appears)
- **Prompt-shaping pass** has overlap with **Item 000 phase 2** (both touch system prompt composition); coordinate when both pick up
- **B2 fleet synthesizer** is substrate-blocked on D1; spine completion gives it a clean substrate when D1 lands
- **Architecture review (2026-06-01)** is a soft phase-checkpoint — agent defers if work is in flight
