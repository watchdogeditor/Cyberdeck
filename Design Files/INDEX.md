# Design Files — inventory

*Inventory only. This is not a navigation aid — it's a complete list.
Forward-looking work is tracked in `cyberdeck-build-plan.md`, where each
slice points at its own design doc(s). Use the build plan for "what
should I work on"; use this for "what files exist and what are they."*

*Last reorganized 2026-05-07.*

---

## Canon (always-loadable; bring up-to-speed reading order)

| File | Role | Update cadence |
|---|---|---|
| `cyberdeck-claude-code-orientation.md` | **Read-first.** Orientation for a fresh session — what the codebase is, hard rules, file-by-file orientation, workflow patterns. | Update on any architectural shift |
| `cyberdeck-state.md` | Current state of record. Filed gotchas (cumulative, sacred), key design decisions, tech debt, what's shipped. | Update on every meaningful landing |
| `cyberdeck-build-plan.md` | Forward-looking plan tree. Shipped → current frontier → near → mid → long → non-goals. Each line item points at its design doc. | Update when slices land or new ones file |
| `cyberdeck-spec.md` | Canonical architecture (the *what*). | Update on architectural shift |
| `cyberdeck-philosophy.md` | Convictions that resolve ambiguity (the *why*). | Rare — only when convictions are revised |

## Operational reference

| File | Role |
|---|---|
| `cyberdeck-project-instructions.md` | Collaboration norms — how a session works, what the netrunner expects, escalation pattern |
| `cyberdeck-platform-portability.md` | Living inventory of every Windows-specific code path with Linux/Pi porting notes. Update whenever new platform-specific code lands |

## In-flight design (`in-flight/`)

Active designs. Slices in `cyberdeck-build-plan.md` reference these by name. Update as implementation reveals new constraints; trim shipped phases out as they land (move shipped-phase recap to `archive/shipped/<slice>-history.md`).

| File | Status | Implementation slot |
|---|---|---|
| `cyberdeck-per-run-workspaces-design.md` | Filed 2026-05-07; v5 final form shipped 2026-05-08 (pending real-deck verification). Move to archive/shipped/ once verified — iteration history is the value | build-plan SHIPPED "Per-run workspace compartmentalization" |
| `cyberdeck-spawn-context-isolation.md` | Phase 1 shipped 2026-05-05; Phase 2 conditional on regression | build-plan item 000 |
| `cyberdeck-maintbot-design.md` | v0/v0.5/v1/v1.5/v1.6/v2 shipped; v3 deferred indefinitely | build-plan items 0d/0e/0g/0h |
| `cyberdeck-model-effort-design.md` | Phases 1-3, 5 shipped 2026-05-04; Phase 4 blocked on quota signal (item 13) | "caliber" cluster |
| `cyberdeck-keymap-revision.md` | ON HOLD since 2026-04-27. Layer 1 inventory done; Layers 2-3 blank. Blocks new global keybinds | build-plan keymap revision |
| `cyberdeck-prompt-shaping-design.md` | Filed 2026-05-07; no code yet. Coordinate with spawn-context-isolation Phase 2 | build-plan "Prompt-shaping pass" |
| `cyberdeck-collections-intake-design.md` | Filed 2026-05-06; queued behind prompt-shaping pass + Mechanic v2 | build-plan collections intake |
| `cyberdeck-tools-default-kit.md` | v2 design 2026-04-30; downstream of retool; no code yet | build-plan default-kit implementation |

## Archive (`archive/`)

Provenance — read on demand, don't update. Each archived doc has a STATUS banner at the top explaining when and why it was archived.

### `archive/shipped/`
Designs whose implementation is complete. Read for the *why* behind shipped behavior; don't update — corrections to shipped behavior belong in code + `cyberdeck-state.md`.

| File | Shipped |
|---|---|
| `cyberdeck-event-stream-design.md` | 2026-04-30 (8/8 phases). Phase 8b cleanup tracked in build plan |
| `cyberdeck-tools-plugins-profiles-retool.md` | 2026-05-04 (5/5 phases). Sub-feature deferrals migrated as build-plan line items |
| `cyberdeck-tools-research-report.md` | Research input → consumed into `in-flight/cyberdeck-tools-default-kit.md` v2 |
| `cyberdeck-tools-research-seed.md` | Original seed → produced research report |

### `archive/case-studies/`
Worked examples — concrete failure-mode evidence that informs future design.

| File | Topic |
|---|---|
| `cyberdeck-tripwire-case-spiralism.md` | Real-deck tripwire authoring under adversarial conditions; seed for tripwire slice 4 |
| `cyberdeck-recon-case-tv-cast.md` | First successful production run (2026-05-07) — LAN TV recon + AirPlay cast probe; source case for the daemon-construct doubt-language contamination gotcha |

### `archive/deferred/`
Designs that aren't current scope but may return.

| File | Why deferred |
|---|---|
| `cyberdeck_arbiter_design.md` | Wearable form-factor variant. Hardware-blocked + post-Linux-port. Concepts feed into build-plan Phase D (local-model substrate) |

### `archive/journal/`
Historical running-state snapshots extracted from canon docs as they got rebuilt. Read when answering "why did we revert X" or "did we ever consider Y." Not a current source of truth.

---

## Drafts / working

| File | Purpose |
|---|---|
| `PLAN-TREE-DRAFT.md` | Working artifact assembled 2026-05-07 during the design-files restructure. Plan tree from start to finish; informed `cyberdeck-build-plan.md` rebuild. Keep until canon catches up; delete then |

---

## Conventions

- **Add status banner at top of every design doc.** Three values: `IN-FLIGHT`, `SHIPPED <date>`, `DEFERRED`, `ARCHIVED <date>`. Plus a one-line "what to do with this doc" instruction (read for X / don't update / etc).
- **Active files reference archive — never the reverse.** When a doc moves to archive, point at it from the active doc that supersedes it; don't have archive docs cross-link to other archive docs (they freeze together).
- **One job per doc.** Either current OR historical — not both. When a slice ships, lift its status into `cyberdeck-state.md`, lift its forward-looking remnants into `cyberdeck-build-plan.md`, and move the design doc to `archive/shipped/` with a banner.
- **Filenames stay stable across moves.** Cross-references in prose are searchable; markdown links would break, so prose mentions are preferred.
