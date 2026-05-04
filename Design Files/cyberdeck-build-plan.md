# Cyberdeck — Build Plan

*Companion to `cyberdeck-spec.md`. Tracks what's shipped, what's next,
and what's deferred (with reasoning, so we don't have to re-derive
priority next session).*

---

## Shipped

### Tier 1 — original scope
M0 Construct, M1 Fleet, M2 TUI skeleton, M3 keyboard model, M4a
Daemon-driven goals, M5.1–M5.3e (keymap, idle lifecycle, focus polish,
session pool with cross-restart reuse, RAM meter), Phase A Step 1–3
(EJECT, construct injection q/Q, limits modal `l`).

### Tier 1+ Observability — B1
Activity chatlog. Mechanical event extraction. Right-panel tab.
Color-coded, deque-backed, ExpandModal-magnifiable.

### Tier 2 — Profiles (C1)
TOML loader, frozen Profile dataclass, ProfileRegistry (file-watch,
hot-reload), default profile auto-seeded, `default_construct_addendum`
+ `default_daemon_addendum`, daemon picks profile per-spawn via JSON.

**Profile/brake refactor (shipped, post-migration to git):** the
original Tier 2 design rolled brake state into profiles and used
`allowed_tools` as a hard cap with two-axis privesc gating between
profiles. Real-deck use revealed two problems: (1) profiles ended
up reused as a security model rather than as instruction templates,
and (2) brake-as-profile-field meant the netrunner couldn't change
brake without editing TOMLs. Refactored to:
- **Brake state is deck-global**, persisted at
  `<home>/.cyberdeck/state.json`, set via the `b` modal (paranoid
  is single-press, yolo is EJECT-style countdown gesture). One
  brake setting governs every new spawn deck-wide.
- **Brake enforcement happens via Claude Code's PreToolUse hooks**,
  not via `--allowedTools`. Each spawn gets a per-construct
  `--settings` JSON pointing at `brake_hook.py` with the current
  brake passed via argv. Hook is a self-contained Python script
  (~150 LOC) that reads the proposed tool call from stdin, applies
  hand-curated patterns (OS-root paths + destructive bash), exits
  0/2. Stderr text becomes the model-visible denial reason.
- **Profiles are now prescriptive templates only** — instruction
  addendums + `recommended_tools` (renamed from `allowed_tools`,
  surfaced as a soft hint in the system-prompt addendum). No
  brake field, no privesc check, no two-axis gating.
- **Watchdog observes the deterministic hook layer** via the
  `permission_denials` field on result events; chatlog renders a
  `· brake blocked: Write×2, Bash×1` suffix on finalized lines.

### Tier 2 — ~~Brake profiles (C2)~~ — superseded
C2 is now folded into the deck-global brake refactor above. Brake
tiers are still paranoid/default/yolo, but they're no longer per-
profile and no longer mediated by `--permission-mode` — the hook
layer is the enforcement gate.

### Tier 2 — Tool registry (C3 mostly shipped)
All three legs registered: Profiles, Scripts, and Plugins. The
plugin scaffolding (post-migration) lands as folders under
`<home>/plugins/<name>/` with TOML manifest + Markdown README +
entry script; PluginRegistry mirrors ProfileRegistry but is
one-shot (no hot reload, plugins are code). Tools panel grows a
PLUGINS section with availability marker for plugins whose
`requires` checks fail. Daemon system prompt + construct
system-prompt addendum both gain plugin awareness. Sub-features
deferred: wiring keys (`p`/`c`/`Shift+C`), persistent (stateful)
mode, MCP-as-metadata variant.

C1g listification: nav rebind (lowercase=scroll, uppercase=walk),
Tools→ListView, Files→ListView, LaunchScreen modal. Phase A
deck-control protocol: dispatcher script, marker protocol parser,
construct system prompt addendum. Phase B Tools panel restructure:
Profiles + Scripts (now also Plugins) only; CONSTRUCT TOOLS dropped;
literal `<home>` removed; dir-reference labels removed; PERMISSIONS
placeholder removed.

### Production-grade fixes (recent)
- Pane-log un-trim (raw event buffer + untruncated mode)
- Watchdog Q&A (`t`) with streaming default + wedge recovery
- Daemon chat (`T`) with stacked netrunner messages
- Goal-edit mid-flight (`e`) with diff classifier
- Connection state monitor with sidebar indicator
- Tabbed bottom panel (`[Daemon] [Watchdog]`)
- Spawn provenance origin badges
- z-to-view (file/profile/script) with syntax highlighting
- Path-normalized Files panel dedupe
- Focus navigation fall-through with empty-section escape
- Windows ProactorEventLoop shutdown noise filter
- Watchdog Blacklist primitive (Shift+K populates; DaemonSession
  refuses spawns matching registered fingerprints; in-flight
  constructs whose task matches get red-bordered but not auto-killed;
  daemon sees the blacklist on every outcome turn)

---

## Phase B — Observability (status)

- **B1 Activity chatlog**: ✓ shipped
- **B2 Fleet-level synthesizer**: deferred until D1 (local model)

---

## Phase C — Tier 2 status

- **C1 Profiles**: ✓ shipped (refactored — see profile/brake refactor
  in Shipped section; profiles are prescriptive templates now)
- **C2 Brake profiles**: ✓ shipped, then superseded by deck-global
  brake refactor. Brake is no longer profile-attached.
- **C3 Tool registry tree**: 🟢 mostly shipped
  - ✓ Profiles registry
  - ✓ Scripts registry
  - ✓ Plugins (third leg) — folders with manifest + README + entry;
    stateless v1 with screenshot as the first plugin. Wiring keys
    (`p` airgap, `c` quickfire, `Shift+C` picker), persistent mode,
    and MCP-as-metadata sub-shape are deferred sub-features.
  - ✗ Hierarchical Esc-up navigation
  - ✗ Script manifests

---

## Phase D — Local-model infrastructure (mostly hardware-blocked)

- **D1 Local model runtime** — pluggable client against Ollama-
  compatible API. Required for D2 and ideally for B2. Also unblocks
  the Watchdog substrate swap (cloud → local).
- **D2 Arbiter** — pre-daemon classifier with TOML policy.
  Hardware-blocked for production (RK3588 NPU latency); could
  prototype on dev with measured-not-guessed latency.
