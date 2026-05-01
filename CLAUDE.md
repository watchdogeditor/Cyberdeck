# Cyberdeck — project memory

A keyboard-first Textual TUI that orchestrates Claude Code subprocesses.
The "daemon" coordinator decomposes goals; "constructs" execute in
parallel; the "watchdog" oracle answers questions about fleet activity.
Personal hobby project, in active production use on Windows.
~19k LOC across 23 Python modules at the deck-source root (as of
2026-04-30, post-spine + post-y/Y-copy + post-limits-rework).

The user is the "netrunner." Match the cyberpunk vocabulary in code
and prose — these are the right names for what each thing does.

## Read first

The single most important file in this codebase is the **orientation
doc**. Read it whenever you start fresh on a non-trivial task:

  `Design Files/cyberdeck-claude-code-orientation.md`

Then the four other canon docs, in this order:

  - `cyberdeck-state.md` — what's shipped, design decisions, gotchas
  - `cyberdeck-build-plan.md` — milestone status, what's next
  - `cyberdeck-spec.md` — canonical architecture (the *what*)
  - `cyberdeck-philosophy.md` — convictions that resolve ambiguity (the *why*)

Three architecture docs for the major in-flight initiatives:
  - `cyberdeck-event-stream-design.md` — the spine (one canonical
    event bus, role-derived filters). Phases 1-7 shipped 2026-04-30;
    Phase 8 cleanup is the last remaining slice.
  - `cyberdeck-maintbot-design.md` — the Mechanic (separate-process
    supervisor + on-demand LLM session). Two-tier architecture
    landed 2026-04-30; v0 (supervisor only — subprocess janitor) is
    the next implementation slice.
  - `cyberdeck-model-effort-design.md` — caliber (per-spawn model +
    effort + fast-mode). Daemon picks per construct based on task
    + remaining quota; netrunner overrides via Limits modal or
    daemon chat. Filed 2026-04-30; implementation queued behind
    Mechanic v0. Phase 4 (quota-aware fallback) hard-blocks on
    build-plan item 13.

Two more for specific moments: `cyberdeck-project-instructions.md`
(collaboration norms), `cyberdeck-tools-research-seed.md` (seed for a
future tools-research conversation). `cyberdeck_arbiter_design.md` is
a deferred wearable-form-factor variant — not current scope.

## Where the deck is right now (2026-04-30, late)

Spine 7/8 phases shipped (event_bus + every producer migrated +
file logger as bus subscriber + quit discipline). Plus a follow-up
session on 2026-04-30 added:

- **y/Y copy keybind.** Vim-yank focused widget to clipboard
  (lowercase = rendered text, uppercase = structured JSON of the
  underlying data — bus snapshot for chatlog, raw events for
  ConstructPane, dataclass dicts for list items). New `clipboard.py`
  module (ctypes Win32 + pbcopy + xclip/wl-copy cascade, stdlib
  only). Sidesteps Ctrl+C-as-copy SIGINT-into-subprocesses pain.
  Two diagnosis detours filed as gotchas: (1) `text=True` + cp1252
  encoder silently exploding on Unicode then timing out;
  (2) clip.exe preserving the UTF-16-LE BOM into clipboard contents.
- **Limits modal rework.** Hard ceiling on max_concurrent (was 9)
  retired. Defaults bumped (max_concurrent 5→10, max_total_spawns
  20→30, pool_size 3→5). pool_size now editable in the modal.
  Pool refill gate added so a lowered target stops oversubscribing.
  Latent `max_total_spawns == 0 = no cap` daemon-session guard
  finally honors what the modal had long advertised.
- **Mechanic v0 — supervisor only.** Sibling Python process
  (`mechanic.py`, ~270 LOC) that watches the deck PID, tails the
  file logger's NDJSON for live claude subprocess pids, and kills
  them on detected deck death. Cross-platform stdlib + ctypes
  (Windows `OpenProcess`/`GetExitCodeProcess`; POSIX `os.kill(pid,
  0)`). Deck-side: `pid` field on `log_header` (so mechanic
  discovers the deck PID by self-reading the header) + `pid` on
  `fleet.spawn` payloads via new `Construct.pid` property.
  `launch.bat` spawns mechanic in a minimized sibling 1s after the
  deck launches. Real-deck verified at attach + tracking; orphan-
  cleanup path verified by synthetic smoke test. Known limitation:
  only constructs are tracked — daemon / watchdog Q&A / authoring
  one-shots / pool warmer subprocesses still orphan their pre-
  mechanic way (filed as a follow-up).
