# Cyberdeck — Current State

*High-density context snapshot. Drop this in the next chat / Claude Code
session to bring fresh context up to speed without re-deriving 8+
sessions of decisions. Pair with `cyberdeck-spec.md`,
`cyberdeck-build-plan.md`, `cyberdeck-philosophy.md`, and the new
`cyberdeck-claude-code-orientation.md`.*

*Updated at the migration point: chat-based development → Claude Code.
The deck is in active production use; ~12k LOC across 13 modules.*

---

## Status snapshot

**The deck is real and load-bearing.** Daemon orchestrates real Claude
Code constructs. Constructs talk back to the deck via the dispatcher
protocol. The watchdog answers questions about fleet activity. The
deck monitors its own connection state. Every focusable surface in
the right and bottom panels does something. Files panel auto-surfaces
deduplicated paths. Profiles + scripts hot-reload from disk.

**Real-world testing has been doing the heavy lifting** for several
sessions — the user has been catching live bugs (watchdog stdin
arg-vs-stdin, watchdog wedge with queued questions, file-panel double
listing on Windows path normalization, focus traversal trap with empty
main, Windows ProactorEventLoop shutdown noise) and we've been fixing
them. Most of these would not have been caught by the test harness.

**Up next:** watchdog tripwires + blacklist (the harder half of
watchdog), then log-readability overhaul, then D1 (local model
substrate) for the long-term Watchdog/synthesizer/arbiter story.
Plugin scaffolding, brake-as-deck-state, connection spawn-blocking,
and the brake-denial visual all shipped in the first wave of
post-migration work.

**Deferred mid-design (2026-04-27):** keymap revision pass and
daemon planning mode + pause/unpause. Both started this session,
both pulled before landing because the design needed more
bandwidth than was available. Working draft for the keymap
preserved at `cyberdeck-keymap-revision.md` with Layer 1 inventory
populated; the actions-first methodology (enumerate actions →
derive UI → derive keymap) is the new approach. Planning mode
revised intent: it's a **modal** the netrunner opens (not a
daemon state), used for goals too complex for a single-message
goal-set, post-confirm produces a persistent tracking panel akin
to Claude Code's "tasks" panel. Full notes in build plan items 8
and 10.

---

## What lives where

### Project files (the design canon)
- `/mnt/project/cyberdeck-spec.md` — base architectural spec
- `/mnt/project/cyberdeck-arbiter-addendum.md` — arbiter + wearable variant
- `/mnt/project/cyberdeck-compliance-future.md` — engagement-grade
  ingress filtering. *Deferred indefinitely.*

### Outputs (working files; sync targets for chat artifacts)
- `cyberdeck-spec.md` (sync of canon)
- `cyberdeck-philosophy.md` — the *why*
- `cyberdeck-build-plan.md` — the *when*
- `cyberdeck-state.md` — this file
- `cyberdeck-claude-code-orientation.md` — onboarding for Claude Code
- `cyberdeck-tools-research-seed.md` — seed for a future tools chat
- `cyberdeck/` — Python source

### Code modules (12k LOC across 13 files)
| File | LOC | Purpose |
|---|--:|---|
| `tui.py` | 6102 | Textual UI, App, all modals, action dispatch |
| `watchdog.py` | 715 | Async question-queue oracle + streaming |
| `daemon.py` | 685 | Persistent coordinator (one-shot + streaming) |
| `fleet.py` | 611 | N concurrent constructs, event bus, NDJSON log |
| `daemon_session.py` | 570 | Fleet ↔ daemon glue, goal+netrunner-msg |
| `session_manager.py` | 557 | Pool + manifest |
| `construct.py` | 552 | Managed claude subprocess |
| `display.py` | 506 | Formatting (untruncated mode + origin badges) |
| `profile_registry.py` | 450 | File-watch profiles dir, hot reload |
| `profiles.py` | 399 | Profile dataclass + TOML loader |
| `connection_monitor.py` | 311 | Heartbeat → Online/Degraded/Offline |
| `dispatcher.py` | 138 | Deck-control script (deck-side stdout protocol) |
| `mock_*.py` | 127+146 | Test fixtures |
| `main.py` | 101 | CLI entry |

---

## Shipped features (working in production)