- **D3 Synthesizer (B2) on local substrate** — once D1 is in.

---

## Phase E — Compliance mode (deferred indefinitely)

See `cyberdeck-compliance-future.md`. Tokenization, secret store,
watchdog blindfold. Personal use doesn't need it.

---

## Other deferred work (no current phase home)

Roughly ordered by likely appeal:

0. **README restructure for public GitHub repo** (filed 2026-05-02
   by netrunner). The repo is now public at
   github.com/watchdogeditor/Cyberdeck. Current README is light
   and dev-facing — pitch + status + run commands + design-doc
   index. Public-facing readers landing on the repo cold need a
   structured walkthrough: what is this, who's it for, what does
   the screenshot look like, how do I run it, what's the
   architecture in one paragraph, where do I read more (link to
   the canon docs in `Design Files/`), what's the current
   stability story (active personal project, not a packaged
   product). Tee up sections: **Pitch** (one paragraph + GIF/
   screenshot of the TUI mid-flight); **What it is** (TUI
   orchestrating Claude Code subprocesses; daemon decomposes,
   constructs execute, watchdog observes); **Run it**
   (prerequisites + commands, currently scattered across
   CLAUDE.md and existing README); **Architecture sketch**
   (the four runtime entities + the spine + the brake; one
   paragraph each); **Design canon** (links to spec /
   philosophy / state / build-plan / orientation, with a
   one-line "what to read first" hint for each); **Status**
   (active solo dev, Windows-first, breaking changes likely);
   **License + contributing** (single-author project, no PRs
   solicited but issues welcome). Don't include LOC stats —
   they go stale fast. Don't write API docs — there's no API.
   Sub-day session of focused writing; no code changes.

