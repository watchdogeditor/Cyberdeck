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
8. **Daemon planning mode + pause/unpause (`E`).** Originally framed
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
9. **Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`).**
10. **Keymap revision pass.** Real-deck use surfaced that the keymap
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
    planning mode (item 8) sits behind this when both unblock.
11. **Retrospective observability — the morgue + watchdog log.**
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
12. **Quota-aware throttling.** Daemon gates spawns on remaining Max
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
  Pending real-deck smoke: confirm rung-1 fork composes cleanly
  against a live streaming session.

**Next priorities:**
1. **The spine — unified event stream.** Full design at
   `cyberdeck-event-stream-design.md`. One canonical event bus that
   every event source publishes to; every observer subscribes via a
   role-derived filter. Replaces the current 11-callback-chain sprawl
   that decays silently as new event types land (the magnified-view
   and watchdog-Q&A-context bugs from slice 2 testing both fall out
   of that decay). Single source of truth + introspectable visibility
   ("what does the watchdog see?" → check its filter). Absorbs the
   prior logger + quit discipline slice — the file logger becomes a
   bus subscriber, lifecycle events including SIGINT-swallow /
   smart-Ctrl+Q / EJECT-responsiveness publish through the same
   stream. 8 phased mini-slices, each shippable independently:
   (1) bus + DeckEvent primitives, (2) Fleet → bus, (3) Daemon → bus,
   (4) Tripwires + Blacklist → bus, (5) Brake + Connection +
   Profiles + Plugins → bus, (6) direct chatlog writes → bus
   (replaces the tactical buffer fix), (7) file logger as bus
   subscriber + quit discipline, (8) cleanup + remove deprecated
   `add_listener`/`on_*` shims. Substrate for maintbot, morgue,
   list-names, tripwires slice 3, B2 synthesizer.
2. **Maintbot v1 (diagnose-only).** Filed 2026-04-29; full design
   at `cyberdeck-maintbot-design.md`. Separate process; outlives
   the deck. Two activation paths: heartbeat-fired on unclean exit
   (new `wt` window, auto-triages) and deliberate-summon (menu /
   keybind from the deck). Reads `<deck source>/logs/`, deck source
   tree, deck state files; cannot touch the world. Cloud Claude
   substrate for v1; D1 eventual. Blocked on the unified event
   stream slice (maintbot is one of the bus's designed consumers).
3. **Log-readability overhaul** — fleet/chatlog/watchdog/daemon
   scattered across windows is hard to follow at a glance; needs
   structural thinking, not just CSS. Distinct from the file-log
   work in the spine slice (this is in-deck UI composition, not
   file shape). Probably composes better post-spine — display
   surfaces become "subscriber + filter + formatter" units, easier
   to rearrange.
4. Connection consequences round 2: daemon parking on connection-
   blocked spawns + recovery flow.
5. Tripwires slice 3 — severity-aware rendering (critical pulls
   focus, warning badges, low logs only). Severity tiers are
   already in the DSL; just need the visual routing. Trivial under
   the spine — one filter predicate per severity-bucket.
6. Tools-research chat (from `cyberdeck-tools-research-seed.md`).
7. Plugin sub-features: airgap `p`, quickfire `c`, picker `Shift+C`.

(Keymap revision pass was tee'd up here on 2026-04-27 but moved to
deferred mid-design — needs more brain cells to do well than were
available that session. Working draft preserved; see deferred list.)

---

*This file is the source of truth for milestone ordering. When in
doubt, this beats memory.*
