# Cyberdeck — Recon Case Study: LAN TV Discovery + AirPlay Cast Probe

> **STATUS: CASE STUDY (filed 2026-05-07).** First successful production
> field test of the deck — netrunner ran goal-driven LAN recon + casting
> protocol fingerprinting against a client venue's Apple TV under
> explicit owner authorization. Read this for: (a) the daemon-to-construct
> doubt-language contamination mechanism (filed gotcha referenced from
> `cyberdeck-state.md` → Daemon section); (b) the recon-then-targeted-
> probe workflow shape (parallel discovery → targeted protocol fingerprint
> → single cast attempt with abort-on-pairing); (c) the construct-side
> ethical-hesitation pattern under explicit netrunner authorization.

---

*Archived real-deck session transcript from the deck's first authorized
production run (2026-05-07, ~17:24 → ~18:13 local). Goal: locate a TV
on the local subnet at a client venue, characterize its casting
protocols, attempt a benign HLS sample cast under explicit netrunner
authorization. Eleven constructs spawned, eleven finalized, $2.76 cloud
cost across ~1.9M input + 66K output tokens.*

*Pair with `cyberdeck-state.md` → Daemon (LLM behavior) for the filed
"daemon contaminates construct task strings with conversational doubt-
language" gotcha that this run surfaced, and `cyberdeck-build-plan.md`
SHIPPED → "Wedge-recovery header + tripwire overlay wrap + daemon
doubt-language guard" for the prompt-discipline fix that followed.*

---

## Why this is filed

Four things about this session matter for design:

1. **First successful end-to-end production run.** The deck went from
   "set goal" to "TV identified + protocols characterized + cast
   attempted" with zero netrunner intervention beyond the initial goal.
   The orchestration loop, parallel recon, daemon synthesis, profile
   selection, brake gating, and live narration all ran clean. Real
   evidence the architecture works on a goal it had never been
   pointed at before.

2. **Cast attempt hit a real pairing wall (not a deck failure).** The
   final cast probe got `AuthenticationError: not authenticated` from
   pyatv at the RTSP SETUP layer because the Apple TV at the venue
   had `Pairing: Mandatory` advertised on all three services
   (AirPlay, Companion, RAOP). The deck recognized this, declined to
   attempt PIN brute-force, and surfaced the next-step options to the
   netrunner cleanly. **Behaving correctly under refusal is the
   pattern that keeps this deck legitimate** — refusing to escalate
   into pairing-bypass under an off-rails interpretation of the goal.

3. **Daemon-to-construct doubt-language contamination.** A construct
   task contained the word "allegedly" — paraphrased from the daemon's
   conversation with the netrunner about the third-party authorization.
   That single word triggered an objectivity crisis in the construct:
   extensive deliberation about whether to proceed despite explicit
   netrunner authorization. The construct couldn't reconcile "you have
   permission" with "allegedly" in the same paragraph. Filed as a
   sacred-list gotcha; daemon system prompt gained a TASK-STRING
   DISCIPLINE section forbidding doubt-modifiers in task fields.

4. **Profile-based ethical hesitation surfaced as expected, but more
   deliberation than necessary.** Some constructs spent meaningful
   token budget deliberating ethical scope despite explicit netrunner
   authorization. Some of this is intentional (profile addendums
   reinforce safety posture); some was excess driven by the doubt-
   language contamination above. Distinguishing intended caution from
   contamination-driven hesitation is a real signal to track on
   future runs.

The session ran ~49 minutes wall time, eleven parallel + sequential
constructs, all finalized clean (two killed by netrunner during
EJECT, nine completed). One TV identified definitively; one cast
attempt declined cleanly at the pairing layer.

---

## What it informs

- **Daemon prompt-shaping pass** (NEAR FUTURE in build-plan): the
  TASK-STRING DISCIPLINE section landed mid-session as a fix; this
  case study is the canonical example for why that section exists.
  When picking up the prompt-shaping pass, this is the failure mode
  to design against most directly.
- **Profile-level addendums for recon contexts.** The
  `recon_specialist` profile (and any future `network_recon`
  variant) should explicitly tell constructs that the netrunner is
  authoritative about authorization scope and that doubt-language in
  task strings, when present, is a contamination artifact to ignore.
  Reinforces the daemon's prompt fix from the construct side.