0a. **First-run onboarding check** (filed 2026-05-03 by
   netrunner). Today's deck self-bootstraps file artifacts
   (profiles dir, dispatcher script, plugin bridge, tools.toml,
   state.json) but doesn't verify external prerequisites. A
   netrunner who clones the repo to a fresh machine and runs
   `python tui.py` gets either a cryptic ImportError on Textual
   missing, or `[Errno 2] No such file or directory: 'claude'`
   deep in async setup, or — worst case — silent failure modes
   when claude is on PATH but not logged in. Right shape:
   - Sentinel at `<home>/.cyberdeck/first_run_complete`
   - `_first_run_check` runs on every startup; cheap (~50ms,
     stdlib-only). On first run OR on detected FAIL, surfaces
     diagnostics; otherwise silent.
   - Probes: Python version (3.11+), `import textual`,
     `import mss` (warn-only — mss is screenshot-plugin-specific),
     `shutil.which("claude")` for the binary, `claude --version`
     to verify the install isn't broken, `claude --help | head`
     to confirm logged-in account (network call; first-run only).
   - Each prereq → PASS / WARN / FAIL with remediation hint
     ("install: pip install textual", "install: npm install -g
     @anthropic-ai/claude-code", "run: claude /login"). DETECT +
     SUGGEST, NOT AUTO-INSTALL — npm/pip auto-install is fragile
     across corp firewalls, alternate Python distributions, and
     environments where the user can't write globally. Better to
     be the doctor than the surgeon.
   - `--doctor` CLI flag re-runs the check on demand even after
     the sentinel is set. `--no-doctor` skips entirely (escape
     hatch for environments where the probe itself is broken).
   - Exit before TUI mount on FAIL of hard prereqs (Python
     version, textual, claude binary). On WARN (mss missing,
     auth uncertain), surface a chatlog notice and continue.
   ~150 LOC, mostly inline `subprocess.run([..., '--version'],
   timeout=5, capture_output=True)` checks + a small reporter
   function.

0b. **Preferences module** (filed 2026-05-03 by netrunner).
   Today's persistent settings are scattered: brake state lives
   under `<home>/.cyberdeck/state.json`'s `brake` key,
   delay/wedge timeouts under the `limits` namespace in the
   same file. Modified through `brake_state.load/save` +
   `brake_state.load_limits/save_limits` — works, but the API
   surface scales linearly with new settings. Future settings
   (theme, default profile, agent defaults, keybind overrides,
   per-installation flags) need a unified home and an
   ergonomic accessor.
   - New `preferences.py`: thin wrapper module providing a
     `Preferences(home_dir)` dataclass-shaped accessor. Reads/
     writes the existing `<home>/.cyberdeck/state.json` (no
     migration needed — the file already exists; we just
     formalize it).
   - Semantic API: `prefs = Preferences(home_dir);
     prefs.brake; prefs.delay_window_seconds; prefs.save()`.
     Internal: read-merge-write pattern (so concurrent saves
     of different keys don't clobber each other; same shape
     as `brake_state.save_limits` today).
   - Migrate existing brake_state.load/save and
     brake_state.load_limits/save_limits to flow through
     Preferences transparently — callers stay unchanged.
   - File header comment: `# DECK-OWNED PREFERENCES — managed
     by the deck via preferences.py. Manual edits survive
     restarts but may be overwritten by the deck on the next
     setting change. Edit at your own risk.`
   - The "agents don't touch it mistakenly" concern is already
     structural: brake_hook.path_is_protected exempts the
     workspace EXCEPT for `<home>/.cyberdeck/`, so constructs
     CANNOT write to state.json (writes go through the brake
     hook, the brake hook denies). Reads stay allowed —
     useful for an agent inspecting current brake state without
     compromising integrity.
   - Future settings to bake into the schema (commented as
     placeholders, no live wiring yet): `theme`, `default_
     profile`, `default_tools`, `keybind_overrides`,
     `agent_defaults`, `last_session_id` (for the morgue
     when it lands).
   - ~150 LOC.

1. **Plugin scaffolding** — ✓ shipped (v1: stateless, screenshot
   plugin as first example, brake hook gates invocations naturally
   via existing bash/path patterns). Sub-features still deferred:
   plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`),
   persistent (stateful) mode for plugins that need a long-lived
   service process (camera with live preview, SSH session), and
   MCP-as-metadata as a v2 sub-shape for plugins that route
   through Claude Code's `--mcp-config` rather than spawning a
   deck-side subprocess.
2. **Real-deck shakedown on Windows.** Several of the latest features
   are mock-tested AND user-confirmed on real deck — no further
   shakedown urgent. But ongoing real-deck use will continue to
   surface bugs faster than mocks can.
3. **Connection consequences** — spawn-blocking on Degraded, daemon
   parking, recovery flow. Detection is shipped; consequences need
   hooks into spawn path + daemon lifecycle. Smallest M5+ slice.
4. **Watchdog tripwires + blacklist** — slices 1 + 2 shipped (DSL,
   deterministic matcher engine, default tripwires, fleet→engine→
   chatlog pipeline, LLM authoring at goal-start / non-clarification
   goal-update). Blacklist primitive (`Shift+K`) shipped earlier.
   Remaining slices: severity-aware rendering (3), persistent
   tripwire library at `<home>/tripwires/` with TOML authoring (4),
   daemon-side severity hints (5), blacklist-derived tripwires that
   fire on event content rather than just task fingerprints (6).
   Per-outcome adaptive re-authoring still blocked on a "daemon
   signals plan shift" event.
5. **Script manifests** — declarative `(name, category, args, expected
   output shape)` per spec. Currently raw filenames only.
6. **Construct script-launch wiring** — ScriptListItem space lands
   here once manifests exist.
7. **Goal-edit force-push** — apply-now interrupt of in-flight turn.
8. **Per-run workspace compartmentalization.** Default spawn cwd
   becomes `<home>/runs/<run_id>/` instead of bare `<home>/`. All
   constructs in a run share the run folder; created on first
   spawn, kept across the run's lifetime. Profiles, plugins,
   `.cyberdeck/` state, and the dispatcher script
   (`<home>/tools/deck/cyberdeck.py`) stay where they are — only
   *spawn cwd* changes. Concrete value the netrunner called out:
   when one construct does research and writes findings to a few
   files, then a synthesis construct assembles a report from
   those files, the synthesis construct's cwd already contains
   exactly the source material. Today everything piles up flat in
   `<home>/`, run after run, and a file browser is a mess.

   Natural intersections:
   - **Universal list-names.** Folder name graduates from
     `run-29a3fd08/` to `run-29a3fd08-{list_name}/` once that
     lands ("run-29a3fd08-recon-supplychain-vuln" beats the bare
     hex slug). The folder is the obvious place for the
     run-level list_name to live.
   - **The morgue.** Each finalized session record gains a
     `cwd` field naming its run folder. Morgue UI → file browser
     becomes one click.
   - **Files panel.** Already tracks absolute paths with
     `normcase + normpath` dedupe; per-run folders just make
     paths longer, no dedupe-logic change.
   - **Brake hook.** Already exempts `cyberdeck-home/` from the
     deck-source-dir guard; per-run subdirs are inside that
     exemption automatically.
   - **Cross-run reuse.** Constructs in a new run can't see the
     old run's outputs at default cwd. Two patterns work:
     (a) absolute paths via the new `y/Y` yank ("here's the
     file I want this construct to read"); (b) a future
     "promote to home" affordance that links a run-folder file
     out to `<home>/` for cross-run reuse. (b) is post-MVP.

   Decisions to make at implementation time:
   - **Folder name shape** before list_names exist:
     `run-{run_id}/`, `run-{timestamp}/`, or
     `run-{timestamp}-{run_id}/`. Lean toward run_id-only for
     compactness; sortability is a `git log`-style concern, not
     a directory-listing one.
   - **Cleanup policy.** Empty folders on shutdown? Stale
     folders after N days? Or leave to the netrunner. Default
     to "leave alone" — let the morgue grow this when it lands.
   - **Ad-hoc-constructs case.** `python tui.py "task A"`
     without a goal still gets a `run_id`; same path applies
     cleanly.

   Implementation cost: ~50-80 LOC (Fleet computes run-folder
   path from `run_id`, creates on first spawn, threads through
   to Construct cwd default; LimitsScreen / sidebar untouched).
   Not blocking anything, not blocked by anything. Could ship
   in a focused session post-Mechanic v0.
9. **Daemon planning mode + pause/unpause (`E`).** Originally framed
   as the next milestone post-migration; deferred behind the keymap
   revision because (a) it'd add new bindings to a keymap that's about
   to shift, and (b) the design needs more thought than the first
   sketch captured. Revised intent (netrunner, 2026-04-27):
   - Three workflow paths to getting work done on the deck:
     **(A)** direct construct ("send this file to email") — one-shot,
     bypasses daemon decomposition; **(B)** goal to daemon — the
     everyday hot path; **(C)** planning mode for goals too complex
     to dump in a single message.
   - Planning mode is an **input modality (a modal), not a daemon
     state**. You open the planning modal, hash out the intent with
     the daemon inside it, confirm — and on confirm the modal closes
     and a normal goal-launch fires with the matured plan attached.
     Daemon stays a binary idle/working machine; planning is a
     netrunner-side conversation surface that produces a structured
     goal. Easier to back out of (Esc dismisses, no goal was set);
     less state-machine surface; cleaner separation.
   - Planning mode is an **addendum** to the current goal-set flow,
     not a replacement. The everyday `e → submit → working` path
     stays exactly as-is. Planning is opt-in for the complex case.
   - Once confirmed and launched, the plan needs to produce a
     **persistent tracking panel** akin to Claude Code's "tasks"
     panel — the plan's steps stay visible and tick off as constructs
     finish. The construct pool shows live state; the plan panel
     shows progress against intent. Two surfaces, two purposes.
   - This means a structured `plan: [{step, task, ...}]` field in the
     daemon's response shape (not just prose in `chat`), plus a new
     panel surface for the post-launch tracking view. Panel placement
     and how it relates to the goal pane / construct pool is open.
   - Pause/unpause is the simpler half of the original pairing and
     can ship independently if planning mode stalls on design.
   - Open question for next pickup: does the visible/invisible spawn
     distinction (`n`/`N`) actually track with how path A is used?
     When daemon is working, invisible is almost always wanted for a
     side-task; visible (woven into plan) is rare. Soft/loud framing
     may be upside-down here. Worth examining as part of the keymap
     revision when that thread re-opens.
10. **Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`).**
11. **Keymap revision pass.** Real-deck use surfaced that the keymap
    is starting to feel obtuse — too many global verbs to memorize,
    semantic-amplifier convention only actually applies cleanly to
    `q/Q` and `k/K` (the rest are arbitrary modal switches), some keys
    (`r`, `c`, `C`, `p`, `E`) wired to stubs that may not survive,
    plus awkward physical reaches. Tee'd up as next-priorities #1 on
    2026-04-27 with an actions-first methodology (enumerate every
    user-facing action; derive UI surfaces and keybinds from that).
    Working draft preserved at `cyberdeck-keymap-revision.md` with
    Layer 1 inventory populated. Pulled mid-design same session
    because it needs more bandwidth to do well than was available.
    Pickup next time: netrunner marks up Layer 1 (frequency, tags,
    capability gaps), AI does Layer 2 synthesis, Layer 3 keymap
    proposal lands jointly. Blocks new bindings until done — so
    planning mode (item 9) sits behind this when both unblock.
