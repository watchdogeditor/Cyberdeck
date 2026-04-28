# Cyberdeck

A local orchestration client for coordinating multiple AI agents through a keyboard-first, one-hand-operable interface. Runs on modest hardware. Delegates heavy lifting to remote model APIs. Grows capability by adding interface plugins, saved scripts, and reusable construct profiles.

---

## Vision

The netrunner fantasy from *Edgerunners* — multiple windows, each making progress on something, a human steering the whole thing — is achievable now, minus the brain interface. What's missing isn't compute or model capability; it's **supervision bandwidth**. One human can spawn twenty agents trivially. One human cannot watch twenty agents trivially.

The cyberdeck's job is to make human attention go further than it should.

**Thesis: express intent, delegate mechanics.** The deck has excellent UI for expressing what you want. Mechanics — finding the API, writing the script, executing the call — are the agents' problem.

**Secondary thesis: capability accumulates.** Anything the netrunner does well once should become a saved tool the deck can do cheaply forever. The deck is a *personal capability library that grows with use*.

This is a personal project. Adoption is not a concern. Defaults can be opinionated. Configuration can be "edit the TOML and if it breaks that's a you problem." If it lands on GitHub eventually, it lands on GitHub.

---

## Architecture

### Roles

- **You** — the netrunner. Sets goals, steers in real time, intervenes when drift occurs.
- **Daemon** — the always-running coordinator. Takes your goal, decomposes it into subgoals, spawns constructs, routes messages between layers, holds canonical intent. Reactive by nature: condition → response. Named per cyberpunk canon (slightly self-aware software that runs other code in response to conditions).
- **Constructs** — task-focused workers. Each gets one subgoal, its own context window, its own scoped tool access. Hermetic by default; can be explicitly wired to other constructs via a directed graph.
- **Watchdog** — observational overseer. Reads all construct and daemon streams. Authors tripwires at spawn time and when intent shifts. Does not act; fires events with severity and suggested action. Two-eyes check on the daemon. Also serves as a queryable oracle — see *Watchdog as oracle* below.

### Separation of concerns

The entity noticing problems must not be graded on whether its plans work. Daemon and watchdog are intentionally separate roles. Collapsible into one process for cheap tasks where the tradeoff is acceptable; separate by default.

### Supervision model