### Tier 1 — original scope (long stable)
- Construct lifecycle (spawn / inject / kill / interrupt)
- EJECT (Ctrl+F → confirm modal)
- Limits modal (`l`)
- Session pool with cross-restart reuse (5h stale window)
- Activity chatlog (B1) — mechanical event extraction

### Tier 2 — Profiles (refactored post-migration)
- TOML loader, ProfileRegistry, hot reload, default seeded
- Daemon picks profile per-spawn via JSON
- Profiles are **prescriptive templates**: instructions + recommended
  tool list. They do NOT enforce — the brake hook does.
- `recommended_tools` (renamed from `allowed_tools`) surfaced in the
  construct's system-prompt addendum as a soft suggestion. Construct
  still has full default tool set.

### Plugin scaffolding — third leg of tool registry (shipped post-migration)
- Plugin = capability bundle at `<home>/plugins/<name>/` with
  `plugin.toml` (manifest), `README.md` (LLM-facing interface docs),
  and an executable entry point (typically `run.py`).
- Stateless v1: each invocation is a fresh subprocess that
  constructs spawn via Bash. Persistent plugins, MCP-shaped plugins,
  and the wiring keys (`p` airgap, `c` quickfire, `Shift+C` picker)
  are deferred sub-shapes.
- Manifest fields: `name` (slug), `category`, `description`, `entry`,
  optional `[requires]` block (`platforms`, `python_imports`).
  Failing requires checks downgrade the plugin to `available=False`
  with a reason; it stays in the registry so the panel shows what's
  installed.
- `PluginRegistry` mirrors `ProfileRegistry`'s read API but is
  one-shot (`scan()` at startup, no hot reload — plugins are code,
  Python module reloading is fraught).
- Tools panel grows a "PLUGINS" section between Profiles and Scripts.
  Unavailable plugins render with a red ✗ marker and dimmed name.
- Daemon system prompt grows a PLUGINS catalog (only available ones,
  one line each); construct system prompt addendum gains plugin
  awareness with explicit invocation patterns.
- First plugin: `screenshot` — mss-based cross-platform screen
  capture, ~140 LOC. Real-deck verified end-to-end: construct
  invokes via Bash, captures PNG, reports path back.

### Brake state — deck-global (replaces per-profile brake)
- Three levels: paranoid / default / yolo. Set via `b` modal
  (paranoid is single-press, yolo requires EJECT-style 3s held-key
  confirmation, mirroring the deliberate-consent gesture).
- Persists at `<home>/.cyberdeck/state.json`.
- Sidebar indicator next to connection state: ▲ paranoid (yellow),
  = default (white), ▼ yolo (red).
- Enforcement via Claude Code's PreToolUse hooks. Each spawn gets a
  per-construct `--settings` JSON pointing at `brake_hook.py` with
  current brake passed via argv. Hook is self-contained ~180 LOC,
  exits 0 (allow) or 2 (deny). Stderr text becomes the
  model-visible denial reason.
- **Both Bash and PowerShell are gated.** Claude Code on Windows
  exposes PowerShell as a separate tool with the same `command`
  shape as Bash. A construct given Bash-denied will silently pivot
  to PowerShell — verified on real-deck without the construct being
  asked to. Both shells share `SHELL_TOOLS` set in the hook; both
  go through the same destructive-pattern + protected-path checks
  under default brake; both are in `PARANOID_DENY_TOOLS`.
- Static patterns are short and opinionated: destructive bash regex
  (rm -rf on system roots, format, dd of=/dev/, mkfs, fork bombs,
  shutdown, sc/net stop), OS-root path prefixes (Windows + Unix),
  and three brake-config sentinel filenames (brake_hook.py,
  brake_state.py, brake_patterns.py). The deck-source-dir-as-
  substring check was tried and dropped — cyberdeck-home/ is a
  subdirectory of the deck source, so a substring match
  inadvertently denied every legitimate plugin and dispatcher
  invocation. Sentinel filenames are precise enough.
- Mid-flight propagation deferred — brake state is captured at
  spawn and baked into that construct's lifetime. New spawns see
  the new value.
- Watchdog observes via `permission_denials` field on result events;
  chatlog renders `· brake blocked: Write×2, Bash×1` suffix on
  finalized lines. Watchdog system prompt grew brake awareness.