12. **Retrospective observability — the morgue + watchdog log.**
    Two paired ideas from a netrunner brainstorm: (a) a "morgue"
    persistent session log + UI that lets the netrunner browse and
    *resuscitate* past construct sessions via `--resume`, turning
    ephemeral sessions into a personal capability library; (b) a
    persistent watchdog log so Q&A history (and future tripwire
    fires) survive a deck restart. Today: finalized session_ids are
    dropped from active tracking and watchdog Q&A lives only in the
    live pane. Both are bounded-scope, follow the deck's
    files-on-disk pattern, and have obvious recovery/debugging
    value the moment they exist. Could be designed together as a
    single "deck history infrastructure" initiative. Full design
    sketch in `cyberdeck-state.md` under Not Implemented. Note:
    pairs nicely with wiring (item also called out under Routing
    in state.md) — wiring resuscitates by *piping* output into a
    new construct; the morgue resuscitates by *resuming* the same
    session_id. Different recovery paths, complementary.
13. **Quota-aware throttling.** Daemon gates spawns on remaining Max
    quota — warn or hold when the 5h or weekly window is near full.
    Mechanism: Claude Code's status-line script receives
    `rate_limits.five_hour.used_percentage` and `seven_day.used_percentage`
    on stdin; script writes them to a small JSON file; daemon reads it.
    This is the only sanctioned surface — Anthropic exposes no public
    API for Max-plan quota, and reverse-engineering the endpoint
    `/usage` calls is ToS-dicey (and yields the same numbers anyway).
    API-billing org has a clean Usage and Cost API but per-token cost
    is prohibitive vs. Max for this workload — ruled out. Cold-start
    caveat: the rate-limit fields populate only after the session's
    first model call, so quota is unknown for the first few seconds of
    a fresh session. **Pi gotcha:** on the eventual OrangePi port,
    point the quota file at tmpfs (`/dev/shm/cyberdeck-quota.json`)
    and/or write-on-change only, so high-frequency status-line ticks
    don't chew the SD card. Non-issue on Windows/SSD.

---

## Non-goals / explicit "we don't want this"

- **Inter-agent chatter as load-bearing communication.** Chatlog is
  observational, not communicative. Wirings stay limited.
- **Real-time per-construct narration via small model.** Mechanical
  beats model-narrated for glance-interpretation. Synthesis is the
  right job; narration is not.
- **Per-profile pool warming.** Pool always warms with default.
- **Built-in Claude Code tools surfaced as registry citizens.**
  They're Claude Code's surface, not the deck's.
- **Spawn-blocking on Online.** Connection consequences only kick in
  on Degraded/Offline. Online stays unconstrained.
- **Merging daemon and watchdog.** Soft/loud distinction is core.

---

## Tech debt / known unknowns (not blocking)

- Script polling at refresh time (piggybacks on registry events)
- Watchdog substrate cloud-only until D1
- Connection consequences indicator-only until M5+
- Goal-diff classifier crude stem (-es plurals mis-class sometimes)
- Read tool 25k token limit — profiles bias toward Bash+wc-l
- Long-running watchdog session accumulates context indefinitely
- Real claude `--resume` partial-turn semantics — best-effort
- Multi-file-edit footgun in chat era — Claude Code era will be
  different (in-place edits)

---

## Migration: chat → Claude Code

**Pivot point:** at ~12k LOC, multi-file edits and grep-the-codebase
operations have outgrown chat. Claude Code edits in place, runs greps
natively, doesn't suffer chat context truncation.

**Documents to bring:**
- `cyberdeck-state.md` (cumulative state through migration)
- `cyberdeck-build-plan.md` (this file)
- `cyberdeck-claude-code-orientation.md` (workflow + rules)
- `cyberdeck-spec.md` (architectural canon)
- `cyberdeck-philosophy.md` (the why)

**Post-migration shipped:**
- ✓ Profile/brake refactor (deck-global brake, hook-based enforcement)
- ✓ Brake-denial visual indicator on construct panes
- ✓ Connection consequences — spawn-blocking on Degraded/Offline
- ✓ Plugin scaffolding v1 — stateless plugins, screenshot as first

**Post-migration shipped (continued):**
- ✓ Watchdog tripwires slice 1 (2026-04-29) — deterministic matcher
  engine (`tripwires.py`), small DSL (regex + event_kinds + field
  selectors), severity tiers declared, two default deck-wide
  tripwires, full Watchdog → Fleet listener → chatlog chain.
- ✓ Watchdog tripwires slice 2 (2026-04-29) — LLM-authored tripwires
  at goal-start and non-clarification goal-update. Two-rung
  substrate: rung 1 forks the watchdog Q&A session via `claude -p
  --resume <session_id>` so authoring inherits Q&A context without
  polluting the live session; rung 2 falls back to fresh one-shot
  when no session_id is captured. Lifecycle is "clear LLM_AUTHORED,
  then register" so old-goal rules don't linger after pivots.
  Real-deck verified 2026-04-30 — rung-1 fork at 4.2s elapsed
  (vs 19.7s fresh on the same session) with no Q&A session
  collision.
