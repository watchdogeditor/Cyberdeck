# Cyberdeck — Collections Intake Design

> **STATUS: IN-FLIGHT (filed 2026-05-06; no code yet).**
> Updated 2026-05-07. Implementation queued behind the prompt-shaping
> pass and Mechanic v2. Line item in `cyberdeck-build-plan.md` →
> NEAR FUTURE. Read the whole doc when picking it up; the recipe
> shape and assembler design are the load-bearing parts.

---

*Filed 2026-05-06 after a heist of transilienceai/communitytools surfaced
the "github-distributed reference collection" as a recurring need (PATT,
SecLists, nuclei-templates, GTFOBins, HackTricks) that doesn't fit the
post-retool tools/plugins/profiles taxonomy cleanly. This doc defines an
**intake** mechanism that scaffolds a normal plugin from a recipe — one
recipe per collection, one assembler script, no fourth category. The
netrunner's framing won out over an earlier "fourth category" proposal:*

> *"what we REALLY need is some sort of file that provides instructions
> for assembling the interface plugin — because if we can make some
> standards, we could literally just delegate the deck to grabbing the
> gh, mirroring it locally, and then filing it away and having it
> appear in the tools tab as a plugin."*

*Implementation queued behind the prompt-shaping pass and Mechanic v1.
Read alongside `cyberdeck-tools-plugins-profiles-retool.md` — intake
produces standard plugins under that retool's framework.*

---

## Why this exists

Most github-distributed security tooling arrives as a **cluster of
files with a README and varying amounts of in-repo tooling**. PATT is
the canonical example — ~80 directories of curated payload markdown +
README + supporting media. SecLists, nuclei-templates, HackTricks,
GTFOBins, LOLBAS all follow the same shape with different file
layouts.

The post-retool taxonomy doesn't have a clean home for these:

- **Tools** are single binaries or single registered scripts. A
  directory-of-files collection isn't one tool.
- **Plugins** are deck-source-resident behavior bundles. A collection
  CAN be a plugin, but if you do it naively each collection is a
  hand-rolled `plugin.py` exposing essentially the same shape: list
  available items, fetch one, return contents. Scales badly.
- **Profiles** are recipes — wrong layer.

Two ways out were considered:

1. **A fourth `collections` category** sitting alongside
   tools/plugins/profiles, with a shared `collection_bridge.py` and
   per-collection manifests resolved at runtime. Adds a permanent
   runtime layer to the framework and a permanent maintenance
   surface.

2. **Intake-driven plugin scaffolding** (this doc). The abstraction
   lives in a one-time **assembly step**, not a permanent runtime
   layer. Plugins stay the only deck-extended-capability shape;
   intake is bookkeeping that fills the existing slot.

Option 2 wins. See *Why not a fourth category* below for the full
breakdown.

---

## The three pieces

### 1. Intake recipe

A TOML file the netrunner writes, one per collection. Lives at
`<deck-source>/intake-recipes/<name>.toml`. Declares everything the
assembler needs to scaffold a working plugin.

```toml
name = "patt"
description = "Curated web vuln payloads from PayloadsAllTheThings."

[source]
repo = "swisskyrepo/PayloadsAllTheThings"
ref = "main"                  # branch, tag, or pinned SHA
update_check = "weekly"       # "weekly" | "manual"
max_size_mb = 200             # assembler refuses to clone past this

[layout]
strategy = "directory_index"  # see Layout strategies below
fetch_pattern = "{key}/README.md"

[interface]
# Functions the generated plugin will expose. Drawn from the
# strategy's function set; listing them explicitly lets the
# netrunner narrow.
functions = ["list", "fetch", "search"]

[aliases]
# Human-friendly query keys → real directory/file names.
"sql-injection" = "SQL Injection"
"xss-reflected" = "XSS Injection"
"ssrf" = "Server Side Request Forgery"

[construct_instructions]
template = """
PATT (PayloadsAllTheThings) is available for payload reference.
Common categories: sql-injection, xss-reflected, ssrf, xxe, ssti, ...
Usage: python <home>/tools/deck/plugin_bridge.py patt -f fetch -a <category>
"""
```

The recipe is small (~30-50 lines per collection), declarative, and
fully replaces hand-rolling a `plugin.py` for each new collection.

### 2. Intake assembler

A deck-side CLI script the netrunner invokes:

```bash
python intake.py <name>            # one-shot: clone + scaffold
python intake.py <name> --update   # refresh from upstream
python intake.py <name> --remove   # uninstall (deletes plugin dir)
python intake.py --list            # show installed collections
python intake.py --check-updates   # poll all collections' upstreams
```

What it does on first install:

1. Read `<deck-source>/intake-recipes/<name>.toml`
2. Validate recipe against schema (clear error messages for malformed
   recipes — schema validation is half the value)
3. Clone `<source.repo>@<source.ref>` into
   `<deck-source>/plugins/<name>/data/`. Submodule preferred for
   version-pinning + reproducible clones; lazy-fetch as fallback for
   collections where submodules feel heavy. Decision per-recipe via
   a `[source].mode = "submodule" | "lazy_clone"` field.