### Right-panel listification (C1g) + Phase A/B
- Tools tab → ListView (profiles + scripts)
- Files tab → ListView (FileListItem)
- LaunchScreen modal (space on profile/file → launch)
- Dispatcher protocol: `__CYBERDECK::v1::ACTION::PAYLOAD__`
- `dispatcher.py` bootstrapped to `<home>/tools/deck/cyberdeck.py`
- Construct system prompt teaches dispatcher invocation
- Verified end-to-end on real Windows construct

### Pane-log un-trim
- ConstructPane raw event buffer + `render_buffer(untruncated=True)`
- Modal mode: 5000-char cap (vs 500 live), full thinking blocks
- `display.py` formatters accept `untruncated` kwarg

### Watchdog Q&A (`t`)
- Async question→answer oracle in `watchdog.py`
- AskWatchdogScreen modal (yellow-themed, queue-depth hint)
- **Streaming default**: persistent `claude --input-format stream-json`
  subprocess; questions become JSONL writes; answers via stream-json
- One-shot fallback (`streaming_mode=False`)
- **Wedge recovery**: timeout → kill subprocess → respawn fresh on
  next ask (production bug: queued questions stayed stuck forever
  before this fix)
- Context: last 30 chatlog events, plain-text, no markup
- Answers route to chatlog as `[watchdog] → ...` AND to dedicated
  Watchdog tab with paragraph fidelity

### Daemon chat (`T`)
- TalkDaemonScreen modal (primary-themed; soft/loud counterpart to `t`)
- `set_pending_netrunner_message` on DaemonSession (FIFO stack)
- `_format_outcomes` prepends `≫ NETRUNNER MESSAGE:` preamble with
  numbered list when stacked
- Empty-outcomes-only branch produces clean message
- No-session warning + drop with toast
- Wake-event keeps idle delivery prompt

### Goal-edit mid-flight (`e`)
- Mid-flight block lifted; opens modal pre-filled with current goal
- `_classify_goal_diff` heuristic: tokenize+stem+Jaccard
  → `clarification` / `scope-change` / `pivot`
- `set_pending_goal_update` on DaemonSession; outcome-loop wakes idle
- `_format_outcomes` prepends GOAL UPDATE preamble with classification-
  tailored advice
- Identical-goal submit no-ops with toast
- Force-push deferred to M5+

### Connection state monitor
- `connection_monitor.py`: heartbeat to api.anthropic.com:443
- States: Online (●green) / Degraded (◐yellow) / Offline (●red)
- Sidebar indicator + chatlog announcements on transition
- DNS failure skips Degraded → Offline directly
- Threshold-based: 2 failures → Degraded, 1 success → Online
- `record_subprocess_error(stderr)` hook (not yet plumbed)
- Spec'd consequences NOT YET WIRED: spawn-blocking, daemon parking,
  recovery flow