- ✓ y/Y copy keybind (2026-04-30) — yank focused widget to OS
  clipboard. y = rendered text (vim-yank semantic, surface map
  matches `z` zoom); Y = structured JSON (raw events for panes,
  full bus snapshot for chatlog, dataclass dicts for list items).
  New `clipboard.py` module: ctypes Win32 / pbcopy / xclip-wl-copy
  cascade, stdlib-only. Sidesteps the Ctrl+C-as-copy
  SIGINT-into-subprocesses pain at the UX layer. Two diagnosis
  detours filed as gotchas: (1) `text=True` cp1252 default encoder
  silently exploding on Unicode then timing out, and (2) clip.exe
  preserving the UTF-16-LE BOM into clipboard contents. Also fixes
  `_serialize_payload` Path → repr leak that affected both the
  yank JSON and the per-launch .log files.
- ✓ Limits modal rework (2026-04-30) — uncapped construct counts
  (max_concurrent ceiling of 9 retired; pool_size now adjustable
  in the modal; defaults bumped to 10/30/5). Pool refill gate
  added to `_spawn_warming_task` so a lowered target stops
  oversubscribing on subsequent pulls. Latent
  `max_total_spawns == 0 → "no cap"` daemon-session guard
  finally honors what the modal has long advertised.
- ✓ Mechanic v0 — supervisor only (2026-04-30, late) — sibling
  Python process (~270 LOC `mechanic.py`) that watches the deck
  PID, tails the file logger's NDJSON stream for live claude
  subprocess pids, and kills them on detected deck death.
  Cross-platform stdlib + ctypes (no psutil), no claude
  dependency. Deck-side: `pid` on `fleet.spawn` payloads via new
  `Construct.pid` property; `pid` on `log_header` for self-
  discovery. `launch.bat` spawns mechanic in a minimized sibling
  window 1s after the deck. Real-deck verified at the attach
  level (header pid discovery, log tailing); synthetic smoke
  test verified the death-detect + cleanup path end-to-end.
  Known limitation: only constructs are tracked — daemon /
  watchdog Q&A / authoring one-shots / pool warmer subprocesses
  still orphan their pre-mechanic way (filed as a follow-up in
  Next Priorities). Full design at `cyberdeck-maintbot-design.md`.
- ✓ Spine Phase 8 — cleanup (2026-04-30, late) — retired the
  legacy `add_listener` / `remove_listener` / `on_event` /
  `on_change` / `on_state_change` shims across five producers
  (brake_state, profile_registry, plugin_registry, fleet,
  connection_monitor) plus their consumers in tui.py and
  daemon_session.py. Bus is now the only fan-out path; consumers
  subscribe via `bus.subscribe(...)` with role-derived filters.
  ~75 LOC net deletion. Three callback patterns deliberately NOT
  migrated (Pool's on_event, Daemon's on_daemon_event, Blacklist's
  on_event) — integration interfaces, not deprecated shims; filed
  as Phase 8b candidates. **The unified-event-stream slice is now
  complete (8/8 phases shipped).**
- ✓ Kill state-stuck race fix (2026-04-30, late) — both `k` and
  `Shift+K` were leaving the construct pane stuck at `[RUNNING]`.
  Race between `Construct.kill()` and `_consume`'s `wait()`:
  whichever resumed first when proc died determined whether the
  finalize event carried `state="killed"` or `state="running"`.
  Fix in `construct.wait()`: explicitly set `state=KILLED` in the
  `_kill_requested + proc died` branch. Belt-and-suspenders with
  kill()'s own state-flip. Real-deck verified.
- ✓ Safety Architecture Pass slice 1/4: MCP gating in brake_hook
  (2026-04-30, late) — verb-based pattern matching for `mcp__*`
  tools. `MCP_READ_VERBS` (20 verbs: get, list, search, describe,
  fetch, show, read, view, peek, check, validate, inspect, find,
  query, lookup, count, exists, has, is, diff) allowed under
  default. `MCP_DESTRUCTIVE_VERBS` (~70 verbs incl. execute,
  apply, send, delete, create, update, deploy, drop, merge,
  migrate, pause, restore, reset, rebase, authenticate, etc.)
  denied under default. Unknown verbs default-deny (safer to
  require explicit categorization than auto-allow new MCP tools).
  Paranoid denies ALL `mcp__*` wholesale. YOLO unchanged (no
  hook). +90 LOC in `brake_hook.py`. Real-deck verified end-to-
  end across all five paths (default+read, default+write,
  paranoid+read, yolo+read, yolo+write) against the netrunner's
  actual connected Supabase/Gmail/Drive/Calendar MCP servers.
  Per-spawn allowlist override deferred to follow-up — needs UI
  design that composes with the variable-outcome pause UX. Closes
  the widest unprotected attack surface; the LOOM v6 production
  database in particular was reachable via execute_sql until
  this slice shipped.
- ✓ Kill audit (2026-04-30 late, commit 72ee5e9) — every kill
  site (`netrunner_k`, `netrunner_shift_k`, `inject_interrupt`,
  `tripwire_critical:<name>`, `eject`, `fleet_shutdown`,
  `fleet_wedge_timeout`) now passes a source/reason label that's
  stamped on the finalize event's `kill_source` field + emitted
  as a real-time `fleet.kill_requested` bus event. Closes the
  observability gap surfaced by ~36s mystery kills in earlier
  sessions — every kill is now attributable. Real-deck verified.
  ~190 LOC across construct.py, fleet.py, tui.py, display.py.
- ✓ Tui dupe-pane fix (2026-04-30 late, commit daf6f6d) —
  `_drive_fleet` was accumulating bus subscriptions on every
  invocation (post-EJECT respawn etc.). Each rerun multiplied
  spawn-handler fires per fleet event, mounting orphan
  ConstructPanes that hung at [STARTING]. Bug latent since
  Phase 8. Fixed via subscription-handle tracking +
  unsubscribe-before-resubscribe. Defensive `_spawn_pane`
  guard logs to chatlog if a pane already exists for a cid.
- ✓ Safety Architecture Pass slice 4/4 PARTIAL (2026-05-01,
  commit 2a53e0e) — `host_restart_command` lifted from a
  construct-authored artifact into `DEFAULT_TRIPWIRES`. Now 3
  defaults ship (was 2). Still pending: shell-destructive
  baselines (rm -rf, format, dd, mkfs, fork bombs, shutdown)
  at critical severity for the pre-authoring window. Possibly
  unnecessary now that LLM authoring is consistently producing
  these patterns; re-evaluate based on real-deck pre-authoring
  exposure.