4. Generate from templates:
   - `<deck-source>/plugins/<name>/plugin.toml`
   - `<deck-source>/plugins/<name>/plugin.py` (thin wrapper over
     `_intake_helpers`, parameterized by layout strategy)
   - `<deck-source>/plugins/<name>/construct_instructions.md`
   - `<deck-source>/plugins/<name>/.intake.json` (provenance:
     recipe-hash + upstream-SHA-at-install + install timestamp;
     used by update-check to detect drift and re-scaffold needs)
5. Print `deck restart required` — plugins are hot-loaded at startup
   only.

The assembler does NOT run during deck startup. It is netrunner-
invoked, deliberately. See *Who triggers intake* below.

### 3. Generated plugin

After intake, the plugin is **indistinguishable** from a hand-rolled
one. Same `plugin.toml` schema, same `plugin_bridge.py` invocation
contract, optionally implements `load_into_deck`, appears in the
Tools panel exactly like the screenshot plugin.

The generated `plugin.py` is a ~30-line wrapper that imports a shared
helper module (`<deck-source>/plugins/_intake_helpers.py`, also
bootstrapped by intake), picks the right layout strategy by name,
and injects this plugin's data path + alias map. The actual fetch /
list / search logic lives in the helper module — defined once,
reused by every intake-generated plugin.

This means:
- Adding a new collection = drop a recipe + run intake. No new code.
- Adding a new layout strategy = extend `_intake_helpers.py`.
  Existing intake-generated plugins don't need regeneration; they
  reference their strategy by name.

---

## Layout strategies

A small fixed set, expanded as new collection shapes demand. v1
ships with three:

### `directory_index`

Top-level directories under `data/` are the queryable items. Fetch
returns the contents of a known file inside (default `README.md`,
configurable via `fetch_pattern`). PATT, HackTricks fit here.

```
data/
  SQL Injection/
    README.md           ← fetch("sql-injection") returns this
    Intruder/...
  XSS Injection/
    README.md
  ...
```

### `flat_files`

Leaf files at a single level (or under one prefix). Fetch returns
the file contents (or path, configurable via `[fetch].return = "contents" | "path"`).
SecLists, GTFOBins-as-one-file-per-binary fit here.

```
data/
  rockyou.txt           ← fetch("rockyou") returns contents (or path)
  sqlmap-payloads.txt
  xss-vectors.txt
```

For wordlists specifically, returning the **path** is usually right
(constructs pipe wordlist files into ffuf/hydra/sqlmap rather than
ingest content into prompts).

### `nested_glob`

Walk an arbitrary tree, filter by glob pattern + optional metadata
field. Fetch returns matching paths or contents. Nuclei-templates
fit here (filter by `tags: sqli` against YAML frontmatter).

```
data/
  http/
    cves/
      2024/
        CVE-2024-3400.yaml
      ...
    misconfiguration/...
```

The strategy module is the only piece that grows when a new
collection shape arrives.

---

## Update awareness

Each generated plugin carries an `update_check()` function that
compares the upstream SHA to the SHA recorded in `.intake.json`.
Cadence is set by the recipe:

- **`manual`** — the plugin never offers updates on its own. The
  netrunner runs `python intake.py <name> --update` deliberately.
- **`weekly`** — the plugin's `load_into_deck(app)` registers a
  periodic check (once per deck launch + once per week thereafter)
  that fetches upstream's `refs/heads/<ref>` SHA, compares to local,
  and posts a chatlog event if drift exists:

  > `📦 patt: upstream has 12 new commits since 2026-04-30. Run
  > 'python intake.py patt --update' to pull.`

**Updates never apply automatically.** Deck-source mutation is
netrunner territory — auto-pulling would put the assembler in the
"thing that writes deck source while the deck is running" position,
which fights the brake hook's deck-source-write protection. The
update-check path can READ upstream metadata (HTTP HEAD against
GitHub) without any local writes; the apply step is always a
netrunner-invoked CLI command, post-deck-shutdown.

---

## Who triggers intake

### v1: netrunner CLI only

```
python intake.py <name> [--update | --remove]
```

The daemon does not have access to intake. The netrunner runs it
between deck sessions (deck restart is required to pick up new
plugins anyway, so intake → restart is one motion).

Why this is the right v1: the assembler writes under
`<deck-source>/plugins/`, which is the brake hook's strictest
protected zone. A daemon that can drive intake is a daemon that can
write deck source. That's a doorway worth keeping closed in v1.

### Deferred — daemon-driven intake

A future "install <gh-url> as a plugin" flow where the netrunner
asks the daemon conversationally and the daemon runs intake itself.
Requires:

