# Wearable Cyberdeck Arbiter — Design Doc

> **STATUS: DEFERRED (form-factor variant, not current scope).** Archived
> 2026-05-07. The wearable build path is hardware-blocked + post-Linux-port.
> Read this when (a) the Pi/Linux port is real and the form-factor question
> reopens, or (b) you want context on the local-first dispatcher / egress
> scrubber architecture — those concepts feed into the local-model substrate
> work tracked in `cyberdeck-build-plan.md` (Phase D).

---

*Local-first dispatcher with cloud escalation, for cybersecurity research workflows.*

---

## Goal

A portable (wearable) device that acts as a "door-greeter" for AI-assisted work: triages requests locally, handles trivial ones inline, escalates harder reasoning to cloud Claude (single or parallel fan-out), and scrubs sensitive data at the egress boundary so client/engagement information never leaves the device.

Throughput-critical (many requests/min). Compliance-aware. Wearable form factor.

---

## Hardware

### Two-part split

The device is split into a **wrist unit** (display + input, dumb) and a **core unit** (compute + battery + radio, hip/back/bag). This solves thermal and power constraints — heat-producing components live where they can dissipate; the wrist gets a sub-1W display.

### Wrist unit

- Small OLED or e-ink display
- Capacitive touch surface and/or physical buttons
- ESP32-S3 or RP2040 microcontroller
- Link to core: BLE (~100ms latency, wireless) or USB-C tether down sleeve (faster, wired)
- Battery: ~500 mAh, 6–8h light use

### Core unit

- **Compute:** Radxa Rock 5C 16GB *(primary recommendation)* or Orange Pi 5 Plus 16GB
  - RK3588(S) SoC: 8 ARM cores (4× A76 + 4× A55), Mali-G610 GPU, 6 TOPS NPU
  - Pi-compatible footprint, USB-C PD input, M.2 NVMe slot
- **Power:** 20000 mAh USB-C PD power bank (65W+ recommended for headroom)
- **Connectivity:** USB cellular modem (LTE/5G) + SIM
- **Storage:** NVMe SSD for fast model loading and audit logs

### Power budget (sustained, typical load)

| Component | Draw |
|---|---|
| RK3588 NPU inference | 2–3 W |
| RK3588 idle | ~1.5 W |
| Cellular modem (TX) | 2–3 W |
| Wrist unit (BLE) | <1 W |
| Misc (storage, USB) | ~1 W |
| **Total active** | **~7–10 W** |

A 20000 mAh / 74 Wh USB-C PD pack yields roughly 7–10 hours of active use, longer if the modem is mostly idle.

### Hardware gotchas

- Cellular modem TX is the second-biggest power draw after the SoC — plan for it.
- BLE wrist link adds ~100 ms per round trip. Fine for chat, annoying for instant tool feedback. Wired is snappier.
- Cyberdecks photograph better than they wear. Mock the form factor in 3D-printed cardboard before committing.

---

## Software stack

```
Hardware:    Radxa Rock 5C 16GB (RK3588S, 6 TOPS NPU)
OS:          Ubuntu 24.04 arm64 (Joshua Riek build) or Armbian
Runtime:     RKLLama (Ollama-compatible API, NPU-accelerated)
Local model: Qwen3 4B (RK3588-converted, w8a8 quant)
             — reasoning OFF for arbiter role
             — reasoning ON available for local escalation if needed
Scrubber:    Python (presidio + detect-secrets + custom rules)
Dispatcher:  Python daemon, async, manages cloud fan-out
Cloud tier:  Claude API (Claude Code or direct), 1–N parallel instances
Audit:       SQLite + age-encrypted backups
```

### Why RKLLama over plain Ollama

- NPU-accelerated inference: ~27× faster than CPU on Phi-3-class models per published benchmarks
- Implements Ollama's `/api/chat` and `/api/generate` — drop-in compatible client side
- Native tool/function calling support for Qwen, Llama 3.2+, others
- NPU draws ~2–3 W vs CPU ~8–10 W; major battery win

### RKLLama caveats

- Younger than Ollama; smaller community, fewer Stack Overflow answers
- NPU model conversion is one-way — wait for Pelochus or others to convert new models, or do it yourself with rknn-toolkit
- Setup is more involved than Ollama (custom Ubuntu, NPU drivers, conversion toolchain)

---

## Architecture

