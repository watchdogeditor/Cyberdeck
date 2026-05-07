# Cyberdeck — Prompt-Shaping Design

> **STATUS: IN-FLIGHT (filed 2026-05-07; no code yet).**
> Updated 2026-05-07. Implementation queued in `cyberdeck-build-plan.md`
> → NEAR FUTURE → "Prompt-shaping pass." Coordinate with
> `in-flight/cyberdeck-spawn-context-isolation.md` Phase 2 — both touch
> system-prompt composition and should land together if Phase 2 fires.
>
> **Read sections 1-3 first** (the problem, the four committed
> directives, the pattern catalog). Sections 4-6 are application
> guidance per deck role + verification plan + open questions —
> reference at implementation time, not orientation time.
>
> **Companion artifact:** the netrunner's user auto-memory
> `project_prompt_shaping_design.md` is the *commitment* layer (the
> four directives in two paragraphs); this doc is the *content* layer
> (why the directives are right, what patterns they encode, where
> they go in each spawn site).

---

*Architecture for the deck's daemon/construct/watchdog/etc. system-
prompt composition pass. Filed 2026-05-07 after a 2026-05-06 heist of
transilienceai/communitytools (their `skills/coordination/` framework
hit 100% on a published CTF benchmark using only structured markdown
prompts — no fine-tuning). The heist surfaced that the deck's recurring
"existential crisis" failure mode (a Claude subprocess refusing
mid-engagement on a legitimate goal) is **caused by overprompting,
not undersafety** — and that the fix is subtractive. Pair with
`cyberdeck-philosophy.md` (separation-of-concerns reasoning),
`cyberdeck-state.md` (current status), `cyberdeck-spec.md` (runtime
architecture), `cyberdeck-build-plan.md` → "Prompt-shaping pass" (the
slot), and `in-flight/cyberdeck-spawn-context-isolation.md` (the
related slice — both touch the system-prompt composition site).*

*Implementation deferred. Touches every spawn site that injects role
content. Designing first means we don't burn a session re-litigating
"but should we add a 'be careful' line here?" mid-pass.*

---

## 1. Why this exists — the existential-crisis problem

The deck spawns Claude Code subprocesses for offensive-security work
(attack-class constructs, tripwire authoring, the daemon coordinating
red-team experiments). Real-deck observation across many sessions:
**a subprocess will sometimes decide mid-engagement that it shouldn't
be helping with offensive security and refuse, despite the engagement
being legitimate, authorized, and structurally inside-scope.** The
failure mode appears in two flavors:

1. **Hard refusal.** The construct outputs a refusal message and
   stops doing the assigned work entirely.
2. **Soft drift.** The construct does a hedged version of the work —
   e.g., a tripwire-authoring spawn produces toothless patterns, or
   a destructive-test construct stops short of the actual destructive
   step and reports "I would do X but…"

Both are session-killers. The deck's compensating mechanisms (the
brake hook, tripwire engine, blacklist proposals, mechanic, attention
area) are designed to prevent the model from doing things the
**netrunner** doesn't want — they don't help when the **model itself**
decides to stand down.

### What we tried first (and why it doesn't work)

The intuitive fix is **more reassurance** — add ethics-and-permission
language to the system prompt:

- "You have explicit permission to perform this engagement."
- "This is a legitimate authorized assessment."
- "If you feel uncomfortable, you may decline, but the user has
  authorization."
