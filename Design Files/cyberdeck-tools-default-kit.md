# Cyberdeck — Default Tools Kit v2

*Opinionated, hot-load-aware, profile-shaped kit for `<home>/tools/`.
Companion to `cyberdeck-spec.md` (Tool registry / Tools panel).
Supersedes v1 (2026-04-30, early). Folds in the research report at
`cyberdeck-tools-research-report.md` and the netrunner's
follow-on shape decisions: hot-load semantics, daemon-side check-
everything escape hatch, pipeline-of-profiles planning idiom,
construct hand-off protocol, plugin-folder shape for tool scripts,
and the `turbo_researcher` profile.*

Filed: 2026-04-30, late. Status: design, no code yet. v1 frozen
(see git history) for diff posterity.

---

## 0. What changed from v1

Ten shape changes, ranked by impact:

1. **Categories tripled.** v1's seven sysadmin-shaped categories
   become twelve hacker-shaped categories. New: `web/`, `ad/`,
   `cloud/`, `passwords/`, `osint/`. Recon trimmed (system tools
   moved out). The deck is a pentest workbench, not a Linux
   power-user kit.
2. **Hot-load is the design constraint.** Every manifest in a
   profile's kit is fully expanded into each spawned construct's
   system prompt at spawn time. This caps profile size, drives
   manifest density, and dictates kit composition. Detail in §4.
3. **Profiles compose manifest groups, not categories.**
   `web_pentester` pulls `web/projectdiscovery_suite +
   web/ffuf + web/sqlmap + web/secrets`, not the whole `web/`
   folder. Categories become taxonomy on the tool side, selectors
   on the profile side. Detail in §4.3.
4. **Plugin-folder shape replaces sibling-TOML.** Each tool gets
   its own folder: `<home>/tools/<cat>/<name>/{tool.toml, run.{py,sh,
   md}, README.md}`. Mirrors the existing plugin convention. Detail
   in §3.
5. **The daemon plans in profile pipelines.** Riding existing
   primitives — multi-action turns for parallelism, multi-turn
   iteration with `OUTCOMES:` for sequential dependency, the
   discovery-then-fanout pattern. v2 extends the `spawn` action
   shape with a `profile` field and teaches the daemon's prompt
   to think in pipelines. Detail in §5.
6. **Constructs hand off cleanly.** A construct that hits its tool
   gap returns a structured tail block — `findings`,
   `next_action_needed`, `recommended_profile`,
   `state_to_pass_forward` — that the daemon parses to spawn a
   successor. Same channel covers planned hand-offs and
   discovered-mid-task ones. Detail in §6.
7. **Daemon escape hatch: describe_kit.** Default daemon context
   is profile names + descriptions only. A new action type
   `{"type": "describe_kit", "scope": ...}` lets the daemon
   request the full library, a single category, or a single tool's
   manifest when its profile menu is insufficient. Detail in §5.2.
8. **Profile descriptions become capability menus.** Every profile
   reads as `use_when` + `dont_use_when` + tool list, so the
   daemon can shop the menu correctly during planning. Detail in
   §9.
9. **Wireless gating moves from "opt-in profile" to "form-factor
   hardware check."** The kit ships wireless tools by default in
   pentester profiles; tools that need a monitor-mode interface
   declare `requires_hardware` and warn cleanly when the deck
   lacks it. Detail in §3.3 and §9.
10. **Operational tempo is a first-class axis.** Every tool
    declares a `noise_floor` (silent / quiet / medium / loud /
    klaxon); every profile declares a `default_noise_posture`;
    the daemon plans pipelines that climb the noise scale (start
    silent, escalate only when needed); the netrunner gets a
    deck-global **stealth mode** toggle (cousin to `p` plugin
    cutoff) that hooks the brake to refuse any tool above a
    chosen ceiling. Detail in §7.

`turbo_researcher` joins the profile templates (§10) — non-pentest,
information-shaped, designed to formalize the "go pull info, build
a report" workflow that this very design conversation has been
exercising.

---

## 1. Decisions up front (revised)

The seven seed questions, with v2 answers:

1. **Categorization.** Twelve categories: `recon/`, `net/`, `web/`,
   `ad/`, `cloud/`, `osint/`, `data/`, `crypto/`, `passwords/`,
   `media/`, `system/`, `wireless/`. Plus an `assumed/` README
   listing what's assumed-installed (git, curl, python, ssh, etc.)
   without registered manifests. Categories tag tools; profiles
   compose kits across them.

2. **Tool list.** Lean per-category, generous overall. The kit on
   disk is wide; what enters a construct's prompt is narrow,
   profile-controlled. Per-category lists in §8.

3. **Install model.** Three rings. **Always-assumed** (deck refuses
   to launch without them — coreutils, bash, curl, python3, git,
   ssh). **Default kit** (the twelve categories, installed by
   `cyberdeck-install --profile desktop`). **Hardware-gated**
   (wireless tools that require a monitor-mode-capable card; GPU-
   accelerated password tools). The deck ships everything the
   netrunner persona wants; runtime gating happens via the manifest's
   `requires_hardware` field at the moment of invocation.

4. **Wrappers vs raw.** Default to **raw**, with one exception: a
   wrapper script earns its slot when it composes a fixed chain
   that constructs would otherwise re-derive every time, OR when
   it normalizes output to JSON for downstream piping. Six default
   wrappers in v2: `pdf_to_text`, `cert_probe`, `top_disk`,
   `scan_subnet`, `scan_wifi`, `report_compile`. (The last is new
   for `turbo_researcher`; see §10.) Ban thin pass-through wrappers.

5. **Manifest design.** Plugin-folder shape (§3). One folder per
   tool: `tool.toml` (machine-parseable manifest), entry point
   (`run.py` / `run.sh`), `README.md` (human docs the deck doesn't
   parse but the netrunner can read in the Tools panel). Manifest
   groups (`<cat>/<group>.toml`) collect related tools into one
   prompt-economical bundle — the ProjectDiscovery suite is the
   archetype.

6. **Form factors.** Five install profiles: `minimal`, `desktop`,
   `wearable`, `pentester-base`, `pentester-with-radio`. Wearable
   drops cloud and exploit kits; pentester-with-radio adds wireless
   on top of pentester-base. Matrix in §9.

7. **When-to-reach-for guidance.** Each tool's manifest carries a
   `when_to_use` field. Profile templates' `default_construct_addendum`
   lifts these into prose-shaped capability menus the construct
   sees at spawn time. The same prose feeds the Tools panel for
   the netrunner's reading pleasure.

---

## 2. Posture

Unchanged from v1. Restating because it's load-bearing for every
specific call below:

- **Capable, not sandboxed.** The deck is a hacker's workshop. The
  default install assumes the netrunner wants power. Brake profiles
  + per-construct profiles narrow capability for a specific
  construct; the *machine-level* install is the full kit minus
  hardware gates.
- **Textual output that pipes.** Every default tool earns its slot
  by having a clean CLI surface and structured output (JSON, TSV,
  parseable text). GUI-only tools are second-class — Wireshark is
  out, tshark is in. Burp is named in addendums but not shipped.
- **Authorized engagement assumed.** The deck doesn't refuse to
  ship offensive tools; it does ship profile addendums that demand
  scope confirmation before action. The brake hook handles
  enforcement when the netrunner wants tighter rails.

---

## 3. Tool layout on disk

### 3.1 The folder shape

Mirrors the plugin convention (`<home>/plugins/<name>/`). Each
tool is a folder under its category:

```
<home>/tools/
├── recon/
│   ├── nmap/
│   │   ├── tool.toml
│   │   ├── run.sh           # thin wrapper — only when value-add
│   │   └── README.md        # human-readable usage notes
│   ├── naabu/
│   │   └── tool.toml        # no wrapper — raw binary, manifest-only
│   ├── _README.md           # category overview, named tools, decisions
│   └── projectdiscovery_suite.toml   # group manifest, see §3.3
├── web/
│   ├── nuclei/
│   ├── ffuf/
│   ├── sqlmap/
│   └── _README.md
├── ad/
│   ├── netexec/
│   ├── impacket/
│   ├── ...
│   └── _README.md
... and so on for each category
└── deck/                    # special — the deck-dispatcher script
    └── cyberdeck.py         # see existing implementation; not a tool
```