```
┌─────────────────── WRIST UNIT ───────────────────┐
│  Display + capacitive input                       │
│  ESP32-S3 / RP2040: BLE or USB to core            │
└──────────────────────┬───────────────────────────┘
                       │
┌──────────────────────┴───────────────────────────┐
│              CORE UNIT (hip/back)                 │
│                                                   │
│  ┌─ Arbiter (Qwen3 4B, NPU, reasoning OFF) ──┐   │
│  │   Classify → tool / escalate / clarify /  │   │
│  │   decline + extract args + select scrub   │   │
│  │   profile                                  │   │
│  └────────────────────┬───────────────────────┘   │
│                       ▼                           │
│  ┌─ Dispatcher (Python async daemon) ─────────┐  │
│  │   Routes per arbiter decision              │  │
│  └─┬──────────────┬────────────────┬──────────┘  │
│    ▼              ▼                ▼              │
│  Local         Scrubber         Decline /         │
│  toolbelt    (deterministic)    Clarify           │
│                  ▼                                │
│              Sanitized                            │
│              payload                              │
│                  ▼                                │
│              ┌───────────────────┐                │
│              │ Cloud Claude (×N) │                │
│              └─────────┬─────────┘                │
│                        ▼                          │
│                   Rehydrator                      │
│                        ▼                          │
│                   User sees                       │
│                   real answer                     │
└───────────────────────────────────────────────────┘
```

---

## Arbiter design

### Role

Classify each input into one of four actions, extract arguments, select a scrub profile. **Never** does heavy reasoning — that's the cloud tier's job. Optimized for first-token latency and JSON adherence.

### Output schema

```json
{
  "action": "local_tool | escalate | clarify | decline",
  "tool": "string | null",
  "args": { ... } ,
  "mode": "single | parallel | null",
  "n": 1,
  "scrub_profile": "code_analysis | narrative | architecture | full | none",
  "preserve_categories": ["IP", "..."],
  "summary": "string",
  "reason": "string | null"
}
```

### Notes

- `local_tool` — fixed manifest of ~10–20 tool families (file ops, web search, comms, hardware control, etc.). Arbiter sees the manifest, not full MCP discovery.
- `escalate` — hands to dispatcher with mode (`single` / `parallel`) and `n` for fan-out count.
- `clarify` — when args are ambiguous, ask the user instead of guessing.
- `decline` — refuse politely with reason; scope-out-of-bounds requests, etc.
- Tool discovery: keep MCP-style dynamic discovery in the **cloud tier**, not the arbiter. The arbiter sees a small fixed manifest; "needs flexibility" maps to `escalate` automatically. Keeps prompts small, arg extraction reliable.

### Modes

- **Reasoning off** for the arbiter hot path (speed)
- **Reasoning on** available for offline-only complex decisions (rare)

---

## Egress scrubber

### Why this exists

Engagement contracts and compliance regimes (CPNI, NDA, SOC2, etc.) require that client-identifying information doesn't end up in third-party logs. Even ignoring policy, leaking client architecture details into N parallel cloud sessions creates correlation risk.

The scrubber sits at the cloud egress boundary, replaces sensitive content with stable placeholders before sending, and rehydrates the response on the way back.

### Why deterministic, not LLM-based

1. **Determinism** — regex/NER either catches the IP or doesn't. LLMs are best-effort. Compliance needs proofs.
2. **Auditability** — when the client asks "prove no CPNI left the device," you point at rules and logs. You cannot audit a 4B model's attention.
3. **Speed** — milliseconds vs. tokens-per-second. No LLM in the egress hot path.

### Layers

**1. Detector (deterministic)**
- IP / CIDR / MAC regex
- Hostname patterns (engagement-specific)
- Credentials & tokens (`detect-secrets`, `truffleHog` patterns, entropy heuristics)
- Phone numbers (`phonenumbers` library)
- Email addresses
- Account / customer IDs (engagement-specific format)
- Generic PII via `presidio-analyzer` (names, SSNs, etc.)

**2. Engagement context (YAML, per-gig)**

```yaml
engagement: acme-q2-2026
aliases:
  - "Acme Corporation"
  - "ACME"
  - "acme.com"
  - "acmecorp"
hostname_patterns:
  - "*.internal.acme.com"
  - "*.corp.acme.local"
ticket_prefix: "ACME-"
custom_patterns:
  customer_id: '^CUST-\d{8}$'
```

