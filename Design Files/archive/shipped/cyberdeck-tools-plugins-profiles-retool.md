# Cyberdeck — Tools / Plugins / Profiles Retool

> **STATUS: SHIPPED 2026-05-04 (5/5 phases).** Archived 2026-05-07 for provenance.
> Read this doc when you need the *why* behind the three-way split
> (tools = registered system CLIs, plugins = deck-extending capability,
> profiles = role context). Sub-feature deferrals (plugin airgap `p`,
> quickfire `c`, picker `Shift+C`, persistent plugins, MCP-as-metadata)
> have been migrated as line items in `cyberdeck-build-plan.md` →
> mid-future. Don't update this doc — corrections to shipped behavior
> belong in code + `cyberdeck-state.md`.

---

*Architectural redesign of how the deck thinks about extensible
capability. Filed 2026-05-02 after a multi-pass conversation that
walked back two earlier framings to land on a clean three-way split.
Implementation queued behind discrete bug fixes (cache-miss-per-
spawn especially) per the netrunner's "that shit is expensive"
direction. When picking this up, read this doc + the existing
`cyberdeck-tools-default-kit.md` (v2) and `cyberdeck-spec.md` →
*Tool registry* together — this doc supersedes parts of both,
specifically the "stateless plugin = subprocess wrapper" framing
and the "scripts are flat files in <home>/tools/" assumption.*

---

## Why this exists

Today's tool surface is incoherent in one specific way the netrunner
articulated cleanly:

> *Some tools are basically wrappers for installed terminal tools,
> and exist primarily to inform the deck that those tools exist and
> are ready to use, while others actually hold unique python scripts
> that are installed in the deck's directory. This makes no sense at
> all and is bad.*

Concrete shape of the incoherence:

- **Plugins** are stateless subprocesses constructs invoke via Bash.
  They live at `<home>/plugins/<name>/run.py`. The Tools panel
  surfaces them as a "PLUGINS" section.
- **Scripts** are flat files in `<home>/tools/<category>/<filename>`.
  The Tools panel surfaces them as a "SCRIPTS" section, raw filenames
  only — no manifests yet (filed deferred).
- **Profiles** carry a `recommended_tools` list (renamed from
  `allowed_tools` post-brake-refactor) that's a soft hint at
  Claude-Code-tool names (Bash, Read, Write, etc.) — NOT registered
  scripts or plugins. The naming is a leftover from the pre-brake-
  refactor era when profiles narrowed tool capability.

So "tools" means three different things in three places: the panel
header, the profile field, and the directory tree. This doc fixes
that.

---

## The three things

### Tools

Registered system-installed CLI tools — binaries on PATH or scripts
on disk. The deck doesn't ship with them. The deck just **knows about
them** and surfaces them to constructs. Hot-loadable; the registry
is a single TOML file the deck mtime-watches.

Each tool entry has:
- `name`: short slug used in profile lists
- `kind`: `"binary"` | `"script"`
- `command`: the executable invocation (e.g., `nmap` or
  `python ${tools_dir}/scan_subnet/main.py`)
- `path` (optional): explicit path; falls back to PATH lookup
  via `shutil.which` if absent
- `description`: short one-liner shown in the construct's prompt
  when this tool is in the active profile's tools list
- `help_text` (optional): longer text returned by `<command> --help`
  or via a deck convention; not always injected into the prompt
  (prompt-bloat-aware)

Existence-check at load time: `shutil.which(command)` for binaries,
`Path(path).exists()` for scripts. Missing entries grey-out in the
Tools panel with a red ✗ glyph + dimmed name + "cannot locate
<command>" tooltip — debug-friendly without crashing on absence.

#### Where tools live on disk

The home tree:

```
<home>/tools/
  tools.toml                ← THE registry (single source of truth)
  deck/                     ← reserved: deck-managed dispatchers
    cyberdeck.py            ← construct→deck marker dispatcher
                              (existing — bootstrapped by deck startup)
    plugin_bridge.py        ← construct→plugin dispatcher
                              (NEW — bootstrapped by deck startup)
  <subdir>/                 ← multi-part scripts. Each subdirectory
                              that the netrunner creates is a script
                              bundle (entry script + helpers + data
                              files). Registered in tools.toml with
                              `kind = "script"` and a `path` pointing
                              at the entry file inside the subdir.
                              `deck/` is reserved; other subdirs are
                              netrunner territory.
```

Subdirectories under `<home>/tools/` are the convention for organizing
multi-part scripts. The deck does NOT auto-register them; the netrunner
adds entries to `tools.toml` that reference the subdir's entry file.
This keeps the registry explicit (no surprise registrations from a
loose directory scan) while giving multi-part scripts a clean home.

#### Construct registration channel

Constructs CAN register a tool dynamically — typical use case: a
construct installs a binary during a recon goal and wants future
constructs to know it's available. Registration goes through the
existing dispatcher marker protocol, NOT raw file write to
`tools.toml`:

    __CYBERDECK::v1::TOOL_REGISTER::{json}__