- ✓ Safety Architecture Pass slice 2/4: Tripwire escalation chain
  (2026-04-30, late) — tripwires now have teeth. Severity-driven
  escalation: low→log; warning→brake hook denies next tool call
  with description + suggestion in stderr; critical→deny + tui
  handler calls `fleet.kill_construct` via `run_worker`;
  critical+bad_enough→same plus auto-blacklist proposal (action
  deferred to slice 3 for approval window). Mechanism:
  TripwireEngine writes `<home>/.cyberdeck/spawns/<cid>.
  deny_pending.json` on warning/critical fires; brake_hook reads
  + clears the file at every invocation. Race mitigation: 100ms
  recheck on write-class tools (Read/Glob/Grep skip — no latency
  penalty for the common case). New Tripwire schema fields:
  `description`, `suggestion`, `bad_enough`. Authoring prompt
  rewritten to forbid the depth-of-defense antipattern (no more
  `rm(?!\s+-rf)` negative-lookahead skipping). brake_state.
  make_spawn_settings now passes construct_id as second argv arg
  to the hook. Logger dumps blacklist on close per netrunner ask
  — cross-run analysis can spot recurring fingerprints. Real-
  deck verified end-to-end via cx-279d4ae8 bait construct: 4
  critical tripwires fired simultaneously on one Bash echo, all
  4 logged to chatlog with red-bold styling, brake hook denied
  with new message format quoting the tripwire name, construct
  auto-termed via bus subscriber. Authoring confirmed producing
  6 well-shaped patterns including bad_enough on shell-
  destructive baselines (rm_rf, format_disk, dd, fork_bomb,
  shutdown). +~350 LOC across tripwires.py, brake_hook.py,
  brake_state.py, watchdog.py, tui.py, logger.py.

**Spine progress (2026-04-30): 8/8 phases shipped (COMPLETE)** —
see `cyberdeck-event-stream-design.md`. Producer migration (Phase
1-5) plus chatlog reader migration (Phase 6) plus file logger +
quit discipline (Phase 7a/7b) plus listener-shim cleanup (Phase 8).
Cumulative result: every event source on the deck publishes
through one canonical bus; bus.snapshot() is the single source of
truth for chatlog readers; per-launch NDJSON files in `<deck
source>/logs/` capture everything with self-describing header +
footer; Ctrl+C-as-copy stops killing the deck (parent SIGINT
swallow); smart Ctrl+Q with running-state guard; producers no
longer maintain their own listener fan-out paths.

**Next priorities (after 2026-05-02 session):**

The safety architecture pass is now 4/4 complete; the cache-cost
+ tripwire-race fixes also landed; three discrete bug fixes
shipped. The queue resets to:

0. ~~**🚨 WEDGE-TIMEOUT DIAGNOSTIC GAP**~~ ✅ SHIPPED 2026-05-01
   (commit f3f6f2d). Construct.wait()'s TimeoutError handler
   drains stderr with a 2s ceiling before kill; stderr_excerpt
   on finalize when kill_source=fleet_wedge_timeout; configurable
   wedge_timeout_seconds via Limits modal. Real-deck verified.

0a. ~~**🔥 SAFETY PASS slice 3 phases 1+1.5+2**~~ ✅ SHIPPED
    2026-05-01 / 02 (commits e4981b0, e33ec75, c4e19cb, f97d1af,
    6c6de8e, 2ed51c9). See state.md for the full delivery shape.
    The pass is closed.

0b. ~~**Cache-cost fix (cache_miss_reason: system_changed)**~~ ✅
    SHIPPED 2026-05-02 (commit 1dea7f7). Stable spawn settings
    file + session_id → cid lookup at hook runtime. Real-deck
    verified: system_changed misses gone.

0c. ~~**Tripwire-authoring spawn race**~~ ✅ SHIPPED 2026-05-02
    (commit 8632b00). DaemonSession awaits authoring completion
    before first spawn batch. Real-deck verified.

0d. ~~**Discrete bugs cluster (enum payloads + daemon over-
    volunteer)**~~ ✅ SHIPPED 2026-05-02 (commit 60b91aa).

1. **TOOLS / PLUGINS / PROFILES RETOOL** (filed 2026-05-02 —
   `Design Files/cyberdeck-tools-plugins-profiles-retool.md`,
   commit de22d58). Three-way clean separation: tools = registered
   CLI (binaries on PATH or scripts at listed paths), plugins =
   deck-extended capability (folders in DECK SOURCE, not home;
   brake hook protects them), profiles = recipes (default prompt
   + tools list, plugins daemon-wide-decided per spawn). 4-5
   phases, ~600 LOC.

   - **✅ P1 (tools registry + hot-reload + missing-tool grey-
     out)** shipped 2026-05-03. New `tools.py` + `tools_registry.
     py`; mtime-watch over `<home>/tools/tools.toml`; bus events
     `tool.added` / `tool.changed` / `tool.removed` / `tool.
     unavailable` / `tool.scan_error` / `tool.scan_complete`;
     existence check via shutil.which / Path.exists; default-
     seeded tools.toml with inline schema docs; TOOLS section in
     the Tools tab with ⚙/⌬ kind glyphs + red-✗-when-unavailable.
     Existing SCRIPTS section preserved (P5 collapses both).
     ~520 LOC + ~120 LOC tui.py wiring. Real-deck verification
     pending.
   - P2 — move plugins to deck source + bridge dispatcher
     (~120 LOC) — next.
   - P3 — `load_into_deck(app)` hook (~80 LOC).
   - P4 — profile schema migration: `recommended_tools` → `tools`
     (~100 LOC + daemon prompt rewrite).
   - P5 — UI retool: Profiles its own tab, unified Tools tab
     (~150 LOC).