- **Spine Phase 8 — listener shim cleanup.** Retired
  `add_listener` / `remove_listener` / `on_event` / `on_change` /
  `on_state_change` across five producers (brake_state,
  profile_registry, plugin_registry, fleet, connection_monitor)
  plus consumers in tui.py and daemon_session.py. Bus is now the
  only fan-out path. ~75 LOC net deletion. **Unified-event-stream
  slice complete (8/8 phases shipped).** Three callback patterns
  deliberately not migrated (Pool, Daemon, Blacklist on_event) —
  integration interfaces, filed as Phase 8b candidates.
- **Kill state-stuck race fix.** Both `k` and `Shift+K` were
  leaving the construct pane stuck at `[RUNNING]`. Race between
  `Construct.kill()` and `_consume`'s `wait()` — both call
  `proc.wait()` on the same Process; if wait() resumed first, it
  correctly skipped DONE/FAILED overwrite (`_kill_requested` set)
  but never wrote KILLED, so `_consume` emitted finalize with
  `state="running"`. Fix in `Construct.wait()`: write KILLED
  explicitly in the `_kill_requested + proc-died` branch.
  Belt-and-suspenders with kill()'s own state-flip. Filed as a
  gotcha (Async / subprocess section).

Real-deck verified: spine 1-6, slice 2 LLM-authored tripwires
(rung-1 fork + rung-2 fresh both work), file logger end-to-end,
magnified view + watchdog Q&A still see all event markers, y/Y
yank against every focusable surface (chatlog, fleet/daemon/watchdog
panes, ConstructPane, magnified view, list items), pool refill
gate (target lowered + spawn doesn't refill above new target),
mechanic attach (header pid discovery, log tailing, 2s heartbeat),
Phase 8 bus subscriptions (every migrated path renders correctly
in the chatlog), kill state transitions (k + Shift+K both move
panes to `[KILLED]` + chatlog shows orange × glyph).

**Next session picks up at: 🚨🔥 SAFETY ARCHITECTURE PASS
(CRITICAL CLUSTER).** Real-deck testing + log analysis on
2026-04-30 (late) revealed the structural truth: **the brake
hook is doing 95% of real safety work alone, and most other
"safety" layers don't compose with it.** Tripwires today are
observation-only stubs (the severity-driven escalation chain was
the intended design but was never wired). Profiles are pure
prescription with zero security weight. Watchdog has teeth only
at spawn-time via Blacklist refusal. **If brake misses a pattern
nothing else stops the call** — and brake misses an entire
category (MCP tools, all of them: `mcp__claude_ai_Supabase__
execute_sql`, Gmail send-after-auth, Drive write, Calendar — log-
confirmed exposed to every construct under default brake). See
`cyberdeck-state.md` "Safety architecture analysis" section for
full layer breakdown + intended-vs-today comparison.

The pass is four composable slices, ship in this order:

1. **🚨 MCP gating in `brake_hook.py`** (critical — closes widest
   unprotected attack surface). Verb-based pattern matching:
   default brake denies destructive verbs (`execute_*`, `apply_*`,
   `send_*`, `delete_*`, `create_*`, `update_*`, `deploy_*`,
   `drop_*`, `merge_*`, `migrate_*`, etc.) and allows read-shaped
   (`get_*`, `list_*`, `search_*`). Per-spawn allowlist override
   for explicit opt-in. ~30 LOC.
2. **🔥 Tripwire escalation chain** (architectural unfinished
   work). `low`=log only (current); `warning`=log + redirect via
   brake-style denial on next tool call; `critical`=auto-term
   with structured "why" bus event; `critical + bad enough`=
   auto-term + auto-blacklist. Hybrid threshold: deterministic
   floor + watchdog LLM judgment + 30s netrunner approval window.
   Turns tripwires into INPUTS to the existing hard-gate layers.
3. **Variable-outcome pause UX** (re-frame from netrunner).
   Brake state determines DEFAULT ACTION; pause window is the
   netrunner's chance to OVERRIDE. YOLO=pause-before-allowing,
   Default=pause-before-denying-destructive,
   Paranoid=pause-before-anything. Tool-calls bus-driven sticky
   panel + Z-keybind to negate the default. Configurable in
   Limits as `pause_window_seconds`, default 0. Subsumes the
   original "review delay" filing AND parts of kill-deny-in-
   flight-tool-calls AND sticky tool-call surface — one
   mechanism, three problems.
4. **DEFAULT_TRIPWIRES expansion + authoring prompt fix.** Default
   set must include shell-destructive baselines (rm -rf, format,
   dd, mkfs, fork bombs, shutdown). Authoring prompt forbids the
   "brake handles X so tripwire skips X" antipattern (real-deck
   observed: authoring negative-lookahead-EXCLUDED `rm -rf`
   because "brake will block it" — exactly the antipattern that
   defeats layered defense).

Plus discrete bugs / observations from log analysis to land
alongside the cluster:
- **Enum payloads serialize as empty `{}`** in
  `_serialize_payload` (3-line fix).
- **Kill doesn't interrupt in-flight assistant turns** — model
  finishes turn before SIGTERM lands.
- **Daemon over-volunteers destructive content** (added
  `shutdown -h now` unprompted to a rm-rf-style test).
- **Construct refusal text buried in result event** — should be
  a structured `kind=construct.refused`.
- **~30k token cache miss per spawn** — system prompt drift
  invalidates prompt cache.

After the safety architecture pass, the queued slices in priority
order: caliber selection (per-spawn model + effort + fast-mode —
see `cyberdeck-model-effort-design.md`); daemon narrative fix
(mislabel brake-hook denials as tripwire fires); log-readability
overhaul; Mechanic v1 (LLM session half); Mechanic v0 follow-ups
(track non-construct subprocess sources); Phase 8b (Pool + Daemon
callback cleanup).

## Running it

```
python tui.py                       # idle — set goal in-app with `e`
python tui.py "task A" "task B"     # ad-hoc constructs, no daemon
python tui.py --goal "..."          # daemon-driven mode
CLAUDE_BIN=./mock_claude.py python tui.py ...   # offline smoke test
```

Smaller entry points: `main.py` (one construct, console),
`fleet.py` (multi-construct, console).

Real claude needs `npm install -g @anthropic-ai/claude-code` and a
logged-in Max account. Windows: run from Windows Terminal or
PowerShell 7 (not cmd.exe) for TUI rendering. On PowerShell, set env
vars with `$env:NAME = "..."`, not bash syntax.

## Layout

- `*.py` (root) — source. `tui.py` is the heart (~8.1k LOC after
  spine + y/Y + limits rework, well-organized but huge — grep for
  similar patterns before adding a feature). `event_bus.py` (the
  spine), `logger.py` (DeckLogger + per-launch NDJSON), and
  per-source translators (`fleet._fleet_event_to_deck_event`,
  `daemon_session._daemon_event_to_deck_event`) are the
  2026-04-30 spine additions. `clipboard.py` (cross-platform
  clipboard write, ctypes Win32 + pbcopy + xclip/wl-copy) is the
  late-2026-04-30 y/Y addition.
- `<deck source>/logs/` — per-launch log files. Operational artifacts;
  brake hook protects them from constructs by default. `latest.log`
  always points at the current run.
- `Design Files/` — the canon. Update these when major decisions change;
  don't let docs drift behind code.
- `cyberdeck-home/` — runtime working dir. Profiles, plugins,
  dispatcher script, logs, ejection snapshots. Constructs are
  soft-sandboxed here (cwd default; not a hard sandbox — absolute
  paths bypass). Override with `--home <dir>` or `$CYBERDECK_HOME`.
- `Previous Versions/` — milestone snapshots (zips). Read-only history.
- `README.md` — current; pitch + status + run commands + design-doc
  index. Lighter than the canon; trust the design docs for depth.

## Hard rules

The orientation doc explains the reasoning behind each. Condensed:

- **Real-claude testing beats mock testing** for anything touching
  subprocess lifecycle, streaming, or Windows quirks.
- **Close the loop on each refactor before stopping.** Half-finished
  refactors leave landmines for the next session.
- **The gotchas list is cumulative and sacred** (`cyberdeck-state.md`
  → *Filed gotchas*). Re-introducing a known bug wastes a session.
  Read it when touching subprocess lifecycle, modal screens, or async
  cleanup.
- **Push back politely when the user is wrong.** They expect it.
- **Opinionated changes over flag soup.** Pick one path; document why.
- **Don't merge daemon and watchdog.** Soft/loud distinction is core.
- **Don't conflate netrunner and daemon.** The human is a participant,
  not an input the daemon receives.

## Repo state

Live at **github.com/watchdogeditor/Cyberdeck** (private). Default
branch `main`. `.gitignore` already excludes `Previous Versions/`,
`cyberdeck-home/*` (except `profiles/` and `plugins/`),
`__pycache__/`, image files, and `.claude/` machine-local files.

## Git conventions

- **Do NOT add `Co-Authored-By:` trailers to commits.** GitHub
  parses that syntax and shows a second contributor on the commit
  page (e.g. "watchdogeditor and claude"), which makes Claude look
  like a separate user account. The netrunner runs this repo solo;
  one contributor on the GitHub UI matches reality. The default
  Claude Code instructions say to add the trailer; override that
  here.
- **Credit Claude in the commit body** (not as a trailer) when the
  AI did substantive work. A line like `Built with Claude Code
  (claude-opus-4.7, 1M context)` at the bottom of the body is just
  text — GitHub doesn't parse it, doesn't add a contributor, and
  the credit lives in the log for anyone who looks. Skip the credit
  on small fixes / cosmetic edits where it'd be noise.
- Commit messages otherwise follow the existing style: subject
  line under ~72 chars, body explains the *why* with paragraph
  breaks, multi-line. Look at `git log` for tone.