Autonomy is **not** a function of model confidence alone (models are often most confident exactly when they're wrong). Autonomy is a function of **confidence × action risk**:

| Action class | Default policy |
|---|---|
| Reads, idempotent queries | Auto-execute at medium+ confidence |
| Writes to scoped state | Prompt for ack |
| Destructive / irreversible | Always confirm |
| Network egress, credential use, remote state changes | Always confirm |
| Plugin / hardware actuator calls | Subject to brake profile *and* hardware airgap (see below) |

Risk classes are per-action-type, not per-command. The deck-global **brake state** (`Paranoid / Default / YOLO`) determines how restrictive the runtime gating is — paranoid blocks all side-effect tools wholesale (investigate-only mode), default blocks destructive bash patterns + writes to OS-root paths and the deck source dir, yolo installs no hook at all. The netrunner sets the brake via the `b` modal (paranoid is single-press; yolo is an EJECT-style 3-second held-key gesture). The watchdog can ratchet *up* (toward paranoid) but never down — that's exclusively the netrunner's call.

Enforcement happens at Claude Code's `PreToolUse` hook layer: each new construct spawns with a per-construct `--settings` JSON pointing at `brake_hook.py` parameterized by the current brake. The hook is deterministic (regex/path matching, no LLM in the hot path); the watchdog observes denials via the `permission_denials` field on result events and authors hook policy over time (LLM authors, deterministic enforces).

The hook gates both `Bash` and `PowerShell` tools equivalently — Claude Code on Windows exposes PowerShell as a separate tool with the same `command` shape, and a construct denied one shell will silently pivot to the other if the gate is asymmetric. This is a general principle for any tool-gating layer: gate equivalent capabilities, not specific tool names.

### Tripwires

The watchdog uses LLM capability to *author* tripwires but does not put the LLM in the hot path of detection.

- Model writes rules at spawn or intent-shift: expected phrases, error patterns, silence thresholds, drift indicators.
- Rules execute via deterministic matchers (regex, grammar, timers).
- Sub-millisecond reaction. No hallucination risk. Tripwires are inspectable artifacts you can edit, version, and share across tasks.
- The daemon hints severity when delegating ("this step is blocker-critical"), giving the watchdog priors rather than asking it to infer severity purely from symptoms.

### Construct communication

Default: constructs talk *through* the daemon. Explicit wiring allows direct channels between specific constructs (directed graph, not mesh). Wired pairs get a turn budget to prevent spiraling.

### Tool registry

The deck's capability ceiling is the union of its registered tools. Three types, all addressable by the daemon:

- **Plugins** — code in the deck that exposes hardware or external APIs. In-process. Examples: camera, IR blaster, NFC, MCP servers, filesystem connectors, SSH, HTTP client.
- **Scripts** — standalone executables on disk (`.sh`, `.py`, anything runnable). The deck does not constrain their internals; that's the netrunner's responsibility. Constructs invoke them via Bash. Each script has a small declarative manifest: name, category, args, expected output shape. Scripts are the primary way a working pattern from a session graduates into a permanent capability.
- **Profiles** — saved Construct configurations. TOML files on disk (one per profile) with the following shape:

  ```toml
  name = "recon_specialist"
  category = "Recon"
  description = "Network and wireless reconnaissance work."

  default_daemon_addendum = """
  When this profile is active, prefer scripts over fresh tool composition.
  Use SHRDLU-style targeting language. ...
  """

  default_construct_addendum = """
  You are operating in a recon context. ...
  """

  recommended_tools = ["Bash", "Read", "WebSearch"]
  default_scripts = ["scan_wifi", "geolocate_subject"]
  ```

  Addendums are *appended* to the deck's baseline daemon and construct system prompts when the profile is active. They steer behavior; they do not replace foundation. `recommended_tools` surfaces in the construct's system-prompt addendum as a soft suggestion ("for this kind of work, prefer X / Y / Z"); the construct still has access to the full default tool set. **Profiles are prescriptive templates, not enforcement** — runtime tool gating is the brake hook's job (see *Brake state* below). The daemon picks a profile *before* writing a task — pick the right tool, then decide what to do with it.

  *Refactor note:* earlier drafts of this spec described `allowed_tools` as a hard cap and `brake_profile` as a per-profile field with two-axis privesc gating. Both were dropped during the brake refactor. Brake state is now deck-global and netrunner-controlled; tool narrowing is delegated to the brake hook regardless of profile.

All three types live in a hierarchical registry on disk, browsable as a folder tree. The registry emits events when tools are added, changed, or removed; the daemon subscribes and learns about new capabilities the moment they're saved. Profile file changes also notify the netrunner if pre-warmed sessions of that profile are now stale (see *Session pool* below).

A single principle covers all three: **tools are personal-knowledge-capture made addressable.** Anything you do well once should become callable cheaply forever.

### Goal pane, two layers

- **Your goal** — source of truth. What you typed. Editable at will. Edits are diffed against the previous version, classified as *clarification / scope-change / pivot*; only material changes propagate. Committed edits propagate to constructs at next natural break (outcome turn). Force-push available via explicit gesture for "now."
- **Current subgoal** — read-only. Daemon's current step within its decomposition. Glance check for drift.

### Persistence

- Run logs are **append-only and annotated**: tripwire fires, daemon decisions, user injections, construct spawns/deaths, tool registry changes — all correlated with timestamps and run IDs.
- A **session manifest** records every spawned subprocess and its session_id, updated continuously as sessions advance. Used by the recovery flow (see *Connection state* below).
- Sessions are autopsy artifacts, not durable workflows. The deck offers *opportunistic resume* on relaunch — best-effort, graceful-fail. If resume succeeds, work continues; if it fails, the affected constructs are marked failed and the deck proceeds to a fresh idle daemon. No retry storms, no ambitious recovery semantics.
- Root goals, configs, plugin state, and the tool registry persist as files on disk.

### Connection state

The deck does not assume always-on connectivity. A Pi-class deck on the move loses connection routinely: Wi-Fi handoffs, cellular dead zones, deliberate radio reconfiguration (monitor mode), and physical environment (tunnels, faraday-ish buildings). The deck has three connection states:

- **Online** — full capability. Daemon active, constructs spawn freely against remote models.
- **Degraded** — connection unstable or in transition (sim handoff, monitor mode active, intermittent packet loss). Remote-model spawns blocked; in-flight constructs allowed to finish or fail naturally; daemon paused. Local-model constructs (deferred) continue to work.
- **Offline** — no connection at all. Remote-model work blocked; daemon parks. Local-model constructs continue.

A connection monitor watches reachability via cheap heartbeats to `api.anthropic.com` and via `EAI_AGAIN`/`ECONNRESET` patterns in subprocess stderr. State transitions broadcast events; sidebar status reflects current state at all times.

#### Recovery flow

When the connection returns (or the deck relaunches), the recovery flow classifies sessions from the manifest and acts per the netrunner's brake profile:

| Session state | Default | YOLO | Paranoid |
|---|---|---|---|
| `clean_finish` (already done before drop) | Auto-recover, silent | Auto-recover, silent | Prompt |
| `mid_stream` (was active when connection died) | Prompt: resume / discard / inspect | Auto-resume, silent | Prompt |
| `pre_init` (no session_id ever issued) | Discard, silent | Discard, silent | Prompt |

Resume is best-effort: `--resume` semantics for partial turns aren't fully deterministic, so the deck flags resumed-mid-stream constructs in the UI ("output may differ from pre-disconnect state"). This is a hobby-deck tradeoff; production agents would need stricter semantics.

The recovery modal opens automatically on `connection_restored` if the manifest contains anything to recover. F flips between "resume all / discard all / decide individually." Esc treats as "decide later" and leaves the manifest intact.

### Network topology (target)

The deck's connection model is *not* "I have an internet connection"; it's *"I have N independent network interfaces, each with capabilities and constraints."*

| Tier | Role | Notes |
|---|---|---|
| **Primary** | API connectivity, always preferred | Cellular (LTE/5G + SIM) when hardware lands; Wi-Fi (onboard) until then. Wi-Fi pre-empts cellular when an active free network is connected. |
| **Secondary** | Tool radio | USB Wi-Fi adapter (Alfa-class), freely reconfigurable: monitor mode, AP scan, deauth, etc. Distinct physical device from primary so radio operations don't kill API connection. Optional in v1. |
| **Tertiary** | Failover | Pi onboard Wi-Fi when cellular is primary. Captive portal navigation, low-power mode. |

Plugins that touch the network declare which interface they want. Default `interface=auto` resolves to the secondary tool radio for radio operations; the primary is implicitly off-limits for tool use. A tripwire-class rule enforces "never put the primary radio into monitor mode" by default.

Hot-swap awareness: USB radios get yanked, replaced, lose power. The deck's network monitor handles interface enumeration changes gracefully; plugins requesting a missing interface fail with a clean error rather than crashing.

#### Network state in v1

Until cellular and tool-radio hardware land, the deck operates with a single onboard Wi-Fi interface, which is both the API channel and (eventually) the recon target. This means recon plugins are *deferred along with multi-radio hardware* — putting the only Wi-Fi card into monitor mode while the daemon is mid-turn is self-inflicted DoS. The spec captures the target topology so that capability lands cleanly when the hardware does.

---

## Operational model

How the deck *behaves* once you turn it on. Architecture above is what exists; this section is how it moves.

### Daemon lifecycle

The deck is always-on. The daemon mirrors that:

- **Idle** — waiting. No active goal, no constructs spawned. The daemon pane shows `[IDLE]` with an affordance to set a goal.
- **Working / waiting** — a goal is active. Daemon decomposes, spawns, observes outcomes, iterates.
- **Done / failed** — terminal state for the current goal. Daemon pane shows the result and an affordance to return to idle for the next goal.

Goal completion does not kill the app. The daemon returns to idle; the deck stays running. Setting a new goal spins a fresh session.

### Session pool

The deck pre-warms a small pool of construct sessions at app launch so the daemon's first spawn doesn't pay full cold-start cost. This is *initialization optimization*, not session continuity — distinct from connection-loss recovery, even though both touch the session manifest.

**Composition:**
- 3 default-profile construct sessions, pre-warmed at app launch (configurable; tune up if cold-start latency becomes annoying).
- No daemon pool. The deck only ever has one active daemon, cold-started on first goal.

**Refill:** when a construct is pulled from the pool, another starts warming in the background. The visible "Constructs Available" RAM-meter (see *UI → Status indicators*) shows current pool fill and its replenishment.

**Non-default profiles:** rather than maintain N pools for N profiles, the deck pulls a default-profile warm session and *injects the profile's addendum* as part of the first user message. The warm session has the bare-baseline system prompt; the profile's `default_construct_addendum` rides on the first turn's content. One pool serves all profiles.

**Profile-change invalidation:** the registry's filesystem watch detects edits to active profile files. When a profile changes, the deck notifies the netrunner ("profile X edited; refresh N warm sessions?") and acts on the response. Discarded sessions trigger a refill.

**Cross-restart reuse:** the session manifest persists pool sessions across restarts. On launch, the deck attempts to reuse non-stale entries before spinning fresh ones. Stale-by-timestamp sessions (default: >24h old) are discarded silently. If reuse becomes problematic in practice, the deck drops the feature and accepts the cold-start hit.

**Resource cost:** pre-warmed sessions hold server-side state but do not keep subprocesses alive — Claude Code's `-p` mode exits after each turn. The local RAM cost is the manifest entry plus any in-flight warming subprocess. The remote cost is whatever Anthropic charges for empty session storage (currently nothing). The deck is therefore *not* a RAM annihilator at rest; warming is the spike, idle pool is cheap.

### Construct visibility

Every construct is either visible or invisible to the daemon:

- **Visible constructs** — daemon-spawned or user-spawned-toward-goal. The daemon sees their outcomes, counts them against the active session's caps, weaves them into its plan.
- **Invisible constructs** — user-spawned side-quests. The daemon does not see them, does not count them, does not adapt to them. Useful for "I want to do this thing in parallel without disrupting the daemon's plan."

Invisible constructs have their own concurrency cap (default: 3) so an enthusiastic netrunner can't accidentally exhaust quota with side-quests. Their cost and tokens roll into the session totals so the budget signal is honest, but they're visually distinguished in the construct pool.

### Limits

The deck's caps are user-configurable runtime governors, not silent guillotines:

- `max_concurrent` (visible) — peak parallel daemon-driven constructs. Default: 5. Hard ceiling: 9 (matches the keymap's number-key real estate; beyond this, the supervision model breaks).
- `max_concurrent_invisible` — peak parallel side-quest constructs. Default: 3.
- `max_total_spawns` — cap on total constructs per goal. Default: 20.
- `token_cap`, `cost_cap` — optional per-session budget ceilings.

When any cap is hit, the daemon **pauses** rather than halts. The Limits modal opens with the daemon's current state and the option to *adjust the cap and continue* or *terminate the session*. The deck never auto-closes; the netrunner decides.

This is distinct from the daemon's normal operation. The daemon itself is *quota-aware, not quota-paranoid* — it does not pre-throttle. User-set caps are a separate layer for "if it happens anyway, here's what to do."

### Emergency controls

**EJECT** — the everything-stops button. Triggered by `Ctrl+F` held for 500ms (deliberate consent — see *Gestures* below). On engage:

- Every subprocess receives SIGKILL. No grace period.
- Daemon subprocess killed.
- Fleet queue drained, async tasks cancelled.
- A snapshot is written to disk: fleet state, last N events per construct, daemon session history. Filename: `ejected-{run_id}.json`.
- UI goes red-bordered, shows `EJECTED` with the snapshot path.
- Two options: return to goal-select screen, or quit.

EJECT is a safety system, not a UX action. Recovery is not a feature; the feature is *stopping*. The snapshot exists for postmortem, not resume.

**Hardware airgap** — `p` toggles all plugins on or off. Above the brake profile in the privilege hierarchy:

```
HARDWARE AIRGAP (deterministic)    ← p toggles
  Plugins enabled? Y/N
BRAKE PROFILE (per-action policy)  ← user-set
  Paranoid / Default / YOLO
DAEMON / CONSTRUCT REASONING       ← models
  "Should I do this?"
```

Each layer is a stricter veto than the one above. The airgap is enforced in code the model cannot reach. With plugins off, every plugin call (netrunner-initiated or daemon-initiated) is rejected. In-flight plugin work is aborted unceremoniously; restoring the airgap does not auto-resume anything.

Airgap UI follows hybrid drama: first toggle in a session is a visible alert ("AIRGAPPED — no physical-world side effects"), subsequent toggles are quiet status-bar updates. EJECT is full drama every time.

### Hard-kill blacklist

`Shift+K` is the loud variant of `k` (soft kill). Where soft kill says "not like that," hard kill says "not at all":

- Construct is killed immediately.
- The task pattern is added to the blacklist for the active session.
- The blacklist propagates to the Watchdog (when implemented), which authors a tripwire to refuse future spawns matching that fingerprint.
- If the daemon's plan depended on the blacklisted pattern, it halts and asks for direction rather than retrying.

The blacklist lives with the Watchdog because the Watchdog is the *persistent memory of what's forbidden*. The Fleet executes; the Daemon plans; the Watchdog remembers.

### Watchdog as oracle

The Watchdog has spare cycles between tripwire fires. Those cycles are spent answering the netrunner's questions:

- The netrunner can ask the Watchdog anything via `t` (talk-to-watchdog). Async queue: questions stack up, the Watchdog answers when it has bandwidth.
- The Watchdog has read-only access to the full event stream — it knows what's happening at any moment.
- Watchdog answers cannot affect plans. They're informational. This is *why* it's the right component for Q&A: asking it a question can never derail execution.

The Daemon is for *changing what we're doing* (`T` — talk-to-daemon, sync, expensive). The Watchdog is for *understanding what's happening* (`t` — async, casual, free). Two distinct conversation partners with two distinct purposes.

### Injection semantics

Two distinct verbs for user intervention into a construct:

- **Inject-and-interrupt** (`Q`) — kill current construct work, inject new input, resume with summarized prior context + redirect framing. The construct knows it was steered, not rebooted. Timing-critical; hence its own key.
- **Queue-inject** (`q`) — deliver message at next natural break; notify daemon so routing doesn't get confused. The polite version.

Same modal for both, but the delivery mode is pre-selected by which key opened it. F flips between modes inside the modal if the netrunner changes their mind mid-thought.

### Plugin quickfire

Plugins that have a sensible default action expose a *quickfire*: one keypress, capture happens, modal opens for context and routing.

- `c` fires the active plugin's quickfire.
- `Shift+C` opens a picker showing numbered plugin entries; pressing a number sets the active plugin.
- Active plugin shown in the status bar at all times.
- Plugins without a sensible default (filesystem, SSH, generic HTTP) don't define a quickfire. They appear in the Tools panel but C does nothing for them.

After a quickfire, the capture modal:
- Previews the captured artifact (image, audio waveform, sensor reading, etc.).
- Accepts text context from the netrunner.
- Picks a destination: Daemon, focused construct, picked construct, or a new construct.
- F flips destination, Space/Enter sends, Esc cancels.

### Daemon-initiated plugin use

The daemon can call plugins directly via a new action type:

```json
{"type": "tool_call", "tool": "plugins/camera/capture", "args": {}}
```

Subject to brake profile and airgap, just like netrunner-initiated calls.

The daemon can also **request a capture from the netrunner** when its reasoning hits a "I'd need to see this to know" moment. The request appears in the daemon pane as a passive prompt: *"Daemon wants you to take a picture: 'show me what error message is on the screen.' [Press C to comply, Esc to deny.]"* Severity escalates from passive (low urgency) to flash + sound (high urgency) per the existing tripwire severity model.

### Gestures

Patterns that show up across multiple commands:

- **Soft/loud modifier grammar** — `Shift` is a *semantic amplifier*, not just a safety modifier. `q`/`Q` are queue-inject vs interrupt-inject; `k`/`K` are soft kill vs hard kill + blacklist; `t`/`T` are talk-Watchdog vs talk-Daemon. Same verb, different gravity.
- **Deliberate consent** — for actions whose cost makes a quick tap dangerous, the deck requires a *held* keypress (~500ms) instead of a press. A progress bar fills during the hold; release before completion cancels harmlessly. Used for: EJECT, plugin approval in Paranoid mode, brake profile escalation.
- **Hybrid drama** — first invocation of a consequential-but-routine toggle in a session is visually loud; subsequent invocations are quiet. Trains the muscle memory while preserving meaning. Used for: plugin airgap.
- **Esc goes up the hierarchy** — in any tree-shaped UI (tool registry, plugin picker, modals), Esc moves up one level rather than closing entirely. You can't get lost; you can always back out one step at a time.

---

## UI

Target: full-screen TUI on a small display (7–10 inch), running on Pi-class hardware.

### Layout

```
┌─────────────┬────────────────────────────┬─────────────┐
│ SIDEBAR     │ MAIN                       │ RIGHT PANEL │
│             │                            │             │
│ counters    │ construct pool             │ ┌─Files──┐  │
│ goal        │ (vertical scroll)          │ │ Tools  │  │
│ fleet log   │                            │ │ ...    │  │
│             │                            │ └────────┘  │
├─────────────┴────────────────────────────┴─────────────┤
│ DAEMON BAR                                              │
│ thinking / chat / status — full-width, legible          │
└─────────────────────────────────────────────────────────┘
```

Three columns plus a bottom bar. The right panel and daemon bar appear only when daemon mode is active; keyboard-only mode collapses to sidebar + main.

### Attention management

- Construct state badges: `STARTING / RUNNING / DONE / FAILED / KILLED` — readable in under a second.
- Activity indicator per construct (sparkline or phase dots — TBD).
- Watchdog alerts accumulate in ticker by default. **Critical severity pulls focus** (flash, color). Warnings badge silently.
- Severity classified by watchdog, informed by daemon's delegation-time hints.
- Faint number badges always visible on selectable elements within a section; the active section's badges brighten so you always know which section the current digit press will hit.

### Concurrency cap, not tab overflow

Earlier drafts proposed tab-grouping when construct count exceeds 9. This is dropped. The hard concurrent cap of 9 (limit of the digit-key real estate) is the right ceiling for human supervision; needing more concurrent constructs is a different problem class than the cyberdeck addresses. Invisible constructs are counted against their own separate cap and don't compete for these slots.

### Tools panel

Right-panel tab. Hierarchical browser of the tool registry, with `Esc` traversing up one level:

```
Tools/
├── Plugins/
│   ├── Hardware/   (camera, microphone, ir_blaster, ...)
│   └── External/   (mcp servers, ...)
├── Scripts/
│   ├── Recon/      (scan_wifi, geolocate, ...)
│   ├── Files/      (extract_pdf_text, batch_rename, ...)
│   └── Web/        (archive_url, ...)
└── Profiles/       (recon_specialist, code_reviewer, ...)
```

Number keys enter folders or select items; `Esc` goes up one level. Folder breadcrumb visible at the top.

The Tools panel is informational in v1: read-only display, with the active plugin highlighted. Per-construct tool overrides are explicitly out of scope until the registry has stabilized.

### Files panel

Right-panel tab, sibling to Tools. Lists every file written by every construct in the current session, attributed to its producer. Sourced from the `files_written` capture in the construct event stream.

- Quick scan: what artifacts has this session produced?
- Attribution: which construct made which file?
- Future: clickable entries, dedup, relative paths, filter/search.

### Capture modal

Opens after a plugin quickfire (`c`) or daemon-requested capture (when accepted):

- Preview pane: image, waveform, sensor reading, etc.
- Context field: free-text netrunner annotation.
- Destination picker: Daemon / focused construct / pick construct / new construct.
- Bindings: F flips destination, Space/Enter sends, Esc cancels.

### Status indicators

Always visible somewhere in chrome:

- Active plugin name + on/off state (sidebar or status bar).
- Active brake profile (sidebar).
- Connection state (sidebar; Online / Degraded / Offline).
- Daemon state badge.
- Spawn / live / cap counters.
- Cost and token totals.
- **Constructs Available** — RAM-meter style bar showing pool fill (e.g., `┃■■■□□┃ 3/5`). Filled blocks are warm sessions ready to allocate; hollow blocks are slots being warmed in the background. The meter drains when the daemon pulls a session, refills as warming completes. Inspired by *Cyberpunk 2077*'s quickhack RAM bar — capability you can see at a glance.

---

## Keymap

WASD-spatial navigation, no Ctrl-chording for primary actions. Left-hand-heavy by design (right hand is the safety zone). All bindings except EJECT, the cap-Ctrl combos, and modal-typed text are unmodified single keys or simple Shift variants.

### Conventions

- `Shift` = *and mean it* (semantic amplifier, not just safety modifier).
- `Ctrl` = global / cross-section reach.
- `Space` = primary action (click).
- `Enter` = submit (universal in modals, alias to Space outside text contexts).
- `Esc` = cancel / unfocus / up-one-level in hierarchies.
- `?` = keybinds overlay.

### Navigation

| Key | Action |
|---|---|
| `w` `a` `s` `d` | Jump to section above / left / below / right (no wrap) |
| `Tab` / `Shift+Tab` | Cycle focus within current section (includes right-panel tabs) |
| `1`–`9` | Jump to element N in current section |
| `Ctrl+1`–`Ctrl+9` | Jump to construct N globally (from anywhere) |

### Actions on focused element

| Key | Action |
|---|---|
| `Space` | Primary action / accept |
| `Enter` | Submit (in modals; alias to Space elsewhere) |
| `q` | Queue-inject focused construct |
| `Q` | Interrupt-inject focused construct |
| `k` | Soft-kill focused construct |
| `K` | Hard-kill + blacklist (propagates to Watchdog) |

### Daemon and goal

| Key | Action |
|---|---|
| `t` | Talk to Watchdog (async, casual) |
| `T` | Talk to Daemon (sync, redirects plan) |
| `e` | Edit goal |
| `E` | Pause / unpause Daemon |
| `n` | Spawn visible construct |
| `N` | Spawn invisible construct |
| `r` | Routing — wire two constructs |

### Plugins and tools

| Key | Action |
|---|---|
| `c` | Fire active plugin's quickfire |
| `Shift+C` | Open plugin picker (then `<number>` to set active) |
| `p` | Toggle plugins on/off (hardware airgap) |
| `l` | Open Limits modal |

### Modal-local

Inside text-input modals, alphabet keys type literally. Outside text inputs, the global grammar applies.

| Key | Action |
|---|---|
| `f` | Flip toggle (direction, mode, field) inside active modal |
| `Esc` | Cancel modal / go up one hierarchy level |
| `Enter` | Submit |

### Emergency

| Key | Action |
|---|---|
| `Ctrl+F` (held 500ms) | EJECT — full halt, snapshot, recover-or-quit |

### Reserved unassigned

`g`, `z`, `x`, `v`, `b` and their Shift- variants stay free for future verbs. Empty keys are real estate, not bugs.

### Design principles

- **Sub-50ms input latency.** No model in the hot path of any keypress.
- **Input history is sacred.** Up-arrow recall, fuzzy recall of prior intents.
- **No confirmation dialogs.** Use undo, delayed-commit, or deliberate-consent gestures. Modals that *gather input* are fine; modals that *confirm* are friction.
- **Mode indicator always visible.**
- **Numbers always indicate what numbers do.** Faint always; bright when section is active.

---

## Software stack

**Language: Python.** Plugin ergonomics beat everything else for a project that grows by accretion. ("The language that can do anything, badly.")

### Components

- **Core** — process/lifecycle manager for daemon and constructs, inter-role message bus, per-agent state machines.
- **Input layer** — hotkey daemon, focus manager, input history, modal state, deliberate-consent timer.
- **Render layer** — TUI (Textual as default pick; fallback to rich + prompt_toolkit only if layout model fights us).
- **LLM client** — provider-agnostic; the **API-call structure is the unit of backend swap**. Config-driven routing. Claude Code Agent SDK is first-class for Claude; LiteLLM-style normalizer for everything else.
- **Session manager** — single source of truth for session_ids: owns the session pool, the on-disk manifest, the connection-loss recovery flow, and stale-session expiry. The Daemon and Fleet *request* sessions from it rather than minting their own. Centralizes a concern that would otherwise be scattered across components.
- **Watchdog engine** — tripwire DSL + deterministic matcher + alert routing + Q&A queue + blacklist registry.
- **Tool registry** — plugins, scripts, and profiles. Hierarchical, file-backed, event-emitting. Filesystem-watch on profile files. See *Architecture → Tool registry* for canonical definition.
- **Persistence layer** — annotated append-only run logs, file-based config, plugin state, registry contents, session manifest.

### Process isolation and IPC

- **Subprocess per construct.** Crash isolation (a construct can segfault without taking the deck down), hard-kill actually means hard-kill (SIGKILL the PID), per-PID resource accounting via `/proc`.
- **IPC via ZeroMQ.** Pub/sub for the watchdog (one subscriber reading N streams), req/rep for daemon→construct control. Survives endpoint death cleanly, which matters when killing is a feature.
- **No Redis / no durable queue.** Sessions are for autopsy, not resume; state lives in files.

### Construct implementation

A construct is primarily **a managed `claude` subprocess in headless mode**:

```
claude -p "<task>" \
    --output-format stream-json \
    --allowedTools "<scoped tool list>" \
    --permission-mode <mode_from_brake_profile> \
    --resume <session_id_if_continuing> \
    --cwd <scoped_working_dir>
```

This gives us, for free:

- Full agentic loop with file ops, Bash, and web search already implemented.
- Line-delimited JSON event stream the watchdog can parse directly.
- Per-construct tool scoping via `--allowedTools` — maps directly to brake profiles.
- Per-construct permission mode — matches risk × confidence matrix.
- Session resume via `--resume` — enables clean inject-and-continue semantics.
- MCP server integration for custom tools — our plugin surface has a pre-built delivery mechanism.

The daemon itself is *not* a Claude Code subprocess in the running mode; it's a one-shot-per-turn `claude -p` with `--resume` for session continuity. Streaming JSON input mode was attempted in early M4a but proved underdocumented and version-fragile; per-turn subprocesses cost ~2s of startup overhead but are reliable and reuse existing plumbing.

Non-Claude construct types (e.g., constructs running on local models or other providers) implement the same abstract interface: spawn, stream events, accept input, terminate. Claude Code is one implementation; Ollama-backed constructs are another.

### Model routing

- **Hand-edited config table, not learned**: `{task_type: backend, ...}`.
- **Local model (Ollama, 7B-ish) is load-bearing, not optional.** Carries ~70% of volume: concierge, classification, summarization, tripwire authoring, watchdog prose fallback, cheap chat. Required for boot — the deck must be functional offline and only reach out when the task earns it.
- **Claude Code via Max subscription** is the default remote path for real work (code, file analysis, repo reasoning, web research, command-line tasks). Uses subscription quota, not API credits.
- **OpenRouter** (small pay-as-you-go credit balance, optional) for non-Claude models when a specific task benefits — vision, cross-model comparison, specific open-weight models.
- **Direct Claude API** (optional) for anything Claude Code's harness can't expose cleanly.
- **Connection-aware routing (deferred):** the daemon should prefer local-model constructs when connection state is `degraded`, deferring remote-model work for restoration. This requires local-construct support, deferred along with Ollama integration.
- Log outcomes; revise table by hand; graduate to learned routing only if data clearly warrants.

### Cost philosophy

The project is hobbyist-scale and costs should reflect that. Concrete shape:

- **Local model handles volume.** Free after electricity. Aggressive default routing toward local.
- **Claude Code is a scalpel, not a hammer.** Reserved for tasks that clearly exceed what a 7B model does well. Max quota covers comfortable hobby use; aggressive local routing makes it very hard to exhaust.
- **No unofficial programmatic access.** Session tokens and scraped-web-endpoints are off the table — TOS violation, ban risk, fragile. Claude Code SDK is the sanctioned path.
- **Quota-aware, not quota-paranoid.** The daemon does not pre-throttle. User-set caps (Limits modal) are a separate "if it happens anyway" layer; they pause and ask, never auto-halt.

### Model dialect handling

Prompts are not model-agnostic. Claude prefers XML, OpenAI prefers JSON mode, local models vary. The daemon translates *intent* into each model's preferred idiom when delegating. The API-call-structure abstraction carries intent; connectors render it into the target's dialect.

### Plugin surface

Each interface is a plugin: camera, IR blaster, serial, SSH, HTTP, BLE, SDR, filesystem — whatever gets added later. Self-describing schemas. **The deck's capability ceiling is the union of its registered tools** (plugins + scripts + profiles, per *Tool registry* above). The project never finishes; it grows arms.

---

## Hardware (TBD)

### Constraints

- **One-handed, arm-carry operable.** Load-bearing constraint — rules out precision pointing, two-hand modal UI, look-away inputs.
- **Pi 4-class compute is sufficient.** Models run remotely; the deck handles UI, process management, tripwires, and IO.
- **Full QWERTY keyboard.** Density over minimalism. One-hand reachability is a layout problem, not a key-count problem.
- **Small display (7–10 inch).** Enough for the TUI layout; small enough to carry.
- **Multi-radio capable.** At least two independent network interfaces — see *Architecture → Network topology*. The deck's connectivity model assumes the API channel and the tool channel are physically distinct radios, even if v1 ships with only one.

### Open

- Chassis form factor.
- Display specifics.
- Cellular modem choice (Quectel? SIMCom? HAT vs USB?) and SIM/eSIM strategy.
- USB tool-radio choice (Alfa AWUS036 family is the obvious default; others to evaluate).
- Bluetooth/BLE as separate radio space — onboard or third add-on?
- Back-of-deck interfaces (camera, IR blaster, ports).
- Battery strategy.
- Input peripherals beyond the main keyboard.

---

## Open questions

- Sparklines vs phase dots for construct activity indicator.
- Concrete local model pick (model + quantization) — Qwen 7B, Llama 3.1 8B, and Mistral are all candidates.
- Tripwire DSL syntax (now unblocked — we have real stream-json event shape to design against).
- How wired-pair construct communication is exposed in the UI.
- Plugin manifest format and sandboxing stance (MCP gives us delivery; sandboxing still TBD).
- When a tripwire fires, does the watchdog route to daemon only, or can it notify the user directly for critical alerts?
- Goal-edit diff classifier: deterministic heuristic (edit distance) or model-driven? Both have tradeoffs.
- Watchdog Q&A response latency expectations — how stale can answers be before they're misleading?
- Blacklist persistence — does it expire with the goal, the session, or stick around forever?
- Tool registry export/import format (deferred to v2; design with future sharing in mind).
- Capture modal artifact storage — where do photos / recordings / sensor dumps actually live on disk?
- Active plugin discoverability — does the deck remember the last-active plugin across sessions, or always reset to a sensible default?
- Connection monitor heartbeat cadence — how often is too often? How stale is too stale before degrading?
- Recovery flow scope — should `clean_finish` sessions silently re-emit their final outcomes to the daemon for plan continuity, or are completed sessions truly "done" once recovered?
- Cellular failover policy — when wifi drops mid-session, do we proactively migrate to cellular (potentially metered surprise) or wait for explicit netrunner approval?
- Local-model construct interface — same `Construct` abstraction or a sibling class? Tool-call format translation (Anthropic XML → Ollama function-call format) lives where?
- Monitor-mode-on-primary-radio policy — disallowed entirely, allowed with deliberate consent, or warned?
- Anthropic session expiration — do warm sessions expire server-side after N days of inactivity? If so, the session manifest needs proactive ping-or-discard logic.
- Pool sizing tuning — start at 3 default-profile constructs; tune up based on how often the netrunner outpaces the refill rate.
- Profile-addendum injection — does prepending the addendum to the first user message work cleanly with Claude Code's `--resume` flow, or does it confuse the session because the system prompt was set at warm time and now we're "adding" instructions mid-conversation?
- **Thinking block content rendering empty in real Opus 4.7 output** — field name, redaction, or missing flag? Classifier identifies the kind correctly but `_render_block` returns empty content. Needs investigation against raw event samples.

---

## Implementation status

Milestones shipped, in order:

- **M0** — `Construct`: a single managed Claude Code subprocess with event streaming and lifecycle management.
- **M1** — `Fleet`: N concurrent constructs with shared event bus, NDJSON logging, and per-run accounting.
- **M2** — TUI skeleton over the fleet: live construct panes, fleet log, keyboard quit.
- **M3** — Keyboard-driven cyberdeck: focus management, jump keys, spawn/kill modals, expand/collapse.
- **M4a** — Daemon-driven goals: persistent coordinator decomposes a goal into spawns, observes outcomes, iterates to done. Includes max_concurrent gating, max_total_spawns cap, respawn-loop detection, and `files_written` propagation through outcomes so the daemon recognizes file-creating success.
- **M5+ Profile/brake refactor** — separated brake state from profiles; brake is now deck-global with PreToolUse hook enforcement (see *Brake state* in the supervision model section). Profiles became prescriptive templates with `recommended_tools` (renamed from `allowed_tools`, no longer a hard cap). Watchdog gained brake awareness via the `permission_denials` feed on result events. See `brake_state.py`, `brake_hook.py`, and the orientation doc's Brake state subsection for the full implementation map.
- **M5+ Plugin scaffolding (third leg of tool registry)** — plugins are capability bundles at `<home>/plugins/<name>/` with a TOML manifest, a Markdown README (LLM-facing interface docs), and an executable entry point. Stateless v1: each invocation is a fresh subprocess that constructs spawn via Bash. PluginRegistry mirrors ProfileRegistry's read API; one-shot scan at startup (no hot reload — plugins are code). Plugin awareness lands in both the daemon's system prompt (catalog of available plugins) and constructs' system-prompt addendum (with explicit invocation patterns). First plugin is `screenshot` — mss-based cross-platform screen capture, real-deck verified end-to-end. Sub-features deferred: airgap (`p`), quickfire (`c`), picker (`Shift+C`), persistent (stateful) mode, MCP-as-metadata variant.
- **Brake-denial visual indicator** — construct panes whose finalize event carries non-empty `permission_denials` get a yellow border (`.-blocked` CSS class) and a header badge (`[⚠ blocked: Write×2, Bash×1]`). Two-channel visibility: border for at-a-glance scanning, badge for what specifically got caught. Survives compact mode at 60% opacity.
- **Connection consequences (spawn-blocking)** — Fleet's `spawn()` checks the `connection_state_provider` first; non-`ONLINE` states emit a `spawn_blocked` meta event with reason and refuse the spawn before the semaphore is acquired. In-flight constructs continue. Daemon parking and recovery flow are still deferred.

Layout and observability work landed alongside M4a: 3-column + bottom-bar TUI, Files / Tools right panel, token + cost tracking, configurable bounded-time `Construct.wait()` to prevent shutdown hangs.

### Tech debt (tracked, not blocking)

- ~~`spawn()` slices `_build_command()[1:]` to splice in the resolved binary path — builder API should take the resolved path as an argument instead.~~ Fixed: `_build_command(claude_bin)` now takes the resolved path as a required arg.
- ~~`kill()` sets `state = KILLED` before confirming termination; should transition only after the process is confirmed dead.~~ Fixed: split intent (`_kill_requested` flag, set immediately) from confirmation (`state = KILLED`, set after process dies). wait() reads the intent flag so the race is closed without changing the visible state-flip semantics.
- `tools` default hardcoded in `Construct.__init__` — bake policy into a module-level `DEFAULT_TOOLS` constant.
- ~~`classify_event` kind values are bare strings — should be an enum or constant set.~~ Fixed: `EventKind` class-as-namespace in `construct.py` defines constants for every recognized return value; consumers in `display.py` reference them. Open-ended pass-through (raw type strings for shapes the deck doesn't have a special case for) preserved deliberately so tripwire/watchdog future code can still see novel types.
- 2KB `final_output` truncation may be aggressive for long reports; reconsider when use cases warrant.

---

## Glossary

- **Netrunner** — the human operator.
- **Daemon** — the always-running coordinator role.
- **Construct** — a task-scoped worker agent. Subprocess-per-instance.
- **Watchdog** — observational role: tripwire-based alerts plus oracle Q&A.
- **Tripwire** — deterministic rule authored by a model, executed on streams.
- **Plugin** — in-process tool exposing hardware or external APIs.
- **Script** — standalone executable on disk, invoked by constructs via Bash.
- **Profile** — saved Construct configuration: instruction addendums (daemon-side and construct-side) plus a `recommended_tools` list surfaced as a soft hint. Prescriptive template, not enforcement.
- **Inject** — user input delivered to a construct, with two modes (interrupt, queue).
- **Wiring** — explicit channel between two constructs.
- **Brake state** — deck-global runtime gating policy (Paranoid / Default / YOLO). Set by the netrunner via the `b` modal. Persisted at `<home>/.cyberdeck/state.json`. Enforced at construct spawn time via Claude Code's `PreToolUse` hook (see `brake_hook.py`).
- **Hardware airgap** — deterministic on/off for all plugin access; sits above brake state in the privilege hierarchy.
- **Quickfire** — a plugin's default action, triggered with one keypress.
- **Visible / invisible construct** — visibility to the daemon. Invisible = side-quest, daemon does not see or count it.
- **Blacklist** — user-declared "do not retry this pattern" list, propagated through the Watchdog.
- **EJECT** — emergency full-halt, deliberate-consent triggered.
- **Deliberate consent** — press-and-hold gesture for escalating-privilege actions.
- **Soft / loud** — modifier-key convention: lowercase is the polite verb, uppercase is the emphatic one.
- **Connection state** — Online / Degraded / Offline; deck-level signal that gates remote-model spawns and pauses the daemon when degraded.
- **Session manifest** — on-disk record of every spawned subprocess and its session_id. Source of truth for the recovery flow.
- **Recovery flow** — UX for resuming sessions after connection loss or deck restart. Per-state policy (clean_finish / mid_stream / pre_init), modulated by brake profile.
- **Primary / secondary / tertiary radio** — network topology tiers. Primary carries the API connection, secondary is the tool radio (free to be reconfigured), tertiary is failover.
- **Session pool** — pre-warmed default-profile construct sessions, ready for immediate allocation. Drained by spawns, refilled in the background. Visible to the netrunner as the "Constructs Available" RAM-meter.
- **Session manager** — component owning session_id lifecycle: the pool, the manifest, recovery, and stale expiry.
- **Profile addendum** — text appended to baseline daemon or construct system prompt when a profile is active. Steers behavior without replacing foundation.
- **Pre-warmed session** — a server-side session_id with system prompt already processed but no real work done. Resumable for immediate use. Cheap to hold (no live subprocess), expensive to create (one cold start each).