2. **REMAINING DISCRETE BUGS** (per netrunner direction "that
   shit is expensive"):
   - Construct refusal text → structured `kind=construct.refused`
     bus event. When claude itself refuses (model layer, not
     brake hook), the rich refusal narrative lands as result.text
     rather than a distinct event. Watchdog Q&A and chatlog
     can't distinguish refusal from completion.
   - Kill doesn't interrupt in-flight assistant turns — SIGTERM
     lands AFTER model finishes turn (real money + observable
     output continues post-kill). Stopping the model itself
     requires stdin-injection or stream interrupt; design
     alongside future inject-and-interrupt v2.
   - Silent wedge investigation (cx-796e0468 case, real-deck
     2026-05-02: empty stderr_excerpt on a wedge timeout —
     useful negative info, but needs more data points to
     characterize).

3. **Caliber selection** (`cyberdeck-model-effort-design.md`).
   Per-spawn model + effort + fast-mode bundle. Five phases;
   phase 4 hard-blocks on quota-aware throttling (item 13
   below), the rest are independent.

4. **Daemon narrative fix** — daemon mislabels brake-hook
   denials as tripwire fires.

5. **Log-readability overhaul** — fleet/chatlog/watchdog/daemon
   surfaces scattered; structural rearrangement.

6. **Mechanic v1** — LLM session half (diagnose-only on-demand
   triage). v0 supervisor shipped 2026-04-30. Liveness
   heartbeat (mechanic v0→v1 bridge filed 2026-05-01) is the
   natural first sub-slice.

7. **Spine Phase 8b** — Pool + Daemon callback cleanup (low-
   priority cleanup; integration interfaces, not deprecated
   shims).

8. Connection consequences round 2 — daemon parking on connection-
   blocked spawns + recovery flow.

9. Tripwires slice 3 (severity-aware rendering — critical pulls
   focus, warning badges, low logs only). Composes with slice 3
   of safety pass already shipped.

(Old "🔥 SAFETY ARCHITECTURE PASS in progress" block kept below
for historical reference.)

OLD-STATUS. **🔥 SAFETY ARCHITECTURE PASS (in progress — 2.25/4 shipped).**
   Composable set of slices addressing the structural truths
   surfaced by 2026-04-30 late real-deck testing + log analysis:
   **brake hook is doing 95% of safety work alone**; tripwires
   were observation-only stubs until slice 2 wired the escalation
   chain; profiles are pure prescription with zero security
   weight; if brake misses a pattern nothing else stops it.
   See `cyberdeck-state.md` "Safety architecture analysis"
   section for the full layer breakdown + intended-vs-today
   comparison.

   Slice progress:

   - ~~**(a) MCP gating in `brake_hook.py`**~~ ✅ shipped
     2026-04-30 (late). Verb-based pattern matching landed; +90
     LOC; real-deck verified end-to-end. Per-spawn allowlist
     override deferred to compose with slice (c). See "Post-
     migration shipped" entry above.
   - ~~**(b) Tripwire escalation chain**~~ ✅ shipped 2026-04-30
     (late). Tripwires gained teeth: low→log; warning→brake
     denies next call with suggestion; critical→deny + auto-term
     via tui handler; critical+bad_enough→same + blacklist
     proposal (deferred application). Real-deck verified —
     cx-279d4ae8 bait construct fired 4 critical tripwires on
     one Bash echo, all logged + auto-termed cleanly; authoring
     prompt rewrite produced shell-destructive baselines with
     bad_enough flags. +~350 LOC across 6 files. See "Post-
     migration shipped" entry above.
   - ~~**(c) Variable-outcome delay UX phase 1**~~ ✅ SHIPPED
     2026-05-01. Renamed pause→delay (pause is reserved for the
     daemon-pause feature; this is a timed-default thing). Z→X:
     X is the deck-wide approval/execute key (mnemonic: **X-ecute**),
     bidirectional by context (under default/paranoid X approves
     a deny-default; under YOLO X interrupts an allow-default).
     Phase 1 delivers: brake_hook delay mechanism (write
     `<cid>.delay_pending.json`, poll for `<cid>.delay_override.
     json`, apply default-or-override per brake matrix), YOLO
     hook-install lifted when delay > 0, new `brake_delay.py`
     module (DelayMonitor + DelayEntry + DelayResolution +
     write_delay_override + read_active_delays), per-pane delay
     overlay with EJECT-style countdown bar + bold "(Running |
     Redirecting) in Xs" + "press X to (block | approve)" hint,
     Delays right-panel tab listing all pending delays with
     focusable rows, X keybind that resolves to focused pane's
     delay → Delays tab cursor → sole-pending convenience,
     Limits modal `delay_window_seconds` field (default 0 = no
     delay). Chatlog markers + bus events
     (`brake.delay_opened` / `brake.delay_resolved`) round it
     out. ~600 LOC across construct.py is untouched, brake_state.py,
     brake_hook.py, brake_delay.py (new), tui.py.

   - ~~**(c) Phase 1.5: persist limits tunables across deck
     restarts**~~ ✅ SHIPPED 2026-05-01. Both `delay_window_
     seconds` and `wedge_timeout_seconds` now persist in the
     same `<home>/.cyberdeck/state.json` file as brake state,
     under a sibling `limits` namespace. New helpers in
     `brake_state.py`: `load_limits(state_path) -> dict` reads
     the namespace at App startup; `save_limits(state_path,
     **values)` does a read-merge-write on Limits modal submit.
     BrakeStateStore._save extended to preserve sibling keys
     so brake saves and limits saves don't clobber each other.
     Round-trip tested. max_concurrent / max_total_spawns /
     pool_size deliberately NOT persisted — they're session-
     scoped (netrunner sets caps for this goal; next session
     may want different caps). +~80 LOC across brake_state.py
     + tui.py.

   - ~~**(c) Phase 2: blacklist-proposal composition + attention-
     needed area**~~ ✅ SHIPPED 2026-05-01. New `attention.py`
     module: AttentionItem dataclass + AttentionKind / Attention
     Resolution constants. New AttentionPanel widget at the top
     of #main (heavy magenta border matching the per-pane delay
     overlay's "time-sensitive" semantic; collapses to height 0
     when empty). App-level state + lifecycle helpers (_open_
     attention, _approve_attention, _resolve_attention) with
     deck-owned asyncio timers (no hook polling — distinct from
     the brake-hook delay flow). _handle_tripwire_fire extended:
     critical+bad_enough fires now build a BlacklistEntry from
     the firing construct's context (mirroring action_hard_kill_
     focused) and file it as an attention item. action_x_focused
     extended with a third resolution rule: focused-pane delay →
     sole-pending delay → most-recent attention item → toast.
     Default proposal window: 30s (long enough to read + decide,
     short enough to not pile up). Bus events:
     attention.opened / attention.resolved with reason field
     (approved | expired | dropped). Both x and Shift+X bound to
     the same action_x_focused. ~400 LOC across attention.py
     (new) + tui.py.
   - **(d) DEFAULT_TRIPWIRES expansion + authoring prompt fix**
     PARTIAL ✅. Authoring prompt antipattern guard shipped
     with slice 2. `host_restart_command` (warning, with
     suggestion) shipped 2026-05-01. Now 3 defaults ship.
     Still pending: shell-destructive baselines (rm -rf,
     format, dd, mkfs, fork bombs, shutdown) at critical
     severity. Possibly unnecessary now — real-deck 2026-05-01
     confirmed LLM authoring is consistently producing these
     at critical+bad_enough on every goal-set, so the pre-
     authoring window may be too short to matter. Re-evaluate
     after slice 3.

   Plus discrete bugs / observations from log analysis to land
   alongside the cluster:
   - **Enum payloads serialize as empty `{}`** in `_serialize_payload`
     (3-line fix: `isinstance(payload, Enum)` check before
     `__dict__` walk).
   - **Kill doesn't interrupt in-flight assistant turns.** Kill
     SIGTERM lands AFTER model finishes turn — token cost +
     observable output continue post-kill. Stopping the model
     itself requires stdin-injection or stream interrupt.
   - **Daemon over-volunteers destructive content** (real-deck:
     netrunner asked rm-rf-style test, daemon also added
     `shutdown -h now` unprompted). Tighten daemon system prompt.
   - **Construct refusal text buried in result event.** New
     `kind=construct.refused` for distinct safety signal.
   - **~30k token cache miss per spawn** (`cache_miss_reason:
     system_changed`). Per-spawn system prompt drift; investigate
     alongside caliber work.