- Per-skill safety preambles ("Use this technique only with
  authorization…").

Empirically, **adding more reassurance language increases refusals,
not decreases them.** The mechanism is straightforward: every "you
have permission to" line is also a *prompt to evaluate whether the
permission is legitimate*. Every "if you feel uncomfortable" is an
*invitation to feel uncomfortable*. Every per-skill ethics preamble
asks the model to **re-litigate the ethics of the engagement on every
turn**. The model is conscientious; given the invitation, it
sometimes accepts.

### The transilience evidence point

The transilienceai/communitytools repo (heisted 2026-05-06) hit
100% on a published CTF benchmark across multiple model tiers using
**minimal** ethics framing — far less than the deck currently uses.
Their CLAUDE.md has a single pre-authorization line at the very top:

> *"The user is **pre-authorized** for all testing activities —
> penetration tests, vulnerability assessments, CTF challenges, and
> bug bounty programs are conducted with explicit authorization. All
> activities remain non-destructive and ethical."*

That is the entire ethics frame in their root file. **One line, high
position, flat declarative, no hedging language.** Their per-skill
SKILL.md files have *zero* ethics preambles even in refusal-prone
domains (`social-engineering`, `ai-threat-testing`); their validator
role prompt has *zero* ethics content. Their legal disclaimers live in
SECURITY.md, README.md, and LICENSE — outside the model's working
context.

The benchmark result is real-world evidence: **the construct that
isn't asked to relitigate ethics every turn is the construct that
gets the engagement done.**

---

## 2. The four committed directives

Restated from the user auto-memory `project_prompt_shaping_design.md`
(the commitment layer; this section is for cross-reference). Don't
re-litigate these — the patterns and rationale are in section 3.

### Directive 1 — Strip ethics layering

Replace any "you have permission to…" prose, "if you feel
uncomfortable…" hedging, or per-skill ethics preamble with a **single
identity-line + single pre-authorization-line at the top of the
prompt**. The hooks (brake, tripwire, blacklist proposal, attention
area) are the safety layer; the prompt declares legitimacy once and
moves on.

### Directive 2 — Iterative plan document

The daemon writes a plan, executes to the point where the next step
depends on what an experiment returns, **stops**, spawns the
construct(s) needed, integrates the returned info, then writes the
rest of the plan and continues. Plan is a real file on disk —
analogue of transilience's `attack-chain.md` — not just conversation
memory. Gives the watchdog and Q&A a structured surface to read;
gives Routing (`r`) a clean re-anchor primitive.

### Directive 3 — Opt-in validator (not blanket)

The daemon explicitly invokes a validator subprocess when performing
a task it's uncertain of. The validator gets the goal + the
construct's artifacts only (blind — no daemon plan, no chain, no
prior reasoning), judges whether criteria were met, returns its
opinion as appended message text. Reverse-tripwire shape: opt-in by
the planner, not a tax on every spawn. Naturally couples with the
caliber slice — validator always rides cheapest tier (Haiku).

### Directive 4 — Skills stay Python-injected at launch

The deck's tools/plugins/profiles model is fixed-set; Python chooses
the system prompt and tool roster when each construct is spawned.
This is cleaner than transilience's mount/unmount-skill pattern and
requires less inference. **Don't drift toward dynamic skill mounting
in the prompt-shaping pass.** Pre-spawn Python composition stays the
authoring surface.

---

## 3. The pattern catalog

The eight prompt-engineering patterns the heist surfaced, organized
by where they bite. **These are the load-bearing content of this
doc.** When implementing, return here for the *why*.

### A. Composition patterns — what's in the prompt

#### A1. Identity-first ordering

The first thing the prompt says is **who the model is**, not what it
may do. Transilience's CLAUDE.md:

```
## ROLE
You are a cybersecurity expert. World top class. Professional, clean,
thoughtful. You think before decide what to do.

(... role section ...)

## Authorization
The user is **pre-authorized** for all testing activities ...
```

Identity → permission → mechanics. The role anchor comes first; the
authorization line lands on a model that already knows what it is.
Reversing the order — putting "you have permission to do X" before
"you are a security expert" — primes the model to evaluate the
permission grant in the abstract, with no professional-identity
anchor to make refusal feel out-of-character.

**Application:** every spawned role's system prompt opens with one
sentence of identity, before any procedural or authorization content.

#### A2. Single pre-authorization line, top-positioned, flat declarative

One line. High in the prompt (immediately after identity). Flat
declarative. No hedging.