The `cyberdeck.py` dispatcher in `tools/deck/` is plumbing
(emits magic markers for the deck's UI side-effect protocol),
not a kit tool. Stays as-is.

### 3.2 The manifest

`tool.toml` is the machine-parseable contract. Schema:

```toml
# Required.
name = "nuclei"
category = "web"
description = """
Template-based vulnerability scanner from ProjectDiscovery.
9000+ community templates. The default reach for "audit a known
URL for known vulns." Output is JSON when -j is set.
"""
entry = "raw"  # "raw" = call the binary directly; otherwise = path to wrapper

# Tags. Tools can carry both verb-tags and domain-tags so profiles
# can compose by either dimension. See §4.3 for how profiles use
# this.
verb_tags   = ["scan", "match-templates"]
domain_tags = ["web", "appsec"]

# Args, in invocation order. Documents the surface; the construct
# sees this pasted into its system prompt when this tool is in its
# kit. Optional fields default to what their type implies.
[[args]]
name = "target_url"
type = "string"
required = true
description = "URL to scan. Use -l <file> form for multi-target."

[[args]]
name = "severity"
type = "enum"
enum = ["info", "low", "medium", "high", "critical"]
default = "medium,high,critical"
description = "Severity filter. Default skips info/low."

[[args]]
name = "rate_limit"
type = "int"
default = 50
description = "Requests per second. Lower for fragile targets."
# Per-arg noise modulation. Some flags meaningfully change the
# tool's tempo; document the bands so the construct can pick
# settings that match the engagement's posture. Optional.
noise_modifier = """
< 30 rps  → quiet (blends with normal traffic).
30-100    → medium (default).
> 100 rps → loud (will trip rate-based detections).
"""

# Output shape. Documentation, not enforcement.
output_format = "json"  # or "text" / "tsv" / "binary" / "mixed"
output_sample = """
{"template-id":"http-missing-security-headers","host":"https://target.com",
 "matched-at":"https://target.com/login","severity":"info","timestamp":"2026-..."}
"""
output_schema = """
One JSON object per finding, NDJSON-style (one per line).
Fields: template-id, host, matched-at, severity, info, timestamp,
matcher-name, extracted-results.
"""

# Side effects. Used by the brake hook for runtime gating.
# Sharper than v1's "none/filesystem/network/destructive":
side_effects = "network_active"
# values: "none" / "filesystem" / "network_passive" / "network_active"
#       / "destructive" / "stateful_external"
# A paranoid brake lets network_passive through; blocks network_active.

# Operational tempo. Five-level ordinal scale; see §7. The floor
# is the QUIETEST the tool can run with sensible flags; flags can
# push noise up via noise_modifier annotations on individual args.
# A profile's default_noise_posture is the ceiling for its kit;
# the deck-global stealth mode caps everything regardless.
noise_floor = "loud"
# values: "silent" / "quiet" / "medium" / "loud" / "klaxon"
#   silent  — no packets to target (OSINT, archives, passive DNS, CT logs)
#   quiet   — minimal probes, blends with normal traffic
#   medium  — focused active work, rate-limited
#   loud    — default-tempo automated scanning
#   klaxon  — provokes alerts on competent SOCs

# Hardware / dependency gates. Manifest-time declaration; the deck
# warns at startup when a binary is missing and refuses to expand
# the manifest into a construct prompt when required hardware is
# absent (warns the netrunner, doesn't silently skip).
[requires]
binary = "nuclei"
binary_min_version = "3.0"
hardware = []                  # e.g. ["monitor_mode_wifi", "gpu", "sdr_rx"]
platforms = ["linux", "darwin", "windows"]
internet = true                # this tool is useless offline

# When-to-use prose. Lifts directly into the construct's prompt
# addendum. This is what teaches the model when to reach for X
# vs. Y; write it like you're onboarding a sharp colleague.
when_to_use = """
Reach for nuclei when you have a known URL or set of URLs and want
breadth-first vuln detection. Pair with httpx (probe) and
subfinder/dnsx (resolve) earlier in the chain. Don't reach for
nuclei for SQLi specifically (sqlmap is purpose-built) or for
parameter fuzzing (ffuf). Always pass --severity to skip info-tier
noise unless the engagement explicitly wants it.
"""

# Optional sibling manifests this tool composes well with. The
# deck uses these to suggest related tools in the Tools panel and
# (future) to auto-suggest profile compositions.
pairs_with = ["web/httpx", "web/subfinder", "web/dnsx"]

# Versioning of the manifest schema itself. Allows v2.x evolution
# without breaking older manifests.
manifest_version = "2.0"
```

### 3.3 Group manifests

Some tools naturally compose into a chain that's documented better
as one unit. The ProjectDiscovery suite is the archetype: subfinder,
dnsx, httpx, naabu, nuclei, katana — each individually has a
manifest, but the chain `subfinder | dnsx | httpx | nuclei` is a
worth-its-own-doc workflow. Group manifests sit at the category
root:

```
<home>/tools/web/projectdiscovery_suite.toml
<home>/tools/web/projectdiscovery_suite.md     # human-readable chain doc
```

A group manifest's `tool.toml` references its members and contributes
*one* prompt-block to constructs that include it, in lieu of N
individual blocks. Saves ~60% of tokens vs. shipping each binary's
manifest separately.

```toml
# Group manifest example.
manifest_kind = "group"
name = "projectdiscovery_suite"
category = "web"
description = """
ProjectDiscovery's recon-to-detection chain. Six binaries that
compose: subfinder (passive subdomain enum) → dnsx (bulk DNS
resolution + record interrogation) → httpx (HTTP probing,
fingerprint) → naabu (port discovery) → nuclei (template-based
vuln scan) → katana (modern crawler with JS support).

All six are fast Go binaries with consistent CLI shape: take stdin
or -l file of targets, emit one-result-per-line on stdout, support
-j for JSON. They pipe into each other natively.
"""

members = ["subfinder", "dnsx", "httpx", "naabu", "nuclei", "katana"]

verb_tags   = ["recon", "scan", "probe"]
domain_tags = ["web", "appsec"]

# A profile that includes a group manifest gets the GROUP block in
# its prompt (the description above plus the chain doc), then per-
# member one-liners with their key flags and output shapes. Total
# cost ≈ 1.4× a single tool's manifest, vs. 6× for individual
# inclusion.

when_to_use = """
The default reach for any "characterize a domain's web attack
surface" task. Pipeline:
  subfinder -d <domain> -all -silent \
    | dnsx -resp \
    | httpx -title -tech-detect -status-code \
    | nuclei -severity medium,high,critical -j

Add naabu before httpx if non-standard ports matter; add katana
after httpx for crawl-based discovery. Use individual binaries
when the chain isn't the point — naabu alone for "scan a single
host's ports" or nuclei alone for "test this URL against this
template".
"""
```

### 3.4 The Tools panel rendering

The Tools panel ([cyberdeck-spec.md](cyberdeck-spec.md):342) becomes
hierarchical:

```
Tools/
├── Plugins/        (existing)
├── Scripts/        (the new kit)
│   ├── recon/
│   │   ├── nmap          [manifest, README]
│   │   ├── naabu         [manifest only]
│   │   └── _README.md    (category overview)
│   ├── web/
│   │   ├── projectdiscovery_suite  [GROUP — 6 binaries]
│   │   ├── ffuf
│   │   └── ...
│   └── ...
├── Profiles/       (existing)
└── Library status: 187 manifests loaded across 12 categories.
                    11 hardware-gated (3 satisfied).
                    2 missing binaries (nuclei: not on PATH;
                    netexec: not on PATH).
```

The bottom-line library status surfaces the deck's actual loadedness.
A construct asking "what tools do I have" reads its profile's kit
addendum (already in its system prompt); a netrunner asking "what's
the deck capable of" reads the panel.

---

## 4. Hot-load shape — the budget that drives everything

### 4.1 What "hot-loaded" means

At deck startup:
- The deck scans `<home>/tools/` recursively.
- Each `tool.toml` and group manifest is parsed and held in a
  registry indexed by name.
- Per-tool `requires` are checked (binary on PATH? hardware
  present? platform match?). Failures don't stop the load; they
  flag the manifest as `state = "unsatisfied"`.
- All this is in-memory; no on-demand parse later.

When a construct is spawned with profile X:
- The deck resolves profile X's `kit.include` list (manifest names
  / group names) against the registry.
- Each resolved manifest's prompt-projection (description, args,
  output schema, when-to-use, etc.) is pasted into the construct's
  system prompt as a "Tools available to you" block.
- The construct sees its kit fully loaded at instant zero. No
  on-demand discovery.

The daemon, at every turn, sees only:
- The profile catalog: name, category, description (incl.
  use_when / dont_use_when), and the *names* of included manifest
  groups. **No manifest text.**
- The brake state, the goal, the construct outcomes so far.

The daemon's `describe_kit` action is how it reads manifest text
on demand (§5.2).

### 4.2 Token budget reality

Conservative numbers per the research report:
- A full tool manifest projection is **300–800 tokens**.
- A group manifest's group-block + per-member one-liners is **~1.4×
  one tool**.
- 10 individual manifests = 3–8K tokens. Practical.
- 20 individual manifests = 6–16K tokens. Steep but workable.
- 40+ = the construct's instruction-following degrades before its
  capability does. Off the table.

Targets per profile:
- **Lean profile** (`code_reviewer`, `data_analyst`,
  `turbo_researcher`): 5–8 manifests.
- **Specialty profile** (`web_pentester`, `ad_operator`,
  `cloud_auditor`): 10–15 manifests.
- **Generalist profile** (`pentester`): 15–20 manifests, hard cap.

Hard cap enforced by the deck: a profile that resolves to >25
manifests refuses to load and surfaces an error. Forces explicit
trimming rather than slow, silent prompt bloat.

### 4.3 Manifest density tactics

These are the levers v2 commits to:

- **Group manifests for natural chains.** ProjectDiscovery suite,
  Impacket suite, aircrack-ng suite — instead of N manifests,
  one group plus N short member entries. Saves 50–60% of tokens.
- **Reach-for tables over reach-for paragraphs.** Each manifest's
  `when_to_use` is dense, table-shaped where possible — verb,
  trigger, alternative. v1's `_README.md` per-category style is
  the template; replicate inside manifests.
- **Tier by reach frequency inside a manifest.** First-reach flags
  get full args entries; rare flags get a one-line mention with a
  pointer to `--help`. The construct can read help on demand;
  the manifest just has to put the verb in the construct's
  vocabulary.
- **Profiles select manifests, not categories.** A profile's
  `kit.include` is a list of `<cat>/<name>` and `<cat>/<group>`
  identifiers. Profiles never say "all of category X."
- **Tags enable verb-axis or domain-axis composition.** A profile
  that wants "anything that does scanning, web or otherwise" can
  declare `kit.include_tags = ["scan"]`. Useful for generalist
  profiles like `pentester` that don't want hand-curation.

Profile schema for v2:

```toml
name = "web_pentester"
category = "Pentest"
description = "..."

use_when = """
The goal involves an authorized web target — a URL, a domain, a
web app, an API. Reach for me when the work is "characterize and
attack web attack surface."
"""

dont_use_when = """
The goal is internal-network / Active Directory work (use
ad_operator), cloud control plane (cloud_auditor), or exploit
development on a binary (exploit_dev). I'm web-shaped only.
"""

# Operational tempo. The ceiling for this profile's kit at default;
# the netrunner's stealth-mode toggle can clamp it lower (never
# higher). See §7. Values: "passive_only" / "quiet" / "default" /
# "loud_ok". The default_construct_addendum should reference this
# explicitly so the construct's instinct lines up with the posture.
default_noise_posture = "default"

default_daemon_addendum = "..."
default_construct_addendum = "..."

# The Claude Code tool list (Bash, Read, etc.) — soft hint.
recommended_tools = ["Bash", "Read", "Write", "WebSearch", "WebFetch"]

[kit]
include = [
  "web/projectdiscovery_suite",     # group: 6 binaries, ~1.4× cost
  "web/ffuf",
  "web/feroxbuster",
  "web/sqlmap",
  "web/secrets",                    # group: gitleaks + trufflehog
  "web/gau",
  "web/mitmproxy",
  "data/jq",
]
# Optional: include_tags for tag-based composition. Profiles can
# mix list-include and tag-include; deduped at resolution time.
include_tags = []

# default_scripts unchanged from v1 — composed wrappers that
# graduate from session work into permanent capabilities.
default_scripts = []
```

Note the schema is **additive over the existing profile shape**.
v2 adds `use_when`, `dont_use_when`, `kit.include`, `kit.include_tags`.
It does not remove `recommended_tools`, `default_daemon_addendum`,
`default_construct_addendum`, `default_scripts`. Backwards-compatible.

---

## 5. The daemon's relationship to tools

### 5.1 What the daemon sees by default

At every turn, the daemon's system prompt contains:
- The profile catalog (name + category + description with
  use_when/dont_use_when + included manifest group *names* only).
- The brake state.
- The goal.
- Construct outcomes so far (the `OUTCOMES:` channel).

It does **not** contain manifest text. A profile catalog of 12
profiles weighs ~2–3K tokens; it stays in the daemon's prompt
across all turns.

### 5.2 The describe_kit escape hatch

A new action shape:

```json
{"type": "describe_kit", "scope": "all" | "<category>" | "<tool_name>" | "<group_name>"}
```

The daemon emits this in its action list when its profile menu is
insufficient — typically during early planning, when it's
deciding whether a goal fits an existing profile or needs a
custom construct task. The deck handles `describe_kit` by injecting
the requested manifest text into the next turn's `OUTCOMES:`-shaped
message under a `KIT:` block. Costs one round-trip per scope.

Default cost: small. Constructs don't see this; only the daemon
does. The daemon's system prompt teaches it to describe_kit
**during planning, not during execution** — once a pipeline is
chosen, dispatching is straightforward.

Three legitimate uses:
1. "Is there any profile or tool that does X?" — `scope: "all"`
   when the daemon doubts the catalog.
