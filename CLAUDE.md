# Cyberdeck — project memory

A keyboard-first Textual TUI that orchestrates Claude Code subprocesses.
The "daemon" coordinator decomposes goals; "constructs" execute in
parallel; the "watchdog" oracle answers questions about fleet activity.
Personal hobby project, in active production use on Windows.
~14k LOC across 19 Python modules at the deck-source root.

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

Two more for specific moments: `cyberdeck-project-instructions.md`
(collaboration norms), `cyberdeck-tools-research-seed.md` (seed for a
future tools-research conversation). `cyberdeck_arbiter_design.md` is
a deferred wearable-form-factor variant — not current scope.

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

- `*.py` (root) — source. `tui.py` is the heart (~6k LOC, well-organized
  but huge — grep for similar patterns before adding a feature).
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