Deck-side handler validates the entry, appends to `tools.toml`,
fires a hot-reload. This keeps the brake hook in the loop (the
construct's marker emission goes through Bash → brake gates) and
prevents constructs from corrupting the registry with malformed
entries.

### Plugins

Deck-extended capability bundles. Hot-loaded at startup (NOT
hot-reloaded mid-run), give constructs additional functionality
beyond what's installed on the system. Each plugin can:

1. Define **functions** that constructs invoke via the bridge:
   ```
   python <home>/tools/deck/plugin_bridge.py <plugin_name> -f <func> -a <args>
   ```
   This is a single Bash invocation through one stable script — the
   brake hook gates it like any other Bash command. Constructs never
   get a new tool surface; they just call one binary with structured
   args.

2. Optionally hook into the deck via `load_into_deck(app)`. This
   runs in the deck process at startup (once). Lets the plugin add
   sidebar widgets, register marker handlers, subscribe to bus
   events — whatever it needs to extend the deck's UI / capability.

#### Where plugins live on disk

```
<deck-source>/
  plugin_bridge.py          ← canonical dispatcher source. Bootstrapped
                              to <home>/tools/deck/plugin_bridge.py
                              each startup (idempotent overwrite, same
                              shape as dispatcher.py → cyberdeck.py).
  plugins/                  ← MOVED from <home>/plugins/. Lives in
                              deck source so the brake hook's deck-
                              source-write protection automatically
                              extends to plugin code. Constructs
                              CANNOT write to plugins via any tool —
                              full stop, by virtue of file system
                              layout + brake gating.
    <name>/
      plugin.py             ← the module. Defines functions exposed
                              to constructs + optional load_into_deck.
      plugin.toml           ← manifest: name, description, deck-side
                              capabilities the plugin announces.
      construct_instructions.md
                            ← optional. Injected into a construct's
                              prompt addendum at spawn time when the
                              daemon's spawn action picked this plugin
                              for the construct.
```

#### Why plugins-in-deck-source is the right safety boundary

The brake hook protects `<deck-source>/` from constructs by sentinel-
filename matching (brake_hook.py, brake_state.py, brake_patterns.py)
plus path-prefix protection. Moving plugins under deck source means
constructs CANNOT write to plugin code via Write/Edit/Bash, period.
The netrunner is the only entity that edits plugins (via direct file
edit — there's no construct-facing path). This closes the
"construct writes a half-baked plugin file and the deck hot-loads it
mid-run and self-destructs" failure mode at the filesystem layer.

The construct-facing dispatcher (`plugin_bridge.py` in
`<home>/tools/deck/`) is just a thin imports-and-dispatches script.
It's regenerated on every deck startup (idempotent overwrite, same
pattern as `cyberdeck.py`). Even if a construct corrupted it
mid-session, the next deck launch overwrites with the canonical
version.

#### Lifecycle

- Plugins are loaded ONCE at deck startup. NOT hot-reloaded. Adding
  or editing a plugin requires deck restart.
- Each plugin's `load_into_deck(app)` is called inside a try/except;
  a crashing plugin is skipped + chatlog-warned + logged, but the
  deck still boots.
- Mechanic v2 (deferred): integrity-scan plugins on heartbeat tick,
  fire if a plugin file changes mid-run unexpectedly. Phase 1 of
  this retool relies on the brake hook's deck-source-write
  protection as the integrity guarantee.

### Profiles

Recipes — default prompt + a list of registered tools. Profiles do
NOT list plugins; plugin assignment is daemon-driven per spawn (see
*Visibility model* below).

Schema (post-retool):

```toml
name = "recon_specialist"
category = "Recon"
description = "Network and wireless reconnaissance work."

default_daemon_addendum = """
When this profile is active, ...
"""

default_construct_addendum = """
You are operating in a recon context. ...
"""

# Names from tools.toml registry. Construct's prompt grows a TOOLS
# AVAILABLE section listing each tool's name + short description.
tools = ["nmap", "dnsx", "subfinder", "ripgrep"]
```

`recommended_tools` (today's field) renames to `tools` and changes
semantic from "soft hint at Claude Code tool names" to "explicit
list of registry names." Migration: existing profile TOMLs need
their `recommended_tools` field renamed; default values dropped if
they referenced non-registered Claude Code tool names (Bash, Read,
etc. — those aren't in tools.toml; constructs always have them via
their default tool set).

---

## Visibility model

Today's deck has confused boundaries: profile lists `recommended_
tools`, plugin registry is global, daemon system prompt mentions
all plugins, scripts are surfaced under profile-influenced contexts.
Post-retool:

| Surface | Tools | Plugins |
|---|---|---|
| **Daemon** | Sees all profiles + their tools list (via the existing PROFILES catalog in the system prompt). When picking a profile per spawn, gets the tools that profile lists. | Sees ALL loaded plugins (small set, deck-wide cap of maybe ~20 in practice). PLUGINS AVAILABLE catalog in the system prompt. Picks per spawn which to inject. |
| **Construct** | Sees its profile's `tools` list. Each tool's name + short description goes into the prompt addendum at spawn time. Help text NOT injected (prompt-bloat-aware); construct invokes `<tool> --help` for that. | Sees the daemon-selected plugins for THIS spawn (orthogonal to profile). Each selected plugin's `construct_instructions.md` content gets concatenated into the prompt addendum. |

Spawn action JSON (daemon → fleet) grows an optional `plugins` field:

```json
{
  "action": "spawn",
  "task": "...",
  "profile": "recon_specialist",
  "plugins": ["screenshot", "ir_blaster"]
}
```

---

## UI

Right-panel tabs (after retool):

```
Chatlog | Files | Profiles | Tools
```

Profiles graduates from a section inside Tools to its own tab —
profiles are conceptually distinct from "things constructs invoke"
and deserve the visual separation the netrunner has accumulated
demand for.

**Profiles tab**: lists profiles with description + category +
tools count. `z` to expand → see full TOML including addendums.
`space` → spawn-from-profile launcher (existing behavior).

**Tools tab**: unified list of registered tools + plugins. Each row
prefixed with a kind glyph indicating what it is at a glance:

- `⚙` binary  — system-installed CLI
- `⌬` script  — deck-managed wrapper (subdir under `<home>/tools/`)
- `⊕` plugin  — deck-extended capability

Missing tools render with a red `✗` glyph + dimmed name + "cannot
locate <command>" tooltip on hover. Plugins that failed
`load_into_deck(app)` render with a yellow `⚠` glyph + reason.

---

## Phases

Five sub-slices, sequenced. Each shippable independently; deck
remains usable throughout.

### P1 — Tools registry + hot-reload + missing-tool grey-out

- New `tools_registry.py` module: registry dataclass, mtime watcher,
  shutil.which-based existence check, bus events on change
  (`tool.added`, `tool.removed`, `tool.unavailable`).
- `<home>/tools/tools.toml` schema: array of tool tables with
  name/kind/command/path/description/help_text.
- Bootstrap: deck creates `tools.toml` with sensible defaults on
  fresh install (mirrors profile registry's auto-seed). Migration:
  existing flat files in `<home>/tools/<category>/` get converted
  to registry entries on first launch of new code; old files stay
  in place until netrunner cleans up.
- Tools panel renders only "binary" and "script" kinds at this
  phase — plugin glyph wired but no plugins integrated yet.
- ~150 LOC.

### P2 — Move plugins to deck source + bridge dispatcher

- New `<deck-source>/plugins/` directory (replaces
  `<home>/plugins/`).
- New canonical `plugin_bridge.py` in deck source root.
- Bootstrap: regenerate `<home>/tools/deck/plugin_bridge.py` each
  startup (mirrors `dispatcher.py` → `cyberdeck.py` flow).
- Existing `screenshot` plugin migrates: copy folder from
  `<home>/plugins/screenshot/` → `<deck-source>/plugins/screenshot/`,
  rename `run.py` → `plugin.py`, no behavior change.
- `plugin_registry.py` retargets the new location and the new
  manifest layout.
- Plugins auto-register in tools.toml as `kind = "plugin"` (or
  rendered into the Tools panel from the plugin manifest scan,
  same surface either way — implementation choice at write time).
- ~120 LOC.

### P3 — `load_into_deck(app)` hook

- Defined contract: optional function on each plugin's `plugin.py`
  module; called at deck startup with the App instance after the
  TUI mounts. Plugin can subscribe to bus events, register marker
  handlers, add widgets — whatever its capability needs.
- Wrapped in try/except per-plugin; a crashing hook skips that
  plugin's deck-side integration but doesn't break the deck.
- screenshot plugin remains a no-op for the hook (pure stateless
  capture; nothing to integrate).
- Lays groundwork for future plugins (IR blaster, camera, etc.)
  without committing to specific use cases.
- ~80 LOC.

### P4 — Profile schema migration: `recommended_tools` → `tools`

- Field rename + semantic shift (registry names, not Claude Code
  tool names).
- Default profile auto-seed regenerated with new schema.
- Existing user-edited profiles get migrated on first launch:
  if `recommended_tools` exists and `tools` doesn't, copy the
  list, rename, save back. Idempotent.
- Daemon system prompt rebuilds: PLUGINS AVAILABLE catalog grows;
  profile-tools enumeration injected into daemon's context per
  profile.
- Spawn action JSON shape grows optional `plugins` field; fleet
  spawn threads it through to the construct's prompt builder.
- ~100 LOC + daemon prompt rewrite.

### P5 — UI retool

- Profiles tab gets its own TabPane in the right panel; Tools tab
  loses the PROFILES section.
- Tools tab unified: single ListView of registered tools + plugins,
  each row prefixed with kind glyph.
- Right-panel-focusables list re-wired (per the gotcha note in
  cyberdeck-state.md — hand-curated, not auto-derived).
- ~150 LOC.

**Total: ~600 LOC across 4-5 sessions.** Could compress to 3
sessions if the implementation runs tight, but design-attention
bandwidth is the real constraint, not LOC.

---

## Composition with existing infrastructure

- **Brake hook**: gates `python <home>/tools/deck/plugin_bridge.py
  ...` invocations the same way it gates any Bash command. No new
  surface; no new gating needed for plugin function calls. The
  `<deck-source>/plugins/` directory is protected by the existing
  deck-source-write guard. Net change: zero new safety holes,
  one closed hole (constructs can no longer write to plugin code).

- **Tripwires**: scan tool/plugin invocations the same way they
  scan any other Bash command today. No special-casing. The
  authoring prompt may grow a "consider plugin invocations" note
  if real-deck use shows tripwire authors miss them.

- **Watchdog**: gains awareness of registered tools (read from
  `tools.toml`) and loaded plugins (read from plugin manifest
  scan results) for Q&A. "What plugins are available?" / "What
  did construct cx-... try to use?" answer correctly without
  needing a separate channel.

- **Mechanic**: v0 supervisor unaffected. v1 LLM session can read
  `tools.toml` for context when triaging deck issues. v2
  integrity-scan extension is the natural home for "did a plugin
  file change unexpectedly mid-run?" — see deferred list below.

- **Build plan items**:
  - "Script manifests" deferred item is SUBSUMED by this retool
    (manifests = registry entries).
  - `cyberdeck-tools-default-kit.md` v2's "kit packs" proposal
    composes downstream of this retool — kits become curated
    profile templates with their tools lists pre-populated.

---

## Deferred to future slices

Not part of this retool; filed for later when use cases warrant.

- **Mechanic plugin-integrity scan** (mechanic v2). Heartbeat-tick
  hash check on plugin files; fire if mid-run modification.
- **Plugin permission gating**: today any loaded plugin is callable
  by any construct via the bridge. Future: profile-restricted plugin
  visibility, or per-plugin brake-tier settings.
- **In-deck function exposure beyond `load_into_deck`**: e.g., a
  plugin emits an MCP server that constructs invoke through Claude
  Code's `--mcp-config`. Brake hook MCP gating already exists
  (slice 1 shipped). Future capability; design alongside the use
  case that demands it.
- **Hot-reload of plugins** mid-run. Python module reloading is
  fraught; restart-required is the safe default. Revisit if
  development-loop friction warrants it.
- **`tools-default-kit.md` v2 kit-packs reconciliation**. That
  doc's "profiles compose manifest groups" model layers on top of
  this retool cleanly; no contradiction. The retool ships the
  substrate; kit-packs ship downstream.

---

## Things to NOT do (this retool)

- **Don't merge tools and plugins into one type.** They have
  different invocation models, different lifecycles, different
  trust boundaries. Surface them adjacently in the UI (Tools tab
  with kind glyphs) but keep them structurally distinct.

- **Don't put plugins in `<home>/`.** The brake hook protects
  `<deck-source>/`; that protection is the safety guarantee for
  plugin code. Putting plugins in home (where constructs CAN
  write) re-opens the half-baked-file failure mode.

- **Don't auto-discover scripts in `<home>/tools/<subdir>/`.**
  Registry is explicit; loose directories are organizational.
  Auto-discovery means surprise registrations, malformed entries,
  and unclear ownership.

- **Don't hot-reload plugins.** Restart-required is the safe
  default. Mid-run module reloading would compound any plugin
  bug into a deck crash.

- **Don't list plugins in profiles.** Plugin selection is daemon-
  per-spawn; profile-level plugin lists conflate two
  decision-makers (netrunner authors profile, daemon picks plugins
  per task) and produces confusing per-spawn outcomes when they
  disagree.

---

## Cyberpunk vocabulary alignment

Tools are **what's installed**. Plugins are **what extends the
deck**. Profiles are **recipes** the daemon picks per task. Three
nouns, three jobs, no overlap. Same shape as a real workshop —
the hammer is on the bench (tool), the lighting rig is bolted to
the ceiling (plugin), the project plan is on the whiteboard
(profile). Each has its own concerns; none confuses with the
others.

---

*Filed 2026-05-02. Implementation queued behind discrete bugs
(cache-miss-per-spawn especially — that's real money). When
picking this up, start with P1 (tools registry); P1 alone is
shippable and immediately useful even if P2-P5 land later.*
