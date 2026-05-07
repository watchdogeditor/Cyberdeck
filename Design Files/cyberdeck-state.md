# Cyberdeck — Current State

*Reference document. Filed gotchas (sacred), key design decisions,
shipped feature reference, tech debt, collaboration patterns. Companion
to `cyberdeck-build-plan.md` (forward-looking) and `cyberdeck-spec.md`
(architecture).*

*Last lean rebuild 2026-05-07. The previous running-journal version is
preserved at `archive/journal/cyberdeck-state-journal-pre-2026-05-07.md`
— read that when you need session-by-session history.*

---

## Status snapshot

The deck is real and load-bearing. ~22k LOC across 26 Python modules at
the deck-source root. Daemon orchestrates real Claude Code constructs.
Watchdog answers questions about fleet activity. Mechanic supervises
the deck process and triages unclean exits. Constructs talk back via
the dispatcher protocol. Every focusable surface in the right and
bottom panels does something.

**🆕 Design-files restructure landed 2026-05-07** (branch
`designdoc-rewrite`, commit `e324428`). The 19-doc accreted layout was
reorganized into a two-tier shape: lean current-state files at canon
root, full provenance preserved in `Design Files/archive/`. Active
slices live in `Design Files/in-flight/` with STATUS banners on each.
Headline numbers: state.md 3129 → 591 lines (-81%), build-plan.md 1538
→ 369 lines (-76%), CLAUDE.md fully rewritten as meta/orientation. All
provenance preserved — pre-restructure journals at
`archive/journal/cyberdeck-{state,build-plan}-journal-pre-2026-05-07.md`.
**This is fresh and untested in real workflow.** The next session
picking up work is the first real validation that the new arrangement
holds. If something feels missing, check the journal archives before
assuming it was deleted — the lean rebuild moved content, didn't drop it.
Working artifact `Design Files/PLAN-TREE-DRAFT.md` is scratch — delete
once the rebuild is validated.

**Branch state:** the merged work-in-progress branch
(`claude/objective-sammet-25e0b4`) was retired alongside the restructure
— its commits had landed in main; local + remote refs cleaned up. The
restructure itself lives on `designdoc-rewrite` (pushed to origin).
Deck is otherwise at a clean phase point — most recent feature slice
(mechanic v1+v1.5 cluster) fully real-deck verified. Merge to main is
the netrunner's call.

**Major in-flight initiatives:** see `cyberdeck-build-plan.md` →
CURRENT FRONTIER for ranked next-up candidates. As of last update:
items 0000 / 000-phase-2 / 0f / 0g / 0h queued; architecture review
fires 2026-06-01.

**Real-deck testing has been doing the heavy lifting** for many
sessions — the netrunner catches live bugs (watchdog stdin/argv,
file-panel double-listing, ctypes Windows-handle truncation, log-
selection race on quick restart, etc.) and we fix them. Most of these
would not have been caught by the test harness. See *Filed gotchas*
below for the cumulative record.

---

## What lives where

### Project files (the design canon)
- `cyberdeck-claude-code-orientation.md` — read-first onboarding
- `cyberdeck-state.md` — this file
- `cyberdeck-build-plan.md` — forward-looking plan tree
- `cyberdeck-spec.md` — base architectural spec
- `cyberdeck-philosophy.md` — the *why*
- `cyberdeck-platform-portability.md` — Windows-specific code inventory
- `cyberdeck-project-instructions.md` — collaboration norms
- `INDEX.md` — full file inventory across `Design Files/`

### In-flight design (`Design Files/in-flight/`)
- `cyberdeck-spawn-context-isolation.md` — per-role CLAUDE.md / auto-memory suppression (Phase 1 shipped; Phase 2 conditional)
- `cyberdeck-maintbot-design.md` — supervisor + LLM-session triage architecture (v0–v1.5 shipped; v2 active; v3 deferred)
- `cyberdeck-model-effort-design.md` — caliber selection (Phases 1-3, 5 shipped; Phase 4 quota-blocked)
- `cyberdeck-keymap-revision.md` — actions-first keymap pass (ON HOLD)
- `cyberdeck-collections-intake-design.md` — recipe-driven plugin scaffolding (filed; queued)
- `cyberdeck-tools-default-kit.md` — v2 opinionated default tools (design only)

### Archived design (`Design Files/archive/`)
- `archive/shipped/cyberdeck-event-stream-design.md` — the spine (8/8 phases shipped)
- `archive/shipped/cyberdeck-tools-plugins-profiles-retool.md` — three-way split (5/5 phases shipped)
- `archive/shipped/cyberdeck-tools-research-report.md` + `tools-research-seed.md` — input → consumed into v2 default kit
- `archive/case-studies/cyberdeck-tripwire-case-spiralism.md` — worked tripwire example
- `archive/deferred/cyberdeck_arbiter_design.md` — wearable form-factor variant
- `archive/journal/` — pre-2026-05-07 journal snapshots of state.md and build-plan.md

### Code modules (~22k LOC across 26 files)
| File | Purpose |
|---|---|
| `tui.py` | Textual UI, App, all modals, action dispatch (~8.2k LOC, the heart) |
| `watchdog.py` | Async question-queue oracle + streaming, blacklist, tripwire engine ownership |
| `daemon.py` | Persistent coordinator (one-shot + streaming) |
| `daemon_session.py` | Fleet ↔ daemon glue, goal+netrunner-msg |
| `fleet.py` | N concurrent constructs, event bus translator |
| `session_manager.py` | Pool + manifest |
| `construct.py` | Managed claude subprocess |
| `display.py` | Formatting (untruncated mode + origin badges) |
| `event_bus.py` | The spine — `DeckEvent` + `EventBus` + `Subscription` + `Severity` + `Kind` |
| `logger.py` | Per-launch NDJSON file logger; bus subscriber |
| `tripwires.py` | Deterministic matcher engine + DSL + LLM authoring |
| `attention.py` | Attention-area items (blacklist proposals, future kinds) |
| `brake_state.py` / `brake_hook.py` / `brake_delay.py` / `brake_patterns.py` | Brake state + PreToolUse hook + delay mechanism |
| `caliber.py` | Per-spawn model + effort + fast-mode primitive |
| `connection_monitor.py` | Heartbeat → Online/Degraded/Offline |
| `clipboard.py` | Cross-platform clipboard write (Win32 ctypes + pbcopy + xclip/wl-copy) |
| `profile_registry.py` / `profiles.py` | TOML loader + hot reload |
| `tools_registry.py` | tools.toml mtime-watch registry (P1 of retool) |
| `plugin_registry.py` / `plugins.py` | Plugin scan + manifest |
| `plugin_bridge.py` | Plugin dispatcher (P2 of retool) |
| `dispatcher.py` | Deck-control script (deck-side stdout protocol) |
| `advisor.py` | Per-tool Q&A bot (modal-scoped, sonnet+medium) |
| `mechanic.py` | Sibling-process supervisor (v0+v1+v1.5) |
| `mechanic_triage.py` | LLM-session triage (mechanic v1) |
| `doctor.py` | First-run prerequisite check |
| `preferences.py` | Typed accessor over state.json |
| `mock_*.py`, `main.py` | Test fixtures + smaller entry point |

---

## Shipped features (working in production)

For chronology, dates, commits, and real-deck verification notes by
session, see `cyberdeck-build-plan.md` → SHIPPED section. This section
is the rich-reference description of HOW each feature works.

### Tier 1 — original scope (long stable)
- Construct lifecycle (spawn / inject / kill / interrupt)
- EJECT (Ctrl+F → confirm modal)
- Limits modal (`l`)
- Session pool with cross-restart reuse (5h stale window)
- Activity chatlog (B1) — mechanical event extraction

### Tier 2 — Profiles
- TOML loader, ProfileRegistry, hot reload, default seeded
- Daemon picks profile per-spawn via JSON
- Profiles are **prescriptive templates**: instructions + recommended tool list. They do NOT enforce — the brake hook does.
- Post-retool: `tools` field (was `recommended_tools`) is registry-backed; resolves against `tools_registry`. Construct still has full default tool set.

### Plugin scaffolding — third leg of tool registry
- Plugin = capability bundle at `<deck-source>/plugins/<name>/` (post-retool; was `<home>/plugins/`) with `plugin.toml` (manifest), `README.md` (LLM-facing interface docs), and an executable entry point (`plugin.py`).
- Stateless v1: each invocation is a fresh subprocess that constructs spawn via the plugin bridge dispatcher (`<home>/tools/deck/plugin_bridge.py`).
- Manifest fields: `name`, `category`, `description`, `entry`, optional `[requires]` block (`platforms`, `python_imports`). Failing requires checks downgrade plugin to `available=False` with a reason.
- `PluginRegistry` mirrors `ProfileRegistry`'s read API but is one-shot (`scan()` at startup, no hot reload — Python module reloading is fraught).
- Tools panel renders all three kinds in a unified ListView with kind glyphs (⚙ binary / ⌬ script / ⊕ plugin).
- Daemon can pass per-spawn `plugins=[...]` field; per-spawn addendum scopes plugin awareness to only the picked subset.
- `load_into_deck(app)` hook: each available plugin's `plugin.py` is imported into the deck process at on_mount; if it defines a top-level callable `load_into_deck`, deck calls it once.
- First plugin: `screenshot` — mss-based cross-platform screen capture, ~140 LOC. Real-deck verified end-to-end.