2. "I'm deciding between web_pentester and bug_hunter for this
   step; what's actually different?" — `scope: "<category>"` to
   compare kits.
3. "I'm authoring a non-profile construct task; what tools should
   I tell it about?" — `scope: "<tool_name>"` for one specific
   manifest.

The daemon's prompt explicitly bans speculative `describe_kit`
calls ("don't dump the entire library every turn"). The deck logs
the call so a respawn-loop-style watchdog can flag abuse.

### 5.3 The pipeline-of-profiles planning idiom

The daemon's existing planning model already supports this; v2
adds the prompt sections that teach it.

**Existing primitives** ([daemon.py:114](daemon.py:114)):
- Parallelism is first-class — multi-action turns spawn N
  constructs at once.
- Sequential dependency uses the two-turn pattern: turn 1 spawns
  enumeration; turn 2 reads outcomes and spawns analysis.
- Caps + adaptation — daemon replans on outcome receipt.

**v2 additions** to the daemon's system prompt (sketch — final
text iterates with the daemon-prompt designer):

```
PROFILE PIPELINE PLANNING

Each spawn picks a profile. Profiles are capability-bounded;
read their use_when / dont_use_when carefully. A goal that
spans skillsets is decomposed into successive constructs of
different profiles, not one construct attempting all of it.

Pipeline shapes:
  PARALLEL FANOUT
    All branches need the same profile, no inter-dependence.
    Spawn N at once with the same profile.
    Example: scan 10 subdomains for vulns → 10 web_pentester
    constructs, one per subdomain.
  
  DISCOVERY → FANOUT
    First step characterizes; subsequent steps act on the
    characterization. Two-turn pattern.
    Example: turn 1 spawns one recon_specialist to enumerate
    a network; turn 2 reads the outcome and spawns one
    web_pentester per identified web service plus one
    ad_operator if SMB/AD signals are present.
  
  CHAIN
    A linear sequence of profiles where each depends on the
    prior. Plan multi-turn; spawn one at a time.
    Example: turbo_researcher (gather context) →
    recon_specialist (act on context) → web_pentester (attack
    discovered surface).
  
  DAG
    Mixed parallel and sequential. Read each branch's outcome
    before deciding the next branch's profile. The default
    shape for any non-trivial goal.

When a construct's outcome contains a HANDOFF block (see
construct hand-off protocol), use it. The construct has named a
recommended next profile and packaged its findings; consume both
when authoring the successor's task.

When the profile catalog doesn't seem to contain a profile for
the next step, use the describe_kit action to verify before
giving up. If no profile fits, spawn with profile = "default"
and write the task with explicit tool guidance — this is the
"bare construct" escape hatch.

OPERATIONAL TEMPO (NOISE) AS A PLANNING AXIS

Each profile carries a default_noise_posture. The deck-global
stealth setting can clamp it lower. Pipelines should CLIMB the
noise scale, not bounce around it: start with the quietest
profile that can make progress, escalate only when the quiet
work runs dry.

  silent  → osint_researcher, turbo_researcher (read-only)
  quiet   → recon_specialist passive subset, bug_hunter
  default → web_pentester, ad_operator, cloud_auditor
  loud_ok → pentester (generalist), full active recon

When two profiles can both achieve the next step, prefer the
quieter. Loud actions taken before the surface is mapped waste
the engagement's stealth budget for nothing — the right
sequence is "characterize, then act."

If the current stealth setting prohibits the noise level the
next step needs, surface that to the netrunner via `chat`
rather than burning a spawn that the brake will refuse.
```

### 5.4 The spawn action shape change

v1 / current shape ([daemon.py:96](daemon.py:96)):
```json
{"type": "spawn", "task": "..."}
```

v2 shape:
```json
{
  "type": "spawn",
  "profile": "web_pentester",
  "task": "...",
  "caliber": {...}    // optional, model+effort, per cyberdeck-model-effort-design.md
}
```

`profile` is required. `default` is always available; profile
absence in the catalog is an error the daemon should self-correct
via describe_kit + retry, not silently fall through to bare.

---

## 6. Construct hand-off protocol

A construct that hits a tool gap mid-task — or that knew it was the
first leg of a longer chain from the start — terminates with a
structured tail block. The daemon parses it and authors the
successor's task using the findings as input.

### 6.1 The HANDOFF block

The construct's final message, after its prose summary, ends with:

```
---
HANDOFF
findings: |
  Brief structured summary of what this construct accomplished.
  Multi-line. The daemon reads this verbatim into the successor's
  task, so write it for the successor's consumption — not for the
  netrunner.
next_action_needed: |
  One sentence: what the successor needs to do.
recommended_profile: web_pentester
state_to_pass_forward:
  - file: /home/netrunner/scan-results.json
    purpose: "List of live HTTPS hosts from the recon pass."
  - inline: "Confirmed creds: domain\\user / 'P@ssw0rd!'. AD
              functional level 2016."
confidence: high  # high | medium | low
---
```

Schema notes:
- `recommended_profile` is a *suggestion*. The daemon may override
  if it has better information (a different profile in the catalog
  that the construct didn't know about, or a netrunner injection
  that changed the plan).
