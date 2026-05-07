# Cyberdeck — Spawn Context Isolation Design

> **STATUS: PHASE 1 SHIPPED 2026-05-05; PHASE 2 CONDITIONAL.**
> Updated 2026-05-07.
>
> **Phase 1 (env-var belt) is complete:** per-role
> `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` + auto-memory + git-instructions
> applied to Advisor, Construct, Daemon, Pool warmer, Tripwire-authoring
> Watchdog (KILLED). Watchdog Q&A KEEPS auto-load (deck "security
> analyst" benefits from CLAUDE.md context). See `cyberdeck-state.md`
> Filed gotchas → Async/subprocess for the upstream userEmail leak that
> spawned this work + the multi-line argv truncation gotcha that
> shipped alongside it.
>
> **Phase 2 (role-injection infrastructure) is deferred — conditional.**
> Real-deck verification of phase 1 confirmed daemon + constructs do
> NOT regress without CLAUDE.md auto-load. Pull phase 2 forward only on
> concrete regression. The forward-looking line item is in
> `cyberdeck-build-plan.md` → CURRENT FRONTIER item 2.
>
> **Read phase-1 sections to understand WHY the env-var belt is shaped
> the way it is. Read phase-2 sections only when picking up phase 2.**

---

*Architecture for stopping the deck's `claude` subprocesses from
silently leaking the deck's project memory (CLAUDE.md, auto-memory,
user-level memory, rules dirs) into every turn — and replacing it
with explicit, programmatic per-role prompt injection from
disk-backed role-config files. Filed 2026-05-05 after the discovery
during Advisor verification. Pair with `cyberdeck-spec.md` (runtime
architecture), `cyberdeck-philosophy.md` (separation-of-concerns
reasoning), `cyberdeck-state.md` (current status), and
`cyberdeck-build-plan.md` item 000 (where this slots — highest-
priority deferred slice).*

*Implementation deferred. Touches every spawn site in the deck and
needs careful A/B verification because the daemon and constructs
may have been quietly free-riding on auto-loaded CLAUDE.md content.
Designing first means we don't burn a session discovering the
per-role interactions mid-implementation.*

---

## Why this exists

While verifying the Advisor (Tools-UI 0c sub-feature 3) on
2026-05-05, the netrunner caught the model answering with content
from the deck's project-root `CLAUDE.md` — "From the CLAUDE.md, I
know the deck has a plugin system…" — without using any tool
calls. The Advisor uses `claude -p` with bypassPermissions and was
not granted any way to Read files. Yet it knew the deck's plugin
architecture, file layout, and design conventions.

Walking the official Claude Code docs at
https://code.claude.com/docs/en/memory and
https://code.claude.com/docs/en/env-vars confirmed: **Claude Code
silently auto-loads multiple memory layers from disk at every
session start**. Quoting the docs verbatim:

> "Claude Code reads CLAUDE.md files by walking up the directory
> tree from your current working directory, checking each
> directory along the way for `CLAUDE.md` and `CLAUDE.local.md`
> files. ... All discovered files are concatenated into context
> rather than overriding each other."

> "The first 200 lines of `MEMORY.md`, or the first 25KB,
> whichever comes first, are loaded at the start of every
> conversation."

Every `claude` subprocess the deck spawns — daemon, watchdog
(Q&A), watchdog (tripwire authoring), constructs, pool warmers,
advisors — has been silently inheriting:

1. The deck's project-root `CLAUDE.md` (~700 lines: build plans,
   design notes, gotchas, in-flight slice descriptions, security
   architecture details).
2. Walk-up: any `CLAUDE.md` in parent dirs of the deck source.
3. `~/.claude/CLAUDE.md` (user-level memory).
4. `~/.claude/projects/<git-repo-key>/memory/MEMORY.md`
   (auto-memory, first 200 lines / 25KB; keyed by git repo, all
   worktrees of the cyberdeck share one).
5. `~/.claude/rules/*.md` and `<cwd>/.claude/rules/*.md`.
6. Managed-policy CLAUDE.md, if installed (org-deployed; the
   docs say explicitly it "cannot be excluded").
7. `@path/to/file` imports inside any of the above (recursive,
   max 5 hops).
8. SessionStart hooks, if configured.

This explains three things previously chalked up to other causes:

- **The "mysterious knowledge" the daemon and constructs have
  always seemed to have of the deck's architecture.** Some of
  what we attributed to "the system prompt is working great" was
  actually CLAUDE.md leaking through. The system prompts are
  almost certainly under-specified; we just couldn't tell.
- **The residual ~19k cache_creation tokens per spawn the
  2026-05-02 cache fix left unresolved.** Filed as "Anthropic's
  court" — almost certainly auto-loaded `CLAUDE.md` drifting
  across sessions and invalidating cache.
- **Information leakage of in-flight design work into every
  construct.** Not catastrophic on a solo dev's machine but a
  stretch of trust never intended.

---

## What auto-loads today (the layers)

Each layer is appended to the context as a user message after the
system prompt:

| # | Layer | Disk location | Notes |
|---|-------|---------------|-------|
| 1 | Managed-policy CLAUDE.md | `/Library/Application Support/ClaudeCode/CLAUDE.md` (macOS) / `/etc/claude-code/CLAUDE.md` (Linux) / `C:\Program Files\ClaudeCode\CLAUDE.md` (Windows) | **Cannot be excluded** per docs. Not installed on the netrunner's machine today. |
| 2 | User rules | `~/.claude/rules/**/*.md` | Loaded at launch alongside `~/.claude/CLAUDE.md`. |
| 3 | User CLAUDE.md | `~/.claude/CLAUDE.md` | Personal preferences. |
| 4 | Ancestor CLAUDE.md files | walking up from cwd to filesystem root | Concatenated. Each dir's `CLAUDE.md` and `CLAUDE.local.md` both load. |
| 5 | Project rules | `<cwd>/.claude/rules/**/*.md` | Same path-scoping as user rules. |
| 6 | Project CLAUDE.md | `<cwd>/CLAUDE.md` or `<cwd>/.claude/CLAUDE.md` | The deck's main project memory. |
| 7 | Project CLAUDE.local.md | `<cwd>/CLAUDE.local.md` | Personal project-specific (gitignored). |
| 8 | Auto-memory MEMORY.md | `~/.claude/projects/<git-repo-key>/memory/MEMORY.md` | First 200 lines / 25KB. Keyed by git repo, shared across worktrees. |
| 9 | SessionStart hook output | hook config | Fires on every session including `-p`. |
| 10 | `@path` imports | recursive within any of the above | Max 5 hops. |

---

## The escape valves (verbatim from docs + real-deck testing)

| Mechanism | What it kills | Auth-safe? | Notes |
|-----------|---------------|------------|-------|
| `--bare` flag | hooks, skills, plugins, MCP, auto-memory, ALL CLAUDE.md | ⚠️ **NO — breaks Claude Max OAuth** | See gotcha below. |
| `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` env var | all CLAUDE.md (user, project, ancestors, auto-memory) | ✅ | Per docs: "prevent loading any CLAUDE.md memory files into context, including user, project, and auto-memory files". |
| `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` env var | only auto-memory MEMORY.md | ✅ | Subset of `DISABLE_CLAUDE_MDS`. Belt-and-suspenders. |
| `CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` env var | git workflow + git status snapshot from system prompt | ✅ | The deck doesn't need claude code's git workflow. |
| `--system-prompt "..."` flag | replaces (not appends) the default system prompt | ✅ | Bigger win than it looks. |
| `--tools ""` flag | disables every built-in tool | ✅ | Per docs: 'Use "" to disable all'. |
| `--disable-slash-commands` flag | skills + slash commands | ✅ | Covers what `--bare` was doing for those layers. |
| `claudeMdExcludes` setting | specific CLAUDE.md files by glob | ✅ except managed policy | Top-level setting in `--settings` JSON. |
| `--no-session-persistence` flag | skip transcript writes | ✅ | `-p` mode only. |
| Managed-policy CLAUDE.md | unavoidable | n/a | Not currently installed on the netrunner's machine. |

### 🚨 The `--bare` / OAuth gotcha

Quoting `claude --help` directly (NOT obvious from the memory
docs page):

> "`--bare` ... Anthropic auth is **strictly ANTHROPIC_API_KEY or
> apiKeyHelper via --settings (OAuth and keychain are never
> read).**"

The netrunner's deck uses Claude Max via OAuth/keychain. With
`--bare`, every spawn exits 1 silently because auth never
resolves. **Symptom**: subprocess exits 1 with no stderr.
Diagnosed during round-2 of the Advisor fix; caused two real-deck
pass-throughs.