Loaded at session start, signed with engagement key.

**3. Tokenizer (reversible)**

Stable mapping per request — same input gets same placeholder so Claude can reason about relationships. `acme.com` always maps to `{{CLIENT_1}}` within a request. Mapping lives in memory, dies with the request.

**4. Rehydrator**

Walks the response, substitutes placeholders back. **Critical:** only rehydrates placeholders this request created. If Claude hallucinates `{{IP_47}}` that wasn't in the input, leave it as-is or flag — never silently fill in.

**5. Audit log**

Every request logs (locally, encrypted): timestamp, scrub categories and counts (not values), what came back, what was rehydrated. Compliance evidence.

### Scrub profiles

Different request types need different scrubbing. The arbiter selects:

- `code_analysis` — preserve example values that the bug logic depends on; scrub identifiers
- `narrative` — scrub everything aggressively; reasoning doesn't need real values
- `architecture` — extra-paranoid, also genericizes structural details
- `full` — maximum scrub, for anything sketchy
- `none` — local-only operations, no cloud egress

### Gotchas

- **Co-reference / partial matches.** "Acme Corporation," "ACME," "acmecorp.com" must map to the *same* placeholder, or Claude sees three entities.
- **Structural leaks.** "47 microservices, auth service in Go" has no PII but is uniquely identifying. Regex won't catch this; the operator (you) decides what kinds of analysis to ship.
- **Multi-request correlation.** Parallel fan-out spreads context across N sessions but the composite picture is still one picture. Threat-model accordingly.
- **Code samples** are minefields — hardcoded IPs, internal URLs, account IDs in comments. Run code blocks through a stricter pass.
- **Don't scrub things Claude needs to reason about.** If the bug is *in* IP parsing, replacing all IPs with `{{IP_1}}` removes the analysis target. Hence the profiles.

---

## Tool discovery

Arbiter sees a **fixed manifest** of ~10–20 tool families. Keeps prompt small, improves first-token latency, makes arg extraction reliable on a 4B model.

Cloud Claude instances see the **full MCP surface**. They have the brainpower to navigate dynamic tool discovery; the arbiter doesn't need to.

"Needs MCP-style flexibility" → arbiter routes to `escalate`.

---

## Cloud escalation

### Modes

- **Single** — one Claude instance, full reasoning, sanitized input
- **Parallel** — fan out N instances for compute volume, each gets same sanitized view
- **Offline degrade** — if no connection, fall back to local-only with reduced capability

### Fan-out considerations

- Each instance gets the *same* sanitized payload — no unique tells per session
- Aggregate results locally, rehydrate once, return to user
- Watch for cross-session correlation if instances are long-lived (they shouldn't be — fire and forget)

---

## Stack reference

```
# Detection
presidio-analyzer       # NER + standard PII
detect-secrets          # credentials & tokens
phonenumbers            # international phone parsing
custom regex pack       # IPs, hostnames, MACs, engagement formats

# Tokenization & rehydration
custom Python (~200-300 lines, not a library job)

# Audit
SQLite + age-encrypted backups

# Per-engagement config
YAML loaded at session start, signed with engagement key

# Local LLM
Qwen3 4B (rkllm format) via RKLLama
```

Total scrubber footprint: tens of MB, milliseconds per call, deterministic, auditable.

---

## Build order

1. Stand up RKLLama + Qwen3 4B on whatever Pi-class board you have now (Pi 5 works for prototyping, just slower)
2. Write the dispatcher state machine and the JSON schema for the four-way classifier
3. Build the scrubber as a standalone Python module — test it in isolation against synthetic engagement data
4. Wire arbiter → dispatcher → scrubber → cloud Claude path with Claude Code as upstream
5. Audit-log everything from day one
6. Commit to RK3588 board purchase once RAM and latency budgets are real numbers
7. Wrist hardware last — most fun, least decision-critical

---

## Decisions still open

- Wrist link: BLE vs USB-C tether (latency vs cable management)
- Specific board: Radxa Rock 5C vs Orange Pi 5 Plus (thermal vs PCIe)
- Cellular modem form factor: USB stick vs M.2 card
- Audit log retention policy and encryption-at-rest scheme
- Engagement YAML signing / key management workflow
- Whether to support multiple concurrent engagements (probably not — keep it one-at-a-time)