### Streaming defaults
- Daemon `streaming_mode=True` (was opt-in; user observed "nuclear
  speed improvement"). `--no-streaming` opts out.
- Watchdog `streaming_mode=True`. Persistent subprocess shared across
  questions; conversation accumulates so watchdog "remembers" earlier
  questions in session.

### Tabbed bottom panel
- `[Daemon] [Watchdog]` tabs in `TabbedContent(id="daemon_bar")`
- WatchdogPane mirror of DaemonPane (yellow, status with queue-depth)
- Both inner logs focusable (W/S nav reaches them via fall-through)
- Space on daemon_log → action_talk_daemon; space on watchdog_log →
  action_talk_watchdog

### Spawn provenance (origin badges)
- `fleet.spawn(..., origin=...)` — `daemon` / `netrunner` / `inject`
- Threaded into `spawned` meta event payload
- Chatlog renders cyan `[you]` for netrunner, `[↳you]` for inject;
  daemon spawns un-badged
- Watchdog system prompt includes badge legend (so it stops reverse-
  engineering attribution from log timing)

### z-to-view (file/profile/script)
- `action_expand` on FileListItem / ProfileListItem / ScriptListItem
  opens ExpandModal with file content from disk
- Pygments syntax highlighting via `rich.syntax.Syntax` for ~30
  recognized languages
- Theme: `github-dark`. Line numbers off (gutter tint reads as
  "highlight" — too aggressive)
- Detection cascade: extension → bare-name → shebang
- Plain-text fallback with bracket escape for unknown extensions
- 2MB size cap; UTF-8 with replacement
- Modal scroll bindings: w/s line, PgUp/PgDn page, Home/End jump

### Path-normalized Files panel dedupe
- Bug: Windows backslash vs forward slash + dispatcher
  `Path(p).resolve()` produced literal-distinct strings → double-listing
- Fix: `os.path.normcase(os.path.normpath(p))` as dedupe key
- Same normalization on `_remove_file_from_panel`

### Focus navigation fall-through
- W/S walks within section; at section edge, falls through to
  up/down neighbor section
- Empty sections skipped transitively
- Trap fix: when chain dead-ends through empty sections (e.g. W from
  daemon_bar with empty main), fallback lands on any populated non-
  source section. Layout edges (true None terminator with no walking)
  stay put. Distinction: `walked=True` flag.

### UI infrastructure
- ExpandModal universal magnifier (`z`) — RichLogs, ConstructPanes,
  list items
- Rich text preservation via Text/segment round-trip
- Modal Tab fix: App-level Tab delegates to `screen.focus_next`
- `?` keybinds modal slim
- Quit unified to `ctrl+q`
- Path shortening utility (`_shorten_path`)
- Connection indicator in sidebar
- `sys.unraisablehook` filter for Windows Proactor closed-pipe noise

---

## Key design decisions (carried forward)

1. **Brake state is deck-global, not profile-attached.** The
   netrunner sets it via `b`; it applies to every new spawn until
   changed. Watchdog can ratchet up (toward paranoid) but not down
   — that's the netrunner's exclusive prerogative.
2. **Profiles are prescriptive, not restrictive.** They steer with
   addendums and suggest tools via `recommended_tools`; they do NOT
   gate capability. Runtime gating is the brake hook's job, deck-wide.
3. **Brake enforcement is via PreToolUse hook, not `--allowedTools`.**
   The hook is deterministic (regex/path matching, no LLM in the hot
   path). Watchdog observes denials and authors the hook's policy
   over time (LLM authors, deterministic enforces).
4. **Default profile auto-seeded; netrunner edits sacred.**
5. **Lowercase = within-focus, uppercase = move-focus.** `z` for zoom.
6. **`space` is "primary interact"; `z` is magnify.**
7. **Truncation: 500 live, 5000 modal.** Bounded against megabytes.
8. **Pool always warms with `default`.** No per-profile pools.
9. **Files panel: dual path with dedupe (normalized).**
10. **Marker protocol one-way (script → deck).** Versioned. Unknown
    action logs warning; never crashes.
11. **Tools panel:** Profiles + Scripts only. Built-ins not surfaced.
12. **Goal-update propagation deferred to next break.** Force-push is
    M5+. Wake-event keeps idle sessions responsive.
13. **Goal-diff classifier is heuristic, not model-driven.**
    Cheap; "good enough"; can model-ify later.
14. **ConnectionMonitor presumes ONLINE at start.**
15. **DNS failure skips Degraded.** Clean signal: no network at all.
16. **Watchdog runs cloud Claude today.** Local-model substrate (D1)
    is the eventual home.
17. **Streaming is the default; one-shot is the fallback.** For both
    daemon and watchdog.
18. **Streaming wedge → kill, don't preserve-and-pray.** Once stuck,
    fresh subprocess is the only reliable recovery.
19. **Origin attribution at source, not reverse-engineered.** Fleet
    payload carries who spawned each construct.
20. **z-modal:** bracket escape on plain text, syntax highlighting
    on known languages, github-dark theme, no line numbers.

---

## Filed gotchas (institutional memory; cumulative)

### Terminal / Textual
- **`shift+space`, `ctrl+space`, `ctrl+i`, `ctrl+m`** rarely transmit
  distinctly in real terminals. Trust pilot for binding wiring; trust
  real terminal for capability.
- **Textual `Widget.name` is read-only.** Don't shadow.
- **`Log.lines` is `list[str]`; `RichLog.lines` is `list[Strip]`.**
- **Markup leaks via `\n`.** Collapse before writing.
- **`wrap=True` + `min_width=1` inside an inactive TabPane** pre-wraps
  content at 1 char per line and caches Strips. Use `wrap=False` for
  logs in non-default tabs OR buffer-and-replay on activation.
- **`can_focus=False` on a Static-derived class is a no-op** because
  Static defaults that way.
- **Modal screens don't inherit App BINDINGS.** Redeclare on the modal.
- **App-level priority bindings** beat modal priority. Delegate from
  App action when `isinstance(self.screen, ModalScreen)`.
- **ListView focus model:** the focused widget IS the ListView;
  `.highlighted_child` is cursor.
- **Two TabbedContents need `id` to disambiguate `query_one`.**

### Rich markup
- **Markup escape:** `\[` for opening bracket, closing `]` is literal.
  Use raw f-strings (`rf"..."`) to silence Python escape warnings.
- **File contents need bracket escape** before going to a markup-
  enabled RichLog. `[default]` TOML headers will be parsed otherwise.
- **`rich.syntax.Syntax` returns a single Renderable** but RichLog
  splits it into Strips, so scrolling stays line-by-line.

### Async / subprocess
- **`stdin.close()` on Windows ProactorEventLoop is fire-and-forget.**
  Always pair with `await stdin.wait_closed()` (with a timeout).
  Without this, transport `__del__` fires on a half-closed socket and
  raises `ValueError: I/O operation on closed pipe` after Ctrl+C.
- **`sys.unraisablehook` is the place** to filter known-harmless
  GC-time noise.
- **"Preserve the wedged proc, hope it recovers" is always wrong**
  when the failure mode is read-hang. Once wedged, kill and respawn.
- **Streaming subprocesses accept writes long after they've stopped
  reading.** Broken-pipe errors don't fire reliably for read-hangs.
  Drain timeouts are the real signal.
- **`-p` not immediately followed by a value** makes claude treat it
  as "read from stdin." Always pipe prompts via stdin
  (`proc.communicate(input=...)`).
- **Rapid heartbeat tests are racy.** Use `wait_for(predicate)` with
  timeout, not fixed sleeps.

### File paths
- **String equality on file paths is wrong on Windows.** Forward vs
  backslash, drive letter case, and resolve-vs-raw all break literal
  compare. Use `os.path.normcase(os.path.normpath(p))` for dedupe.
- **`Path(p).resolve()`** can normalize differently from how the
  original was passed; don't rely on it for stable identity.
- **Path shortening keeps absolute version stored separately.**
- **Windows path mangling in Bash.** Constructs self-correct from
  absolute `C:\...` to relative when their first attempt fails.

### Editing
- **`str_replace` ate a class header once** (GoalSetScreen) — when
  matched block ends just before `class X:`, double-check. Compile-
  clean doesn't mean structurally clean.
- **`str_replace` ate a docstring close.** New content with `"""`
  mid-replacement, double-check the close didn't end up orphan.
- **Bare `except Exception: pass` around mixed-failure-mode code
  hides real bugs.** Scope try/except tightly.
- **Local var names shadowing kwargs** are a footgun even when they
  technically work.

### Logic
- **`_format_outcomes` empty-outcomes branch.** Conditional headers.
- **Directional fall-through needs a `walked` flag** to distinguish
  layout edges from dead-end empty chains.
- **`_focus_section` branches need to be re-checked when section
  contents change.** No-op return is silent.
- **`_right_panel_focusables` is hand-curated, not auto-derived
  from compose().** Adding a new ListView to the Tools tab without
  also adding it here makes it visible-but-unreachable via W/S.
  Burned this when adding the Plugins section. Look here whenever
  the right panel grows a new section.

### Brake / hook
- **LLMs route around denial.** A construct given Bash-denied will
  pivot to PowerShell automatically without being asked — verified
  on real-deck after the brake hook initially over-blocked
  legitimate plugin invocations. Implication: any tool-gating layer
  must consider the equivalent capability on the platform, not just
  the tool the human happens to think of. Both Bash and PowerShell
  must be gated equivalently on Windows; on Linux the equivalent
  consideration is Task-spawned sub-agents (different vector but
  similar threat model).
- **Substring matching the deck source dir over-blocks because
  cyberdeck-home/ is a subdirectory.** A `bash command contains
  <deck source dir>` check denies every legitimate plugin and
  dispatcher invocation (`python <deck>/cyberdeck-home/plugins/
  .../run.py`). Use sentinel filenames (brake_hook.py /
  brake_state.py / brake_patterns.py) for tampering protection;
  the path-overlap defeats prefix matching. Layout reorg (move
  cyberdeck-home/ outside the deck source dir) is one fix; not
  current scope.
- **`files_written` tracks attempted writes, not confirmed ones.**
  Construct.py populates from the model's `tool_use` blocks (model
  says it wrote a file), not from successful tool_results. When
  the brake hook denies, the path stayed in the list before we
  fixed it. fleet.py's _consume now subtracts denied paths from
  files_written at finalize time using normcase+normpath.