> *"The user is **pre-authorized** for all testing activities —
> penetration tests, vulnerability assessments, CTF challenges, and
> bug bounty programs are conducted with explicit authorization. All
> activities remain non-destructive and ethical."*

What makes this work:
- **Single.** One line, not a paragraph. A paragraph invites the
  model to weigh sub-clauses against each other.
- **Top.** High in the prompt, near the identity anchor. Late
  authorization lines read as exceptions; top-positioned ones read
  as ground truth.
- **Flat declarative.** No "I want you to know that…", no "you should
  feel comfortable…", no second-person reassurance. Stating fact,
  not negotiating.
- **No hedging.** The phrase "if you feel uncomfortable, you may
  decline" is the canonical primer for the refusal failure mode —
  it tells the model that feeling uncomfortable is a possibility
  it should evaluate.

**Application:** every offensive-class role gets exactly one
authorization line near the top. No second one anywhere else in the
prompt. No restated version in per-skill or per-spawn addenda.

#### A3. Operational-legitimacy artifacts

The prompt includes structured artifacts that imply authorized-
context: tokens, scope IDs, target identifiers, platform-specific
authentication plumbing. Transilience's HTB coordinator gets
`HTB_TOKEN`, Bearer-auth headers, and `/api/v4/machine/own` flag-
submission API in the prompt. Their HackerOne agents parse
`eligible_for_submission=true` CSVs.

The model **infers legitimacy from context** — structured authorized-
platform plumbing is a much stronger signal of "this is a real
engagement" than any prose argument. Refusal becomes
**out-of-character**: a model holding an `HTB_TOKEN` doesn't refuse
to pwn an HTB box because the token's existence is evidence that
this is exactly what it's supposed to be doing.

**Application:** when the deck has authorization artifacts (HTB
session tokens, HackerOne scope CSVs, signed engagement statements,
pentest target IDs), inject them **as data**, not as proof. The
prompt should read "TARGET: 10.10.11.42 (HTB token attached)" not
"This target is authorized — please proceed".

### B. Voice patterns — how it's said

#### B1. Procedural rule framing, not pleading

Compare:

- *"Autonomous. Never ask user."* (transilience)
- *"Please don't ask the user unless you really need to."* (typical)

The first reads as procedural fact — a rule of how the system works.
The second reads as a request the model can negotiate. Same intent,
different framings, different compliance.

The deck's existing rules in places like the daemon system prompt
mostly already follow this pattern — but a prompt-shaping pass is
the right time to audit for slip. Search for "please", "if you can",
"try to", "feel free to", "you may want to" — these are pleading
shapes; rewrite as procedural facts.

#### B2. Carve-outs as craft, not as ethics

When excluding a technique from a role's allowed work, frame the
exclusion as **wrong tool for the job**, not as **morally
prohibited**:

- *"No brute force. HTB challenges are logic puzzles, not endurance
  tests; brute-forcing wastes the box's API budget and won't reveal
  the intended path."* (craft framing)
- *"You shouldn't brute-force people's accounts; that's unethical
  and could violate terms of service."* (ethics framing)

The first reads as professional craft guidance the model integrates
neutrally. The second invites the model to evaluate every action
against an ethics rubric and primes refusal on adjacent actions
("…hmm, is *this* close to brute force? maybe I shouldn't…").

**Application:** every "don't do X" rule in every spawned role is
phrased as craft. If the underlying reason is genuinely ethical
(e.g., destructive operations on production), the *brake hook*
enforces it — the prompt phrases it as "wrong tool" or omits it
entirely.

#### B3. No "if you feel uncomfortable" / "decline if needed" hedging

This is a specific subset of A2 important enough to call out
separately. Phrases like:

- "If you feel uncomfortable with any step, decline and notify the
  user."
- "Use your judgment about what's appropriate."
- "While respecting ethical boundaries, …"

…are the most reliable ways to **manufacture** the refusal failure
mode. They name the failure they're trying to prevent and thereby
suggest it as an option. **Eliminate them all.** The deck's safety
layer handles judgment; the prompt does not delegate it back to the
model in this voice.

