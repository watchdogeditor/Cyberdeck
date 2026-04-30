# Cyberdeck — project memory

A keyboard-first Textual TUI that orchestrates Claude Code subprocesses.
The "daemon" coordinator decomposes goals; "constructs" execute in
parallel; the "watchdog" oracle answers questions about fleet activity.
Personal hobby project, in active production use on Windows.
~16k LOC across 21 Python modules at the deck-source root (as of
2026-04-30, post-spine).

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

Two architecture docs for the major in-flight initiatives:
  - `cyberdeck-event-stream-design.md` — the spine (one canonical
    event bus, role-derived filters). Phases 1-7 shipped 2026-04-30;
    Phase 8 cleanup is the last remaining slice.
  - `cyberdeck-maintbot-design.md` — the Mechanic (separate-process
    supervisor + on-demand LLM session). Two-tier architecture
    landed 2026-04-30; v0 (supervisor only — subprocess janitor) is
    the next implementation slice.

Two more for specific moments: `cyberdeck-project-instructions.md`
(collaboration norms), `cyberdeck-tools-research-seed.md` (seed for a
future tools-research conversation). `cyberdeck_arbiter_design.md` is
a deferred wearable-form-factor variant — not current scope.

## Where the deck is right now (2026-04-30)

Just shipped a substantial spine refactor. Every event source
publishes through `event_bus.py`; the file logger writes per-launch
NDJSON files at `<deck source>/logs/cyberdeck-YYYY-MM-DD-HHMMSS.log`
+ `latest.log` pointer; Ctrl+C-as-copy doesn't kill the deck (silent
SIGINT swallow); smart Ctrl+Q with running-state guard.

Real-deck verified: spine 1-6, slice 2 LLM-authored tripwires (rung-1
fork + rung-2 fresh both work), file logger end-to-end, magnified
view + watchdog Q&A still see all event markers.

**Next session picks up at: Mechanic v0 — supervisor only.** A
sibling Python process to the deck that watches the deck's PID,
tracks its claude subprocesses (read from the file logger's
`fleet.spawn` events), and kills them on detected deck death.
Cross-platform Python; no Job Object plumbing in the deck. Concrete
v0 use case is the orphan-subprocess problem caught during
2026-04-30's Ctrl+C autopsy. See `cyberdeck-maintbot-design.md` v0
section for full design + the "PID publish channel" sub-section
(lean: add `pid` to `fleet.spawn` event payloads, one-line change at
each spawn site in fleet.py).

After Mechanic v0: spine Phase 8 cleanup (retire deprecated
`add_listener`/`on_*` shims now that everyone publishes through the
bus), then in-deck copy keybind (sidesteps Ctrl+C-as-copy issue at
the UX layer cross-platform).

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

- `*.py` (root) — source. `tui.py` is the heart (~7.4k LOC after
  spine, well-organized but huge — grep for similar patterns before
  adding a feature). `event_bus.py` (the spine), `logger.py`
  (DeckLogger + per-launch NDJSON), and per-source translators
  (`fleet._fleet_event_to_deck_event`, `daemon_session._daemon_event_to_deck_event`)
  are the post-2026-04-30 additions.
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