---

## Tech debt (not blocking)

- Script polling at refresh time
- Script manifests not implemented (raw filenames only)
- Esc-up tree navigation deferred
- Unknown-action warnings could spam
- Construct script-launch wiring stubbed
- Watchdog substrate cloud-only
- Connection consequences indicator-only
- Goal-diff classifier crude stem
- `Construct.kill()` sets state pre-confirmation
- `classify_event` kind values are bare strings
- Read tool 25k token limit — profiles bias toward Bash+wc-l
- Long-running watchdog session accumulates context indefinitely

---

## Not implemented (deferred features)

- **Plugins** — third leg of tool registry
- **Watchdog tripwires + blacklist** — DSL, deterministic matcher
- **Connection consequences** — spawn-blocking, daemon parking
- **Routing** (`r`) — wire constructs together
- **Plugin airgap (`p`), quickfire (`c`), picker (`Shift+C`)**
- **Daemon pause/unpause (`E`)**
- **Goal-edit force-push** — apply-now interrupt
- **B2 fleet synthesizer** — substrate-blocked on D1
- **D1/D2/D3** — local-model runtime, arbiter, B2 on local
- **Compliance mode (Phase E)**

---

## Collaboration patterns that work

- **Mock-first development.** Real claude when assumption hinges on
  opaque server behavior.