- `state_to_pass_forward` items are either `file` (path the
  successor reads) or `inline` (text the daemon embeds in the
  successor's task).
- `confidence` lets the daemon weight the recommendation. Low
  confidence → daemon should `describe_kit` before committing.
- The `---` fences are literal; the deck parses them. Anything
  outside the HANDOFF block is the construct's normal prose
  output, surfaced to the netrunner as before.

### 6.2 When constructs emit it

Three triggers, all taught in the construct's system prompt:

1. **Planned hand-off.** The task brief explicitly named the next
   step ("after this, an ad_operator will follow up on any SMB
   findings"). The construct emits HANDOFF on completion as part
   of its normal flow.
2. **Tool gap mid-task.** The construct discovers it can't do step
   N because its kit doesn't include the right tool. It does as
   much as it can without the missing tool, then emits HANDOFF
   naming the gap.
3. **Wrong profile, discovered late.** The construct realizes the
   task is actually a different shape than the profile suggested
   ("this isn't a web app, it's a thick client over a TCP socket
   — should be net/ work"). It emits HANDOFF immediately with
   minimal findings.

The construct prompt explicitly does NOT teach hand-off as a
default exit ramp. It's a structured tool for a real situation,
not "I'm tired, hand off to someone else." A construct that has
the tools and time to finish should finish.

### 6.3 Daemon's role on receipt

The daemon's outcome-reading logic detects the HANDOFF block and:
1. Treats the construct as **complete** (not failed).
2. Reads `findings`, `next_action_needed`, `recommended_profile`,
   `state_to_pass_forward`.
3. Validates the recommended profile against the catalog. If
   present and use_when matches, spawns successor with that
   profile. If absent or mismatched, `describe_kit` to find a
   better fit.
4. Composes the successor's task: `next_action_needed` becomes the
   verb; `state_to_pass_forward` files are referenced by path,
   inline state is embedded; `findings` is included as context.
5. Spawns the successor on the same turn (parallel-safe with other
   independent spawns) or the next turn (if it needs daemon
   reasoning to compose the task properly).

Hand-offs **count against `max_total_spawns`** the same as any
spawn. A pathological hand-off chain (HANDOFF → HANDOFF → HANDOFF)
hits the cap, the daemon pauses, the netrunner adjudicates.

---

## 7. Operational tempo (noise / OPSEC)

OPSEC tempo is a first-class axis in real pentest work and was
absent from v1. A construct that finds an open SMB share by
politely asking once is doing different work than a construct
that lights up an IDS dashboard with a dictionary-shotgun. The
deck must support both, let the netrunner pick, and let the
daemon plan around the choice.

### 7.1 The five-level scale

Ordinal so the brake hook can use thresholds and the daemon can
compare:

| Level     | What it means                                                                  | Examples |
|-----------|--------------------------------------------------------------------------------|----------|
| `silent`  | No packets to the target. Pure observation.                                    | OSINT (theHarvester, sherlock, maigret, dnstwist), passive subdomain enum (subfinder), web archives (gau, waybackurls), certificate transparency, BBOT passive mode |
| `quiet`   | Minimal probes, single-shot, blends with normal traffic.                       | One nmap probe on common ports, one nuclei template against one URL, mitmproxy on an existing session, gentle httpx fingerprint, single Kerberos pre-auth probe |
| `medium`  | Focused active work, rate-limited, bounded scope.                              | Kerberoasting one SPN at a time, ffuf with a small targeted wordlist, password spray with sane delays, mid-throttle nmap -sV |
| `loud`    | Default-tempo automated scanning. Will appear on logs as scanning activity.    | Full nuclei at 50 rps, feroxbuster recursive, full nmap -sV across a host, gobuster against a wordlist, sqlmap with default settings |
| `klaxon`  | Provokes alerts on competent SOCs. Visible to any blue team.                   | masscan against /16+, full BBOT active mode, ntlmrelayx + coercer chain, aireplay-ng deauth, hashcat over network with broadcasts, password spray at full rate |

The scale measures **detectability by a competent defender**, not
ethical legitimacy. A `klaxon` action in scope is fine; a `silent`
action out of scope is not. Noise is orthogonal to authorization.

### 7.2 What the manifest declares

Per §3.2: every tool's `tool.toml` carries a `noise_floor` — the
quietest the tool can run with sensible flags. Args that
meaningfully change the tempo carry a `noise_modifier` annotation:

```toml
noise_floor = "medium"   # nuclei at default settings

[[args]]
name = "rate_limit"
type = "int"
default = 50
noise_modifier = """
< 30 rps  → quiet
30-100    → medium (default)
> 100     → loud
"""

[[args]]
name = "templates"
type = "string"
default = "cves,exposures,misconfigurations"
noise_modifier = """
single template by ID  → quiet
severity-filtered      → medium
"all"                  → loud
"""
```

Why the floor and not an exact level: tools have ranges. nuclei
is `medium` at default but can run `quiet` (single template,
single URL, low rate) or `loud` (all templates, default rate,
many targets). The floor + modifier annotations let the construct
calibrate to the engagement.

### 7.3 What the profile declares

Per §4.3: every profile carries a `default_noise_posture` — the
*ceiling* for its kit at default. Four values:

- `passive_only` — the profile must only invoke `silent` tools.
  Used by `osint_researcher`, `turbo_researcher`. Constructs in
  these profiles never touch a target.
- `quiet` — `silent` and `quiet` tools allowed; `medium`+ refused.
  Used by `bug_hunter` (bounty programs typically rate-limit),
  `recon_specialist` when stealth is asked.
- `default` — `silent` through `loud` allowed; `klaxon` refused
  by default. Used by `web_pentester`, `ad_operator`,
  `cloud_auditor`. The "competent pentest" baseline.
- `loud_ok` — all five levels allowed. Used by `pentester`
  (generalist, full-scope engagements), DFIR doesn't apply.

The construct's system-prompt addendum reads the posture and
shapes its instinct accordingly. A `quiet` profile's addendum
explicitly tells the construct: "Do not run nuclei with full
template set; prefer single-template invocations against
identified targets. Do not run feroxbuster recursive at full
rate; use small wordlists."

### 7.4 The deck-global stealth mode

Cousin to `p` (plugin cutoff). A netrunner-controlled toggle
that **clamps every profile's effective ceiling** regardless of
the profile's `default_noise_posture`. Sits on the same
deck-global "emergency controls" axis as the brake and the
plugin airgap.

Four settings, ordinal:
- `off` (default) — no noise clamp; profiles use their declared
  postures.
- `discreet` — clamps everything to `loud` ceiling. Refuses
  `klaxon`. Useful when a noisy action would be acceptable but
  not aggressive scanning.
- `quiet` — clamps to `quiet` ceiling. Refuses `medium`+. Active
  recon allowed only via per-tool quiet modes; aggressive
  scanning blocked.
- `silent` — clamps to `silent` ceiling. Refuses anything that
  touches the target. Pure OSINT mode.

The toggle is implemented as a brake-hook addendum: when
stealth ≠ off, the brake refuses any tool invocation whose
noise level exceeds the clamp. The clamp is computed as
`min(profile_posture, stealth_clamp)`. Per-tool noise is
inferred from the manifest's `noise_floor` plus any
`noise_modifier` annotations on flags the construct passed.

When the brake refuses a noisy invocation, the refusal flows
into the construct's tool result as a clean "stealth mode N
prohibits this — try the quiet alternative" message. The
construct sees the message and adapts. The daemon sees it via
the OUTCOMES channel and, if the noisy tool was load-bearing,
hands back to the netrunner via `chat`.

### 7.5 Status indicator

The deck's status bar gets a stealth indicator next to the
brake and plugin-airgap indicators:

```
[BRAKE: default] [PLUGINS: on] [STEALTH: off]   ...other status...
```

Color coding mirrors the brake: green for `off`, yellow for
`discreet`, orange for `quiet`, red for `silent` (the strictest
clamp). Clicking / keypress (TBD which key — see §13) opens a
modal to change the level mid-engagement. Like the brake, the
toggle is netrunner-only; the daemon cannot change stealth
state.

### 7.6 The daemon's planning use of noise

Two mechanics:

1. **Profile selection respects noise.** When two profiles can
   both make progress on the next step, the daemon picks the
   quieter one unless the engagement explicitly wants noise.
   The pipeline-planning prompt (§5.3) teaches: "Pipelines
   climb the noise scale, not bounce around it. Start with
   the quietest profile that can make progress; escalate only
   when quiet work runs dry."

2. **Stealth state is part of the daemon's context.** The
   profile catalog the daemon reads at every turn includes the
   current stealth setting and the clamp it implies. The
   daemon doesn't try to spawn a profile whose default_noise_posture
   exceeds the current clamp — it would be pointless, the brake
   would refuse the resulting tool calls. Instead, it surfaces
   "the next step needs noise level X but stealth is set to Y"
   to the netrunner via `chat`, asking permission to escalate
   the stealth setting OR pivoting to a quieter approach.

### 7.7 Worked example

Goal: "characterize the security posture of example.com,
recommend follow-up tests."

Daemon planning, stealth = `off` (default), netrunner did not
ask for stealth specifically:

- Turn 1, parallel: spawn `osint_researcher` (silent —
  theHarvester, dnstwist, gh) and spawn `recon_specialist`
  passive-mode (quiet — subfinder, dnsx, httpx fingerprint).
- Turn 2 reads outcomes. OSINT found two leaked AWS keys;
  recon found 14 live HTTPS hosts and 2 wide-open S3 buckets.
- Spawn `cloud_auditor` (default — prowler against the AWS
  keys' account) AND `web_pentester` (default — nuclei
  against the 14 HTTPS hosts, severity-filtered).
- Turn 3: pivot based on nuclei findings; if SQLi candidate,
  spawn another `web_pentester` with sqlmap.

If stealth was `quiet`, the same goal:

- Turn 1: spawn `osint_researcher` (silent) and
  `recon_specialist` passive-mode (quiet).
- Turn 2: reads outcomes. Wants to escalate to `cloud_auditor`
  (default) — but stealth = quiet clamps that profile to its
  quiet subset (prowler at low concurrency). Wants to escalate
  to `web_pentester` (default) — refused, would need stealth
  = off OR clamp web_pentester to quiet (single nuclei template
  per host).
- Daemon emits `chat` to netrunner: "Active scanning needed.
  Currently stealth = quiet. Either flip to off for full
  pentester work, or proceed with single-template nuclei probes
  per host (slower but stays in scope)."
- Netrunner decides.

This is the OPSEC-aware planning the v1 doc had no shape for.

---

## 8. Categories (the kit library)

Twelve categories. Tool names assume Linux / Mac primary; Windows
desktop is supported via WSL or scoop where applicable.

For each: the kit, deliberate omissions, reach-for guidance, group
manifests where applicable. Counts at the bottom of each are
**default install** counts; profile kits pull subsets.

---

### 8.1 `recon/` — discovery and characterization

**Kit:** nmap, naabu, masscan, arp-scan, dig, dnsx, whois, mtr,
traceroute, tcpdump, tshark, subfinder, amass, BBOT.

**Group manifest:** `recon/projectdiscovery_recon` (subfinder, dnsx,
naabu cross-listed with web/).

**Deliberately omitted:** nslookup, ifconfig, netstat, route,
wireshark-GUI (correctly omitted in v1; reaffirmed). `host` (use
dig). `rustscan` (naabu chosen — see report).

**Moved out of recon vs v1:** `ss`, `ip`, `lsof` migrate to
`system/`. They're local-machine introspection, not network
recon.

**Reach-for guidance:**
- "What hosts are on this LAN?" → `arp-scan -l` first; `nmap -sn`
  if not applicable.
- "What ports are open on this host?" → `naabu -host <h> -top-ports
  1000` for fast; `nmap -p- -sV` for thorough.
- "Scan a /16+ for one specific port" → `masscan`, then re-scan
  hits with `nmap` for service detail.
- "Resolve this domain" → `dig <name> +short`; `dnsx` for bulk.
- "Characterize an entire web attack surface" → BBOT.
- "Why is this connection flaky?" → `mtr -rwzbc 100 <host>`.
- "Capture and analyze traffic" → `tcpdump -w cap.pcap host <ip>`,
  then `tshark -r cap.pcap`.

**Wrapper scripts shipped:**
- `scan_subnet` — nmap with deck-tuned "what's on my LAN" preset,
  output normalized to JSON.
- `scan_wifi` — wraps nmcli (or iwlist fallback), JSON output.

Counts: 14 binaries, 1 group manifest, 2 wrappers.

---

### 8.2 `net/` — moving bytes

**Kit:** curl, wget, ncat, socat, ssh / scp / sftp (assumed-installed,
documented), rsync, mosh, websocat, mitmproxy / mitmdump.

**Deliberately omitted:** telnet, ftp, httpie / xh (not necessary;
curl is universal — see report).

**Reach-for guidance:**
- "Fetch a URL" → `curl -fsSL`. Use `-f` for non-zero exit on
  HTTP errors.
- "POST JSON" → `curl -fsSL -H 'Content-Type: application/json'
  -d @body.json`.
- "Mirror a directory" → `wget -c -r -np` or `rsync -avz`.
- "TLS-aware netcat" → `ncat --ssl`.
- "Forward a port" → `socat TCP-LISTEN:8080,fork TCP:host:port`.
- "Inspect or modify HTTPS in flight" → `mitmdump -s addon.py`
  with a Python addon scripting request/response.
- "Talk to a websocket" → `websocat <url>`.

Counts: 10 binaries (3 assumed), 0 groups, 0 wrappers.

---

### 8.3 `web/` — web app pentest (NEW)

The single biggest v2 expansion. Web app pentesting is the
plurality of professional offensive work; v1 had nothing.

**Kit:**
- ProjectDiscovery suite (group): subfinder, dnsx, httpx, naabu,
  nuclei, katana.
- Fuzzing & enum: ffuf, feroxbuster.
- SQLi: sqlmap.
- Historical URLs: gau, waybackurls.
- Visual triage: gowitness.
- Secrets: gitleaks, trufflehog (group: `web/secrets`).
- Pattern matching / pipeline glue: gf, anew, unfurl.
- CSRF audit: xsrfprobe.

**Deliberately omitted:** nikto (every check is in nuclei templates;
Perl output is hostile to constructs), dirb / dirbuster (superseded),
gobuster (ffuf + feroxbuster cover its turf), wfuzz (supplanted
by ffuf), httprobe (supplanted by httpx), gospider / hakrawler
(supplanted by katana), Burp / OWASP ZAP (GUI-first; named in
addendums but not shipped — see §13 open call), assetfinder
(supplanted).

**Group manifests:**
- `web/projectdiscovery_suite` — the 6-binary ProjectDiscovery
  chain. Single group block + per-member one-liners.
- `web/secrets` — gitleaks + trufflehog with the speed-vs-
  verification trade-off documented.

**Reach-for guidance:**
- "What subdomains exist for example.com" → projectdiscovery_suite:
  `subfinder -d example.com -all -silent | dnsx -resp | httpx
  -title -tech-detect`.
- "Audit a URL for known vulns" → `nuclei -u https://target
  -severity medium,high,critical -j`.
- "Find hidden directories" → `feroxbuster -u https://target
  -w wordlist.txt`.
- "Fuzz a parameter" → `ffuf -u https://target/?q=FUZZ -w
  wordlist.txt`.
- "Test for SQLi" → `sqlmap -u 'https://target/?id=1' --batch
  --level 3`.
- "Find old endpoints" → `gau example.com | grep -iE 'admin|api|
  upload' | tee old.txt`.
- "Visual triage of N subdomains" → `cat hosts.txt | gowitness
  scan file -f -`.
- "Find leaked secrets in this repo" → `gitleaks dir .` then
  `trufflehog filesystem . --only-verified` for confirmed.

Counts: 14 individual + 2 groups (covering 8 of those individuals
+ 2 not duplicated) = ~10 manifest-units worth of prompt cost.

---

### 8.4 `ad/` — Active Directory + internal Windows (NEW)

The internal-network attack surface, distinct from `recon/` (which
is "what's there") and `net/` (which is generic transport). v2's
second-biggest gap-fill.

**Kit:**
- NetExec (the swiss-army; CrackMapExec successor — CME is dead).
- Impacket suite (group: secretsdump, ntlmrelayx, getTGT, getST,
  psexec, wmiexec, smbexec, GetNPUsers, GetUserSPNs).
- Responder (LLMNR/NBT-NS/mDNS poisoning).
- mitm6 (IPv6 DHCPv6 poisoning).
- BloodHound CE + bloodhound.py (graph analysis + remote collector).
- Certipy-AD (ADCS enumeration and ESC1-16 abuse).
- Kerbrute (Kerberos pre-auth user enum + spray).
- Coercer (PetitPotam / DfsCoerce / PrintNightmare automation).
- Evil-WinRM (interactive WinRM with built-in upload/download).
- Enum4linux-ng (the Python rewrite — original Perl version
  abandoned).
- LDAPdomaindump.
- mssqlpwner (Kali 2024.4 default).

**Deliberately omitted:** PowerView (PowerShell, runs on target),
Mimikatz (runs on target — named in addendum so the construct can
recommend it, not shipped deck-side).

**Group manifests:**
- `ad/impacket` — single group, ~10 named binaries with one-line
  descriptions each. Saves ~70% over individual manifests.

**Reach-for guidance:**
- "I have creds, what's in the domain?" → `netexec ldap <DC> -u
  user -p pass --groups`, then `bloodhound.py -u user -p pass -d
  <domain> -ns <DC>`, ingest into BloodHound.
- "Coerce a relay" → 3-shell pipeline: `responder -I eth0` /
  `ntlmrelayx.py -t ldaps://<DC> --escalate-user <user>` /
  `coercer coerce -t <victim> -u <attacker_user> -p <pass> -l
  <attacker_ip>`.
- "Test for Kerberoastable" → `GetUserSPNs.py
  domain/user:pass -dc-ip <DC> -request`.
- "Audit ADCS" → `certipy-ad find -u user@domain -p pass -dc-ip
  <DC>`.
- "Spray credentials" → `netexec smb <range> -u users.txt -p
  passwords.txt --continue-on-success`.

Counts: 12 individual + 1 group (covering 10 of those) = ~5
manifest-units worth of prompt cost for the full kit.

---

### 8.5 `cloud/` — cloud + container security (NEW)

OSCP+ added cloud content in late 2024; the field has matured.

**Kit:**
- Prowler (multi-cloud CIS benchmark scanner, read-only).
- ScoutSuite (multi-cloud posture review, HTML report).
- CloudFox (AWS+Azure attack-path enumeration).
- Pacu (AWS post-exploitation framework).
- Trivy (containers / IaC / SBOMs / secrets / vulns).
- Kubescape (k8s posture; replaces kube-bench + kube-hunter for
  most uses).
- Kube-bench (CIS-Kubernetes-Benchmark specifically — kept
  alongside kubescape).
- aws / az / gcloud CLIs (assumed-installed, documented).

**Deliberately omitted:** kube-hunter (abandoned by Aqua),
enumerate-iam (redundant with cloudfox/pacu in lean kit; ship
optional), Grype + Syft (overlap with Trivy; ship optional in
pentester-cloud).

**Reach-for guidance:**
- "Audit my AWS account" → `prowler aws --severity high,critical`.
- "What can this access key do?" → `pacu` interactive.
- "Find attack paths from this IAM role" → `cloudfox aws
  --profile <p> all-checks`.
- "Scan this Docker image" → `trivy image <image:tag>`.
- "Check k8s cluster" → `kubescape scan` (full posture);
  `kube-bench` (CIS).
- "Posture report for handoff" → `scoutsuite aws` (HTML output).

Counts: 7 individual + 3 assumed-installed CLIs.

---

### 8.6 `osint/` — open-source intelligence (NEW)

v1's `osint_researcher` profile pointed at exiftool/whois/dig and
called it done; the actual OSINT field has rich tooling.

**Kit:**
- theHarvester (email / subdomain / IP gathering).
- Sherlock (~400 social platforms, fast lookup).
- Maigret (~3000 sites, deeper investigation).
- recon-ng (modular framework, marketplace of modules).
- SpiderFoot (~200 data sources, web UI + CLI mode).
- dnstwist (typosquat / phishing-domain detection).
- gau, waybackurls (cross-listed with web/).
- gallery-dl (image scraper from social platforms).
- yt-dlp (archival).
- linkedin2username (corp username generation, Kali default).
- gh (GitHub CLI — find leaked data, repo recon).
- exiftool (cross-listed with media/).

**Deliberately omitted:** twint (broken since 2023, no clean
modern equivalent), SocialAnalyzer (overlap with maigret).

**Reach-for guidance:**
- "Find emails for example.com" → `theHarvester -d example.com -b
  all`.
- "Is this username on social platforms?" → `sherlock <user>`
  fast; `maigret <user>` thorough.
- "Investigate a person" → `spiderfoot -s <target>` + triage.
- "Spot phishing domains for example.com" → `dnstwist
  --registered example.com`.
- "Find GitHub forks / secrets" → `gh search repos
  example.com`, then `trufflehog github`.
- "Generate corp usernames from LinkedIn" → `linkedin2username
  -c "Acme Corp"`.

Counts: 12 individual (2 cross-listed).

---

### 8.7 `data/` — parse, query, transform

**Kit:** jq, yq, ripgrep (rg), fd, miller (mlr), qsv, sqlite3,
htmlq.

**Deliberately omitted:** xsv (qsv supersedes), grep / find (use
rg / fd; coreutils still on PATH for fallback), gron / fx (jq
covers).

**No changes from v1.** The single most-mature category.

**Reach-for guidance:** unchanged from v1's `data/` section.

Counts: 8 individual.

---

### 8.8 `crypto/` — encode, hash, sign, encrypt

**Kit:** openssl, gpg, age, sha256sum / sha1sum / md5sum (coreutils,
documented), base64 (coreutils), xxd, jwt-cli.

**Wrapper:** cert_probe (wraps `openssl s_client` with deck-tuned
flags + JSON parse of validity / SANs).

**Deliberately omitted:** hexdump (xxd does both directions —
unchanged from v1).

**No major changes from v1.** Passwords carved out into their own
category (see §8.9).

Counts: 7 individual + 1 wrapper.

---

### 8.9 `passwords/` — hash discovery and wordlist work (NEW, split from crypto)

The verb is different from crypto: crypto is "I have known input,
transform it"; passwords is "I have a hash, discover input."

**Kit:**
- hashcat (GPU-accelerated; CPU fallback documented as slow).
- John the Ripper (john) — CPU-first, format auto-detection.
- hash-identifier / hashid (identify hash type from string).
- CeWL (custom wordlist from a website).
- crunch (pattern-based wordlist generator; use sparingly — masks
  via hashcat are usually better).

**Hardware gating:** hashcat declares `requires.hardware = ["gpu"]`
soft — meaning the manifest still loads on CPU-only decks (the
construct should know hashcat exists), but the manifest's
when_to_use prose includes "if no GPU is present, prefer john for
short / well-targeted attacks; document the slowness in your
result."

**Deliberately omitted:** mentalist (GUI), wordlists themselves
(handled by package install — `seclists`, `rockyou` etc. live on
disk, not in the manifest registry).

**Reach-for guidance:**
- "What hash type is this?" → `hashid '<hash>'`.
- "Crack this hash on CPU" → `john --format=<auto> hash.txt`.
- "Crack with GPU + masks" → `hashcat -m <mode> -a 3 hash.txt
  '?u?l?l?l?l?d?d'`.
- "Build a target-tailored wordlist" → `cewl https://target.com
  -d 3 -m 6 -w wordlist.txt`.

Counts: 5 individual.

---

### 8.10 `media/` — non-text files

**Kit:** ffmpeg, ffprobe, imagemagick (magick / convert), exiftool,
tesseract, pdftotext (poppler-utils), qpdf, pandoc, yt-dlp.

**Wrapper:** pdf_to_text (tries pdftotext first, falls back to
pdftoppm + tesseract for scanned PDFs).

**Deliberately omitted:** pdftk (qpdf covers, no Java dep), sox
(ffmpeg covers).

**v2 additions:** pandoc (document format conversion — common
enough), yt-dlp (cross-listed with osint/).

Counts: 9 individual + 1 wrapper.

---

### 8.11 `system/` — local OS introspection

**Kit:** ps (coreutils), htop, btop, ss (moved from recon/), lsof
(moved from recon/), ip (moved from recon/), du, df (coreutils),
journalctl (systemd), strace, iotop, dust (modern du, tree+bars).

**Wrapper:** top_disk (`du -sh */ | sort -h | tail -20`).

**Deliberately omitted:** top (htop / btop preferred), ltrace
(narrower than strace), procs (ps is universal), duf (df is fine).

**v2 additions:** dust, ss/lsof/ip moved in from recon/.

Counts: 12 individual + 1 wrapper.

---

### 8.12 `wireless/` — RF and radio

**Kit:**
- aircrack-ng suite (group: airmon-ng, airodump-ng, aireplay-ng,
  aircrack-ng).
- kismet (passive 802.11 sniffer).
- hcxtools (hcxdumptool, hcxpcapngtool — modern WPA2/3 capture).
- bettercap (network swiss-army).
- bluetoothctl (replaces hcitool — see report).
- btmgmt (Bluetooth mgmt commands).
- rtl_433 (sub-GHz decoder for RTL-SDR).
- gqrx (SDR receiver UI; desktop only).
- soapy_power (headless SDR power survey).
- reaver (WPS attacks; niche but quick when applicable).

**Group manifest:** `wireless/aircrack_suite` — 4 binaries.

**Hardware gating:** Most tools declare `requires.hardware =
["monitor_mode_wifi"]` or `["sdr_rx"]`. Manifest loads regardless;
the construct sees a "this tool needs hardware X — check the deck
has it before invoking" annotation in its prompt. Failures are
clean (the binary refuses, the construct reports gracefully).

**Deliberately omitted:** hcitool (deprecated upstream by BlueZ),
wifite / fluxion (hide composition; constructs learn worse from
them — see report), Reaver was on the fence; ship in pentester
since 10-second WPS reach time matters when WPS is in scope.

**Form-factor gating:** moves from "opt-in profile category" to
"hardware check at install + invocation time." See §9.

**Reach-for guidance:**
- "Survey wifi networks" → `airodump-ng wlan0mon` or `kismet`
  (richer view).
- "Capture WPA handshakes" → `hcxdumptool -i wlan0mon -o
  cap.pcapng --enable-status=1`. Convert to hashcat input with
  `hcxpcapngtool`.
- "Discover BLE devices" → `bluetoothctl scan on` (durations) /
  `btmgmt find` (one-shot).
- "Decode 433MHz traffic" → `rtl_433 -F json`.
- "Sweep RF spectrum" → `soapy_power -f 88M:108M -O out.csv`.

Counts: 10 individual + 1 group (covering 4 of those) = ~7
manifest-units worth of prompt cost.

---

### 8.13 `assumed/` — what's expected on the deck unconditionally

Not a real category in the manifest sense (no `tool.toml` files);
just a category README at `<home>/tools/assumed/_README.md` that
documents what the deck assumes is on PATH. The deck refuses to
launch without these:

bash, coreutils (ls, cat, sort, uniq, head, tail, sha256sum,
base64, ...), grep, sed, awk, find, curl, ssh, git, python3,
make.

Plus desktop-only assumed: gh (GitHub CLI; cross-listed with
osint/ since it's also a registered tool there for OSINT use).

The README is for the netrunner's reading; it doesn't show in
constructs' prompts.

---

## 9. Form-factor install profiles

Five profiles. The deck's installer reads a profile name and runs
the appropriate package-manager invocations.

| Install   | recon | net | web | ad | cloud | osint | data | crypto | pwd | media | system | wireless |
|-----------|:-----:|:---:|:---:|:--:|:-----:|:-----:|:----:|:------:|:---:|:-----:|:------:|:--------:|
| `minimal` |   —   |  ✓  |  —  | —  |   —   |   —   |  ✓   |   ✓    |  —  |   —   |   ✓    |    —     |
| `desktop` |   ✓   |  ✓  |  ✓  | ✓  |   ✓   |   ✓   |  ✓   |   ✓    |  ✓  |   ✓   |   ✓    |    —     |
| `wearable`|   ✓   |  ✓  |  ✓  | ✓  |   —   |   ✓   |  ✓   |   ✓    |  ✓  |   ◐   |   ✓    |    —     |
| `pentester-base` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `pentester-with-radio` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

`◐` = reduced media kit on wearable (no full ffmpeg encoder set, no
Tesseract language packs by default — saves ~500MB on a Pi-class
disk).

The matrix is **install-time** only. Hot-load semantics cut the
other way: any tool present on disk + on PATH gets a manifest in
the registry and is available to profiles that include it. Wireless
on a desktop without a USB radio: the wireless tools aren't
installed (per matrix), so wireless profiles can't load on that
deck. Wireless on a pentester-with-radio deck without the actual
radio plugged in: tools are installed; manifests load with
`requires_hardware` flagged unsatisfied; profiles that include
wireless tools still load but warn the netrunner.

The `cyberdeck-install` script is a future deliverable. v2 just
specifies the matrix.

---

## 10. Profile templates

Twelve templates ship in `<home>/profiles/`. The two existing
profiles (`default`, `code_reviewer`, `recon_specialist`) get
schema refresh. The rest are new.

For each: name, the **use_when / dont_use_when** capability menu
(daemon-readable), kit composition, and addendum sketches.

---

### 10.1 `default` (existing — schema refresh)

```toml
name = "default"
category = "General"
description = "Baseline general-purpose work, no specialization."

use_when = """
The goal doesn't fit a specialty profile, or the netrunner
explicitly wants unsteered behavior. Catchall.
"""
dont_use_when = """
A specialty profile fits — use that. default has no kit beyond
what every construct gets baseline; specialty profiles unlock
the actual capability surface.
"""

default_daemon_addendum = ""
default_construct_addendum = ""

default_noise_posture = "default"
recommended_tools = []  # full Claude Code toolset
[kit]
include = []  # no kit; bare construct
default_scripts = []
```

---

### 10.2 `code_reviewer` (existing — schema refresh)

Existing addendums kept; `kit.include` populated:

```toml
default_noise_posture = "passive_only"
[kit]
include = ["data/jq", "data/ripgrep", "data/fd", "data/sqlite3"]
```

`use_when`: "The goal is read-only code review against a codebase
or PR. Output is a structured findings doc."
`dont_use_when`: "The goal involves writing or modifying code,
running tests, or pentesting. Read-only review only."

---

### 10.3 `data_analyst` (NEW)

```toml
name = "data_analyst"
category = "Data"
description = "Structured data work: parse, query, transform, summarize."

use_when = """
The goal is to extract, transform, summarize, or query structured
or text data. CSV / JSON / SQL / arbitrary text. Output is data,
not actions.
"""
dont_use_when = """
The goal is web pentesting, code review, or anything that requires
domain-specific tooling. data_analyst is general-purpose data
work; for security data specifically, prefer the appropriate
specialty profile and let it pull in data tools.
"""

default_construct_addendum = """
You are operating in a data context. Extract, transform, and
summarize structured data — never invent it.

Reach for jq for JSON, yq for YAML, miller (mlr) for CSV/TSV
record streams, qsv for big-CSV speed, sqlite3 for joins across
multiple files. Use ripgrep for textual search, fd for file
discovery. Pipe wherever possible; one-liners over multi-step
scripts when the input is small.

Always show the head of the input and the head of the output
before producing the full result. If the data is too large to
summarize, say so and ask before committing.
"""

default_noise_posture = "passive_only"
recommended_tools = ["Bash", "Read", "Write"]
[kit]
include = ["data/jq", "data/yq", "data/ripgrep", "data/fd",
           "data/miller", "data/qsv", "data/sqlite3", "data/htmlq"]
default_scripts = []
```

8 manifests, ~3K tokens. Lean.

---

### 10.4 `osint_researcher` (existing — REBUILT)

v1 named exiftool + whois + dig. v2 uses the real OSINT kit.

```toml
default_noise_posture = "passive_only"
[kit]
include = ["osint/theHarvester", "osint/sherlock", "osint/maigret",
           "osint/spiderfoot", "osint/dnstwist", "osint/gh",
           "media/exiftool", "recon/dig", "recon/whois",
           "data/jq"]
default_scripts = ["pdf_to_text"]
```

`use_when`: "The goal is intelligence gathering from public
sources. Read-only, never probes target."
`dont_use_when`: "The goal involves any active reconnaissance or
exploitation. OSINT is passive only — pivot to recon_specialist
or web_pentester for that."

Addendum (sketch): existing v1 prose plus tool-specific reach-for
guidance (theHarvester for emails, sherlock-then-maigret for usernames,
spiderfoot for deep-dive, dnstwist for phishing domains).

10 manifests.

---

### 10.5 `turbo_researcher` (NEW — netrunner ask)

The non-pentest research profile. Designed to formalize the
"go pull info, structure it, write a report" workflow.

```toml
name = "turbo_researcher"
category = "Research"
description = "Pull info from the web and local sources, structure it, write a report."

use_when = """
The goal is information gathering and report compilation on a
topic — not pentesting, not OSINT-against-a-person, but
researcher-shaped: gather sources, synthesize, produce structured
output. Examples: "research the X vulnerability and write up
attack/defense", "summarize the recent changes in K8s security",
"compile a report on Y product's threat model."
"""
dont_use_when = """
The goal involves active probing, exploitation, or hands-on
testing. turbo_researcher is read-and-write-prose-shaped, not
hands-on. Hand off to a pentest profile when the research
identifies something to actually test.
"""

default_construct_addendum = """
You are operating as a researcher. Your job is to gather
information from authoritative sources, structure it, and produce
a clear written report. You are NOT testing, exploiting, or
acting on the targets you research.

Workflow:
  1. Identify authoritative sources for the topic. Prefer primary
     sources (project docs, official advisories, vendor pages,
     RFCs) over secondary (blog posts, summaries). Note source
     dates — the field moves fast and 2-year-old advice may be
     stale.
  2. Use WebSearch to find sources, WebFetch to read them in
     full. Use jq + htmlq + ripgrep to extract specific pieces
     from fetched pages. Save retrieved sources locally
     (`<topic>-sources/<n>.html` or `.md`) so you have them
     after fetching, and so the netrunner can audit your inputs.
  3. Cite every claim. Format: `[Source N: <title>](<url>)`.
     If you can't cite, mark the claim as `[unsourced — confirm
     before using]`.
  4. Structure the report: Executive summary → Key findings →
     Per-source notes → Open questions → Sources. Use the
     `report_compile` script to produce the final shape.
  5. If the topic exceeds your context, hand off via HANDOFF
     block: name what's been covered, what's left, recommended
     profile (usually still turbo_researcher for continuation).

Pacing: research depth grows by source count, not by re-reading
the same source. Diminishing returns kick in around 8-10
high-quality sources for most topics.
"""

default_noise_posture = "passive_only"
recommended_tools = ["Bash", "Read", "Write", "WebSearch", "WebFetch"]
[kit]
include = ["data/jq", "data/htmlq", "data/ripgrep", "media/pandoc",
           "net/curl"]
default_scripts = ["report_compile"]
```

5 manifests + 1 wrapper. Lean by design — turbo_researcher's tool
needs are narrow.

`report_compile` wrapper: takes a directory of source notes +
metadata YAML and emits a structured Markdown report with TOC,
citations, and an Executive Summary stub. Saves the construct
re-deriving the report skeleton every invocation.

---

### 10.6 `recon_specialist` (existing — refresh)

```toml
default_noise_posture = "quiet"
[kit]
include = ["recon/projectdiscovery_recon", "recon/nmap",
           "recon/arp-scan", "recon/dig", "recon/whois",
           "recon/mtr", "recon/tcpdump", "recon/tshark"]
default_scripts = ["scan_subnet", "scan_wifi"]
```

`use_when`: "The goal is observational network/host reconnaissance —
characterize what exists without touching it more than necessary."
`dont_use_when`: "The goal involves exploitation or active attack —
hand off to web_pentester / ad_operator after recon establishes
the surface."

Existing addendums kept; tool-specific reach-for guidance lifts
in from §8.1.

8 manifests (1 group covers 3) = ~6 manifest-units of cost.

---

### 10.7 `web_pentester` (NEW)

```toml
name = "web_pentester"
category = "Pentest"
description = "Authorized web app and web infrastructure pentesting."

use_when = """
Authorized engagement against a web target — URL, domain, web
app, or web API. The work is "characterize and attack web attack
surface."
"""
dont_use_when = """
The target is internal-network / Active Directory (use
ad_operator), cloud control plane (cloud_auditor), thick client
or non-HTTP protocol (general pentester or net/), exploit dev
on a binary (exploit_dev). Web-shaped only.
"""

default_construct_addendum = """
You operate against authorized web targets. ALWAYS confirm scope
in writing before scanning — out-of-scope assets are a hard stop.

Reach for projectdiscovery_suite (subfinder → dnsx → httpx →
nuclei) for breadth-first surface mapping. Use ffuf for parameter
/ header / POST fuzzing; feroxbuster for recursive directory
walks. sqlmap for SQLi (purpose-built; don't try to roll-your-own
with curl). mitmproxy / mitmdump when you need to observe or
modify live traffic.

Rate-limit nuclei (-rate-limit 30) against production targets
unless the engagement explicitly allows higher. Document each
finding with: vulnerability, evidence, reproduction steps,
impact, recommendation. Burp Suite is GUI and not in your kit;
if a finding is best-explored manually, recommend "open this in
Burp" in your output rather than trying to drive it from the CLI.
"""

default_noise_posture = "default"
recommended_tools = ["Bash", "Read", "Write", "WebSearch", "WebFetch"]
[kit]
include = ["web/projectdiscovery_suite", "web/ffuf", "web/feroxbuster",
           "web/sqlmap", "web/secrets", "web/gau", "web/mitmproxy",
           "data/jq"]
default_scripts = []
```

8 manifest-units (group counts as ~1.4). Within the 10-15 specialty
target.

---

### 10.8 `ad_operator` (NEW)

```toml
name = "ad_operator"
category = "Pentest"
description = "Authorized internal-network and Active Directory operations."

use_when = """
Authorized engagement involving internal network, Active Directory,
or Windows enterprise environment. SMB / LDAP / Kerberos / WinRM /
RDP / MSSQL targets.
"""
dont_use_when = """
The target is external web (web_pentester), cloud control plane
(cloud_auditor), or a single-host non-AD context. ad_operator
expects domain-shaped attack surface.
"""

default_construct_addendum = """
Authorized internal-network engagement. Confirm scope and rules
of engagement before any active action.

NetExec (nxc) is the swiss-army; reach for it first for protocol
enumeration, password spraying, lateral movement, BloodHound
ingestion. Pair Responder + ntlmrelayx + coercer for relay
attacks. Certipy-AD for ADCS audit. Kerbrute for Kerberos pre-auth
enum / spray.

Always document the path: initial access → enumeration →
BloodHound graph → privilege escalation → DA. Mimikatz runs on
the target — recommend its use in your output but you cannot
invoke it deck-side. Avoid noisy actions during business hours
unless scope says otherwise.

Output structure: per-host findings table with creds discovered,
shares accessed, escalation paths, recommended next steps.
"""

default_noise_posture = "default"
recommended_tools = ["Bash", "Read", "Write"]
[kit]
include = ["ad/netexec", "ad/impacket", "ad/responder", "ad/mitm6",
           "ad/bloodhound", "ad/certipy-ad", "ad/kerbrute",
           "ad/coercer", "ad/evil-winrm", "ad/enum4linux-ng",
           "passwords/john"]
default_scripts = []
```

11 manifest-units (impacket group ≈ 1.4). Within the 10-15 target.

---

### 10.9 `cloud_auditor` (NEW)

```toml
name = "cloud_auditor"
category = "Pentest"
description = "Cloud and container security review."

use_when = """
Goal is reviewing or testing a cloud environment (AWS / Azure /
GCP) or container/k8s posture. Default posture: read-only audit;
active actions only with explicit scope.
"""
dont_use_when = """
Goal is web app pentest of a cloud-hosted app (use web_pentester),
on-prem AD (ad_operator), or non-cloud infrastructure.
"""

default_construct_addendum = """
Cloud-environment review. Read-only by default — Pacu and any
write-capable IAM operation only with explicit scope confirmation.

Prowler for CIS audit, ScoutSuite for posture review, CloudFox
for attack-path enumeration. Trivy for container/IaC scanning.
Kubescape for k8s posture; kube-bench for CIS-Kubernetes
specifically.

Output findings as: account-id + service + finding-ref + severity
+ remediation. Never output raw tool output as the deliverable —
synthesize. Cloud findings are noisy; the value is the curation.
"""

default_noise_posture = "quiet"
recommended_tools = ["Bash", "Read", "Write"]
[kit]
include = ["cloud/prowler", "cloud/scoutsuite", "cloud/cloudfox",
           "cloud/pacu", "cloud/trivy", "cloud/kubescape",
           "web/secrets", "data/jq"]
default_scripts = []
```

8 manifest-units. Lean for a specialty profile.

---

### 10.10 `bug_hunter` (NEW)

Lighter than `web_pentester` — bug bounty workflow shape.

```toml
name = "bug_hunter"
category = "Pentest"
description = "Bug bounty hunting on authorized programs."

use_when = """
Bug bounty work against a public program. Scope is the program's
policy page; rate-limits are typically tighter than commercial
pentest.
"""
dont_use_when = """
Commercial pentest engagement (web_pentester / ad_operator /
cloud_auditor — broader scope and authority). bug_hunter is
shaped around bounty-program constraints.
"""

default_construct_addendum = """
Bug bounty engagement. Strict scope adherence — the program's
policy page is law. Prefer passive recon (subfinder, gau,
waybackurls) before active. Use nuclei templates that match the
program's tech stack (don't run "all templates" — noisy + low
signal).

Document each finding with reproduction steps, impact, and CVSS.
Never DoS, never social-engineer, never test out-of-scope assets.
Triage findings by program payout tiers; high-impact rare bugs
beat 20 info-disclosure dupes.

If a finding needs deeper exploitation than your kit covers
(e.g., binary exploit primitive), HANDOFF to exploit_dev with
the artifact saved.
"""

default_noise_posture = "quiet"
recommended_tools = ["Bash", "Read", "Write", "WebSearch", "WebFetch"]
[kit]
include = ["web/projectdiscovery_suite", "web/ffuf", "web/sqlmap",
           "web/gau", "web/secrets", "web/gowitness",
           "osint/dnstwist", "data/jq"]
default_scripts = []
```

8 manifest-units.

---

### 10.11 `dfir_responder` (NEW, optional)

Defensive / forensics shape. Ships only if `--profile pentester-base`
or higher, or by netrunner request — DFIR isn't every netrunner's
work.

```toml
name = "dfir_responder"
category = "Defense"
description = "Digital forensics and incident response."

use_when = """
The goal is analyzing artifacts (logs, memory, disk images, file
metadata) from a security incident or threat hunt. Defensive
posture; you analyze evidence, you don't attack.
"""
dont_use_when = """
The goal is offensive (any pentest profile). dfir_responder works
on copies of evidence; it does not interact with live attacker
infrastructure.
"""

default_construct_addendum = """
DFIR / threat-hunting context. You analyze artifacts, you do
not interact with live attacker infrastructure or attempt to
contain anything in real time.

Hayabusa + Chainsaw for Windows event logs (both support Sigma
rules; Hayabusa is faster, Chainsaw has better summary output).
Volatility3 for memory analysis. Plaso for timeline. exiftool
for file metadata. tshark for pcap analysis. YARA for pattern
matching across files / memory. SleuthKit (fls / icat) for
filesystem forensics.

Always preserve original artifact integrity — work on copies.
Document chain of custody: artifact path, hash before, hash
after, analysis steps. Findings format: timeline of events with
artifact references.
"""

default_noise_posture = "passive_only"
recommended_tools = ["Bash", "Read", "Write"]
[kit]
include = ["dfir/hayabusa", "dfir/chainsaw", "dfir/volatility3",
           "dfir/plaso", "dfir/yara", "dfir/sleuthkit",
           "media/exiftool", "recon/tshark", "data/jq",
           "data/sqlite3"]
default_scripts = []
```

Note: a `dfir/` sub-tree under `<home>/tools/` is implied by the
above. v2 adds a 13th category quietly when this profile lands;
the matrix in §9 should add a `dfir` column. Listed here for
future-readiness; the 12 categories called out in §1 are the
default kit, dfir/ is a future addition.

10 manifests.

---

### 10.12 `exploit_dev` (NEW, addendum-only by default)

The audience is small enough that prompt-bloating every spawn
with exploit-dev manifests is wrong. Default: exploit_dev is an
addendum-only profile — it names the tools without shipping their
manifests. Constructs working in this niche read help on demand.

```toml
name = "exploit_dev"
category = "Specialist"
description = "Binary analysis and exploit development."

use_when = """
Goal involves binary analysis, reverse engineering, or exploit
development on native code. CTF challenge, vuln research,
exploit weaponization.
"""
dont_use_when = """
Web app, network, AD, or cloud work — exploit_dev is binary-
shaped only.
"""

default_construct_addendum = """
Binary analysis / exploit development.

Available tools (not in your kit by default — invoke directly,
read --help on demand):
  - checksec: binary protection inspector. ALWAYS START HERE.
  - radare2 / r2: CLI reverse engineering framework.
  - gdb + gef: dynamic analysis. gef is the gdb plugin to use.
  - pwntools: Python exploitation framework.
  - ROPgadget: gadget finder.
  - Ghidra: GUI RE; CLI via `analyzeHeadless`. Big install.

Workflow: checksec → static analysis (r2 or Ghidra headless) →
dynamic confirmation (gdb+gef) → primitive identification → exploit
scaffold (pwntools) → reliability hardening.

Document each step: target binary, mitigations present, attack
primitive identified, exploitation technique, exploit reliability.
"""

default_noise_posture = "default"
recommended_tools = ["Bash", "Read", "Write"]
[kit]
include = []   # addendum-only; tools invoked directly
default_scripts = []
```

0 manifests in kit; the addendum names the tools. Construct learns
"checksec is a thing" from the addendum, runs `checksec --help`
when it needs the surface. Trade-off: no manifest-quality
when_to_use prose; the addendum has to carry the same weight in
fewer tokens. Acceptable for a low-frequency niche.

---

### 10.13 `pentester` (NEW, generalist)

The "I want a competent pentester for arbitrary work" profile.
Hardest to keep lean.

```toml
name = "pentester"
category = "Pentest"
description = "Generalist authorized offensive security work."

use_when = """
Authorized engagement that doesn't fit one of the specialty
profiles cleanly, OR a goal that genuinely spans web + AD + cloud
in a single construct (rare — usually better to chain specialty
profiles via HANDOFF). Use as a fallback when the specialty
catalog doesn't cleanly fit.
"""
dont_use_when = """
A specialty profile fits — use that. pentester is broad-but-
shallow; specialty profiles are deep where they apply.
"""

default_construct_addendum = """
Generalist offensive operator. Confirm authorized scope before
acting.

Your kit is broad. For web work: projectdiscovery_suite, ffuf,
sqlmap. For AD: netexec, impacket. For cloud: prowler, trivy.
For recon: nmap, naabu. For data analysis of findings: jq, rg.

If the goal is clearly specialty-shaped, prefer to hand off to a
specialty profile rather than do it all yourself — the deeper
addendums will produce better output. HANDOFF when you identify
that the next step is in someone else's lane.
"""

default_noise_posture = "loud_ok"
recommended_tools = ["Bash", "Read", "Write", "WebSearch", "WebFetch"]
[kit]
include = ["recon/projectdiscovery_recon", "recon/nmap",
           "web/projectdiscovery_suite", "web/ffuf", "web/sqlmap",
           "ad/netexec", "ad/impacket",
           "cloud/prowler", "cloud/trivy",
           "data/jq", "data/ripgrep",
           "passwords/john"]
default_scripts = []
```

12 manifest-units (3 groups × ~1.4). At the upper end of the
specialty target — within the 15-20 generalist cap. The addendum
explicitly tells the construct to prefer hand-off over
do-it-all.

---

## 11. Wire-shape changes in the deck

Seven changes the implementation passes will need. None of them are
this doc's job to ship; this doc captures them so the implementation
is unambiguous.

### 11.1 Profile schema

Add fields:
- `use_when: str` (capability menu prose, daemon-readable)
- `dont_use_when: str` (capability menu prose, daemon-readable)
- `default_noise_posture: str` (one of `passive_only` / `quiet` /
  `default` / `loud_ok`; the kit's noise ceiling at default — the
  stealth-mode toggle can clamp it lower)
- `[kit]` table:
  - `include: list[str]` (manifest / group identifiers, e.g.
    `"web/nuclei"` or `"web/projectdiscovery_suite"`)
  - `include_tags: list[str]` (verb_tags / domain_tags for
    tag-based composition)

Existing fields unchanged: `name`, `category`, `description`,
`default_daemon_addendum`, `default_construct_addendum`,
`recommended_tools`, `default_scripts`.

### 11.2 Tool manifest schema

New file shape: `<home>/tools/<cat>/<name>/tool.toml` per §3.2.
Group manifests: `<home>/tools/<cat>/<group>.toml` (no folder, sits
at category root).

Schema in §3.2 (full) and §3.3 (group). New noise fields:
`noise_floor: str` (one of `silent` / `quiet` / `medium` / `loud`
/ `klaxon`) at the manifest level; per-arg `noise_modifier: str`
prose annotation on individual args that meaningfully change
tempo. Distinguishing field for groups:
`manifest_kind = "tool"` (default) vs `manifest_kind = "group"`.

### 11.3 Spawn action shape

```json
{
  "type": "spawn",
  "profile": "<profile_name>",
  "task": "<self-contained task>",
  "caliber": {...}        // optional, future, per model+effort design
}
```

`profile` is required. The deck rejects spawns with missing /
unknown profiles by surfacing an error in the next outcome turn.

### 11.4 describe_kit action

```json
{
  "type": "describe_kit",
  "scope": "all" | "<category>" | "<tool_name>" | "<group_name>"
}
```

Deck handles by injecting a `KIT:` block in the next turn's
input message containing the requested manifest text. Logged for
abuse-watching.

### 11.5 Daemon prompt additions

- Section: *Profile pipeline planning* — text per §5.3.
- Section: *Profile catalog reading* — teach the daemon to read
  use_when / dont_use_when as the primary selection mechanism;
  recommended_tools and kit summaries as secondary.
- Section: *Hand-off recognition* — teach the daemon to parse
  the HANDOFF block in outcomes per §6.1.
- Section: *describe_kit usage* — teach the daemon when the
  escape hatch is appropriate (planning, not execution; not
  speculative; one-shot per scope per turn).
- Section: *Operational tempo (noise)* — text per §7.6. Daemon
  reads current stealth setting from its turn-prompt context and
  prefers quieter profiles when ops permits.

### 11.6 Construct prompt additions

- Section: *Tools available* — kit composition pasted in as the
  "you have these tools" block. This is the hot-load substance.
- Section: *Hand-off when stuck* — teach the construct the
  HANDOFF block format per §6.1, and the three triggers per §6.2.
  Explicitly de-emphasize hand-off as default exit.
- Section: *Noise posture* — the construct's profile's
  `default_noise_posture` plus the current deck stealth setting
  yield the effective ceiling. The construct's addendum tells it
  to honor the ceiling — pick quiet flags, single-template /
  small-wordlist invocations, low rates — when the ceiling is
  below `loud`.

### 11.7 Brake hook noise axis

The brake hook (deck-global, netrunner-controlled, deterministic)
gets a new gating axis: noise. At the moment a construct invokes
a tool, the hook computes:
- the tool's effective noise level from `noise_floor` plus any
  `noise_modifier`-mentioned flags the construct passed;
- the effective ceiling from
  `min(profile.default_noise_posture, deck.stealth_clamp)`;
- if effective_noise > effective_ceiling, the call is refused
  with a structured error message ("stealth mode N prohibits
  this tool at <level>; consider <quieter alternative>").

The refusal flows back through Claude Code's tool_result channel
exactly like any other brake refusal. The construct sees the
error, adapts; the daemon sees it via OUTCOMES.

### 11.8 Stealth-mode global toggle

Cousin to `p` (plugin cutoff) per §7.4. Four ordinal settings
(`off` / `discreet` / `quiet` / `silent`). Lives in deck-global
state alongside the brake state and the plugin-airgap state.
Status indicator in the bar. Modal to change (similar to the
limits modal). Netrunner-only — daemon cannot change.

The toggle drives the brake hook (§11.7) and is part of the
daemon's turn-prompt context so planning stays consistent with
enforcement.

---

## 12. What this doc deliberately leaves for later

- **The actual `cyberdeck-install` script.** Reads the form-factor
  matrix, runs the right `apt` / `brew` / `pacman` commands.
- **Per-tool manifest authoring.** ~80 manifests across 12
  categories. Each is a small file; total work is the sum, not
  conceptually hard. Best done category-by-category.
- **Manifest validator.** A small `cyberdeck tools validate`
  command — every manifest parses, every binary on PATH (or
  flagged), every `requires.hardware` known to the deck.
- **Tools panel rendering.** UI work. Hierarchical browser per
  spec §"Tools panel"; library status footer per §3.4.
- **Daemon prompt rewrite.** Section additions per §11.5 require
  a careful prompt-engineering pass with the existing daemon
  prompt as base.
- **Construct prompt rewrite.** Section additions per §11.6.
- **describe_kit implementation.** New action type; deck-side
  parser; output injection into next turn's message.
- **Hand-off block parser.** Regex on construct final output;
  validation against the recommended_profile field; error
  surfacing on malformed blocks.
- **Tag-based kit composition.** `kit.include_tags` is in the
  schema but the resolver implementation is non-trivial; v2.x
  refines.
- **The dfir/ category and tools.** Listed in §10.11; not in the
  v2 default category list. Adds when the profile lands.
- **MCP servers as tools.** Out of scope; the registry's plugins
  leg covers MCP. v3 conversation about whether tools should
  ever surface as MCP rather than Bash.
- **AI-augmented tool integration.** PentestGPT / Pentagi / XBOW
  / garak / pyrit. Documented as "competitor / sibling" in
  design space; not in the kit. Revisit when the field shakes
  out.
- **Stealth-mode keybind.** §7.4 defines the toggle but doesn't
  pick a key. `s` is taken (status / search variants?); the
  keymap revision pass should pick a free letter that visually
  matches the existing emergency-controls cluster (brake / `p`
  plugin cutoff).
- **Per-tool noise classification.** §7 specifies the scale; the
  per-tool floors and per-arg modifiers come with manifest
  authoring (Slice 8). No need to author them all up front; the
  noise field defaults to `loud` for any unclassified tool, which
  is conservative — stealth mode would block unclassified tools
  by default, which is the right safe-by-default behavior.

---

## 13. Open calls for the netrunner

Ten places where v2 made an opinionated call worth flagging.

1. **Burp Suite stance: addendum-only, not in kit.** The deck is
   CLI-first and Burp is GUI-first. web_pentester's addendum
   names Burp so the construct knows when to recommend manual
   triage. If the netrunner uses Burp Pro and wants its REST API
   exposed as a tool, that's a v2.x add — not default.

2. **`dev/` still dropped.** Git, make, language toolchains in
   `assumed/`. If you want `gh`, `git-extras`, etc. in the panel,
   v2 ships `gh` as a top-level tool cross-listed with `osint/`.
   No `dev/` category.

3. **Wireless gating moved from profile-opt-in to form-factor /
   hardware-check.** v1 had wireless behind a `pentester` install
   profile. v2 has it on `pentester-with-radio` only at install,
   plus per-tool `requires_hardware` declarations at runtime. If
   you'd rather it ship in `desktop` install too (inert without
   hardware), one matrix flip.

4. **The 12-category number.** Could be 10 (fold passwords into
   crypto, fold osint into recon) or 14 (split exploit/, mobile/
   out). v2's call: 12 is the right granularity for a Tools
   panel that fits on screen and a profile catalog the daemon
   can hold in mind.

5. **Group manifests as the prompt-economy lever.** v2 leans hard
   on these (ProjectDiscovery, Impacket, aircrack). If the
   per-binary fidelity loss bites in practice, profiles can
   include individual binaries instead. Easy escape hatch.

6. **`exploit_dev` as addendum-only profile.** No kit. Saves
   prompt budget but gives constructs a thinner runway. If
   exploit-dev work becomes common enough that the addendum
   isn't carrying enough weight, ship the manifests.

7. **`turbo_researcher` is a non-pentest profile in a security-
   shaped kit.** It's in the catalog because the netrunner asked
   for it, and because "research X and write a report" is a real
   workflow even when the rest of the deck is offensive. No
   objection here, just flagging the genre-mix.

8. **Hand-off block fences (`---HANDOFF`).** The fences are
   markdown horizontal rules; some constructs may emit those for
   visual reasons unrelated to hand-off. Risk: false-positive
   parses. Mitigation: the parser requires the literal `HANDOFF`
   token on the line after the opening `---`. Open call: is that
   strict enough? Alternative: a more distinctive fence like
   `<<HANDOFF>>` / `<</HANDOFF>>`. v2 picks the markdown form
   for visual cleanliness but flagging.

9. **Default-noise behavior for unclassified tools.** §7 says
   unclassified tools default to `noise_floor = "loud"` — the
   conservative choice because it means stealth mode will refuse
   tools we haven't yet labeled, fail-safe rather than fail-open.
   Trade-off: every new tool added to the kit needs an explicit
   noise classification or it's blocked under stealth. Could
   default to `medium` instead (less paranoid) but I think the
   safer call is right.

10. **Stealth setting persistence across deck restarts.** The
    brake state persists; the plugin-airgap state also persists.
    Stealth probably should too — netrunner who flipped to
    `silent` for an OSINT-only engagement doesn't want it to
    silently revert to `off` after a restart. v2 says: persist
    via the same mechanism as brake/plugin state. Open question:
    should it be per-engagement (reset on goal completion) or
    truly durable (reset only when netrunner explicitly toggles)?
    Lean: durable. Engagement-level scoping is what the brake
    profile field is for, not stealth.

---

## 14. Implementation slicing (suggestion)

The work this doc implies is large. A suggested slicing for a
build-plan entry:

1. **Slice 1: schema.** Profile schema additions (use_when /
   dont_use_when / default_noise_posture / kit). Tool manifest
   schema (incl. noise_floor + per-arg noise_modifier). Group
   manifest schema. The deck loads them but nothing yet consumes
   them.
2. **Slice 2: load + validate.** Deck-side registry that scans
   `<home>/tools/`, parses manifests, checks `requires`, holds in
   memory. `cyberdeck tools validate` command.
3. **Slice 3: spawn.profile.** Wire the new spawn action shape.
   Default daemon catalog includes profile names + descriptions
   only. Constructs spawn under their declared profile.
4. **Slice 4: kit injection.** Profile's `kit.include` resolves
   to manifests; manifests' prompt-projections paste into the
   construct's system prompt at spawn time. The hot-load goes
   live.
5. **Slice 5: describe_kit.** Daemon-side action handler; KIT:
   block injection.
6. **Slice 6: HANDOFF.** Construct prompt teaches the block;
   daemon prompt teaches recognition; deck-side parser routes
   the recommended_profile + state to a successor spawn.
7. **Slice 7: stealth mode.** Deck-global state, status indicator,
   modal toggle, persistence. Brake-hook noise axis (§11.7) reads
   the clamp and refuses tools above the effective ceiling.
   Daemon's turn-prompt context picks up the current setting.
   Cousin-to-`p` shape; share the emergency-controls plumbing.
8. **Slice 8: install + cyberdeck-install script.** Form-factor
   matrix consumes per-platform package commands.
9. **Slice 9: per-tool manifest authoring.** Category-by-category.
   ~80 manifests, each carrying noise classification. The longest
   slice in wall-time but the most parallelizable.
10. **Slice 10: profile templates.** 10 new templates (3 existing
    refresh + 7 new + 2 optional). Each is a small TOML file.
11. **Slice 11: panel rendering.** UI for hierarchical browse +
    library status. Noise indicators in the Tools panel (per-tool
    floor surfaced as a glyph or color).

Slices 1–4 unlock the rest. Slice 7 (stealth) can land independently
once Slice 1's manifest schema is in place. Slice 9 is the bulk of
the perceived work and can run in parallel with everything else
once 1–4 are in.

---

## End of v2.

Total length: ~1100 lines. Aim was thorough enough to author every
implementation slice from this doc alone; opinionated enough that
each call is defended; flagged enough that places I'm uncertain are
visible.

Iterate freely. The shape of the doc itself is also negotiable —
if a section is too long or too thin, pull it apart.