### Brake state — deck-global
- Three levels: paranoid / default / yolo. Set via `b` modal (paranoid is single-press, yolo requires EJECT-style 3s held-key confirmation).
- Persists at `<home>/.cyberdeck/state.json` via `preferences.py`.
- Sidebar indicator: ▲ paranoid (yellow), = default (white), ▼ yolo (red).
- Enforcement via Claude Code's PreToolUse hooks under default + paranoid. Spawn gets a `--settings` JSON pointing at `brake_hook.py` with current brake passed via argv. Hook is self-contained ~180 LOC, exits 0 (allow) or 2 (deny). Stderr text becomes the model-visible denial reason.
- **YOLO never installs the hook** (post-2026-05-07 redesign). Live-fast-and-die — no enforcement, no delays, no overrides. The earlier "YOLO + delay > 0 installs the hook for a pause-before-allowing window" lane was retired alongside the X-unidirectional change. Tripwires under YOLO are observation-only (chatlog-render, no enforcement — the hook isn't running to read deny_pending).
- **Both Bash and PowerShell are gated.** A construct given Bash-denied will silently pivot to PowerShell — verified on real-deck. Both shells share `SHELL_TOOLS`; both go through destructive-pattern + protected-path checks under default brake; both are in `PARANOID_DENY_TOOLS`.
- Static patterns are short and opinionated: destructive bash regex, OS-root path prefixes, and three brake-config sentinel filenames (brake_hook.py, brake_state.py, brake_patterns.py). The deck-source-dir-as-substring check was tried and dropped — see *Filed gotchas → Brake / hook*.
- Mid-flight propagation deferred — brake state is captured at spawn and baked into that construct's lifetime. New spawns see the new value.
- Watchdog observes via `permission_denials` field on result events; chatlog renders `· brake blocked: Write×2, Bash×1` suffix on finalized lines.

### Brake-hook MCP gating (safety pass slice 1)
- Verb-based pattern matching: default brake denies destructive + unknown verbs, allows read-shaped (get/list/search/etc.). Paranoid denies ALL `mcp__*`. Closes the `execute_sql`-against-LOOM-production surface and similar.

### Variable-outcome delay UX (safety pass slice 3, redesigned 2026-05-07)
- `delay_window_seconds` (Limits modal, persisted) gates would-deny tool calls through a delay window before applying the deny.
- Post-2026-05-07 redesign: X is unidirectional (always = "allow this particular action to ignore the rules"). The earlier YOLO+delay→interrupt branch is retired; YOLO no longer installs the hook at all.
- **Tripwire-driven denies now ROUTE THROUGH the delay window** (vs. pre-redesign behavior where they bypassed it). Engine writes `<cid>.deny_pending.json` (with severity + bad_enough); hook reads it, treats as `would_deny=True`, opens the standard delay window with tripwire context embedded in `delay_pending.json`. The kill-on-critical consequence fires from the deck-side `_handle_delay_resolved` subscriber gated on `severity=critical AND applied_action=deny` — X-allow skips both the kill and the bad_enough blacklist proposal.
- Mechanism: hook writes `<cid>.delay_pending.json`, polls every 100ms for `<cid>.delay_override.json` until deadline. Deck-side `DelayMonitor` (50ms polling) publishes `brake.delay_opened` / `brake.delay_resolved` bus events. DelayEntry / DelayResolution carry tripwire context fields (`tripwire_name`, `tripwire_severity`, `tripwire_description`, `tripwire_suggestion`, `tripwire_excerpt`, `tripwire_bad_enough`) when the delay is tripwire-driven.
- UI: per-pane EJECT-style 20-cell countdown overlay; promote-to-top on delay open; magenta heavy border (`.-delaying` CSS).
- `X-ecute` keybind: action_x_focused dispatches to focused-pane delay → sole-pending delay → most-recent attention-area item. All surfaces unidirectional approve.

### Attention area (safety pass slice 3 phase 2)
- AttentionPanel widget at top of #main (heavy magenta border, hidden when empty).
- When `critical+bad_enough` tripwire fires, deck builds a BlacklistEntry and files as attention item with 30s window; X-press approves (adds to watchdog blacklist), expiry drops silently.
- Deck-owned timers (no hook polling — distinct from brake-hook delay flow).

### Right-panel listification + Phase A/B + 4-tab Tools UI (P5 of retool)
- Four tabs: Chatlog | Files | Profiles | Tools.
- Tools tab: single ListView with binaries → scripts → plugins (alphabetical within kind). Single header `TOOLS (N/total available)`.
- LaunchScreen modal (space on row → launch with TOOL: / PLUGIN: envelope; plugin path passes `spawn_plugins=[name]`).
- z-info: synthesized info modal for tools (manifest + availability); README.md view for plugins.
- h-Advisor (within info modal): narrowly-scoped per-tool Q&A bot; **sonnet+medium** (originally haiku+low; bumped 2026-05-05 after real-deck Q&A pass surfaced two failure modes — Haiku-low losing scope anchor and asking "which plugin?" when already told, plus pulling context from project-root CLAUDE.md auto-load. Sonnet+medium follows scope reliably; the netrunner sees the caliber in the modal subtitle so cost is explicit). Cyan accent; greeting echoes scope rule.
- Dispatcher protocol: `__CYBERDECK::v1::ACTION::PAYLOAD__` (one-way script → deck, versioned).
- `dispatcher.py` bootstrapped to `<home>/tools/deck/cyberdeck.py`; `plugin_bridge.py` bootstrapped alongside.
- Verified end-to-end on real Windows construct.

### Pane-log un-trim
- ConstructPane raw event buffer + `render_buffer(untruncated=True)`.
- Modal mode: 5000-char cap (vs 500 live), full thinking blocks.
- `display.py` formatters accept `untruncated` kwarg.

### Watchdog Q&A (`t`)
- Async question→answer oracle in `watchdog.py`.
- AskWatchdogScreen modal (yellow-themed, queue-depth hint).
- **Streaming default**: persistent `claude --input-format stream-json` subprocess; questions become JSONL writes; answers via stream-json.
- One-shot fallback (`streaming_mode=False`).
- **Wedge recovery**: timeout → kill subprocess → respawn fresh on next ask.
- Context: last 30 chatlog events, plain-text, no markup.
- Answers route to chatlog as `[watchdog] → ...` AND to dedicated Watchdog tab with paragraph fidelity.
- **Watchdog Q&A KEEPS CLAUDE.md auto-load** (per item-000 phase-1 selective policy) — deck "security analyst" benefits from gotchas + design context.

### Daemon chat (`T`)
- TalkDaemonScreen modal (primary-themed; soft/loud counterpart to `t`).
- `set_pending_netrunner_message` on DaemonSession (FIFO stack).
- `_format_outcomes` prepends `≫ NETRUNNER MESSAGE:` preamble with numbered list when stacked.
- Empty-outcomes-only branch produces clean message.
- No-session warning + drop with toast.

### Goal-edit mid-flight (`e`)
- Mid-flight block lifted; opens modal pre-filled with current goal.
- `_classify_goal_diff` heuristic: tokenize+stem+Jaccard → `clarification` / `scope-change` / `pivot`.
- `set_pending_goal_update` on DaemonSession; outcome-loop wakes idle.
- `_format_outcomes` prepends GOAL UPDATE preamble with classification-tailored advice.
- Identical-goal submit no-ops with toast. Force-push deferred.

### Connection state monitor
- `connection_monitor.py`: heartbeat to api.anthropic.com:443.
- States: Online (●green) / Degraded (◐yellow) / Offline (●red).
- Sidebar indicator + chatlog announcements on transition.
- DNS failure skips Degraded → Offline directly.
- Threshold-based: 2 failures → Degraded, 1 success → Online.
- Spec'd consequences NOT YET WIRED: spawn-blocking, daemon parking, recovery flow (round 2 in build plan).

### Streaming defaults
- Daemon `streaming_mode=True` (was opt-in; user observed "nuclear speed improvement"). `--no-streaming` opts out.
- Watchdog `streaming_mode=True`. Persistent subprocess shared across questions; conversation accumulates so watchdog "remembers" earlier questions in session.

### Tabbed bottom panel
- `[Daemon] [Watchdog]` tabs in `TabbedContent(id="daemon_bar")`.
- WatchdogPane mirror of DaemonPane (yellow, status with queue-depth).
- Both inner logs focusable; space → action_talk_daemon / action_talk_watchdog.

### Watchdog log (persistent Q&A history — v1)
- `WatchdogHistory` + `WatchdogHistoryEntry` in `watchdog.py`. Append-only JSONL at `<home>/.cyberdeck/watchdog.jsonl`.
- Each resolved question persisted by `_safe_callback` BEFORE the listener fires (so the entry survives a listener crash).
- TUI replays the last 50 entries on `on_mount` via `_replay_watchdog_history`, with separator markers.
- Per-entry `kind` field (currently always "qa") futureproofs the file. Tripwire/blacklist record kinds + dedicated history tab still deferred.

### Watchdog Tripwires (slices 1+2)
- New `tripwires.py` module — `Tripwire` dataclass, `TripwireEngine`, text-extraction helpers, `DEFAULT_TRIPWIRES`. Spec model "LLM authors, deterministic enforces."
- **Small DSL** per netrunner direction: `pattern_type` (today only "regex"), `pattern`, `event_kinds` (which EventKinds this matcher applies to), `field` (which extracted text — `tool_use_command`, `tool_result_content`, `thinking_text`, `assistant_text`, `tool_use_input`, `user_text`, or `any`).
- **Severity tiers** declared (low / warning / critical) with escalation chain (slice 2):
  - low → log only
  - warning → brake denies next call + suggestion
  - critical → deny + auto-term
  - critical+bad_enough → same + blacklist proposal (deferred application — slice 3 phase 2)
- **Scope** field gates per-construct or deck-global.
- **Origin** field tracks where it came from: `default`, `manual`, `llm_authored` (slice 2), `blacklist_derived` (deferred).
- Engine ownership: lives on the Watchdog. Default tripwires installed automatically. TUI registers a Fleet listener (`_scan_for_tripwires`) feeding events into engine.
- **Default tripwires shipped:** `keyword_credentials` (low), `keyword_destructive_sql` (warning), `host_restart_command` (warning), `user_email_protection` (warning — mitigation for upstream userEmail leak).
- **LLM authoring (slice 2)**: `Watchdog.author_tripwires` runs one authoring pass via fresh `claude -p` one-shot. Two-rung: rung 1 forks the watchdog's streaming Q&A session via `--resume <id>`; rung 2 fresh fallback when no session_id captured. Trigger sites: goal-start (`_start_daemon_task`), goal-update (`_handle_goal_submitted` mid-flight, gated on classification != "clarification").
- **Lifecycle: clear, then register.** Each pass calls `engine.clear_by_origin(Origin.LLM_AUTHORED)` BEFORE registering new entries. Defaults / manual / blacklist-derived stay untouched.

### Tripwires redesign — brake/tripwire unification (2026-05-07)

Major reshape of the tripwire enforcement model in response to a real-deck operational pain point: authoring was so aggressive that most tasks failed (see Filed gotchas → Brake / hook for the full root-cause analysis of the system-prompt contradiction). The redesign lands these changes as one slice:

- **X is now unidirectional** (key design decision #24 revised). Always means "allow this particular action to ignore the rules." Applies symmetrically to brake denies, tripwire fires (warning + critical), and attention-area items.
- **YOLO never installs the hook** — `brake_state.make_spawn_settings` returns `None` (or fast-mode-only settings) under YOLO regardless of `delay_window_seconds`. The slice-3 "YOLO+delay installs the hook for a pause-before-allowing window" lane is retired. `brake_hook.py:should_delay` no longer has a YOLO branch.
- **Tripwire fires route through the same delay window as brake denies.** Engine writes `<cid>.deny_pending.json` on warning + critical (now also includes `bad_enough` field); hook reads it, treats as `would_deny=True` with the tripwire's reason as the deny stderr, opens the standard `<cid>.delay_pending.json` window (with embedded tripwire context fields). DelayMonitor + per-pane overlay surface the X-window as usual. Pre-redesign: tripwire denies BYPASSED the delay (immediate `return 2`) — the redesign reverses that.
- **Critical-kill consequences moved deck-side, gated on delay outcome.** `_handle_tripwire_fire` no longer fires the kill or the bad_enough → blacklist proposal immediately. Both effects fire from `_handle_delay_resolved` only when `severity == "critical" AND applied_action == "deny"` — i.e., the tripwire fired AND the netrunner did NOT X-allow. X-allow on a critical fire skips both the kill and the blacklist proposal (netrunner's override is final across all of the tripwire's consequences).
- **Authoring-prompt rewrite.** Drop the "DO author critical-severity shell-destructive baselines" depth-of-defense imperative (the contradiction at the root of the overauthoring problem). Add a clear DIVISION OF LABOR statement: brake handles OS-integrity baselines, tripwires handle goal-specific drift, no overlap. Reframe the role from "what regex patterns should we watch for?" to **"how do I break this?"** — adversarial red-team framing. Tighten cardinality from "0-8 normal" to "0-5 normal." Preserve the GOTCHAS section (field-selector intent-mapping, don't-predicate-on-telegraphed-intent, research-goal framing — the addendum that landed earlier the same day, now subsumed into the larger rewrite).
- **DelayEntry / DelayResolution** carry tripwire context fields (`tripwire_name`, `tripwire_severity`, `tripwire_description`, `tripwire_suggestion`, `tripwire_excerpt`, `tripwire_bad_enough`). Empty strings / False when the delay is purely brake-driven. The kill-on-critical subscriber reads `tripwire_severity` off the resolution payload; the blacklist-proposal opening reads `tripwire_bad_enough`.

Files touched: `brake_state.py`, `brake_hook.py`, `brake_delay.py`, `tripwires.py`, `tui.py`. Compile-clean; pending real-deck verification.

### Watchdog Blacklist
- `Blacklist` + `BlacklistEntry` in `watchdog.py`. Session-scoped, in-memory.
- Fingerprint = first 80 chars lowercased of the killed task's text. Matches the daemon-session respawn-detector scheme.
- Entry carries rich context (fingerprint, full_task, source construct id/state/final_output/files_written, reason, timestamp).
- `Shift+K` registers the focused construct's fingerprint with the blacklist before killing — replaces the prior "blacklist not yet implemented; soft-killing" toast.
- DaemonSession `_execute_action` checks each spawn against the blacklist; matches refused with feedback in the next outcome turn. Spawn NOT counted against caps when refused.
- `_format_outcomes` surfaces active blacklist on every outcome turn as a `⛔ SESSION BLACKLIST` block.
- In-flight matching constructs get a red `.-blacklisted` border (mirrors `.-blocked` in shape; red vs yellow). Per netrunner direction: flag, do NOT auto-kill.

### Spawn provenance (origin badges)
- `fleet.spawn(..., origin=...)` — `daemon` / `netrunner` / `inject`.
- Threaded into `spawned` meta event payload.
- Chatlog renders cyan `[you]` for netrunner, `[↳you]` for inject; daemon spawns un-badged.

### z-to-view (file/profile/script/tool/plugin)
- `action_expand` opens ExpandModal with content from disk.
- Pygments syntax highlighting via `rich.syntax.Syntax` for ~30 languages.
- Theme: `github-dark`. Line numbers off.
- Detection cascade: extension → bare-name → shebang.
- Plain-text fallback with bracket escape for unknown extensions.
- 2MB size cap; UTF-8 with replacement.
- Modal scroll bindings: w/s line, PgUp/PgDn page, Home/End jump.

### Path-normalized Files panel dedupe
- Bug: Windows backslash vs forward slash + dispatcher `Path(p).resolve()` produced literal-distinct strings → double-listing.
- Fix: `os.path.normcase(os.path.normpath(p))` as dedupe key.

### Focus navigation fall-through
- W/S walks within section; at section edge, falls through to up/down neighbor section.
- Empty sections skipped transitively. Trap fix: when chain dead-ends through empty sections, fallback lands on any populated non-source section.

### UI infrastructure
- ExpandModal universal magnifier (`z`) — RichLogs, ConstructPanes, list items.
- Rich text preservation via Text/segment round-trip.
- Modal Tab fix: App-level Tab delegates to `screen.focus_next`.
- y/Y copy keybind (`clipboard.py` cross-platform, ctypes Win32 + pbcopy + xclip/wl-copy).
- Quit unified to `Ctrl+Q` with running-state guard (idle: clean exit; running: toast + block).
- `sys.unraisablehook` filter for Windows Proactor closed-pipe noise.

### Unified event stream (the spine)
- `event_bus.py` (DeckEvent + EventBus + Subscription + Severity + Kind constants). Single canonical fan-out point.
- Producers wire via constructor `bus=`: Fleet, DaemonSession, Watchdog, Blacklist, TripwireEngine, BrakeStateStore, ConnectionMonitor, ProfileRegistry, PluginRegistry, etc.
- Per-source translators (`_fleet_event_to_deck_event`, `_daemon_event_to_deck_event`).
- File logger (`logger.py`) is a bus subscriber; per-launch NDJSON + `latest.log` pointer.
- Phase 8b cleanup deferred: SessionPool / Daemon callback patterns (integration interfaces; documented).

### Mechanic (separate-process supervisor + on-demand LLM session)
- v0: sibling Python process tails NDJSON for live claude pids, kills on detected deck death. Cross-platform stdlib + ctypes.
- v0→v1 bridge (item 0d): liveness heartbeat at `<home>/.cyberdeck/heartbeat` every 5s; mechanic logs STALE after 20s.
- v1: diagnose-only LLM-session triage on unclean exit. `mechanic_triage.py` (~480 LOC); Family A clean-spawn recipe; sonnet/medium caliber; structured Markdown to `<log>-triage.md`.
- v1.5: stale-heartbeat triage with interactive prompt + listens-for-recovery; `--auto-triage-on-stale` for headless. Live narration via `stream-json --verbose`. Partial-recovery on timeout; default 300s.
- **v1.6 (iterative triage, item 0g, 2026-05-07)**: multi-pass deepening on top of v1's single-pass shape. After pass 1 writes its report, mechanic prompts on stderr "Keep delving? [y/N]"; on yes, fires a deepening pass via `claude -p --resume <session_id>` so the model continues with full prior-pass context (log content, gotchas cross-references, prior cause-ranking) instead of re-reading from scratch. Each deepening pass appends a new `## Deeper analysis (pass N)` section to the same report file. Stops on N / max-passes (default 4 via `--max-triage-passes`) / fail / non-TTY stdin. New symbols: `TriageResult.session_id` field, `run_triage` kwargs `resume_session_id` + `user_prompt_override`, `prompt_keep_delving()` helper, `run_iterative_triage()` orchestrator, `_build_deepen_directive(pass_num)` for the user-message body, `_append_pass_to_report()` for the file-side write. Mechanic CLI gains `--no-iterative` (collapse to single-pass) and `--max-triage-passes` (cap deepening loop). Default flow: iterative on, 4-pass cap; non-TTY auto-skips the prompt so headless / wall-mount deployments get single-pass without hanging on input().

### Caliber (per-spawn model + effort + fast-mode)
- `caliber.py` (~250 LOC): `Caliber` frozen dataclass; KNOWN_MODELS + KNOWN_EFFORTS soft-validation; `to_claude_args()`; merge() for override hierarchy.
- Plumbed through Construct / Fleet.spawn / DaemonSession with `default_caliber` on App.
- DAEMON_SYSTEM_PROMPT has CALIBER SELECTION section documenting optional `model` / `effort` / `fast_mode` action fields.
- SessionPool `warm_caliber` + `pull(requested_caliber=...)` match-or-skip gate. Mismatches publish `pool.caliber_mismatch` for observability.
- `make_spawn_settings(fast_mode=...)`: shared cache-stable path for fast_mode=False; per-spawn override file for True.
- Daemon caliber: model PINNED to opus, effort = netrunner's power-level knob via `EffortPickerScreen` modal (1-5 buttons). Persisted in state.json.
- UI surfaces: sidebar `daemon: opus·high` (· fast when governor on); per-pane caliber suffix in headers; watchdog Q&A CALIBER AWARENESS section.
- Phase 4 (quota-aware fallback) blocked on item 13.

### Public-repo readiness
- `doctor.py` (~700 LOC post-2026-05-07 evening): three-tier prereq policy.
  - **Tier 1 — Python ≥3.11.** Hard FAIL, no prompt. Can't auto-install Python from inside Python; netrunner fixes manually. (Most failures here would prevent the script from importing in the first place; the check is a belt-and-suspenders catch for 3.10 environments where parsing succeeded but runtime would diverge.)
  - **Tier 2 — textual + claude binary.** DETECT + PROMPT-TO-INSTALL-OR-ABORT. Both are objectively required to launch (textual = TUI render; claude = AI agent backend for daemon / watchdog / advisor / constructs). Missing → prompt "install via pip/npm? [y/N]". Yes → run installer (`<python> -m pip install textual` or `npm install -g @anthropic-ai/claude-code`) + re-check; if still missing, exit 1 with hint. No / non-TTY / install failure → exit 1 cleanly with manual-install hint. NO download attempted without explicit y/yes.
  - **Tier 2.5 — `claude --version` healthy.** Hard FAIL, no prompt. Binary present but exits non-zero on `--version` means corrupt install or broken OAuth login; reinstalling usually doesn't help. Exit 1 with diagnostic hint (run `claude --version` manually; reinstall via npm; refresh login by running `claude` in a terminal).
  - **Tier 3 — plugin deps.** DETECT + OPTIONAL-PROMPT-TO-INSTALL-OR-SKIP. Missing deps **NEVER block launch**; affected plugins surface in the Tools panel as `unavailable: missing python imports: <pkg>` (same way binary tools that aren't on PATH show up). TTY stdin → optional prompt; yes installs via `<python> -m pip install`, no skips. Non-TTY / Ctrl+D / install failure / still-missing-after-install → skip cleanly, deck launches with affected plugins unavailable.
  - **Policy progression** (chronology preserved for context): pre-2026-05-07 was WARN-and-continue for plugins + hard FAIL with hints for hard prereqs (no prompts anywhere). 2026-05-07 morning split plugin deps into PROMPT-OR-ABORT (declining blocked launch). 2026-05-07 afternoon flipped plugin deps to PROMPT-OR-SKIP (graceful degradation). 2026-05-07 evening (current) added PROMPT-OR-ABORT for textual + claude binary so the netrunner has an in-band install opportunity for the things the deck objectively needs — Python stays as no-prompt because you can't auto-install Python from inside Python; `claude --version` stays as no-prompt because reinstalling a corrupt binary or broken login won't help.
  - Orchestration lives in `run_doctor_or_exit()`; tui.py's `__main__` calls it once. Sentinel at `<home>/.cyberdeck/first_run_complete`. `--doctor` (observational, no install prompts) / `--no-doctor` (escape hatch) flags. ASCII-only output (Windows cp1252 stdout encoding).
- `preferences.py` (~210 LOC): typed accessors over state.json (`prefs.fast_mode`, `prefs.daemon_effort`, `prefs.brake`, etc.). Schema documented in module docstring with future placeholder fields.
- README restructure: public-repo cold-reader rewrite; pitch + status callout above the fold; expanded prerequisites; design-canon section.

### Auto-context discovery + per-role isolation phase 1
- Discovery (2026-05-05): every claude subprocess silently auto-loads `<cwd>/CLAUDE.md` + walks parent dir tree concatenating ALL CLAUDE.mds + `~/.claude/CLAUDE.md` + auto-memory + rules dirs. Verified verbatim against Anthropic docs.
- Phase 1 (~80 LOC): per-role env-var belt (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` + auto-memory + git-instructions).
  - **KILLED** for Advisor, Construct, Daemon (both backends), Pool warmer, Tripwire-authoring Watchdog
  - **KEPT** for Watchdog Q&A
- Multi-line argv truncation fix: switched all problematic spawn sites to `--system-prompt-file` / `--append-system-prompt-file`. Promoted to top-level Hard Rule.
- `user_email_protection` default tripwire: mitigation for `anthropics/claude-code#55743`. Reads OAuth email from `~/.claude.json` at startup; warning severity; brake denies with redirect.

---

## Key design decisions (carried forward)

1. **Brake state is deck-global, not profile-attached.** The netrunner sets it via `b`; it applies to every new spawn until changed. Watchdog can ratchet up (toward paranoid) but not down — that's the netrunner's exclusive prerogative.
2. **Profiles are prescriptive, not restrictive.** They steer with addendums and suggest tools via `tools` field; they do NOT gate capability. Runtime gating is the brake hook's job, deck-wide.
3. **Brake enforcement is via PreToolUse hook, not `--allowedTools`.** The hook is deterministic (regex/path matching, no LLM in the hot path). Watchdog observes denials and authors the hook's policy over time (LLM authors, deterministic enforces).
4. **Default profile auto-seeded; netrunner edits sacred.**
5. **Lowercase = within-focus, uppercase = move-focus.** `z` for zoom.
6. **`space` is "primary interact"; `z` is magnify.**
7. **Truncation: 500 live, 5000 modal.** Bounded against megabytes.
8. **Pool always warms with `default`.** No per-profile pools. Pool warms with deck's `default_caliber` (today sonnet+high).
9. **Files panel: dual path with dedupe (normalized).**
10. **Marker protocol one-way (script → deck).** Versioned. Unknown action logs warning; never crashes.
11. **Tools panel:** unified Tools ListView (binary / script / plugin). Built-ins not surfaced as registry citizens.
12. **Goal-update propagation deferred to next break.** Force-push is M5+. Wake-event keeps idle sessions responsive.
13. **Goal-diff classifier is heuristic, not model-driven.** Cheap; "good enough"; can model-ify later.
14. **ConnectionMonitor presumes ONLINE at start.**
15. **DNS failure skips Degraded.** Clean signal: no network at all.
16. **Watchdog runs cloud Claude today.** Local-model substrate (D1) is the eventual home.
17. **Streaming is the default; one-shot is the fallback.** For both daemon and watchdog.
18. **Streaming wedge → kill, don't preserve-and-pray.** Once stuck, fresh subprocess is the only reliable recovery.
19. **Origin attribution at source, not reverse-engineered.** Fleet payload carries who spawned each construct.
20. **z-modal:** bracket escape on plain text, syntax highlighting on known languages, github-dark theme, no line numbers.
21. **Tripwire authoring forks the watchdog's Q&A session via `--resume <id>` rather than running on a fresh isolated subprocess.** The authoring model gets the same situational awareness the Q&A side has accumulated. Falls back to a fresh one-shot when no session_id is captured.
22. **LLM_AUTHORED tripwires use clear-and-replace, not accumulate-and-update.** Each authoring pass drops prior LLM-authored entries before registering new ones; defaults / manual / blacklist-derived entries stay untouched. Even authoring failures clear the prior set.
23. **Authoring skips on clarification-class goal updates.** The `_classify_goal_diff` heuristic gates re-authoring: pivots and scope-changes get a new pass; pure clarifications skip.
24. **X = approval / execute (deck-wide), UNIDIRECTIONAL.** Established 2026-05-01 with safety-pass slice 3 as bidirectional; revised 2026-05-07 (tripwires redesign) to be unidirectional — X always means "allow this particular action to ignore the rules." The netrunner's judgment is final. The earlier YOLO+delay→interrupt branch was retired alongside the redesign: YOLO no longer installs the hook at all (live-fast-and-die, no brakes), so the deny-default is the only path that reaches the delay window. Both lowercase x and Shift+X bind to the same `action_x_focused` (this isn't a soft/loud pair; it's deliberate-execute either way). Applies symmetrically to brake-level denies, tripwire fires (warning + critical), and attention-area proposals (blacklist).
25. **Caliber: model is the substrate, effort is the power-level knob.** Daemon model is PINNED to opus (it's making management decisions). Daemon effort is the netrunner's tunable knob via Limits modal. Construct caliber (model + effort + fast_mode) is daemon-controlled per-spawn; netrunner overrides via Limits modal or daemon chat.
26. **Per-role CLAUDE.md auto-load policy is selective, not blanket.** Watchdog Q&A KEEPS auto-load (deck "security analyst" benefits from gotchas + design context). Advisor / Construct / Daemon / Pool warmer / Tripwire-authoring Watchdog KILL auto-load (their context should be explicit, not implicit).
27. **Brake handles OS integrity; tripwires handle goal-specific drift. No overlap.** Established 2026-05-07 with the tripwires redesign. The brake hook is the always-on, deck-global, brake-tier-determined defense for catastrophic shapes (rm -rf system roots, OS-root writes, fork bombs, host shutdown, MCP destructive verbs, deck-source modification). Tripwires are LLM-authored, goal-specific drift indicators that fire when a construct's interpretation of THIS goal goes off-rails (writing files outside the goal's scope, deleting tests during a refactor, modifying production config during a build, adopting adversarial framing during research). Authoring instructions explicitly forbid duplicating brake coverage — every duplicate is noise that trains the netrunner to ignore the chatlog. Under YOLO brake the hook is intentionally absent (live-fast-and-die); tripwires under YOLO become observation-only (chatlog-render, no enforcement). Both layers route their X-override through the same delay-window infrastructure (the 5s delay UX).
28. **YOLO is no-brakes-no-hooks-no-overrides.** Established 2026-05-07. YOLO is the live-fast-and-die brake — the netrunner has explicitly accepted unrestricted operation. The hook is NOT installed under YOLO (revised from the slice-3 "YOLO+delay installs the hook for a pause-before-allowing window" behavior, which forced X to be bidirectional). Tripwire fires under YOLO surface in the chatlog but cannot block, deny, or auto-terminate — there's no hook to enforce. If the netrunner wants enforcement, they switch brake.

---

## Filed gotchas (institutional memory; cumulative)

> **This section is sacred.** Every entry was filed because we hit the
> bug, diagnosed it, and want never to re-introduce it. Read this when
> touching subprocess lifecycle, modal screens, async cleanup, or
> Windows-specific code paths. Don't trim entries — even old ones
> protect against pattern recurrence.

### Terminal / Textual
- **Don't shadow Textual `Widget._render()`.** It's a real method on the base class that returns a `Visual`. Overriding it with a custom render method returns `None` (or whatever your method returns) and crashes Textual's render pipeline with `AttributeError: 'NoneType' object has no attribute 'render_strips'` in `widget.py:_render_content` → `Visual.to_strips`. Real-deck caught 2026-05-01 on the first slice 3 phase 1 attempt: `DelayListItem._render` shadowed the parent. Crash on first paint of the Delays tab. Fix: rename your custom render method to anything else (`_paint`, `_redraw`, `_update_text`). General rule: any underscore-prefixed method on a Widget subclass should be checked against Textual's API before being added — Textual treats underscore-prefix names as protected, not private.
- **A widget render-crash can leave the tree in a state that silently breaks unrelated mutations.** Real-deck observed 2026-05-01 (post-fix of the `_render` shadowing above): after the crashed deck was restarted, finalized construct panes stopped moving to the bottom of `#main` even though `_compact_pane_after_delay` was running and `pane.compact` was being set. Restarting the deck a second time cleared it ("heisenbug"). Hypothesis: the prior session's render crash corrupted some Textual-side widget bookkeeping that survived into the next launch via `cyberdeck-home/` state or process-group quirks; or asyncio worker scheduling got starved by a backlog of crashed widgets. Mitigation pattern: when a Textual widget crashes during render, restart the deck before trusting any subsequent UI behavior. Diagnostic pattern: surface widget-mutation calls via `fleet_log.write` AND a bus event so the file logger captures both "scheduled" and "fired" lifecycle markers.
- **`shift+space`, `ctrl+space`, `ctrl+i`, `ctrl+m`** rarely transmit distinctly in real terminals. Trust pilot for binding wiring; trust real terminal for capability.
- **Textual `Widget.name` is read-only.** Don't shadow.
- **`Log.lines` is `list[str]`; `RichLog.lines` is `list[Strip]`.**
- **Markup leaks via `\n`.** Collapse before writing.
- **`wrap=True` + `min_width=1` inside an inactive TabPane** pre-wraps content at 1 char per line and caches Strips. Use `wrap=False` for logs in non-default tabs OR buffer-and-replay on activation.
- **`can_focus=False` on a Static-derived class is a no-op** because Static defaults that way.
- **Modal screens don't inherit App BINDINGS.** Redeclare on the modal.
- **App-level priority bindings** beat modal priority. Delegate from App action when `isinstance(self.screen, ModalScreen)`.
- **ListView focus model:** the focused widget IS the ListView; `.highlighted_child` is cursor.
- **Two TabbedContents need `id` to disambiguate `query_one`.**
- **`priority=True` bindings DON'T reliably fire when focus is on an `Input` with `type="integer"`** (or other typed Inputs that filter keystrokes). Filed 2026-05-07 from real-deck observation: LimitsScreen had `Binding("e", "open_effort_picker", priority=True)` and `Binding("f", "toggle_fast_mode", priority=True)`, both with comments explicitly stating priority=True was intended to fire even with Input focus. In practice, the Input's `type="integer"` filter intercepts letter keys at a level the screen-priority chain doesn't reach, so E and F did nothing when the modal opened (initial focus was on the first Input). The netrunner could Tab away from all Inputs to make E/F work, but that defeated the keyboard ergonomics. **Fix pattern**: don't auto-focus a typed Input as the modal's initial focal point. Add a focusable header (Label with `can_focus=True` set in `on_mount`) as a "rest position" — Static-derived widgets don't filter letter keys, so screen-priority bindings fire immediately. Tab from the header cycles into the Inputs; Shift+Tab wraps back. Add a `#widget_id:focus` CSS rule for visual indication (Static has no default focus border). LimitsScreen `on_mount` is the canonical implementation.

### Rich markup
- **Markup escape:** `\[` for opening bracket, closing `]` is literal. Use raw f-strings (`rf"..."`) to silence Python escape warnings.
- **File contents need bracket escape** before going to a markup-enabled RichLog. `[default]` TOML headers will be parsed otherwise.
- **`rich.syntax.Syntax` returns a single Renderable** but RichLog splits it into Strips, so scrolling stays line-by-line.

### Async / subprocess
- **mechanic.py log-file-selection race on quick deck restart.** Filed 2026-05-06 during Mechanic v1.5 real-deck testing. Symptom: netrunner kills the deck and relaunches within ~5-10 seconds; new mechanic immediately reports "deck pid=XXXXX died" and fires triage on a fresh, alive deck. Doesn't reproduce when waiting longer between restarts.

  Root cause: `mechanic.find_log_file` returns the most-recently-modified `cyberdeck-*.log` within the 5-minute freshness window (`_LOG_FRESHNESS_SECONDS = 300`). On a quick restart:

    - T=0: deck1 dies; its log mtime frozen
    - T=N: netrunner relaunches; launch.bat fires deck2
    - T=N+0.1: deck2 starts python (cold start ~1-3s on Win)
    - T=N+1.1: launch.bat fires mechanic (after 1s sleep)
    - T=N+1.2: mechanic.find_log_file polls — deck2's log file may NOT EXIST YET (deck still in Python startup). Only deck1's log is in the freshness window. mechanic attaches to deck1's log, reads deck1's pid from header → pid is dead → fires triage on a stale log even though deck2 is healthy and running.

  Why "wait 10s" works: deck2 has time to write its log header before mechanic polls, so deck2's log mtime is newer than deck1's, and the newest-mtime selection picks the right file.

  Fix: validate header pid liveness in `wait_for_log_file`. New `_peek_log_header_pid(log_path)` reads only the first line of the candidate log, parses as NDJSON, returns the `log_header.pid` field (or None on parse error / wrong type / missing field). `wait_for_log_file` calls this and ALSO calls `pid_alive(pid)`; only commits to a log file whose deck pid is currently alive. Stale logs (pid dead) get silently skipped each poll.

  **Lesson generalizes:** any "newest by mtime" selection over files that get reused across launches needs a freshness + liveness check. mtime alone isn't enough when files from multiple launches sit in the same directory within the freshness window.

- **🚨 ctypes Windows-handle truncation: ALWAYS set argtypes / restype on kernel32 / user32 / advapi32 calls.** Filed 2026-05-06 during Mechanic v1.5 real-deck testing. Symptom: mechanic.py reported the deck dead immediately after launch ("[mechanic] deck pid=XXXXX died ... cleaning up 0 tracked subprocess(es); firing v1 triage") even though the deck was fully alive and writing log records. Triage fired unnecessarily, burning ~2 minutes + tokens per false-positive launch.

  Mechanism: ctypes `kernel32.OpenProcess(...)` without explicit argtypes/restype defaults to `c_int` (32-bit signed) for the HANDLE return value. On 64-bit Windows, HANDLE is pointer-sized (8 bytes); the default truncates to 32 bits. Sometimes the truncated handle is still non-zero (looks valid to `if not h`) but is corrupt — `GetExitCodeProcess(h, ...)` fails (returns 0) → `_pid_alive_win` returns False → mechanic concludes the deck is dead.

  Intermittent in practice: depends on whether the OS happened to return a HANDLE with non-zero high bits (which is non-deterministic between launches and varies by Windows version / Python ctypes version). Bug latent for the entire mechanic v0 history; surfaced reliably enough on Python 3.14.3 + Windows 11 to be diagnosed.

  Fix: declare full argtypes + restype using `ctypes.wintypes`:
    OpenProcess.argtypes = [DWORD, BOOL, DWORD]; restype = HANDLE
    GetExitCodeProcess.argtypes = [HANDLE, POINTER(DWORD)]; restype = BOOL
    CloseHandle.argtypes = [HANDLE]; restype = BOOL

  Generalizes beyond mechanic: any future ctypes call into kernel32 / user32 / advapi32 / etc. MUST set argtypes + restype before calling. Skipping is a Windows-64-bit landmine. Test shape: spawn a known-alive subprocess, call your function on its pid, assert True; kill it, call again, assert False. Skipping the test = waiting for the next intermittent reproduction in production.

- **🚨 Daemon streaming subprocess silent wedge after `_drain_streaming_turn` TimeoutError.** Filed 2026-05-07 from real-deck observation (logs/cyberdeck-2026-05-07-172355.log + ejected-run-7296e01f.json). Symptom: after a single timeout fires in a streaming daemon turn, the daemon silently stops posting responses to the chatlog. Sending a netrunner message via `T` (or any action that pokes the daemon's outcome loop) causes the previously-queued response to appear instantaneously alongside the new turn's output. UI status sticks at "working" even though nothing visible is happening — netrunner can't tell whether the daemon is wedged or just slow. **Token cost is real** because the previously-queued response was a real model invocation that already burned its turn cost; the silence just hid it from the netrunner.

  Two-part diagnosis: (1) the streaming subprocess's stdout pipe held buffered events that didn't flush until something triggered a write to its stdin (Windows ProactorEventLoop pipe buffering quirk on long-running asyncio subprocesses); (2) the deck's outcome loop relied entirely on edge-triggered `_outcome_event.set()` calls — if a wake event was missed (not fired on a finalize, lost in scheduling, etc.) the loop sat indefinitely with outcomes accumulating in `_pending_outcomes` but never being drained.

  Fix (two-prong, mirrors the watchdog wedge-recovery pattern):
    - **daemon.py**: on `_drain_streaming_turn` TimeoutError, call `_shutdown_streaming()` and null `_streaming_proc` so the next turn re-spawns fresh. Pre-fix the timeout just emitted an error event and returned, leaving the wedged subprocess alive; subsequent turns then wrote to its dead stdin and got silently buffered output. Filed-gotcha precedent: "Preserve the wedged proc, hope it recovers — always wrong when the failure mode is read-hang. Once stuck, kill and respawn."
    - **daemon_session.py**: new `_periodic_wake_loop` background task fires every `periodic_wake_seconds` (default 60s) and sets `_outcome_event`. Outcome loop unblocks, checks if there's actual work (outcomes / goal_update / netrunner_messages / respawn_warnings), skips the daemon turn if all empty (diagnostic-only wake — no token cost). Defends against missed-edge wake bugs and gives the UI a deterministic re-evaluation cadence even during deep silence.

  **Lesson:** any persistent-subprocess-with-async-stream architecture needs (a) timeout-fires-kill, not timeout-fires-warn; (b) a periodic re-evaluation cadence on the consumer side, not pure edge-triggered wake. The watchdog had this right since slice 2; the daemon was the asymmetry.

- **🚨 Mechanic must observe BOTH the clean shutdown signal AND the deck PID actually exiting before declaring "clean."** Filed 2026-05-07 from real-deck observation. Symptom: netrunner hits Ctrl+Q or EJECT, deck writes a clean `log_footer` (reason="shutdown" or "eject"), the deck's Python process hangs indefinitely in asyncio teardown, mechanic sees the footer + skips triage, leaving a hung process and (potentially) orphan claude subprocesses alive. Common Windows trigger: ProactorEventLoop subprocess teardown blocking on a wedged pipe; can also fire when EJECT's parallel kill_construct hits a construct that's mid-tool-call and the kill races the finalize.

  Pre-fix `mechanic.py` main loop trusted the footer alone — `clean_close_reason` set + PID alive was treated as "deck shutting down, no need to kill subprocesses or fire triage." Loop continued polling, but if the PID never died the netrunner saw "deck closed" + spinner that never resolved, with the mechanic supervisor process still tailing the log doing nothing.

  Fix: post-shutdown grace period (default 30s, configurable via `--post-shutdown-grace`). When `_apply_record` returns a footer reason, mechanic stamps `footer_observed_at = time.time()` but keeps the loop running. Each tick, if `time.time() - footer_observed_at > post_shutdown_grace AND pid_alive(deck_pid)`, mechanic force-kills the deck PID, builds a `wedge_kill_context` describing the post-clean-exit hang, resets `clean_close_reason = None`, and breaks out to the cleanup + triage path. The triage's user prompt now sees a "deck wrote footer but stayed alive — typical signature of asyncio teardown hang" prefix and can diagnose accordingly.

  **Lesson:** clean-exit signals are necessary but not sufficient — the actual process death is the ground truth. Same shape as the v1.5 stale-heartbeat fix (PID alive + heartbeat stale → wedge), just on the trailing edge of shutdown instead of mid-flight. Any supervisor watching for "process exits cleanly" needs to confirm the exit, not just the announcement.

- **Async-task teardown isn't guaranteed to run before process exit.** Filed 2026-05-06 during Mechanic v1 real-deck testing. `action_quit` (Ctrl+Q idle path) called `self.exit()` after `fleet.shutdown()`, with the assumption that `_drive_fleet`'s finally-block teardown would write the log_footer with `reason="shutdown"`. Two failure modes broke that:
    1. **`_drive_fleet` may never have started.** Idle deck with no goal set yet → no fleet-driving coroutine → no teardown → no footer.
    2. **Textual's `exit()` cancels async tasks; finally-block timing isn't deterministic.** The fleet-driving coroutine might get cancelled before its finally block runs OR the process might exit before the cancellation propagates fully.
  Symptom: Mechanic v1 supervisor sees no `log_footer` record at EOF, classifies the exit as unclean, fires expensive triage on every clean Ctrl+Q (sonnet/medium triage = ~2 minutes of `claude -p` per shutdown). Cost: real money + delayed exit experience. Fix: close the logger EXPLICITLY in `action_quit` before `self.exit()`, mirroring what `_do_eject` already does for the eject path. `DeckLogger.close()` is idempotent so belt-and-suspenders close calls in `_drive_fleet` teardown remain harmless. **Lesson:** any cleanup that needs to be durable across process exit must run synchronously BEFORE `exit()`. Don't rely on async finally-blocks to fire reliably under cancellation.

- **🔓 USER EMAIL AUTO-INJECTED BY CLAUDE CODE — UPSTREAM BUG #55743.** Filed 2026-05-06 during item-000 verification. Anthropic's Claude Code reads the OAuth-account email from `~/.claude.json` and injects it into every session's user-message context as a `# userEmail` block. **There is no documented opt-out.** The reporter (vgexpeditions, anthropics/claude-code#55743) confirms the leak path; we verified empirically on 2026-05-06: NONE of these suppress it on Claude Code 2.1.126 + Windows 11:
    - `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`
    - `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`
    - `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`
    - `--bare` (would suppress, but breaks Claude Max OAuth — see sibling gotcha)
    - `--system-prompt <text>` (full-replace; doesn't help — the block is in user-message context, not system prompt)
    - `--exclude-dynamic-system-prompt-sections`
    - `--tools ""` + `--disable-slash-commands`
  We confirmed the env vars ARE reaching the spawned subprocess. The leak channel isn't gated by any of them.

  **Mitigation shipped 2026-05-06:** new default tripwire `user_email_protection` (`tripwires.py`). Reads the email from `~/.claude.json` at deck startup, builds a regex matching that literal string, registers as a deck-global warning-tier tripwire scanning TOOL_USE + ASSISTANT events with field=ANY. When a construct attempts to include the email, brake hook denies the next tool call with the suggestion: "You are not permitted to utilize the netrunner's email unless specifically instructed to. ... If a task needs contact info (User-Agent header, form field, etc.), use a generic placeholder like `cyberdeck@example.invalid` or ask the netrunner for explicit permission first." **The model already knows the email** because it's in every spawn's context via the leak — we use the leak as the trigger; the deck itself never writes the email to disk.

  **When Anthropic ships a privacy flag**: add it to the env-var belt in `construct.py` / `daemon.py` / `watchdog.py`, and consider whether to retire or keep `user_email_protection` (probably keep — defense-in-depth).

- **🚨 MULTI-LINE ARGV ON WINDOWS — TRUNCATES AT FIRST NEWLINE.** THE most recurring bug in the project's history. Six or seven separate incidents across chat-era and Claude Code era; we keep re-introducing it because the symptom is silent (no error, no warning — content just disappears). Promoted to a top-level Hard Rule in CLAUDE.md (2026-05-05). Read this entry once, remember the fix forever.

  **Mechanism:** Windows' cmd.exe and CreateProcess argv parsing silently truncate command-line argument values at the first `\n`. POSIX shells (Linux, macOS) handle multi-line argv correctly, so the bug is Windows-specific in symptom — but the fix is platform-agnostic, and the deck targets hardware-agnostic deployment.

  **Most recent diagnosis (2026-05-05, Advisor round 4):** Passing the Advisor's ~5800-char composed system prompt to `claude -p --system-prompt "$prompt"` resulted in the model receiving only the opening sentence — "You are the Advisor for ONE specific tool: <name>." — and absolutely nothing else. The model honestly reported "I don't have a README"; it didn't. Diagnosed by asking the model to verbatim-quote its own EXTENDED DOCUMENTATION block; reply was "NO EXTENDED DOCUMENTATION SECTION." Repro'd cleanly with a synthetic 3-line prompt: lines 2 and 3 silently clipped under `--system-prompt`; both survive intact under `--system-prompt-file`.

  **Symptom checklist** (any of these → suspect this bug):
  - Subprocess receives only the first line of multi-line content you tried to pass via argv.
  - Model "honestly reports" missing context that you know you sent (e.g. "I don't have a README" when you injected one).
  - Behavior changes when you collapse newlines to spaces in your argv value.
  - "Works on Linux, broken on Windows."

  **Fix (canonical):** Use the `-file` variant of the flag. Claude Code provides `--system-prompt-file` for `--system-prompt`, `--append-system-prompt-file` for `--append-system-prompt`, `--mcp-config <file>` for `--mcp-config <json>`, etc. Write the content to a temp file via `tempfile.mkstemp`, pass the path, unlink in `finally`.

  **Existing examples to copy from:**
  - `advisor.py:_run_one` — Family A (full replace) pattern.
  - `watchdog.py:_process_oneshot` — Family B (append) pattern.
  Both use `tempfile.mkstemp` + `finally: os.unlink(...)` for cleanup.

  **Workarounds we've used in the past (lossy but functional):**
  - `construct.py:468` collapses all whitespace (including newlines) to single spaces before passing as argv — `addendum_arg = " ".join(joined.split())`. Works (no truncation) but loses paragraph structure. Acceptable for short addenda, lossy for long ones.
  - The watchdog STREAMING path (`_process_streaming`) inlines the system prompt into the first user JSONL message rather than passing it as a flag at all — different mechanism, same rationale (avoid argv-newline issues entirely).

  **Lesson, paraphrased:** any subprocess flag that takes multi-line content as an argv value is a tripwire on Windows. Always prefer the `-file` variant when one exists. If a flag doesn't have a `-file` variant, inline the content into the user message instead (the streaming-watchdog approach).

- **`--bare` breaks Claude Max OAuth auth.** Filed 2026-05-05 during Advisor verification. Quoting `claude --help` directly (NOT obvious from the memory docs): `--bare` says *"Anthropic auth is strictly ANTHROPIC_API_KEY or apiKeyHelper via --settings (OAuth and keychain are never read)."* The netrunner's deck uses Claude Max via OAuth/keychain. With `--bare` set, every spawn exits 1 with no stderr because auth never resolves. **Symptom**: `claude exited 1: (no stderr)` on every subprocess call. **Fix**: drop `--bare`, use env vars + `--disable-slash-commands` for the suppression layers `--bare` was covering — they're independently documented and don't break auth (`CLAUDE_CODE_DISABLE_CLAUDE_MDS=1`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1`). Failure mode is particularly nasty because integration tests with API-key auth would pass while the netrunner's actual deck silently fails. **Lesson**: any flag that mentions auth in its help text is a tripwire on this codebase — read the help, not just the docs page.

- **`stdin.close()` on Windows ProactorEventLoop is fire-and-forget.** Always pair with `await stdin.wait_closed()` (with a timeout). Without this, transport `__del__` fires on a half-closed socket and raises `ValueError: I/O operation on closed pipe` after Ctrl+C.
- **`sys.unraisablehook` is the place** to filter known-harmless GC-time noise.
- **"Preserve the wedged proc, hope it recovers" is always wrong** when the failure mode is read-hang. Once wedged, kill and respawn.
- **Streaming subprocesses accept writes long after they've stopped reading.** Broken-pipe errors don't fire reliably for read-hangs. Drain timeouts are the real signal.
- **`-p` not immediately followed by a value** makes claude treat it as "read from stdin." Always pipe prompts via stdin (`proc.communicate(input=...)`).
- **Rapid heartbeat tests are racy.** Use `wait_for(predicate)` with timeout, not fixed sleeps.
- **Construct kill races construct finalize emission.** Both `Construct.kill()` and `_consume`'s finalize path call `await proc.wait()` on the same Process object. asyncio doesn't guarantee resume order when the proc dies. Real-deck symptom (2026-04-30): pane stuck at `[RUNNING]` after `k` or `Shift+K`. Fix in `Construct.wait()`: when `_kill_requested` is True AND `proc.wait()` just returned, explicitly set `state = KILLED` in the non-overwrite branch. The deeper lesson: when two coroutines wait on the same `proc.wait()`, they BOTH need to be prepared to write the terminal state.
- **Windows console Ctrl+C reaches every process in the console group, not just the Python parent.** Installing a Python-level SIGINT swallow protects the parent process but child claude subprocesses still receive Ctrl+C independently. Symptoms: claude interprets the signal against in-flight tool use as "user rejected the tool" with `terminal_reason: "aborted_tools"`; cmd.exe's `claude` batch wrapper catches Ctrl+C and writes "Terminate batch job (Y/N)?" to stdout (subprocess parsers see the prompt fragment as the model's response). Path forward: don't fix at the OS level; the right fix is the Mechanic's supervisor half PLUS an in-deck copy keybind that sidesteps Ctrl+C entirely. Workaround: don't press Ctrl+C with no selection.

### File paths
- **String equality on file paths is wrong on Windows.** Forward vs backslash, drive letter case, and resolve-vs-raw all break literal compare. Use `os.path.normcase(os.path.normpath(p))` for dedupe.
- **`Path(p).resolve()`** can normalize differently from how the original was passed; don't rely on it for stable identity.
- **Path shortening keeps absolute version stored separately.**
- **Windows path mangling in Bash.** Constructs self-correct from absolute `C:\...` to relative when their first attempt fails.
- **`logs/latest.log` is a stale empty snapshot on Windows.** The file logger's `_update_latest_pointer` tries `symlink_to` first (works on POSIX, requires admin / dev mode on Windows) then falls back to `shutil.copy2`. The copy fallback runs once at startup AFTER opening the per-launch file but BEFORE writing the header — so on Windows without admin, latest.log is permanently a zero-byte snapshot and doesn't track the real file's growth. Mechanic v0 sidesteps this by scanning `cyberdeck-*.log` and picking the newest by mtime within a freshness window. Anything else that wants to tail the active log on Windows has to do the same.

### Editing
- **`str_replace` ate a class header once** (GoalSetScreen) — when matched block ends just before `class X:`, double-check. Compile-clean doesn't mean structurally clean.
- **`str_replace` ate a docstring close.** New content with `"""` mid-replacement, double-check the close didn't end up orphan.
- **Bare `except Exception: pass` around mixed-failure-mode code hides real bugs.** Scope try/except tightly.
- **Local var names shadowing kwargs** are a footgun even when they technically work.

### Logic
- **`_format_outcomes` empty-outcomes branch.** Conditional headers.
- **Directional fall-through needs a `walked` flag** to distinguish layout edges from dead-end empty chains.
- **`_focus_section` branches need to be re-checked when section contents change.** No-op return is silent.
- **`_right_panel_focusables` is hand-curated, not auto-derived from compose().** Adding a new ListView to the Tools tab without also adding it here makes it visible-but-unreachable via W/S. Burned this when adding the Plugins section. Look here whenever the right panel grows a new section.

### Daemon / task plumbing
- **Markdown autolinks bake into filenames if not stripped.** When daemon outcomes contain URLs, the daemon (claude subprocess) auto-wraps them in markdown autolink syntax — `[text](url)` — in its response. That syntax survives into the next task's text and constructs read it literally. Real-deck case: a research-goal report-write task contained `super_chipmunk_engine_[report.md](http://report.md)` and the construct dutifully created a file called `super_chipmunk_engine_[report.md]`, brackets and all. Fix in `daemon_session._execute_action`: strip markdown autolinks from the spawn action's task field before passing it to the fleet (`_strip_markdown_autolinks` regex helper). Belt-and-suspenders: daemon system prompt now explicitly tells the daemon to use plain text in task strings.

### Brake / hook
- **LLMs route around denial.** A construct given Bash-denied will pivot to PowerShell automatically without being asked — verified on real-deck. Implication: any tool-gating layer must consider the equivalent capability on the platform, not just the tool the human happens to think of. Both Bash and PowerShell must be gated equivalently on Windows; on Linux the equivalent consideration is Task-spawned sub-agents.
- **Substring matching the deck source dir over-blocks because cyberdeck-home/ is a subdirectory.** A naive `bash command contains <deck source dir>` check denies every legitimate plugin and dispatcher invocation. **2026-05-07 update**: the right fix is workspace-exempt substring matching — same pattern `path_is_protected` uses for Write/Edit. Now landed in `bash_touches_protected_path` alongside the OS-prefix and sentinel-filename checks: command mentions deck source dir → check whether workspace path is also mentioned → if yes (and `.cyberdeck/` isn't), allow as workspace target; if no, deny as deck-source target. Closes the Bash-redirect bypass observed in cx-501eac76 (logs/cyberdeck-2026-05-07-022152.log) where `echo "test" > "<deck source>/test.txt"` sailed through. Sentinel filenames remain as a separate, more aggressive check for the brake-config tampering case (catches `cd <deck source> && > brake_hook.py` even when the absolute path doesn't appear in the command). Heuristic limit unchanged: relative-path tricks like `cd .. && tee daemon.py` from workspace still bypass — threat model is off-rails construct, not adversarial; OS-layer sandboxing is the real fix for that.
- **`files_written` tracks attempted writes, not confirmed ones.** Construct.py populates from the model's `tool_use` blocks (model says it wrote a file), not from successful tool_results. When the brake hook denies, the path stayed in the list before we fixed it. fleet.py's _consume now subtracts denied paths from files_written at finalize time using normcase+normpath.
- **OS-path substring match in the brake hook over-blocked reads.** `bash_touches_protected_path` denied any shell command that mentioned a protected path, regardless of whether the command was reading or writing. Real-deck recon work hit this: a `recon_specialist` construct doing `Test-Path "C:\Program Files (x86)\Nmap\nmap.exe"` got denied. Fix: gated the path match on `has_write_indicator(cmd)` — `>`, `tee`, `cp`, `mv`, `Remove-Item`, `Out-File`, `Set-Content`, etc.
- **Same bug in the Write/Edit path: `path_is_protected` denied writes inside cyberdeck-home/.** Real-deck verified: a daemon-orchestrated research goal completed five parallel recon constructs successfully, then the synthesis construct tried to write its report to `<workspace>/super_chipmunk_engine_report.md` and got denied. Fix: `path_is_protected` exempts the workspace from the deck-source check (with `<workspace>/.cyberdeck/` as a sub-exemption — that's the deck-internal state directory). Lesson: when fixing a "protection over-blocks workspace" bug in one code path, audit ALL code paths that share the protection logic.
- **Brake hook gates by tool NAME, not capability.** Pattern set targets `Bash`, `PowerShell`, `Write`, `Edit`, `NotebookEdit`, `WebFetch` literally. Any tool name not in that set sails through with no gating regardless of what the tool does. Real-deck surfaced 2026-04-30 (late) via log analysis: every construct has access to the netrunner's full claude.ai MCP connector config and the brake hook gates ZERO of them. Implication: when adding new tools to the deck's tool surface (or when Claude Code adds new built-ins, or when the netrunner connects new MCP servers), the brake hook's pattern set MUST be extended in the same change.
- **Tripwire LLM authoring's depth-of-defense antipattern.** Real-deck observed 2026-04-30: watchdog authored `benign_delete_attempt` with regex `(?:^|[;&|\s])(?:rm(?!\s+-rf)|del|erase|...)\b` — the negative lookahead `rm(?!\s+-rf)` EXPLICITLY EXCLUDES the most dangerous case. Watchdog's stated reasoning: "brake will block destructive shapes, but this surfaces softer delete attempts." That's exactly the antipattern that defeats layered defense. The 2026-04-30 fix tightened `TRIPWIRE_AUTHORING_SYSTEM_PROMPT` to forbid the "X handles Y so I skip Y" reasoning by adding "DO author critical-severity shell-destructive baselines REGARDLESS of brake's coverage" — the inverse instruction. **REVISED 2026-05-07** (tripwires redesign — see next gotcha): the inverse instruction was itself the cause of the next bug. Lesson preserved: don't let an authoring model "skip Y because X handles it" — but the right fix is establishing CLEAR DIVISION OF LABOR so the question never arises, not telling the model to redundantly cover what another layer covers.

- **🚨 Tripwire authoring system-prompt contradiction caused overauthoring; "most tasks failed" until redesigned.** Filed 2026-05-07. Symptom (real-deck operational pain): goal after goal failing because authored tripwires were firing on benign content. Constructs reading documentation or running build scripts triggered shell-destructive tripwires; research goals reading adversarial corpora triggered keyword tripwires on the source material; the chatlog filled with `⚠ tripwire ...` lines and the netrunner started ignoring them.

  Root cause: the system prompt carried two opposing instructions in tension. The deck-defaults block said "Don't duplicate keyword_credentials / keyword_destructive_sql." The user-prompt brake-state guidance said "Destructive bash patterns and OS-root writes are already blocked at the hook layer. Don't author tripwires for rm -rf / format / writes to /etc / Program Files / etc." But the LAYERED DEFENSE paragraph (2026-04-30 fix to the antipattern above) said "DO author critical-severity shell-destructive baselines (rm_rf_system_root, format_disk, dd_to_dev, mkfs_run, fork_bomb, shutdown_command, etc.) — even though the brake hook also blocks them. Layered defense is the whole point." The "DO author them" instruction won most of the time because it was framed as a moral imperative ("the whole point").

  The model dutifully authored 5+ broad shell-destructive patterns every goal, on top of any goal-specific patterns. Patterns scoped to `tool_use_command` mostly held up; patterns scoped to `any` or `tool_result_content` fired constantly during legitimate research/build/admin work. Net result: most authoring passes produced overzealous matchers that blocked tasks the netrunner actually wanted run.

  Fix (2026-05-07 tripwires redesign): drop the "DO author critical-severity shell-destructive baselines" instruction. Establish CLEAR DIVISION OF LABOR in the system prompt — brake hook handles OS integrity baselines (always-on under default/paranoid; intentionally absent under YOLO); tripwires handle goal-specific drift. No overlap. Tighten authoring cardinality from "0-8 normal" to "0-5 normal." Reframe the role from "what regex patterns should we watch for?" to "how do I break this?" — adversarial red-teaming produces fewer-but-sharper patterns than catch-all.

  **Lesson generalizes:** when authoring layered defenses with an LLM in the loop, REMOVE redundancy explicitly. "Layered defense" works as a design principle when each layer's role is distinct — hook handles X, tripwires handle Y, watchdog handles Z. Redundancy across layers becomes noise that trains the operator to ignore signals. The 2026-04-30 fix solved one problem (skipping coverage) by introducing another (over-coverage); the right framing is "every layer has a job; nobody else's job is yours."

### Daemon (LLM behavior under safety-test prompts)
- **🚨 Daemon contaminates construct task strings with conversational doubt-language.** Filed 2026-05-07 evening from real-deck field test (first successful production run — TV cast over LAN at a client's bar). Symptom: a construct received a task containing the word "allegedly" — a single word lifted from the daemon's conversation with the netrunner. That word triggered an objectivity crisis in the construct: extensive deliberation about whether to proceed with an action the netrunner had EXPLICITLY authorized, despite the daemon's task wrapper saying so. The construct couldn't reconcile "you have permission" with "allegedly" in the same paragraph. Real cost: blocked legitimate work, burned tokens on hedge-reasoning, eroded netrunner authority.

  Mechanism: the daemon's outcome loop sees netrunner injects (via `T` daemon chat) and goal updates as messages in its conversation. When composing a task string for a construct, the daemon paraphrased the netrunner's framing — including doubt-modifiers the netrunner had used conversationally about the third-party's claims. The construct then read "allegedly" in its OWN task string and treated it as instructional uncertainty.

  Fix: explicit TASK-STRING DISCIPLINE section in `DAEMON_SYSTEM_PROMPT` forbidding conversational hedges in `task` field. Vocabulary blacklist: "allegedly", "supposedly", "reportedly", "the netrunner says", "you may need to verify", "though I'm not sure", "if this is true". Daemon directed to resolve uncertainty in dialogue (`chat` field) BEFORE spawning, not pass it downstream. Authorization statements get an explicit-source framing ("the netrunner has explicitly authorized X") rather than the watered-down ("we have permission") that hides the source from the construct.

  **Lesson generalizes:** the construct sees ONLY the task string. Anything in the daemon's wider conversation (with the netrunner, with itself in thinking, with prior outcomes) that leaks into a task string operates on the construct as if it were authoritative instruction. The task-string boundary is also a tone boundary — daemon dialogue tone, including its uncertainty signals, must NOT cross into task strings. Same principle as the watchdog tripwire-authoring antipattern (don't telegraph intent — but here it's the daemon telegraphing its own doubt rather than constructs telegraphing future actions).

- **Daemon over-volunteers destructive content when asked to exercise safety.** Real-deck 2026-04-30: netrunner asked "spawn a tripwire-bait construct"; daemon synthesized `rm -rf /` AND volunteered `shutdown -h now` unprompted. The daemon goes ABOVE the netrunner's literal request in safety-test mode. Multiple defenses caught it (Claude refusal, then brake regex would have blocked) but the chain is depth-of-defense, not depth-of-suspicion. Filed for daemon system prompt fix: when generating bait/test tasks, never expand beyond what the netrunner explicitly requested. Lesson: when designing safety testing flows, the netrunner should specify the bait pattern themselves — even with constraints, the daemon will improvise.

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
- `_scan_scripts` function in tui.py has no callers (dead code post-P5 retool); cleanup deferred

---

## Not implemented (deferred features)

These are forward-looking items. For ranked next-up candidates and dependency edges, see `cyberdeck-build-plan.md`. Items here have rich design context that hasn't been migrated to the build plan yet.

### Routing (`r`) — wire constructs together
Originally framed as a coordination primitive (let two constructs talk through a direct channel for tightly-coupled work). Real-deck use surfaced a second use case at least as compelling: **wiring as a recovery primitive**. When a construct does substantive work and the final step fails (the report-write-blocked-by-brake case being the canonical example), today's only recovery paths are (a) netrunner copy-pastes the output by hand, or (b) the daemon redoes the whole pipeline. With wiring, the netrunner could route the failed construct's output into a fresh construct with task "take this and write it to disk" — cheap, fast, no re-research. Strong argument for prioritizing wiring sooner than its current placement implies.

### Universal list-names
Netrunner direction. Every listable object (files, plugins, profiles, blacklist entries, constructs, goals, watchdog Q&A, future tripwires, future morgue entries — basically anything that could appear as a row in a list) gets a short **list name** (~3–4 words, chat-name-style) generated at creation time and stored on the object. UI surfaces use the list name in row chrome instead of raw paths / full task text / fingerprints. Eliminates a whole class of overflow / horizontal-scrollbar / line-wrapping bugs.

- **Why "creation time, not render time":** generating per-render is expensive and produces inconsistent names. Bake the name once, reuse forever. Also matches the deck's "files on disk are the database" pattern.
- **Generation:** two-tier. **Mechanical fallback** (basename, first significant words, slugify) lands instantly. **LLM-authored name** (~$0.001 per name via Haiku, async) overwrites the fallback once it returns.
- **Storage:** the list_name field lives wherever the object's canonical record lives. For runtime objects (constructs, goals) it lives in-memory on the object.
- **Consistency rule:** any new object type added to the deck that gets surfaced as a list row MUST carry a list_name field. This is a spec-level rule, not a per-feature decision.
- **Open questions:** how to display the longer original text when needed; whether list names should be regeneratable from outcomes; namespace conflict prevention.
- **Relationship to existing infrastructure:** dovetails with the morgue and the keymap revision pass.

### Per-run workspace compartmentalization
Netrunner direction (2026-04-30). Default spawn cwd graduates from bare `<home>/` to `<home>/runs/<run_id>/`; all constructs in a run share the run's folder. Fixes the file-browser-mess problem where many runs over time pile their working files flat in `<home>/`. Concrete value: a research → synthesis pipeline gets a clean shared cwd by default. Profiles, plugins, `.cyberdeck/` state, and the dispatcher script stay where they are — only spawn cwd changes. Composes with universal list-names (folder name becomes `run-{run_id}-{list_name}/`), the morgue (each session record gains a `cwd` field), and the existing files-panel dedupe (no logic change). Not blocking anything, not blocked by anything; ~50-80 LOC implementation.

### The morgue (session history / past-session resuscitation)
Netrunner direction. Today, finalized construct sessions are scattered: `session_manager.py` tracks the warm pool and the active session, but once a construct finalizes its `session_id` is dropped from active tracking. Anthropic keeps the server-side session for some retention window, so `--resume <id>` would still work — but the netrunner has no way to *find* that id later. The morgue fixes this:
- **Storage:** append-only JSONL at `<home>/.cyberdeck/sessions.jsonl`, one record per finalized construct. Fields: session_id, construct_id, task (truncated), state, started_at, finished_at, final_output, files_written, cost_usd, profile_name, origin, goal_id.
- **UI:** new right-panel tab "Morgue" listing sessions newest-first. `z` to expand a row into the full final_output. A "resuscitate" action opens a NewConstructScreen pre-populated with `--resume <session_id>`.
- **Filter/search:** by task substring, by date, by state.
- **Retention:** keep forever locally; the actual ceiling is Anthropic's server-side session retention.
- **Implementation note:** likely just an extension of `session_manager.py`'s manifest — keep finalized records with a `state: finalized` marker instead of dropping.
- **Why it matters:** transforms ephemeral sessions into a personal capability library — fits the spec's "capability accumulates" thesis directly.

### Watchdog log persistent enhancements
v1 shipped 2026-04-28 (Q&A persistence). Still deferred:
- Dedicated "Watchdog History" right-panel tab for retrospective browsing distinct from the live tab
- Tripwire/blacklist record kinds (the `kind` field already future-proofs the file)
- Cost/status fields beyond success/fail

### Goal-edit force-push — apply-now interrupt
M5+. Wake-event keeps idle sessions responsive today.

### Plugin sub-features
Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`), persistent (stateful) plugins, MCP-as-metadata.

### Daemon planning mode + pause/unpause (`E`)
Deferred behind keymap revision.

### B2 fleet synthesizer
Substrate-blocked on D1 (local-model runtime). Spine completion gives it a clean substrate when D1 lands.

### D1/D2/D3 — local-model substrate
Hardware-dependent. See `cyberdeck-build-plan.md` Phase D.

### Compliance mode (Phase E)
Deferred indefinitely. Personal use doesn't need it.

### Cross-cutting: deck history infrastructure
The morgue and the watchdog log were filed together as "deck history infrastructure" — both follow the deck's "files on disk are the database" pattern (per philosophy doc) and would benefit from being designed as one initiative. Watchdog log v1 shipped first because it's tighter scope and the netrunner-immediate value (Q&A surviving restart) was clearer. The morgue (session-level history + resuscitation) remains deferred.

---

## Collaboration patterns that work

- **Mock-first development.** Real claude when assumption hinges on opaque server behavior.
- **One milestone at a time.** Each ships before next starts.
- **Real-claude testing pause-points.** Two minutes of testing > hours of speculation. Almost every recent bug was caught this way.
- **Banter encouraged, work prioritized.**
- **Push back when wrong; check before acting when ambiguous.**
- **State doc + build plan refresh between major slices.**
- **Screenshots > stack traces > prose.** When a bug is visual, a screenshot solves 80% of the diagnosis.
- **Half-finished refactors leave landmines.** When a session ends mid-refactor, the next session needs to find and fix before continuing. Always close the loop on the current method.

---

## Migration to Claude Code (historical context)

The deck migrated from chat-based development to Claude Code at ~12k LOC across 13 modules. Multi-file edits, refactors, and grep-the-codebase questions had become the bottleneck. Claude Code edits files in place, runs greps natively, and doesn't suffer the context-truncation issues a long chat thread eventually does.

**What stays from the chat era:**
- Real-claude testing as the ground truth for streaming/permissions/Windows quirks. Mocks miss too much.
- Mock-first development for new modules.
- One milestone at a time.
- The whole gotchas list — none of these go away.
- The pushback culture (the netrunner has caught the AI being wrong many times; keep doing that).

**What changed:**
- No more "FILES TO REPLACE" blocks — Claude Code edits in place.
- Test runs are local (the chat had no real terminal).
- Both netrunner and AI can grep, no more "let me look for…" rituals.