**Lesson:** any flag that mentions auth in its help text is a
tripwire on this codebase. Read the help, not just the docs page.
Filed in `cyberdeck-state.md` under Filed gotchas, Async /
subprocess section.

For all spawn sites in this slice, we use env vars +
`--disable-slash-commands` instead of `--bare`. Same suppression,
no auth issue.

---

## The proposed mechanism: programmatic role-prompt injection

The netrunner's framing (2026-05-05):

> "We need a programmatic integration of 'role' prompt-config
> files. This is basically claude.md, but injected into the
> command when the role is launched. Goes before profile for
> constructs; has a default value that is restored on deck launch
> if the contents of the file are empty. So we'd have a 'Roles'
> folder in the source, and in that source there would be
> 'Construct', 'Daemon', 'Advisor', and 'Watchdog'. Each one
> would have a commented out note at the top that said 'If this
> file is saved blank, it will be automatically returned to
> default'."

This is a better shape than the alternatives I'd considered:

- **No spawn-cwd shenanigans.** We don't depend on cwd at all,
  so walk-up is moot.
- **No `--bare` / OAuth conflict.** Injection is via
  `--append-system-prompt` (or `--system-prompt` for Family A),
  which doesn't break OAuth.
- **No auto-memory project-key juggling.** Env vars suppress
  auto-load entirely.
- **No managed-policy concern.** Managed CLAUDE.md is
  uncircumventable, but our injected text composes harmlessly
  with whatever it says.
- **No `@path` import recursion concern.** We control the text
  end-to-end.
- **Brake-hook protection comes for free.** Role files live in
  deck source; brake hook denies construct writes/edits to
  deck-source paths automatically (post-2026-05-03 plugin
  retool established this protection). Constructs cannot edit
  their own startup behavior — which the netrunner explicitly
  flagged as cursed and unwanted.
- **Updates flow through git.** Defaults ship with deck updates
  via `git pull`. The netrunner can edit role files locally;
  git treats them as tracked files. Bundled defaults are
  authoritative; local edits are temporary calibration. (This
  matches what the netrunner explicitly wanted.)

### Layout

```
<deck-source>/
  roles/
    daemon.md            # the daemon coordinator
    construct.md         # task-scoped workers (also used by pool warmers)
    watchdog-qa.md       # the Q&A oracle half of the watchdog
    watchdog-authoring.md # the tripwire-authoring half of the watchdog
    advisor.md           # per-tool Q&A bot
  general.toml           # netrunner identity + global preferences
```

Each `.md` role file:

```markdown
<!--
  Role: Daemon (the persistent coordinator)
  This file is the source of truth for the daemon's role prompt.

  If you save this file blank (or with only this comment block),
  the deck will restore the bundled default on next launch.
-->

You are the Daemon of a Cyberdeck — a TUI orchestration system
for AI agents. You decompose the netrunner's goals into actions
(spawn / kill / etc.) and dispatch them via JSON.

[... rest of the daemon role prompt ...]
```

### `general.toml` shape

```toml
# General config — applies to every spawn (daemon, constructs,
# watchdog, advisor). Goes BEFORE role + profile in the system
# prompt addendum chain.

[identity]
# Optional. The netrunner's preferred handle. When set, agents
# will refer to the netrunner by this name in chat.
name = "Watchdog"

# Optional. Pronouns to use when the agent refers to the
# netrunner.
pronouns = "they/them"

# Optional. Free-text notes — anything that doesn't fit a
# structured field. Appended to system prompts as a single
# block.
notes = """
Prefer concise answers. I don't need preamble or
explanations of obvious things.
"""
```

The deck renders `general.toml` into a brief prose block
prepended to every spawn's system prompt:

```
The netrunner's chosen handle is "Watchdog" (they/them).
[notes content, if any]
```

Empty / missing `general.toml` → block omitted entirely.

### Composition order

For each spawn, the system prompt is built as:

```
[claude code default system prompt — REPLACED via --system-prompt]
+ general identity block      (from general.toml; may be empty)
+ role addendum               (from roles/<role>.md)
+ profile addendum            (constructs only — current pattern, unchanged)
+ per-spawn addendum          (current state, current task — unchanged)
+ plugin block                (constructs only — unchanged)
```

