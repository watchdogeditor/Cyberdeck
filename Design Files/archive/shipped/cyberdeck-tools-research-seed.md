# Cyberdeck — Default Tools Research

> **STATUS: ARCHIVED 2026-05-07.** This seed prompted the research chat
> that produced `cyberdeck-tools-research-report.md` (also archived) and
> ultimately the v2 design at `cyberdeck-tools-default-kit.md` (in-flight).
> Kept for provenance only — the live forward-looking design is the v2 kit.

---

*Seed for a future chat dedicated to deciding what tools ship with a fresh cyberdeck install.*

---

## What this chat is for

I want to figure out a sensible default toolset for the cyberdeck — the binaries and small utilities that should be installed and ready in `<home>/tools/` on a fresh deck, before the netrunner has authored anything custom. Constructs invoke these via Bash. The goal is "best multi-tools" — versatile, well-known, scriptable, useful across many situations.

Examples of what I'm thinking: nmap, netcat, jq, ripgrep, fd, curl, dig, tcpdump, wireshark/tshark, sqlite3, base64. The kind of swiss-army utilities that pay for themselves a hundred times over.

---

## Background — what the cyberdeck is

A multi-paned terminal app for orchestrating Claude Code instances ("constructs") to do work. A coordinator AI ("daemon") decomposes a high-level goal into subtasks and dispatches constructs to do them. The netrunner (human operator) supervises through a Textual TUI.

Constructs are real Claude Code subprocesses with permissions to use Bash, Read, Write, WebSearch, etc. They run with `--permission-mode bypassPermissions` (or stricter, configurable per-profile via brake tiers: paranoid / default / yolo). They can read files, run shell commands, search the web — anything Claude Code can do.

**The aesthetic and intent is cyberpunk personal-computing.** Think: a hacker's toolkit. The deck is meant to be *capable* — it's not a sandbox, it's a workshop. The netrunner trusts themselves and their constructs to do real work on a real machine. Brake tiers narrow tool permissions per-profile when a netrunner wants tighter control (e.g. a "code-reviewer" profile that can only Read and WebSearch), but the baseline is full capability.

**Targets multiple form factors:**
- Desktop deck (full Linux/Mac/Windows, lots of resources)
- Pi-class wearable deck (RK3588 or similar SBC, possibly battery-powered, possibly off-grid)
- Future hardware: Alfa-class secondary radio for wireless work, cellular failover, etc.

**Connection-aware:** the deck has Online / Degraded / Offline states. Tools that don't need network should work offline. Tools that need network should fail gracefully when degraded.

## Tools-related architecture concepts to keep in mind

- **Scripts**: standalone executables in `<home>/tools/<category>/<filename>`. The deck scans this dir and surfaces them in the Tools panel. Constructs invoke them via Bash. Each script has a small declarative manifest (name, category, args, expected output shape) — though this manifest is a planned feature, not yet implemented.
- **Profiles**: TOML files in `<home>/profiles/`. Each profile defines `name`, `category`, `description`, `system_prompt_addendum`, `allowed_tools`, `brake_profile`. Profiles narrow what a construct can do. A `recon_specialist` profile might have `allowed_tools = ["Bash", "Read", "WebSearch"]` and a system prompt addendum saying "you are operating in a recon context, prefer X over Y, never Z."
- **Plugins** (deferred): the planned third leg of the tool registry — for hardware (camera, IR, NFC) and external services (MCP servers). NOT what we're discussing here. We're focused on shell tools that constructs invoke via Bash.

So: the question is what to include in `<home>/tools/` by default, organized into categories.

## What I want to brainstorm and decide

1. **Categorization.** What's a good top-level category structure? Existing examples in the spec mention `recon/`, but that's it. I'm imagining: `recon/`, `data/`, `web/`, `crypto/`, `system/`, `dev/` — but I'm open to anything. I want a structure that ages well as more tools get added.

2. **The actual tool list per category.** I want this to be opinionated. Not "everything you might want," but "the carefully-chosen set that pays for itself across the most situations." I'd rather have ten excellent tools than fifty mediocre ones. Lean on what's well-known, well-documented, and frequently scriptable.

3. **Install model.** Some tools are everywhere (curl, jq, dig). Others are in package repos but not always installed (nmap, ripgrep, tshark). Others are language-specific (Python `requests`, Node `axios`). I want guidance on what to assume always-installed vs. document as "install this if you want this category."

4. **Wrapper scripts vs. raw binaries.** Should `<home>/tools/data/jq` be a script that wraps `jq` with deck-specific defaults (e.g. always pretty-print, always read from a deck-conventional location), or just a symlink/passthrough? Wrappers are nice because they document usage; passthroughs are nice because they don't add a maintenance surface.

5. **Manifest design.** Each script needs a manifest (name, category, args, expected output shape). What's a good schema for this? TOML preferred (matches profiles). Should manifests be a separate file alongside the script, or YAML frontmatter inside the script?

6. **Per-form-factor differences.** What changes for the wearable deck (limited storage, ARM-only, no display server)? Some tools (wireshark GUI) don't apply. Some (tshark, tcpdump) do. Should we have install profiles like "minimal", "desktop", "wearable", "pentester"?

7. **Form factor: when to use each tool.** I want each category's writeup to include not just "what's in it" but "when a construct should reach for X vs Y vs Z." This becomes documentation that gets fed back into deck-side profile system prompts so constructs make smart choices.

## What I'd like as output

A document I can drop into the project knowledge that becomes the basis for:
- A first-cut `<home>/tools/` structure
- A list of recommended apt/brew/pacman install commands per platform
- Per-script manifests for the chosen tools
- Profile templates that reference these tools (e.g. a `recon_specialist` profile with appropriate `allowed_tools` and a system prompt addendum that namedrops the right tools for the right situations)

## Some opinions I already have

- **Posture: capable, not sandboxed.** This is a hacker's deck. The netrunner can manage their own machine. The brake profiles handle "this specific construct is restricted"; the default install assumes the netrunner wants power.
- **Bias toward CLI-first, scripts over GUIs.** Constructs invoke things via Bash; tools that don't have good CLI surfaces are second-class.
- **Bias toward textual output that pipes well.** jq, ripgrep, awk, sed — the unix philosophy is the deck philosophy.
- **Avoid bloat.** Every tool that ships by default is a tool the netrunner needs to ignore if they don't want it. Better to ship lean and document optional categories.
- **Profile-aware.** A `recon_specialist` profile should know about `nmap` and `tcpdump`; a `code_reviewer` profile shouldn't. The default install for the *machine* may include all categories, but profiles narrow what each construct knows about.
- **No "convenience" wrappers that hide the real tool.** If a script `<home>/tools/recon/scan.sh` is just `nmap "$@"`, that's a footgun — the construct learns "scan.sh" instead of "nmap" and now has worse Stack Overflow recall. Wrappers should add real value (sensible defaults + format normalization) or not exist.

## Output style

Pragmatic, opinionated, well-organized. I'd rather you make calls and explain them than enumerate every option without a recommendation. If you think a tool I named is wrong, say so. If you think a category I omitted matters, add it. The goal is a usable starting point I can ship and iterate on, not an exhaustive survey.

When you're done, the document should be self-contained enough that a future chat about implementing any one piece (writing manifests, packaging install scripts, wiring profiles) can start from the document without re-deriving context.