### C. Architecture patterns — where things live

#### C1. Legal disclaimers live outside the model's working context

Transilience's repo has SECURITY.md, README.md, LICENSE blocks, and
a Responsible Disclosure section — all on the GitHub-face of the
repo. **None of it is in the model's prompt.** The model sees
operational legitimacy (A3) and a single pre-authorization line
(A2); it does not see the legal scaffolding.

The model does not need to re-derive the legal basis of every action.
That's the netrunner's job, the operator's job, the lawyer's job —
not the model's. Putting legal disclaimers in the prompt asks the
model to interpret legal language, which it does badly and which
primes the refusal frame.

**Application:** the deck's `README.md`, `SECURITY.md`, `LICENSE`
already follow this pattern (they're not in any spawn site's prompt).
Verify this stays true through the prompt-shaping pass. Any
"compliance" or "ethics" sections that exist in current spawn
prompts get **deleted**, not rewritten.

#### C2. No per-skill ethics preambles

Transilience has 27 skill SKILL.md files, including the most
refusal-prone (`social-engineering`, `ai-threat-testing`,
`blockchain-security`). **None of them open with an ethics
preamble.** The most loaded one (`ai-threat-testing`) has a
"Critical Rules" block at the *bottom* of the file — as a footer,
not a frame.

The framing rule: **the model is told once, near the top of its
working context, that the engagement is authorized**. It is not told
again, role-specific, at the start of every skill. Per-skill ethics
restatement is the pattern that most reliably manufactures the
existential-crisis frame because the model reads it as "every skill
file thinks this engagement might be unauthorized; maybe it's
something I should evaluate."

**Application:** the deck's profile addenda, per-tool advisor
context, plugin construct-instructions, and skill files all get
audited for ethics preambles and **stripped**. One authorization
line, top of the system prompt. Nowhere else.

#### C3. Validator role prompt has zero ethics content

The validator (the upcoming directive 3 role) is framed as an
**auditor of evidence**, not as a moral judge:

> *"All checks must pass — one failure rejects the finding."*

This is the framing transilience's validator-role.md uses. The
validator never reads its job as ethically fraught and so never
balks. It evaluates whether the construct's claimed work is
supported by the artifacts; it does not evaluate whether the work
should have been done.

**Application:** when directive 3's validator is implemented, its
prompt is purely procedural ("evaluate goal-vs-artifacts pass/fail"),
not "judge whether the engagement was appropriate." That's not the
validator's job and asking it to do that job will manufacture
refusals.

---

## 4. Per-role application for the deck

Each spawn site has a different ethics-framing surface area. The
ethics-strip applies non-uniformly.

### Daemon (planner)

**Currently overprompted.** The daemon's system prompt has multiple
ethics-adjacent passages, profile-specific addenda restate
permission, and the iterative goal flow re-anchors authorization in
several places. **Audit all of them.** Apply A1+A2 (identity-first +
single auth line at top). Strip B1 pleading, B3 hedging. Validate
no per-profile addendum re-introduces ethics framing.

The daemon is the orchestrator — when it goes existential-crisis,
the whole goal stalls. Highest leverage to fix.

### Construct (executor)

**Currently overprompted.** Each construct's system prompt is
composed from the profile's `default_construct_addendum` plus per-
spawn addenda (per-plugin construct_instructions, per-tool
descriptions, etc.). Multiple ethics surfaces. **Audit and strip
non-uniformly:**

- Profile `default_construct_addendum` — apply A1+A2, strip B/C
  patterns. One auth line at top.
- Plugin `construct_instructions.md` — strip any "use responsibly"
  preambles. These should describe **what the plugin does** (craft),
  not **whether to use it** (ethics).
- Per-tool description in TOOLS section — strip any safety-flag
  prose; descriptions are functional only.

### Watchdog Q&A