For some roles (Family A — see below), the deck uses
`--system-prompt` to fully replace claude code's default. For
others (Family B), the deck uses `--append-system-prompt` to
preserve claude code's tool-use scaffolding.

### Role-by-role mapping

| Spawn type | Role file | Family | Notes |
|------------|-----------|--------|-------|
| Daemon (streaming + one-shot) | `daemon.md` | B (append) | Has tools (no-op in practice); preserve claude code's default scaffolding for tool-use awareness |
| Construct (daemon-driven) | `construct.md` | B (append) | Heavy tool user; needs default scaffolding |
| Construct (netrunner-direct) | `construct.md` | B (append) | Same as above |
| Pool warmer | `construct.md` | B (append) | Becomes a construct on pull; warm-up uses construct role |
| Watchdog Q&A | `watchdog-qa.md` | A (replace) | Read-only; no tool use needed |
| Watchdog tripwire authoring | `watchdog-authoring.md` | A (replace) | One-shot regex authoring; no tool use needed |
| Advisor | `advisor.md` | A (replace) | Already shipped using Family A recipe (2026-05-05) |
| Mechanic v1 (future) | `mechanic.md` | A (replace) | Triage one-shot; no tool use needed |

**Family A — "ZERO context" (no tools)**: Advisor, Watchdog Q&A,
Watchdog tripwire authoring, Mechanic v1.

```python
# Write composed prompt to temp file (argv truncates at \n —
# see "--system-prompt truncates at the first newline" gotcha
# in cyberdeck-state.md). Cleanup in finally.
fd, sysprompt_path = tempfile.mkstemp(...)
os.close(fd)
Path(sysprompt_path).write_text(composed_system_prompt, encoding="utf-8")
try:
    cmd = [
        "claude", "-p",
        "--system-prompt-file", sysprompt_path,
        "--tools", "",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--model", model, "--effort", effort,
    ]
    env = {
        **os.environ,
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }
    # ... spawn, communicate, decode ...
finally:
    os.unlink(sysprompt_path)
```

**Family B — "CURATED context, tool-using"**: Daemon, Constructs,
Pool warmers.

```python
# Same temp-file pattern for --append-system-prompt-file (the
# argv-newline-truncation bug applies to --append-system-prompt
# too — verified via the same diagnostic).
fd, addendum_path = tempfile.mkstemp(...)
os.close(fd)
Path(addendum_path).write_text(composed_addendum, encoding="utf-8")
try:
    cmd = [
        "claude", "-p",   # or streaming flags for the daemon
        "--append-system-prompt-file", addendum_path,
        "--disable-slash-commands",
        "--model", model, "--effort", effort,
    ]
    env = {
        **os.environ,
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }
    # ... spawn, communicate, decode ...
finally:
    os.unlink(addendum_path)
```

The key difference: Family A replaces the system prompt
(`--system-prompt-file`); Family B appends to it
(`--append-system-prompt-file`). Family A roles don't need
claude code's built-in tool-use scaffolding; Family B roles do.