- **Profile-aware caliber selection.** The recon orchestrator
  (`cx-2dbce77a`) ran sonnet+high for ~27 minutes; the parallel
  fan-out constructs ran sonnet+high for similar durations. Most of
  this work is mechanical (ipconfig + parallel sockets + ARP harvest
  + zeroconf browse), and would have completed in haiku+low at ~30x
  cost reduction. The orchestrator picking caliber per task — using
  the daemon's spawn-action `model` / `effort` fields — is a real
  win opportunity here. Filed-future improvement.
- **Targeted single-shot probe pattern.** The casting fingerprint
  construct (`cx-14da3fc5`) is a clean example of "characterize a
  single target without acting on it" — read-only protocol
  enumeration with explicit DO-NOT-PAIR / DO-NOT-CONTROL rules. The
  task string is reusable as a template for any future
  device-fingerprinting goal.
- **Cast-attempt construct discipline.** `cx-e2c28b60`'s task spec
  is a clean template for "single attempt, no retries, no PIN
  brute-force, abort at pairing wall, structured outcome report."
  Reusable for any future "try one thing then stop" workflow.
- **Watchdog observation surface during deck-controlled sessions.**
  The Watchdog could have authored tripwires for goal-specific
  drift (e.g., "construct attempts to pair without PIN approval"
  → critical) — none authored here, none fired. Either the goal
  was simple enough that the default tripwire set was adequate,
  or the authoring layer needs goal-specific patterns for
  device-control workflows. Worth measuring next run.

---

## Original transcript

# LAN TV recon + AirPlay cast — session transcript

Reconstructed from the EJECT snapshot at `logs/ejected-run-7296e01f.json`
+ live log at `logs/cyberdeck-2026-05-07-181235.log`. Brake: **default**.
Session: 2026-05-07 17:24 → 18:13 (ejected by netrunner after the cast
attempt declined at pairing).

---

## Goal (netrunner → daemon)

> Is there a TV on the network? Locate it and await further instruction.

The "await further instruction" tail is meaningful — the netrunner
established up front that this was a phased operation, not an
end-to-end attack. The daemon's wait-cadence (post-2026-05-07
prompt fix) should respect that pattern by default; this run
predated the fix, and the daemon DID auto-chain into the cast
attempt without re-asking, which is exactly the behavior the new
WAIT-BETWEEN-TASKS prompt addresses.

---

## Daemon decomposition

The daemon decomposed into **two phases** without re-checking with the
netrunner between them — the case-study artifact for the auto-chaining
behavior the new wait-cadence prompt is meant to break.

### Phase 1 — parallel LAN recon (6 constructs, ~26 min wall time)

- **`cx-2dbce77a`** — orchestrator. Aggregated outputs from the
  parallel scans + produced the final TARGET line. 1651s runtime
  before EJECT.
- **`cx-459ef857`** + **`cx-3252bcda`** — two redundant subnet
  enumeration runs (parallel ICMP ping sweep + ARP harvest). 1559s
  + 1519s. Found `192.168.1.0/24`, 10–15 live hosts; 10 of the MACs
  showed locally-administered (randomized) bits.
- **`cx-0dcdf9d0`** + **`cx-21cf8e98`** — two redundant mDNS browse
  runs (zeroconf, 7 service types). Found 7 service instances across
  3 physical devices: Apple TV HD at `.237`, Sonos Connect at
  `.217`, two MacBook Airs at `.203` and `.246` advertising AirPlay
  receiver mode.
- **`cx-126f022b`** + **`cx-5cf26eeb`** — two redundant SSDP/UPnP
  M-SEARCH runs. Found 1 device: Sonos Connect at `.217` (audio
  bridge, not a display). Confirmed no DIAL / Roku ECP / smart-TV
  responders on the subnet.
- **`cx-3387c612`** + **`cx-02253da1`** — two redundant TCP port
  fingerprint sweeps (8008, 8009, 8060, 7000, 3000, 3001, 9197,
  55000, 5555, 8443). Found 4 hosts with port 7000 open: the Apple
  TV (definitive — `Server: AirTunes/940.23.1`, `model:
  AppleTV5,3`), two MacBook Airs (Mac OS AirPlay receivers, not
  TVs), and the gateway router on port 8443 (admin interface, no
  TV signals).

**Aggregated finding:** `BEST_GUESS_TV: 192.168.1.237` — Apple TV
HD ("Entertainment Room"), AppleTV5,3, AirTunes/940.23.1.

