# Cyberdeck — Keymap Revision Working Draft

> **STATUS: ON HOLD as of 2026-04-27.** Tee'd up as next-priorities
> #1 and pulled the same session — needed more design bandwidth than
> was available. Layer 1 (actions inventory) is populated; Layer 2
> and Layer 3 are blank. Pickup path: netrunner marks up Layer 1
> (frequency, tags, capability gaps) → AI does Layer 2 synthesis →
> joint Layer 3 keymap proposal → spec/orientation/CLAUDE.md/tui.py
> updates per the spec-impact checklist at the bottom. See build plan
> item 10 for context.

*Filed 2026-04-27. Working document for revising the keymap based on
real-deck usage. Methodology shifted (also 2026-04-27) from
"audit current bindings" to **actions first** — enumerate every
discrete user-facing action the deck performs (or is spec'd to),
then derive UI surfaces and keybinds from that list.*

*Three layers stacked: (1) actions inventory, (2) themes that emerge
from marking it up, (3) derived UI and keymap. Layer 1 is populated
from the codebase + spec by the AI; the netrunner marks up frequency,
tags actions to keep/drop/move/add, and flags capability gaps. Layer
2 is the AI's synthesis pass after markup. Layer 3 is the joint
proposal.*

---

## Spec constants (don't violate without conscious revision)

- **WASD-spatial.** Lowercase scroll within widget; uppercase walk
  focus. No Ctrl-chording for primary actions.
- **One-hand-operable, left-hand-heavy.** Right hand is safety zone.
- **Space = primary action; Enter = submit alias outside text.**
- **Esc = cancel / unfocus / up one level.**
- **Sub-50ms input latency.** No model in the keypress hot path.
- **No confirmation dialogs** — use deliberate-consent (held keys ~500ms).
- **Input history is sacred** — up-arrow recall in modals.

The semantic-amplifier convention (Shift = "louder same-verb") is up
for revision in this pass — it currently applies cleanly to `q/Q` and
`k/K` but is doing arbitrary modal switching for `t/T`, `n/N`, `c/C`.

---

## Layer 1 — Actions inventory

*Status legend: ✓ live · ⚠ stub (key wired, action not implemented or
incomplete) · ⏳ deferred (not yet in scope per build plan).
Frequency / Tag / Notes for the netrunner to fill.
Tag values: `keep`, `drop`, `move`, `add`, `?`.*

### Goal & intent

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Set initial goal | First goal of session, kicks daemon | `e` (when no goal) → `GoalSetScreen` | global | ✓ | | | |
| Edit goal mid-flight | Replace active goal; daemon receives as goal-update next turn, classified clarification/scope-change/pivot | `e` (when goal active) → `GoalSetScreen` | global | ✓ | | | |
| Talk to daemon (queue message) | Stash netrunner message for delivery on next outcome turn — plan-affecting | `T` → `TalkDaemonScreen` | global | ✓ | | | |
| Ask watchdog (queue question) | Stash question for the async oracle — observational, non-plan-affecting | `t` → `AskWatchdogScreen` | global | ✓ | | | |
| Plan goal before launch | Daemon decomposes interactively, netrunner reviews, launches when ready | (deferred) | global | ⏳ | | | Refined intent in build plan: addendum to current flow, plan persists post-launch as tracking panel |

### Daemon control

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Pause daemon | Hold; in-flight constructs continue; outcomes accumulate | `E` | global (daemon WORKING) | ⚠ | | | Toast-only stub today |
| Unpause daemon | Resume from PAUSED | `E` | global (daemon PAUSED) | ⚠ | | | Same binding as pause |
| Launch from planning | Transition PLANNING → WORKING (deferred) | (deferred) | global (daemon PLANNING) | ⏳ | | | |
| Abort planning | Cancel a planning session, return to idle | (none — EJECT only) | global (daemon PLANNING) | ⏳ | | | |
| Force-push goal update | Interrupt current daemon turn to apply edit immediately | (none — natural-break delivery only) | global | ⏳ | | | M5+ per spec |

### Spawn

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Spawn visible construct | Daemon-aware spawn, counts against caps | `n` → `NewConstructScreen` | global | ✓ | | | |
| Spawn invisible construct | Side-quest, daemon-blind, separate cap | `N` → `NewConstructScreen` | global | ✓ | | | |
| Launch with profile | Spawn pre-configured for a profile picked from Tools panel | Focus profile in Tools list → `Space` → `LaunchScreen` | contextual (Tools list) | ✓ | | | |
| Launch with file context | Spawn with file path injected into prompt | Focus file in Files list → `Space` → `LaunchScreen` | contextual (Files list) | ✓ | | | |
| Launch with script | Spawn configured to use a script as primary tool | Focus script → `Space` | contextual (Tools list) | ⚠ | | | Stub — emits "not yet implemented" notice |
| Wire two constructs | Direct-channel routing between a construct pair | `r` | global | ⚠ | | | Stub |

### Construct manipulation (focused construct)

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Queue-inject | Stash message for next natural break | `q` → `InjectScreen` | focused construct | ✓ | | | |
| Interrupt-inject | Kill current work, redirect with new prompt | `Q` → `InjectScreen` | focused construct | ✓ | | | |
| Soft-kill | Terminate gracefully | `k` | focused construct | ✓ | | | |
| Hard-kill + blacklist | Terminate; blacklist task pattern | `K` | focused construct | partial | | | Kill yes; blacklist deferred |
| Toggle expand/collapse | Expand pane in main layout | `Space` on construct pane | focused construct | ✓ | | | Distinct from `z` magnify |

### Magnify / view

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Magnify pane / item | Open `ExpandModal` with full content | `z` on focusable | universal | ✓ | | | Construct panes, list items, RichLogs |
| Refresh expanded content | Re-read source from disk inside ExpandModal | `r` inside ExpandModal | modal-local | ✓ | | | |

### Tool / capability invocation (deferred sub-features)

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Fire active plugin's quickfire | One-keypress capture on the active plugin | `c` | global | ⚠ | | | Stub; needs plugin runtime + airgap |
| Pick active plugin | Open picker to set the active plugin | `Shift+C` | global | ⚠ | | | Stub |
| Toggle hardware airgap | All plugins on / all off | `p` | global | ⚠ | | | Stub |

### Brake & safety

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Set brake state | Pick paranoid / default / yolo | `b` → `BrakeScreen` (p/d/y picks; yolo held 3s) | global | ✓ | | | Yolo uses deliberate-consent gesture |
| EJECT | Full halt; SIGKILL all subprocesses; snapshot to disk | `Ctrl+F` (held 500ms) | global | ✓ | | | Survivor of redesign |
| Open limits modal | View/adjust max_concurrent, max_total_spawns, etc. | `l` → `LimitsScreen` | global | ✓ | | | |

### Panel / view manipulation

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Switch right-panel tab | Files / Tools / Plugins | `Tab`/`Shift+Tab` when focused on tab bar | contextual | ✓ | | | |
| Switch bottom-panel tab | Daemon / Watchdog | `Tab`/`Shift+Tab` when focused on tab bar | contextual | ✓ | | | |
| Walk focus across sections | Move focus N/S/E/W between sections | `W`/`A`/`S`/`D` | global | ✓ | | | Falls through empty sections |
| Scroll within widget | Line scroll inside focused widget | `w`/`a`/`s`/`d` | contextual | ✓ | | | |
| Cycle focus within section | Tab through focusable items | `Tab`/`Shift+Tab` | contextual | ✓ | | | Priority binding |
| Jump to element N in section | Number-jump | `1`–`9` | contextual | ✓ | | | Bright when section active |
| Jump to construct N globally | From anywhere | `Ctrl+1`–`Ctrl+9` | global | ✓ | | | |
| Unfocus | Clear current focus | `Esc` | global | ✓ | | | |

### Files panel actions (dispatcher-driven from constructs)

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Add file to Files panel | Construct calls `cyberdeck files add <path>` | dispatcher marker (not a key) | from construct | ✓ | | | |
| Remove file from Files panel | Construct calls `cyberdeck files remove <path>` | dispatcher marker | from construct | ✓ | | | |

### Modals (each has its own micro-grammar; one row per modal)

| Modal | Opens via | Inner verbs | Status | Tag | Notes |
|---|---|---|---|---|---|
| `GoalSetScreen` | `e` | type goal, Enter submit, Esc cancel | ✓ | | |
| `TalkDaemonScreen` | `T` | type message, Enter submit, Esc cancel | ✓ | | |
| `AskWatchdogScreen` | `t` | type question, Enter submit, Esc cancel | ✓ | | |
| `NewConstructScreen` | `n`/`N` | type task, Enter submit, Esc cancel | ✓ | | |
| `InjectScreen` | `q`/`Q` | type message, F flip mode, Enter submit, Esc cancel | ✓ | | |
| `LaunchScreen` | `Space` on profile/file/script | type task, Enter submit, Esc cancel | ✓ | | |
| `LimitsScreen` | `l` | edit fields, Ctrl+S save, Esc cancel | ✓ | | |
| `BrakeScreen` | `b` | `p` paranoid, `d` default, `y` yolo (held 3s), Esc cancel | ✓ | | |
| `EjectScreen` | `Ctrl+F` held | Space confirm, Esc cancel | ✓ | | |
| `EjectedScreen` | post-EJECT auto | `i` return idle, `q` quit, Esc return idle | ✓ | | |
| `KeybindsScreen` | `?` | Esc/`?`/Space dismiss | ✓ | | |
| `ExpandModal` | `z` on focusable | w/s line scroll, PgUp/PgDn page, Home/End jump, `r` refresh, `z`/Esc dismiss | ✓ | | |
| `YoloConfirmScreen` | inside `BrakeScreen` after `y` | Space (held) confirm, Esc cancel | ✓ | | |

### Meta / emergency

| Action | Description | Current invocation | Scope | Status | Frequency | Tag | Notes |
|---|---|---|---|---|---|---|---|
| Show keybinds overlay | Help modal | `?` | global | ✓ | | | |
| Quit app | Terminate cleanly | `Ctrl+Q` | global | ✓ | | | |
| EJECT | (see Brake & safety row) | `Ctrl+F` | global | ✓ | | | |

### Capabilities the spec promises but no current invocation reaches

| Action | Description | Spec source | Tag | Notes |
|---|---|---|---|---|
| Tripwire fire response | Watchdog deterministic match → severity-routed alert | spec → Tripwires | | Watchdog tripwires + blacklist (deferred next-priorities chunk) |
| Blacklist persistence | Hard-killed pattern remembered across spawns | spec → Hard-kill blacklist | | Pairs with `K`; deferred |
| Connection-loss recovery flow | Resume / discard / inspect per-session post-reconnect | spec → Recovery flow | | Deferred (round 2 of connection consequences) |
| Daemon parking on Degraded/Offline | Auto-pause vs. block-spawn-only | spec → Connection state | | Spawn-blocking shipped; parking deferred |
| Daemon-requested capture | "Show me what's on the screen" passive prompt + accept gesture | spec → Daemon-initiated plugin use | | Deferred with plugin capture flow |
| Per-step plan tracking panel | Plan persists post-launch like Claude Code's tasks panel | netrunner direction 2026-04-27 | | Deferred with planning mode |

---

## Layer 2 — Themes (synthesis after markup)

*The AI fills this in after the netrunner has tagged Layer 1. Themes
to look for: stubs that should be dropped vs implemented; actions
that share a focused-thing target and could collapse into Space-on-X;
shift-variant pairs that aren't really amplifiers; capability gaps
that warrant new affordances; physical-reach problems on the keymap.*

### Stubs to drop vs. implement

-

### Actions that could become "Space on X" (focused-thing actions)

-

### Shift-variants that aren't genuine amplifiers

-

### Capability gaps the netrunner flagged

-

### Physical-reach problems

-

### Surprises (actions netrunner didn't know existed, or thought existed but doesn't)

-

---

## Layer 3 — Derived UI and keymap

*Joint proposal after Layer 2 synthesis.*

### Surfaces that need to become focusable

*(Today only some are; UI-driven keymap requires expanding the set.)*

-

### Final global keymap (minimal)

*(Navigation primitives + emergency/meta + verbs that genuinely have
no UI home.)*

| Key | Action |
|---|---|

### Contextual keymap (per surface, when focused)

*(Each focusable surface gets its primary `Space` action and any
shift-variant or surface-specific extras.)*

| Surface | `Space` | Other |
|---|---|---|

### Modal-local changes

-

### Bindings dropped entirely

-

### Bindings added

-

---

## Spec impact

*(Once Layer 3 is settled, list every doc that needs updating.)*

- [ ] `cyberdeck-spec.md` — Keymap section (rewrite)
- [ ] `cyberdeck-spec.md` — UI section (focusable surfaces + chrome hints if added)
- [ ] `cyberdeck-claude-code-orientation.md` — any key references
- [ ] `cyberdeck-state.md` — note the revision in shipped/decisions
- [ ] `cyberdeck-philosophy.md` — if "semantic amplifier" survives or is replaced as a named convention
- [ ] `CLAUDE.md` — if any keys are mentioned in run-commands
- [ ] `tui.py` — `App.BINDINGS` + per-screen overrides
- [ ] `?` keybinds modal text
- [ ] Chrome hints in panel headers (if added)

---

*Once Layer 3 is implemented, this doc retires to "keymap revision
2026-04 — for posterity" status. Future revisions get their own
working draft.*