**🚨 Always use the `-file` variants of system-prompt flags.**
Argv mode silently truncates multi-line content at the first
newline. Verified on Claude Code 2.1.126 + Windows during
Advisor round-4 fix (2026-05-05). Filed in `cyberdeck-state.md`
Filed gotchas. Streaming subprocesses (daemon's stream-json
backend, watchdog's streaming Q&A) hit the same flag, so this
applies to them too.

---

## Default-restoration mechanism

Each role file ships with bundled default content as a Python
constant (`roles/_defaults.py`). At deck launch, for each role
file:

1. Read the file from disk (`<deck-source>/roles/<role>.md`).
2. Strip the comment header (HTML comment block at the top).
3. Strip whitespace.
4. If remainder is empty: write the bundled default to the file
   and use that. Otherwise: use disk content.

Empty file → restore. File with content → use disk content
(netrunner's edits are honored).

The comment header is preserved verbatim across restoration so
the netrunner can always see the "save blank to restore" hint.

### `_defaults.py` shape

```python
# roles/_defaults.py
DAEMON_DEFAULT = """\
You are the Daemon of a Cyberdeck — a TUI orchestration system
for AI agents. ...
"""

CONSTRUCT_DEFAULT = """\
You are a Construct of a Cyberdeck — a task-scoped worker
spawned by the Daemon. ...
"""

WATCHDOG_QA_DEFAULT = """\
You are the Watchdog of a Cyberdeck — a read-only oracle. ...
"""

WATCHDOG_AUTHORING_DEFAULT = """\
You are the tripwire-authoring half of the Watchdog. ...
"""

ADVISOR_DEFAULT = """\
You are the Advisor for ONE specific tool. The deck composes
the per-target metadata at spawn time; this file holds the
generic scope rules and style guidance. ...
"""
```

The default content is what's currently in:

- `daemon.py` `DAEMON_SYSTEM_PROMPT`
- `construct.py` (the construct's portion of the system prompt
  composition — needs identifying)
- `watchdog.py` `WATCHDOG_SYSTEM_PROMPT`
- `tripwires.py` `TRIPWIRE_AUTHORING_SYSTEM_PROMPT`
- `advisor.py` `build_system_prompt` template (with the
  per-target slots stubbed)

Phase 2 below moves these into role files; the code constants
become thin shells that read from disk.

---

## Hot-reload

Role files hot-reload from disk on mtime change (mirror
profile_registry's pattern). Edits to `roles/daemon.md` apply
to the next daemon spawn. Edits to `general.toml` apply to the
next spawn of any role.

Hot-reload is best-effort:

- Mid-flight subprocesses keep their composed system prompt
  (cached at spawn time; we can't mutate it).
- New spawns see new content.
- The streaming daemon and watchdog don't see new content until
  their subprocess respawns (goal-set, restart, etc.). Same
  semantics as profile hot-reload today.

A `roles.changed` bus event fires on edit so the netrunner sees
in the chatlog when a role file changes. Mirrors
`profile.changed` from `profile_registry`.

---

## Phasing

### Phase 0 — Pre-flight measurement

Before changing any spawn site, capture baseline behavior so we
can A/B per role. Instrument `claude -p --debug api` to log the
exact bytes sent to the API for one representative invocation
of each subprocess type. Store under
`<deck-source>/diagnostics/spawn-context-baseline-YYYY-MM-DD/`.

What to capture per role:

- Exact system prompt content
- All user messages (where CLAUDE.md content gets injected)
- Tool catalog
- MCP server tool descriptions
- Token count breakdown

This gives us:

- Concrete evidence of what's leaking (vs. our current
  inference)
- Data to compare against post-fix behavior
- Numbers for the cache-cost claim (the ~19k ephemeral_1h
  reduction we expect)

### Phase 1 — Build infrastructure

New module: `roles_registry.py`. Mirrors `profile_registry.py`
in shape:

- Loads role files from `<deck-source>/roles/`
- Default-restoration on empty
- mtime-watch + hot-reload
- Bus events: `roles.loaded`, `roles.changed`, `roles.error`
- `RoleRegistry.get(role_name) -> RoleAddendum`

New module: `general_config.py` (or extend `preferences.py`).
Loads `<deck-source>/general.toml` (or `<home>/general.toml`?
— TBD; netrunner choice). Renders into a prose block via
`render_general_block(general) -> str`.

New module: `spawn_context.py`. Owns the clean-spawn recipe.
Single function returning `(cmd args, env dict)` for a given
spawn. Composes the system prompt from `general + role + profile
+ per-spawn`.

```python
# spawn_context.py
def build_system_prompt(
    role: RoleName,
    profile_addendum: str = "",
    per_spawn_addendum: str = "",
    plugin_block: str = "",
) -> str:
    """Compose the full system prompt for one spawn.
    
    Order: general (identity) → role → profile → per-spawn → plugin.
    """
    parts = []
    general_block = general_config.render_general_block()
    if general_block:
        parts.append(general_block)
    role_addendum = roles_registry.get(role).text
    parts.append(role_addendum)
    if profile_addendum:
        parts.append(profile_addendum)
    if per_spawn_addendum:
        parts.append(per_spawn_addendum)
    if plugin_block:
        parts.append(plugin_block)
    return "\n\n".join(parts)


def build_subprocess_args_and_env(
    role: RoleName,
    family: Literal["replace", "append"],
    composed_prompt: str,
    base_args: list[str] = (),
    base_env: Optional[dict[str, str]] = None,
) -> tuple[list[str], dict[str, str]]:
    """Returns (full args, full env) for a clean spawn."""
    flag = "--system-prompt" if family == "replace" else "--append-system-prompt"
    cmd = [
        *base_args,
        flag, composed_prompt,
        "--disable-slash-commands",
    ]
    if family == "replace":
        cmd.extend(["--tools", ""])
    env = {
        **(base_env or os.environ),
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }
    return cmd, env
```

### Phase 2 — Bootstrap role files from existing prompts

Move the current role-prompt content from code constants to
disk files, with no functional change:

1. `daemon.py` `DAEMON_SYSTEM_PROMPT` → `roles/daemon.md`. The
   constant becomes `DAEMON_SYSTEM_PROMPT = roles_registry.get(
   "daemon").text` (lazy lookup at module import).
2. `construct.py` system-prompt composition → `roles/construct.md`.
3. `watchdog.py` `WATCHDOG_SYSTEM_PROMPT` → `roles/watchdog-qa.md`.
4. `tripwires.py` `TRIPWIRE_AUTHORING_SYSTEM_PROMPT` →
   `roles/watchdog-authoring.md`.
5. `advisor.py` `build_system_prompt` template → `roles/advisor.md`
   (with `{target.name}`, `{sibling_block}`, etc. as placeholder
   tokens that the advisor renders at spawn time — the role file
   holds the static parts).

Verify with Phase 0 baseline that the composed prompt is
byte-identical to what the deck spawned before the migration.

### Phase 3 — Migrate spawn sites behind a flag

Add `prefs.role_injection` (default `false` initially). When
`true`, every spawn site routes through `spawn_context.py` with
the appropriate role + family. When `false`, current behavior
(bypass roles_registry, read from code constants).

Spawn sites to migrate:

1. `daemon.py` — both backends (one-shot + streaming)
2. `daemon_session.py` — wherever the streaming subprocess is
   started
3. `construct.py` — `Construct.__init__` command builder
4. `fleet.py` — anywhere it directly spawns
5. `session_manager.py` — pool warming `_warm_one`
6. `watchdog.py` — both `_process_oneshot` and
   `_process_streaming` / `_spawn_streaming`
7. `watchdog.py` `Watchdog.author_tripwires` — the authoring
   spawn
8. `advisor.py` — already uses the env-var recipe; route
   through `spawn_context.py` for consistency

The flag exists to A/B. With it on, the netrunner runs a fresh
goal and verifies the daemon + constructs still behave correctly.
With it off, current behavior. Real-deck testing flips between
the two until we're confident the new path is at least as good.

### Phase 4 — Add general.toml support

Bootstrap on first deck launch: write `<deck-source>/
general.toml` with all fields commented out (showing the schema
but no values set). Empty / missing → block omitted from system
prompts.

Renderer composes the prose block. Spawn sites pick it up via
`spawn_context.build_system_prompt()`.

### Phase 5 — A/B verify per role

Real-deck testing per role with the flag on. See "Test plan"
below.

### Phase 6 — Tune role files where regressions surface

When the flag is on, the daemon/constructs lose their
auto-loaded CLAUDE.md content. Some of what they were
free-riding on is real — they actually used the deck's
vocabulary, the dispatcher protocol description, the
brake-state semantics, etc. Without auto-load, their role
files may be under-specified.

For each role that regresses:

1. Identify the missing content — what specifically did the
   role assume?
2. Add it to that role's `.md` file (and update the
   `_defaults.py` constant so newly-empty files restore to the
   improved version).
3. Re-test until parity.

### Phase 7 — Flip flag default-on, retire flag

Once Phase 6 settles and the netrunner has used the deck across
a few real goals with `role_injection=true` and is satisfied,
flip the default. After ~2 weeks of stable operation with no
regressions, remove the flag entirely.

---

## Risks and open questions

### Risks

1. **Behavioral regression in daemon / constructs / watchdog.**
   They've been free-riding on CLAUDE.md content. Removing the
   auto-load will reveal under-specified role files. Mitigation:
   Phase 0 baseline + flag-gated rollout + Phase 6 prompt
   tuning. The deck stays usable throughout (flag default-off
   until verified).

2. **`--resume` re-loads context fresh.** If a session is
   resumed from a different cwd than it was created in,
   different CLAUDE.md content gets injected mid-conversation.
   Once role injection lands, this is a non-issue (cwd doesn't
   matter for our content), but during the migration period
   (flag-off behavior) it remains.

3. **Cache effects.** Once stable, this should DROP per-spawn
   cache_creation tokens substantially (the ~19k ephemeral_1h
   writes filed as "Anthropic's court" on 2026-05-02). The
   role-file content is stable across spawns within a session
   (no drift like CLAUDE.md has), so cache hits should improve.
   Phase 0 baseline + Phase 5 measurement validates the
   reduction.

4. **Constructs editing role files.** Role files live in
   `<deck-source>/roles/`. Brake hook's existing protection
   (`path_is_protected` in `brake_hook.py`) covers all of
   `<deck-source>/` except the workspace. So constructs cannot
   write/edit role files via Write/Edit/Bash. ✓ The "constructs
   editing their own startup behavior is cursed" concern is
   handled mechanically by existing brake protection.

5. **Mechanic v1 (future spawn site).** If item 000 lands first,
   mechanic v1 inherits the role pattern from the start. If
   mechanic v1 lands first, it'll need migration later.
   **Phasing-wise, item 000 should land BEFORE mechanic v1** —
   noted in `cyberdeck-maintbot-design.md`.

6. **Profile composition with role.** Profiles already provide
   `default_construct_addendum`. With role files, the chain
   becomes general + construct role + profile addendum. The
   construct role describes "what is a construct", the profile
   describes "what flavor of construct" (e.g.
   `code_reviewer`). This composition should be fine but watch
   for duplication or contradiction during Phase 5.

### Open questions

- **Walk-up stopping condition.** Does claude code stop walking
  up at the filesystem root, the git repo boundary, or
  somewhere else? Docs are silent. Resolve in Phase 0 by
  empirically testing from a deeply nested cwd. (Less critical
  with role injection since env vars suppress walk-up entirely,
  but useful to confirm.)

- **Does `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` cover `.claude/
  rules/`?** The env var docs say it covers "any CLAUDE.md
  memory files" — rules dirs aren't strictly CLAUDE.md files.
  Resolve in Phase 0.

- **Does `--debug api` actually surface the user-message
  content for CLAUDE.md, or only the API call shape?** Resolve
  in Phase 0. If it doesn't show the content, we'll need a
  different observation path.

- **MCP server descriptions with `--tools ""`.** With no tools,
  the model has nothing to call. But MCP server tool
  descriptions might still get injected into context (just
  unused). Verify in Phase 0 — if so, add `--strict-mcp-config
  --mcp-config <empty file>` to Family A's recipe.

- **`general.toml` location.** Deck source (per netrunner's "git
  pull SHOULD clobber it" framing) or `<home>/`? My read is
  deck source — same rationale as roles. The netrunner's
  identity / preferences are part of the deck's curated
  defaults, with personal calibration on top.

- **`construct.py`'s current system prompt composition.** The
  construct doesn't have a single named constant today —
  system prompt is built from claude code's default + the
  daemon-side addendum + profile addendum + per-spawn
  addendum + plugin info. Phase 2 needs to identify the
  "construct role" portion of that composition. Probably most
  of what's in the per-spawn addendum that's NOT
  spawn-specific (the cyberdeck protocol, dispatcher usage,
  brake awareness) — that's the "construct role" content.

- **Watchdog Q&A vs Watchdog tripwire-authoring as separate
  files vs one file with two sections.** Two files (the design
  above) is cleaner — they're distinct subprocess types with
  distinct prompts. Reconfirm during Phase 2.

- **Empty-detection rules.** Should "empty" mean (a) no
  characters except whitespace, (b) no characters except
  whitespace + the comment header, or (c) something else?
  Probably (b) — the netrunner intentionally clearing edits
  shouldn't lose the "save blank to restore" hint. Implement
  as: strip HTML comment blocks + strip whitespace; if
  remainder is empty, restore.

---

## Test plan

### Unit-shape tests

- `roles_registry.get(role_name)` returns the disk content,
  falling back to `_defaults.py` on empty.
- Empty-detection logic: file with only the comment header is
  detected as empty; file with header + content is not.
- Hot-reload fires `roles.changed` on mtime change.
- `general_config.render_general_block()` handles missing /
  partial / fully-populated `general.toml`.
- `spawn_context.build_system_prompt()` composes in the right
  order.
- `spawn_context.build_subprocess_args_and_env()` produces
  correct args/env for both Family A and Family B.

### Real-deck integration tests

The failure modes of this slice are subtle (model knows or
doesn't know things from auto-loaded context); unit tests
can't catch them. Real-deck testing is the actual safety net.

Per-role checklist:

**Advisor** (already verified 2026-05-05):
- [x] Tool advice query returns clean answer
- [x] Deck-context query ("what is the cyberdeck") fails to
      recognize the term — CLAUDE.md properly suppressed
- [x] OAuth auth works (no silent exit-1)

**Daemon**:
- [ ] Goal-set works, daemon decomposes correctly
- [ ] Daemon uses deck vocabulary fluently in chat (or system
      prompt addendum is updated to restore the vocabulary)
- [ ] Action JSON schema is followed
- [ ] Mid-flight goal update works
- [ ] Daemon doesn't reference content from CLAUDE.md it
      shouldn't know (e.g. specific build-plan items)

**Constructs**:
- [ ] Construct emits dispatcher markers correctly
- [ ] Construct understands brake state behavior
- [ ] Construct's tool use stays within scope
- [ ] Pool-warmed construct (resume) behaves identically to
      fresh-spawned

**Watchdog Q&A**:
- [ ] Q&A reasons over chatlog correctly
- [ ] Knows badge legend ([you], [↳you], etc.)
- [ ] Awareness of brake-blocked markers

**Watchdog tripwire authoring**:
- [ ] Authoring still produces good patterns
- [ ] Patterns register correctly with the engine
- [ ] No regression in the bait-construct test

**Mechanic v1** (when it lands):
- [ ] Triage spawn doesn't see deck source content
- [ ] Triage emits structured report

### Cost / cache verification

Run a representative goal with the flag off (baseline) and on
(post-fix), capturing cache_creation tokens per spawn. Expect
a substantial drop.

### Default-restoration verification

For each role file:

1. Save it blank → restart deck → confirm it's restored.
2. Save it with arbitrary content → restart deck → confirm the
   content is preserved.
3. Save it with only the comment header (deleting all content
   below) → restart deck → confirm it's restored.

### `general.toml` verification

1. Missing file → no general block in system prompt.
2. File with empty `[identity]` block → no general block.
3. File with `name = "Watchdog"` set → general block contains
   "The netrunner's chosen handle is 'Watchdog'." (or whatever
   we end up rendering).
4. File with `notes = "..."` populated → notes appear in
   general block.

---

## Related decisions

- **This slice replaces what I originally drafted as "per-role
  spawn cwd + per-role CLAUDE.md".** The role-injection
  mechanism is cleaner (no cwd shenanigans, no walk-up
  concerns, no auto-memory project-key juggling) and gives the
  netrunner a programmatically-owned interface for tuning role
  prompts.

- **Don't combine this slice with the Mechanic v1 LLM-session
  half** unless they cleanly compose. Item 000 (this slice)
  should land first; mechanic v1 then inherits the role
  pattern.

- **Don't ship general.toml + role files as separate slices.**
  They compose at spawn time and need to be tested together.
  One slice, one A/B verification cycle.

- **Don't change the brake-hook protection in
  `<deck-source>/roles/`.** The default protection (deck-source
  paths denied for constructs) does exactly what we want here.
  Adding role-specific exceptions would be a security
  regression.

- **Don't put `_defaults.py` in `<home>`.** It's bundled deck
  content, ships with the source, lives in
  `<deck-source>/roles/_defaults.py`.

---

## Success criteria

- All deck spawn sites route through `spawn_context.py` with
  appropriate per-role profiles.
- `prefs.role_injection` flag flipped default-on for ~2 weeks
  without regressions.
- Cache cost: per-spawn cache_creation drops measurably (target
  ~50% reduction on the "Anthropic's court" 19k tokens).
- Real-deck role-by-role checklist passes.
- The `cyberdeck-state.md` "Filed gotchas" entry on `--bare` /
  OAuth has companion "spawn isolation shipped" notes
  confirming no recurrence.
- `cyberdeck-spec.md` updated with a Spawn Context Isolation
  section.
- Build-plan item 000 marked SHIPPED.

---

*Last updated: 2026-05-05 (filed; refactored same day around the
netrunner's role-injection proposal). Revise once Phase 0
baseline data is in hand.*
