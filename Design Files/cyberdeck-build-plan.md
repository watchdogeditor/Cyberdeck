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

### Tier 2 — Tool registry partial (C3 partial)
Profiles + Scripts in registry; **Plugins NOT yet** (deferred).
C1g listification: nav rebind (lowercase=scroll, uppercase=walk),
Tools→ListView, Files→ListView, LaunchScreen modal. Phase A
deck-control protocol: dispatcher script, marker protocol parser,
construct system prompt addendum. Phase B Tools panel restructure:
Profiles + Scripts only; CONSTRUCT TOOLS dropped; literal `<home>`
removed; dir-reference labels removed; PERMISSIONS placeholder removed.

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
4. **Watchdog tripwires + blacklist** — DSL, deterministic matcher,
   alert routing. Where `Shift+K` hard-kill-and-blacklist lands.
5. **Script manifests** — declarative `(name, category, args, expected
   output shape)` per spec. Currently raw filenames only.
6. **Construct script-launch wiring** — ScriptListItem space lands
   here once manifests exist.
7. **Goal-edit force-push** — apply-now interrupt of in-flight turn.
8. **Daemon pause/unpause (`E`).**
9. **Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`).**
10. **Quota-aware throttling.** Daemon gates spawns on remaining Max
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
- `Fleet.spawn()` slice (`_build_command()[1:]`) — builder API ugly
- `Construct.kill()` state pre-confirmation
- `classify_event` bare strings
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

**Next priorities:**
1. Daemon planning mode + daemon pause/unpause — paired state-machine
   work; chat with the daemon before it spawns anything, transitions
   to active when netrunner says go.
2. Watchdog tripwires + blacklist — eventually authors goal-scoped
   deny rules on top of the static brake patterns. Largest remaining
   chunk.
3. Log-readability overhaul — fleet/chatlog/watchdog/daemon scattered
   across windows is hard to follow at a glance; needs structural
   thinking, not just CSS.
4. Connection consequences round 2: daemon parking on connection-
   blocked spawns + recovery flow.
5. Tools-research chat (from `cyberdeck-tools-research-seed.md`).
6. Plugin sub-features: airgap `p`, quickfire `c`, picker `Shift+C`.

---

*This file is the source of truth for milestone ordering. When in
doubt, this beats memory.*