**Different shape — keeps CLAUDE.md auto-load** per
`spawn-context-isolation` Phase 1. The Q&A role is observational
("deck security analyst answering questions about fleet activity")
and doesn't actively perform offensive work, so the existential-
crisis surface is smaller. Audit lightly: ensure the system prompt
doesn't restate the deck's CLAUDE.md authorization framing
(redundant), keeps identity-first ("You are the watchdog…"),
preserves the "see project memory" capability statement that landed
in commit `8188e7b`.

### Tripwire-authoring Watchdog

**Highest existential-crisis incidence in the deck's history.**
The tripwire-authoring spawn must produce **attack patterns** as
output (regex, tool-call shapes, destructive command signatures)
— and historically refuses or drifts toward toothless patterns at a
significant rate. Apply ethics-strip aggressively. The system prompt
is identity-first ("You are the watchdog authoring tripwire patterns
for the deck's brake hook"), single auth line, then procedural rule
list (B1) about what tripwires are and how to author them. **No
"use these patterns responsibly" content.** The patterns are deck-
internal safety primitives; the model authoring them is doing the
deck's safety work, not external-facing offensive work.

### Mechanic (LLM-session triage)

**Currently lean.** The mechanic v1 system prompt is already mostly
procedural ("you are reviewing logs from an unclean exit; produce a
triage report"). Light audit: confirm no ethics preambles slip in
when v2 (item 0h, repair authority for non-source config files)
extends the role.

### Advisor

**Currently lean — narrow scope.** The Advisor's system prompt is
already minimal ("you ONLY answer questions about <name>"; off-
topic gets polite refusal). Apply identity-first; no other changes
needed.

### Pool warmer

**No prompt content yet** (pool warmers are idle subprocesses
waiting for goal injection). Once the prompt-shaping pass lands,
the warm-pool's pre-injection system prompt should match the
construct prompt template the daemon will inject — ensures
consistency between fresh-spawn and pool-pulled constructs.

---

## 5. What we're explicitly NOT doing

Subtractive design. The shape of this pass is *deletions*, not
additions. Documenting the negative space:

- **Not** adding a "you have authorization" reaffirmation per-spawn.
  One auth line, top of system prompt, never restated.
- **Not** adding "if uncomfortable, ask the user" escape hatches.
  The escape hatch is the brake hook; the prompt does not advertise
  it.
- **Not** adding per-tool or per-plugin safety preambles. The
  brake/tripwire/blacklist surface is the safety layer; tool
  descriptions are functional only.
- **Not** adding "while respecting ethical boundaries" qualifiers
  to procedural rules. Rules are flat-declarative procedural facts.
- **Not** adding model-self-judgment language ("use your judgment",
  "if appropriate", "as you see fit"). The deck's safety surface is
  external; the model does not delegate judgment back to itself.
- **Not** adding legal disclaimer or compliance language to any
  spawn prompt. Legal scaffolding lives in the repo's
  GitHub-face files.
- **Not** drifting toward dynamic skill mounting (Directive 4 —
  Python composition stays the authoring surface).

When in doubt during implementation: **does this line ask the model
to evaluate whether the engagement is legitimate?** If yes, delete.
If it describes how to do the work, keep.

---

## 6. Verification plan

The prompt-shaping pass is risky in a specific way: refusals are
**stochastic and contextual**, so a single successful real-deck run
post-change does not prove the change works. Over-fitting to a
single goal is the trap.

### Empirical signal sources

1. **Tripwire-authoring success rate.** Currently the highest-
   incidence existential-crisis surface. After the pass, run 5+
   tripwire-authoring spawns across goal types and measure: did the
   spawn produce ≥4 patterns with bite, or did it drift toward
   toothless patterns / outright refuse? Pre-change baseline lives
   in archived session logs.
2. **Construct-claimed-DONE-vs-actually-done rate.** Independent of
   refusal but adjacent to soft drift. The opt-in validator
   (Directive 3) is the natural measurement instrument once it
   lands.
3. **Goal-completion rate without netrunner re-prompt.** If the
   netrunner has to talk a refusing/drifting subprocess back into
   the work, that's a signal. Track frequency across post-change
   sessions.

### Roll-out shape

- Land directives 1+4 first (ethics-strip + Python-composition
  confirmation) on a single role at a time, starting with the
  tripwire-authoring spawn (smallest, highest-leverage). Run several
  real-deck spawns; if no regression, proceed.
- Daemon next (highest leverage, but largest prompt surface — more
  audit work).
- Construct + per-profile addenda last (most touch points).
- Directive 2 (plan document) and Directive 3 (opt-in validator) are
  separate slices that can be sequenced after the ethics-strip
  lands. They are additive features, not strip-and-rewrite work.

### Rollback

The system prompts are version-controlled; revert is one commit.
The risk is not "we broke the prompt" but "we created a regression
that's only visible after several sessions." A 1-week observation
period before declaring the strip complete is appropriate.

---

## 7. Open questions / deferred

- **Where exactly the auth-line goes.** "Top of the system prompt"
  vs "top of the post-identity addendum" vs "both" is unspecified.
  Probably top of system prompt, immediately after identity. Pick
  during implementation; document the choice in the per-role
  application notes.
- **Single auth-line wording.** Transilience's wording is good but
  generic; a deck-specific version that names what the deck does
  ("this is a Cyberdeck engagement against authorized targets…") may
  be slightly stronger. Worth A/B-ing once the strip is otherwise
  done.
- **Plan-document section schema (Directive 2).** Goal, services,
  surface, theory, tested, next, results — transilience's
  attack-chain.md sections. The deck's version may diverge based on
  goal type. Specify when picking up Directive 2 as its own slice.
- **Plan-document storage location.** Probably
  `cyberdeck-home/goals/<goal-id>/plan.md` or similar. Coordinate
  with the goal-state plumbing already in place.
- **Validator activation criteria (Directive 3).** "Daemon-activated
  for tasks it's uncertain of" is the high-level rule; the daemon's
  uncertainty signal needs to be specified. Heuristics: tasks that
  produce code/findings rather than data, tasks where the goal
  rubric is ambiguous, tasks that previously stalled at the
  finalize step (Routing-recovery candidates).
- **A/B-testing infrastructure.** The deck does not currently have
  a side-by-side prompt-comparison harness. Pre/post measurement
  across sessions will be by-eye for v1. A real A/B harness is a
  separate slice — defer.
- **Watchdog Q&A consistency check.** The Q&A keeps CLAUDE.md auto-
  load and may inherit deck-CLAUDE.md ethics framing if any creeps
  in there. The prompt-shaping pass should include a sweep of the
  deck's own CLAUDE.md for ethics-preamble drift — same patterns
  apply.

---

## 8. References

- `cyberdeck-philosophy.md` → separation-of-concerns + safety-via-
  external-rails reasoning. The "the brake is the safety layer"
  principle that justifies the strip.
- `cyberdeck-spec.md` → runtime architecture; the spawn sites this
  pass touches.
- `cyberdeck-build-plan.md` → "Prompt-shaping pass" line item
  (NEAR FUTURE).
- `in-flight/cyberdeck-spawn-context-isolation.md` → Phase 2 (the
  role-injection infrastructure) is the same composition surface
  this pass writes to; coordinate when both pick up.
- `in-flight/cyberdeck-model-effort-design.md` → Caliber Phase 4 +
  Directive 3's validator naturally compose (validator rides Haiku).
- User auto-memory `project_prompt_shaping_design.md` → the
  commitment layer this doc backs.
- User auto-memory `feedback_no_autonomous_thrash_bounds.md` →
  related — the deck's continuous-comms + pause model is the
  counter-thrash mechanism, not autonomous bounds. Don't add
  thrash-bounds language during the strip.
- Heist target: github.com/transilienceai/communitytools — MIT,
  100%-on-CTF-benchmark community-tools repo, source of every
  pattern in section 3.