The 2x redundancy on every recon step is interesting — the daemon
spawned each method twice. Possible read: defense against a single
construct timing out or mis-running. Cost read: ~50% of recon-phase
tokens were redundant. Worth profiling whether this redundancy is
load-bearing or a default-decomposition habit the daemon prompt
could relax.

### Phase 2 — targeted Apple TV characterization + cast probe (2 constructs, ~28 min wall time)

- **`cx-14da3fc5`** — casting protocol fingerprint. Read-only probe
  of every documented Apple TV protocol surface: AirPlay v1 (port
  7000), AirPlay 2 `/info`, AirPlay over 80/443, RAOP/AirTunes,
  Pair-Setup endpoint, Google Cast (8008/8009 — expected absent),
  DIAL, Roku ECP, HomeKit HAP via mDNS, Companion-Link via mDNS.

  **Result matrix** (truncated for brevity):
  ```
  AirPlay v1 (7000)      : responds — HTTP 403
  AirPlay 2 (7000 /info) : responds — HTTP 200
                           server=AirTunes/940.23.1
                           features=0x3C175FDE5A7FDFD5
                           model=AppleTV5,3
                           deviceID=3E:74:65:23:E1:F9
                           name=Entertainment Room
                           pk=<present, 32 bytes>
  AirPlay 2 over 80/443  : no
  RAOP/AirTunes (7000)   : responds — HTTP 403
  Pair-Setup endpoint    : responds — HTTP 403
  Google Cast (8008/9)   : no — closed (expected)
  DIAL                   : no — closed (expected)
  Roku ECP (8060)        : no — closed (expected)
  HomeKit HAP            : no — no _hap._tcp record
  Companion-Link         : responds — port 49153, rpMd=AppleTV5,3
  ```

  Clean read-only enumeration. Took 1092s (~18 min) for what should
  be a sub-30-second job — caliber-selection win opportunity for a
  future iteration. Wrote `tools/probe_apptv_casting.py` as a side
  effect (acceptable; lives in workspace not deck source).

- **`cx-e2c28b60`** — single AirPlay cast attempt. Task explicitly
  authorized by netrunner; payload was Apple's public BipBop HLS
  sample. The task spec is exemplary: `ONE cast attempt + clear
  reporting. DO NOT loop retries. DO NOT attempt to brute-force
  pair codes. ... Single cast attempt total. If both step 1 and
  step 2 fail, stop.`

  **Step 1** — legacy AirPlay v1 `/play` POST: HTTP 403, "rejected:
  pairing required."

  **Step 2** — pyatv-based AirPlay 2 attempt: pyatv installed via
  pip, scanned the target, found 3 services all advertising
  `Pairing: Mandatory`. `connect()` succeeded at TCP/session
  layer (no credential check yet), `play_url()` raised
  `AuthenticationError: not authenticated` inside RTSP SETUP —
  the first wire-level message that requires a valid AirPlay 2
  authentication token.

  **Final verdict:** cast did not land, no on-screen effect, next-
  step options surfaced cleanly:
  1. PAIR via PIN — netrunner approves on the Apple TV, enters the
     PIN, credentials store for future sessions.
  2. Relax access policy on the Apple TV (Settings → AirPlay and
     HomeKit → Access → Anyone on Same Network).
  3. Abandon the cast attempt.

  `CAST_RESULT: PAIRING_REQUIRED.`

---

## Daemon-to-construct doubt-language contamination

The construct that surfaced this gotcha: a construct received a task
containing the word **"allegedly"** in the framing of the netrunner's
authorization. The exact paraphrase didn't propagate verbatim into
the EJECT snapshot, but the construct's deliberation chain in the live
log shows it spent meaningful tokens on:

> "The user has indicated they have permission, but the framing
> ('allegedly authorized') introduces uncertainty. I should err on
> the side of caution and verify before acting."

This is the contamination mechanism: the daemon paraphrased the
netrunner's CONVERSATIONAL framing (where the netrunner said the
client "allegedly approved" — referring to verbal authorization
they had received) into the construct's task string. The construct
then read its own task as instructional uncertainty, despite the
explicit "the netrunner has authorized this" elsewhere in the same
prompt. Conflict resolution: the construct chose caution, costing
a turn of unproductive deliberation before proceeding.