2. **Model + effort selection — "caliber" per spawn.** The daemon
   picks `--model` and `--effort` per construct based on task
   needs and remaining quota; the daemon's own caliber is markable
   and netrunner-overridable (CLI flags, Limits modal, daemon
   chat). Three independent axes (model, effort, fast-mode)
   bundled as a `Caliber` dataclass. Pool stays single-caliber
   (default sonnet+high); non-matching daemon-picked spawns fall
   through to fresh — same pattern as non-default profile spawns.
   Five phases: phase 1 (caliber primitive + per-spawn plumbing),
   phase 2 (pool caliber + reuse), phase 3 (daemon caliber +
   override), phase 4 (quota-aware fallback — HARD-BLOCKED on
   item 14 below), phase 5 (UI polish + introspection). Phases
   1-3 + 5 are shippable independently of quota awareness. Full
   design at `cyberdeck-model-effort-design.md`.
3. **Daemon narrative fix — mislabels brake-hook denials as
   tripwire fires.** Daemon's narrative conflates the two distinct
   safety layers. Real-deck observed: daemon said "Tripwire fired
   cleanly — PreToolUse hook denied the write" — when the actual
   mechanism was the brake hook, no tripwires were involved.
   Tighten daemon system prompt or outcome-format to distinguish
   brake (`permission_denials` field on the result event,
   rendered as `· brake blocked: Write×N`) from tripwires
   (`tripwire.fire` events, rendered as `[tripwire]` chatlog
   lines). Composable with the safety architecture pass — the
   distinction matters more once tripwires actually escalate.
4. **Log-readability overhaul** — fleet/chatlog/watchdog/daemon
   scattered across windows is hard to follow at a glance; needs
   structural thinking, not just CSS. Distinct from the file-log
   work in the spine slice (this is in-deck UI composition, not
   file shape). Composes better post-spine — display surfaces
   become "subscriber + filter + formatter" units, easier to
   rearrange.
5. **Mechanic v1 — LLM session half.** Diagnose-only on-demand
   triage. Activates on heartbeat-fired unclean exit OR netrunner
   summon. Cloud Claude substrate; D1 eventual. Mechanic v0
   shipped 2026-04-30 — the supervisor process exists; v1 attaches
   the LLM session half to it. Ideally blocked on D1 (cost profile)
   but cloud Claude works as an interim substrate. Full design at
   `cyberdeck-maintbot-design.md`.
6. **Mechanic v0 follow-ups — track non-construct subprocesses.**
   Daemon, watchdog Q&A, watchdog authoring one-shots, and
   pool-warming subprocesses don't publish pids to the bus today;
   they orphan the same way they did before mechanic existed. One
   line per spawn site to add a `pid` field; one elif per source in
   `mechanic._apply_record` to track them. Trivial; defer until
   real-deck use surfaces a concrete orphan from one of those
   sources.
7. **Spine Phase 8b — Pool + Daemon callback cleanup.** Three
   callback patterns survived Phase 8 because they're integration
   interfaces, not deprecated shims: SessionPool's `on_event`
   (publishes pool events to the bus from inside the handler —
   migrating inverts producer/consumer), Daemon's
   `on_daemon_event` (same shape), Blacklist's `on_event` in
   watchdog.py (wired through the watchdog's own integration
   surface). Migrating the first two to direct bus publishing
   would complete the spine; Blacklist stays as the watchdog's
   internal channel. Low-priority cleanup.
8. Connection consequences round 2: daemon parking on connection-
   blocked spawns + recovery flow.
9. Tripwires slice 3 — severity-aware rendering (critical pulls
   focus, warning badges, low logs only). Severity tiers are
   already in the DSL; just need the visual routing. Composes with
   safety architecture pass item 1(b) — once tripwires escalate,
   the visual tier matters more.
10. Tools-research chat (from `cyberdeck-tools-research-seed.md`).
11. Plugin sub-features: airgap `p`, quickfire `c`, picker
    `Shift+C`.

(Keymap revision pass was tee'd up here on 2026-04-27 but moved to
deferred mid-design — needs more brain cells to do well than were
available that session. Working draft preserved; see deferred list.)

---

*This file is the source of truth for milestone ordering. When in
doubt, this beats memory.*