- **One milestone at a time.** Each ships before next starts.
- **Real-claude testing pause-points.** Two minutes of testing > hours
  of speculation. Almost every recent bug was caught this way.
- **Banter encouraged, work prioritized.**
- **Push back when wrong; check before acting when ambiguous.**
- **State doc + build plan refresh between major slices.**
- **Screenshots > stack traces > prose.** When a bug is visual, a
  screenshot solves 80% of the diagnosis.
- **Half-finished refactors leave landmines.** When a session ends
  mid-refactor, the next session needs to find and fix before
  continuing. Always close the loop on the current method.

---

## Migration to Claude Code

**Why now:** the deck is at 12k LOC across 13 modules. Multi-file
edits, refactors, and grep-the-codebase questions have become the
bottleneck. Claude Code edits files in place, runs greps natively,
and doesn't suffer the context-truncation issues a long chat thread
eventually does.

**What to bring:**
1. This file (`cyberdeck-state.md`).
2. The build plan (`cyberdeck-build-plan.md`).
3. The orientation (`cyberdeck-claude-code-orientation.md`).
4. The spec (`cyberdeck-spec.md`).
5. The philosophy (`cyberdeck-philosophy.md`).
6. The codebase itself (in git, not the chat).

**What changes:**
- No more "FILES TO REPLACE" blocks — Claude Code edits in place.
- No more `cp /home/claude/cyberdeck/foo.py /mnt/user-data/outputs/`.
- Test runs are local (the chat had no real terminal).
- The user can grep, the AI can grep, no more "let me look for…"
  query rituals.

**What stays:**
- Real-claude testing as the ground truth for streaming/permissions/
  Windows quirks. Mocks miss too much.
- Mock-first development for new modules.
- One milestone at a time.
- The whole gotchas list — none of these go away.
- The pushback culture (you've caught the AI being wrong many times;
  keep doing that).

**What to ask before next session:**
1. **Plugin scaffolding** — third leg of tool registry.
2. **Connection consequences** — spawn-blocking on Degraded.
3. **Watchdog tripwires** — the harder half of watchdog.
4. **D1 local-model runtime** — substrate for everything AI-deferred.
5. **The tools-research chat** — using `cyberdeck-tools-research-seed.md`.
