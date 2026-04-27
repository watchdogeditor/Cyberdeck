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

**Up next:** plugin scaffolding (third leg of tool registry), then
connection consequences (spawn-blocking on Degraded), then D1 (local
model substrate) for the long-term Watchdog/synthesizer/arbiter story.

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

### Tier 2 — Profiles + Brake tiers
- TOML loader, ProfileRegistry, hot reload, default seeded
- Daemon picks profile per-spawn via JSON
- `allowed_tools` narrowing
- Brake tiers: paranoid / default / yolo, two-axis privesc check

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

1. **Daemon cannot escalate.** Two-axis privesc (tier + tool subset).
2. **Default profile auto-seeded; netrunner edits sacred.**
3. **Lowercase = within-focus, uppercase = move-focus.** `z` for zoom.
4. **`space` is "primary interact"; `z` is magnify.**
5. **Truncation: 500 live, 5000 modal.** Bounded against megabytes.
6. **Pool always warms with `default`.** No per-profile pools.
7. **Files panel: dual path with dedupe (normalized).**
8. **Marker protocol one-way (script → deck).** Versioned. Unknown
   action logs warning; never crashes.
9. **Tools panel:** Profiles + Scripts only. Built-ins not surfaced.
10. **Goal-update propagation deferred to next break.** Force-push is
    M5+. Wake-event keeps idle sessions responsive.
11. **Goal-diff classifier is heuristic, not model-driven.**
    Cheap; "good enough"; can model-ify later.
12. **ConnectionMonitor presumes ONLINE at start.**
13. **DNS failure skips Degraded.** Clean signal: no network at all.
14. **Watchdog runs cloud Claude today.** Local-model substrate (D1)
    is the eventual home.
15. **Streaming is the default; one-shot is the fallback.** For both
    daemon and watchdog.
16. **Streaming wedge → kill, don't preserve-and-pray.** Once stuck,
    fresh subprocess is the only reliable recovery.
17. **Origin attribution at source, not reverse-engineered.** Fleet
    payload carries who spawned each construct.
18. **z-modal:** bracket escape on plain text, syntax highlighting
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
- `Fleet.spawn()` slices `_build_command()[1:]` — builder API ugly
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