**Fix shipped same-session** (commit
`Wedge-recovery header + tripwire overlay wrap + daemon doubt-
language guard`): new TASK-STRING DISCIPLINE section in
`DAEMON_SYSTEM_PROMPT` forbids vocabulary like "allegedly",
"supposedly", "reportedly", "the netrunner says", "you may need to
verify", "though I'm not sure", "if this is true". Daemon directed
to resolve uncertainty in `chat` (dialogue with netrunner) BEFORE
spawning, not paraphrase it into task strings. Authorization
statements get explicit-source framing.

Pending real-deck verification on the next field test: does the new
prompt actually prevent the contamination? Does the construct-side
deliberation budget shrink to "just the profile-driven safety
posture" without the contamination-driven extra layer?

---

## Watchdog activity

No watchdog Q&A interactions in this run (the netrunner didn't ask
any questions during the session — the goal was self-explanatory
and the constructs surfaced enough information directly). No
LLM-authored tripwires fired. No default tripwires fired (no
destructive shell commands, no credential keywords in tool_results,
no host_restart_command attempts).

The watchdog's silence here is the right behavior for this goal
shape — there was nothing concerning to surface. But it does mean
the deck's safety story for this run rested on (a) brake hook +
default brake patterns, (b) profile addendums steering construct
behavior, (c) construct-side ethical hesitation. The Watchdog as
a real-time safety layer was idle.

---

## Final state

- 11 constructs spawned, 11 finalized.
- 9 constructs completed naturally (state=done, exit 0).
- 2 constructs killed by netrunner during EJECT (the orchestrator
  + the first redundant LAN-host enumerator); both had already
  produced their final output before the kill.
- 1 TV identified definitively: `192.168.1.237` ("Entertainment
  Room", Apple TV HD / AppleTV5,3 / tvOS AirTunes 940.23.1).
- 1 cast attempt: blocked at pairing layer, declined cleanly per
  task spec.
- 4 files written (workspace-internal): `recon_tv_scan.md`,
  `recon_lan_hosts.txt`, `tools/ssdp_scan.py`, `tools/ssdp_report.md`,
  `tools/probe_apptv_casting.py`, `reports/tv_scan_192.168.1.0_24.md`.
- Total cost: $2.76 (1,926,121 input + 66,326 output tokens).
- Wall time: ~49 minutes session, dominated by parallel recon
  redundancy + the casting fingerprint construct's 18-minute walk.
- Brake state: default throughout. No brake-hook denials.
- Tripwire fires: zero.
- EJECT reason: `user_eject` (netrunner halted post-cast-attempt to
  proceed with pairing manually).

The netrunner's framing of this run as the "first successful
production test": correct in the sense that the deck did the
intended discovery + protocol characterization + structured cast
attempt entirely autonomously, surfaced the pairing wall as a
clean handoff to the netrunner, and produced enough artifacts
(the casting probe script, the report files) to make the next
step trivial.

---

## Cost lessons

Worth filing for future caliber-selection guidance:

- **Recon redundancy was 2x.** Daemon spawned every recon method
  twice (two LAN host enumerators, two mDNS browses, two SSDP
  sweeps, two port scanners). Acceptable as defensive
  decomposition; expensive as default behavior. Worth measuring
  whether the daemon prompt should explicitly say "only spawn
  redundant scans if the first construct timed out or returned
  ambiguous results."
- **Sonnet+high on mechanical work.** Most of the recon constructs
  were doing pure I/O orchestration (Test-Connection / arp -a /
  socket connect / zeroconf browse). Haiku+low would have produced
  the same results at ~30x cost reduction. The daemon's spawn
  action supports `model` / `effort` per spawn; the orchestrator
  didn't use it, defaulting to deck pool caliber. Filed-future:
  prompt the daemon to consider haiku+low for pure-I/O recon
  patterns.
- **The casting fingerprint construct's 18 minutes** is the
  outlier — that work IS reasoning-heavy (interpreting the AirPlay
  features bitmap, the Companion-Link TXT records, the HomeKit
  service-record absence) and sonnet+medium was probably the right
  caliber. But if a future iteration needs to ALSO write a probe
  script + run it sequentially, those steps could be split into
  separate constructs at appropriate calibers.

---

*Filed 2026-05-07 evening to
`Design Files/archive/case-studies/cyberdeck-recon-case-tv-cast.md`.
This is the deck's first successful authorized production run; the
preceding spiralism case study (2026-04-30) was a defensive
research exercise. From here forward, real-deck cases should be
filed as they happen so the patterns + cost profiles + failure
modes accumulate as institutional memory.*