- A signed/approved-recipe registry the daemon can match against
  (so the daemon isn't authoring arbitrary recipes itself).
- Brake-hook carve-out for the assembler under daemon control.
- Probably attention-area integration so the netrunner approves
  the intake action before the assembler runs.

Filed as deferred. Not v1.

---

## Why not a fourth category

The earlier draft of this conversation proposed a `collections`
category sitting alongside tools/plugins/profiles, with a shared
`collection_bridge.py` and per-collection manifests resolved at
runtime. That design was strictly worse:

- **Permanent runtime surface.** A new category means new code in
  the deck's startup path, the Tools panel, the brake hook's path
  awareness, the bus event topology, the spawn payload schema, the
  daemon's "what does this construct have access to" model. Intake
  produces plugins; everything downstream stays unchanged.
- **Two access patterns to maintain.** Plugins via plugin_bridge,
  collections via collection_bridge. Intake collapses to one.
- **Update logic centralized vs distributed.** A central refresh
  tower that knows about all collections is harder to evolve than
  per-plugin update functions. New strategies break the bridge;
  new strategies under intake just add a helper.
- **Worse failure isolation.** A misbehaving collection takes the
  bridge down for everyone; a misbehaving generated plugin only
  fails its own load (existing plugin try/except wrap).

The reframing — **the abstraction is a recipe, not a runtime
category** — is the strictly better answer. Plugins are the only
abstraction; intake is the bookkeeping that fills it.

---

## Concrete first-collection candidates

Ordered by likely value to the deck's actual workload:

1. **PATT** (`directory_index`) — payload reference for injection /
   server-side / client-side / api-security work. The triggering
   example. ~50MB.
2. **SecLists** (`flat_files`) — wordlists for fuzzing, brute-force,
   recon. Always needed once subdomain enumeration or directory
   brute lands. ~1.5GB; will need shallow-clone / sparse-checkout.
3. **GTFOBins / LOLBAS** (`flat_files`) — Linux/Windows privesc
   binary cheat sheets. Small (~200 entries each) but extremely
   high-leverage when a construct lands a foothold.
4. **HackTricks** (`directory_index` or `nested_glob`) — knowledge
   base. Big. Probably want topic-search rather than full
   clone-and-grep. May exceed the simple-strategy set.
5. **nuclei-templates** (`nested_glob`) — only if/when the deck
   does automated scanning. Lower priority for the netrunner's
   current workload.

PATT is the v1 target. The other four are validation that the
intake design holds up across different layout strategies — building
intake with PATT alone risks overfitting to `directory_index`.
Recommended: scaffold PATT first, then validate against SecLists or
GTFOBins before declaring v1 done.

---

## Open questions / deferred

- **Submodule vs gitignored-data.** Submodule wins on
  reproducibility but bloats fresh deck clones (and trips Defender
  on the markdown-as-webshell pattern — confirmed real, see the
  2026-05-06 PATT-clone Defender alert in the netrunner's session
  log). Lazy-fetch is quieter but means a fresh deck has no
  collections until intake re-runs. Probably ship both modes via
  per-recipe `[source].mode` and let the netrunner decide per
  collection.
- **Recipe authoring ergonomics.** Hand-writing a recipe per
  collection is manual upfront cost. A
  `python intake.py --scaffold-recipe <gh-url>` mode that probes
  the repo (top-level structure, README presence, file extensions)
  and proposes a recipe could cut that. Deferred but plausible.
- **Defender exclusion path.** If submodule mode is the default,
  the netrunner will want a single Defender exclusion path
  (`<deck-source>/plugins/*/data/`) to suppress the cascade of
  signature hits. Document in deck setup notes; not enforceable
  by intake itself.
- **Collection size limits.** PATT ~50MB, SecLists ~1.5GB,
  HackTricks-clone multi-hundred-MB. The recipe's `max_size_mb`
  field is a guard; the assembler refuses to clone past the limit
  without `--force`. For monster collections, support
  `[source].sparse_checkout = ["path1/", "path2/"]` to clone subsets.
- **Plugin registry / Tools panel surfacing.** Generated plugins
  appear in the Tools panel via the existing plugin model. Whether
  intake-installed plugins should be visually distinguished
  (different glyph, "from intake" tag, version + last-updated
  shown on hover) is a UI question. Probably yes — netrunner can
  tell at a glance which plugins came from upstream collections vs
  hand-rolled, and the upstream-version readout is useful.
- **Multi-collection construct prompts.** When a construct gets
  multiple intake plugins assigned (e.g., injection construct gets
  patt + seclists), the per-plugin `construct_instructions.md`
  blocks concatenate naively. May need a templating layer or a
  daemon-side aggregator. Defer until the second collection ships
  and the problem is concrete.

---

## References

- `cyberdeck-tools-plugins-profiles-retool.md` — the framework
  intake produces plugins for. Required reading before
  implementing.
- `cyberdeck-spec.md` → *Tool registry* — what intake plugins look
  like from the construct's side once registered.
- `CLAUDE.md` (root) — gotchas and design conventions.
- Project memory: `project_prompt_shaping_design.md` — the
  prompt-shaping pass intake will integrate with (construct
  instructions, daemon-side plugin assignment per spawn).
