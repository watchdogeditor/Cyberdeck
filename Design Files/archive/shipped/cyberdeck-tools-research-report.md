# Cyberdeck — Default Tools Research Report

> **STATUS: ARCHIVED 2026-05-07.** This research report fed into
> `cyberdeck-tools-default-kit.md` v2 (now in `Design Files/in-flight/`).
> Conclusions and recommendations are consumed there. Kept for provenance —
> read if you want the source-by-source analysis behind a v2 kit decision.
> Don't update this doc; the live forward-looking design is the v2 kit.

---

*Critique and extension of `cyberdeck-tools-default-kit.md` v1, against the
2024-2026 state of the offensive-security tooling field. Internet-research
pass: prioritized changes, not exhaustive enumeration. Inputs to a v2
redesign.*

Filed: 2026-04-30. Status: research, no code.

---

## 0. Framing

The v1 draft is a **system-administration toolkit**, not a hacker's
toolkit. It treats security-testing work as one specialty inside
"Linux power user" — wireless gets a category, recon is mostly nmap
plus DNS, and the entire web/cloud/AD attack surface is invisible.
That's the wrong shape for a deck whose tagline is "Kali-shaped use
cases."

The hot-load constraint pulls the design in the opposite direction:
profile-shaped kits, not category-shaped kits. A construct spawned
under `web_pentester` should see ~10 web tools in its prompt, not 80
tools across 7 generic categories of which 60 are noise. The right
v2 mental model is **kits assembled per-profile from a shared tool
library**, with categories as a *taxonomy* on the tool side and
*selectors* on the profile side — not as the unit of installation.

The rest of this report defends that thesis and fills in specifics.

---

## 1. Executive summary

Top 10 changes I'd push into v2, ordered by impact:

1. **Add a `web/` category, and make it big.** The single largest gap
   in the v1 draft. ProjectDiscovery's suite (nuclei, httpx,
   subfinder, katana, dnsx, naabu) is the modern web-pentest backbone
   and is essentially absent. Plus ffuf, sqlmap, feroxbuster,
   gowitness, gau/waybackurls. See §3.1.

2. **Add an `ad/` (Active Directory + internal Windows) category.**
   The internal-network attack surface is huge and the v1 draft has
   nothing for it. Core kit: NetExec (the CrackMapExec successor —
   CME is dead, NetExec is the canonical tool now), Impacket
   (secretsdump, ntlmrelayx, getTGT, etc.), Responder, mitm6,
   BloodHound CE + bloodhound.py, Certipy, kerbrute. See §3.2.

3. **Add a `cloud/` category.** Prowler, ScoutSuite, CloudFox, Pacu,
   Trivy, kubescape. Particularly important because cloud
   pentest content was added to OSCP's 2024-2025 syllabus — the
   industry has caught up. See §3.3.

4. **Reshape `recon/` around the modern stack.** Keep nmap/masscan,
   but ProjectDiscovery's `naabu` + `dnsx` + `subfinder` belong here
   (or in web/ — see §4 on the cross-cutting question), `arp-scan`
   and `mtr` are fine, but `nslookup`/`ifconfig`/`netstat` are
   already removed in your "deliberately omitted" list, which is
   correct. **Add BBOT** as the swiss-army recon orchestrator —
   it's effectively "all of ProjectDiscovery + nmap, glued
   together" and is increasingly the single recon tool that bug
   hunters reach for. See §3 below.

5. **Add `mitmproxy` to whatever category holds HTTP work.** Not in
   v1 at all. It's the right answer for "I need to inspect or
   modify HTTPS traffic from the CLI" and has Python scripting that
   constructs can drive. Wireshark is GUI-only, tshark is for pcaps,
   mitmproxy is for live MITM and request munging — different niche,
   and the missing one in your kit.

6. **Promote `nuclei` to a top-tier tool.** It's not just a web
   scanner — it's the closest thing the open-source world has to a
   "run a battery of detection signatures and tell me what
   matched" engine. 9000+ templates, actively maintained by
   ProjectDiscovery, used for everything from CVE scanning to
   subdomain takeover detection. Even if you only adopt one new
   tool from this report, make it nuclei.

7. **Add a `passwords/` category (or fold cleanly into `crypto/`),
   with hashcat + john + cewl + hash-identifier.** v1 punts on
   hashcat because of GPU dependency; that's fine for *install*
   but the *manifest* should still ship so a construct can hit
   "we have hashes" and reach for the right tool by name. Document
   "GPU optional, will fall back to CPU" — that's the real ergonomic
   shape, not "deferred."

8. **Reshape `osint_researcher` profile around real OSINT tools.**
   v1 names exiftool/whois/dig and that's it. The actual OSINT kit
   in 2025 is theHarvester, sherlock, maigret, recon-ng, SpiderFoot,
   plus the dump-from-archives stack (gau, waybackurls). See §3.5
   and §6.

9. **Drop the `wireless/` opt-in framing — it should be in
   `pentester` by default.** A pentester profile without aircrack-ng
   is a pentester profile that's missing 25% of its job. The opt-in
   gating belongs at the *form-factor* level (does this deck have
   a monitor-mode-capable card?) not at the profile level.

10. **Reshape the kit unit from "profile" to "kit pack."** The hot-
    load constraint means a profile pulling in 7 categories of 10
    tools each = 70 tool manifests in every spawn's system prompt.
    That's catastrophic. v2 should let profiles compose **selected
    tool sets** from the library — `web_pentester` pulls in
    `recon-mini + web-full + crypto-mini`, not `recon + web + crypto`
    in full. See §5.

The remaining 7 sections work through this in detail.

---

## 2. Per-category audit (against v1 draft §3)

### 2.1 `recon/` audit

**Keep:** nmap, masscan, arp-scan, dig, whois, mtr, traceroute,
tcpdump, tshark, ss, ip. These are correctly chosen.

**Add:**

- **`naabu`** (ProjectDiscovery). Faster TCP port discovery than nmap;
  nmap stays for service/version detection. The 2024-2026 modern
  pentest workflow is "naabu (or rustscan) for discovery → nmap
  for fingerprint." See [Medium: RustScan vs Naabu (2025)][naabu].
- **`rustscan`** as alt to naabu — actually *faster* (claims
  65k ports in 3s on local) but less feature-rich. Pick one;
  naabu is the more pentest-pipeline-native of the two and ships
  Kali default. **Recommend naabu.**
- **`subfinder`** (ProjectDiscovery). Passive subdomain enumeration.
  Should arguably live under `web/` instead — it only does
  domains/subdomains. See §4.
- **`dnsx`** (ProjectDiscovery). Fast multi-purpose DNS toolkit:
  bulk resolve, wildcard detection, CNAME chasing, brute-force
  with wordlists. `dig` is single-query; `dnsx` is bulk-mode.
  Both belong; they're complementary.
- **`amass`** — kept on Kali default; comprehensive subdomain
  enumeration (active + passive). **But:** the community has
  largely moved to subfinder for speed and BBOT for depth.
  Amass is fine as a "deep mode" fallback; not first-reach.
- **`BBOT`** (Black Lantern Security). Recursive recon orchestrator.
  Effectively wraps subfinder + nuclei + nmap + many others into
  one CLI with shared state. "BBOT consistently finds 20-50% more
  subdomains than other tools." See [BBOT GitHub][bbot]. Heavy
  but pays for itself. Worth shipping in `pentester`-tier profile
  even if not in the lean default.

**Remove from v1's recon/:**

- *Move to `system/`*: `ss`, `ip`, `lsof`. These are local-machine
  introspection, not network reconnaissance. The category boundary
  is muddy in v1. Recon should be "what's out there"; system should
  be "what's here."

**Keep your "deliberately omitted" list as-is.** All correct calls
(nslookup, netstat, route, wireshark-GUI).

**Manifest density tip:** instead of one manifest per binary,
consider a single `recon/discovery.md` that documents the
"naabu → nmap → service-probe" chain and one per binary for
the long tail. Constructs benefit more from "here is the chain
for X" than from 8 separate boilerplate manifests.

[naabu]: https://medium.com/fmisec/rustscan-vs-naabu-9d7cfbd18424
[bbot]: https://github.com/blacklanternsecurity/bbot

### 2.2 `net/` audit

**Keep:** curl, wget, ncat, socat, ssh/scp/sftp, rsync, mosh,
websocat. Solid set, no objections.

**Add:**

- **`mitmproxy`** / `mitmdump` / `mitmweb`. The Python-scriptable
  HTTPS intercepting proxy. v1 doesn't have anything in this niche.
  The CLI form (`mitmdump`) is what constructs would script
  against. See [mitmproxy 2025 features][mitmproxy].
- **`httpie`** or **`xh`**. Friendlier-output curl alternatives.
  Honestly not necessary — `curl` is fine and constructs already
  know it. **Skip both** unless the netrunner specifically wants
  them; they're a minor convenience, not a capability gap.
- **`yt-dlp`** for media download from URLs. Borderline `media/`,
  but the verb is "fetch" not "transcode." Useful enough to ship.

**Remove:** nothing.

**Reach-for guidance to add:**

- "Inspect or modify HTTPS traffic in flight" → `mitmdump -s
  script.py` with a Python addon. Pair with a capture script
  that logs request/response pairs as JSON for downstream parsing.

[mitmproxy]: https://www.mitmproxy.org/

### 2.3 `data/` audit

**Keep:** jq, yq, ripgrep, fd, miller, qsv, sqlite3, htmlq.
Excellent set. The single best-curated category in v1.

**Add (optional):**

- **`xsv`** stays correctly omitted. ✓
- **`duf`** (df replacement, colorized table) and **`dust`** (du
  replacement, tree-with-bars) — but these belong in `system/`,
  not `data/`. If you ship them, ship them there.
- **`fx`** or **`gron`** for JSON exploration. Skip — `jq` covers
  it, and another tool just bloats prompts.

**No changes needed otherwise.** This category is mature.

### 2.4 `crypto/` audit

**Keep most:** openssl, gpg, age, sha256sum, base64, xxd, jwt-cli.
Good list.

**Add:**

- **`cosign`** if you ever care about container signing — probably
  skip for v1 unless cloud kit lands.
- **`sshuttle`** — VPN-over-SSH. Useful for pentesting through a
  jump host. Belongs in `net/` arguably, not crypto.

**Reshape:** consider whether `passwords/` (hashcat/john/cewl)
should be its own category or a `crypto/` sub-zone. **Recommend
its own category** for v2 — see §3.4. The verbs are different:
`crypto/` is "I have a known input and want a transform"; `passwords/`
is "I have a hash and want to discover the input." Different mental
model, different reach.

### 2.5 `media/` audit

**Keep all:** ffmpeg, imagemagick, exiftool, tesseract, pdftotext,
qpdf, ffprobe. Solid.

**Add (optional):**

- **`pandoc`** for document format conversion. Useful enough that
  netrunners will want it; not a security tool per se.
- **`yt-dlp`** see §2.2.

**No removals.** `pdftk` and `sox` correctly omitted.

### 2.6 `system/` audit

**Keep:** ps, htop, btop, ss (cross-listed), lsof, du, df, journalctl,
strace, iotop. Good set.

**Add:**

- **`dust`** as modern `du` alternative. Tree view with size bars.
  See [opensource.com on dust][dust].
- **`duf`** as modern `df` alternative. Colored table, multi-fs
  view. Borderline; skip if minimizing.
- **`procs`** as modern `ps` — really not necessary; `ps auxf`
  is universal and constructs know it. **Skip.**

[dust]: https://opensource.com/article/21/6/dust-linux

**Move from recon/:** `ss`, `ip`, `lsof` are system tools. Already
flagged.

### 2.7 `wireless/` audit

**Keep:** aircrack-ng suite, kismet, hcxtools, bettercap, bluetoothctl,
hcitool, rtl_433, gqrx, soapy_power. Good list, well-curated.

**Add:**

- **`reaver`** — you correctly call out as niche. Still WPS, still
  niche. Real call: ship in `pentester` because the 10-second
  reach time matters when WPS-enabled APs are in scope.
- **`hcxlabtool`** — newer companion to hcxdumptool. Optional.
- **`wifite`** correctly omitted, but consider that the hot-load
  shape *favors* tools like wifite (one wrapper that does the chain)
  for low-skill profiles. Trade-off: the construct learns "wifite"
  not "hcxdumptool". Stick with the v1 call (omit wifite); the
  prompt-bloat math for one wrapper is worse than the recall
  preservation argument.

**Reshape framing:** "wireless is opt-in via pentester profile" is
the wrong axis. Wireless tools are opt-in based on whether the deck
has the *hardware* — a monitor-mode-capable USB card, an SDR. That's
a form-factor concern (`--profile pentester-with-radio`), not a
"do you do pentesting" concern. Document the form-factor split
explicitly: `pentester-base` (no radio) gets web/ad/cloud, full
`pentester` adds wireless when hardware is present. See §5.

---

## 3. New categories (the case for each)

### 3.1 `web/` — RECOMMEND, large

This is the single biggest hole in v1. Web app pentesting is the
plurality of professional pentest work today; the kit is fairly
canonical and cleanly CLI-driven.

**Proposed kit:**

- **`nuclei`** (ProjectDiscovery). Template-based vulnerability
  scanner with 9000+ community templates. The single most
  important tool in this category. See [ProjectDiscovery
  Nuclei][nuclei].
- **`httpx`** (ProjectDiscovery). Fast HTTP probing — status
  codes, titles, technologies, TLS info, fingerprinting.
  Companion to subfinder/dnsx (resolve → probe → which are
  alive). Note: not the Python `httpx` library; the
  ProjectDiscovery Go binary.
- **`katana`** (ProjectDiscovery). Modern crawler with JavaScript
  parsing, headless browser support, custom scope.
- **`subfinder`** (ProjectDiscovery). Passive subdomain enum.
- **`dnsx`** (ProjectDiscovery). DNS bulk operations.
- **`naabu`** (ProjectDiscovery). Port discovery (cross-list with
  `recon/` — see §4).
- **`ffuf`** (Joohoi). Fast web fuzzer. Directory/file
  enumeration, parameter fuzzing, vhost discovery, POST body
  fuzzing. **Pick over gobuster** — ffuf is more flexible and
  has roughly the same speed in Go. See [pentest-book fuzzer
  comparison][ffuf-vs].
- **`feroxbuster`** as alt to ffuf for *recursive* directory
  discovery. Rust, fast, recursive by default. Pick one as
  default and document when to reach for the other:
  `ffuf` for parameters/headers/POST/general, `feroxbuster`
  for "recursively walk a known site." Recommend: **ship both**;
  prompt cost is small (~600 tokens for two manifests) and
  recall divergence is real.
- **`sqlmap`** — still the canonical SQLi tool. No real
  alternative.
- **`gau`** + **`waybackurls`**. Fetch historical URLs from
  Wayback Machine, CommonCrawl, AlienVault OTX, URLScan. Bug
  bounty staple. Gau is the modern of the two; ship both
  because they're tiny.
- **`gowitness`** or **`aquatone`**. Web-screenshot-at-scale.
  Useful for quick visual triage of "what does this subdomain
  serve."
- **`gitleaks`** + **`trufflehog`**. Secrets scanners. Both —
  Gitleaks for speed/regex coverage, TruffleHog for verification.
  Per [Jit comparison][secrets].
- *Skip:* nikto. "Still useful but every check it does is also
  in nuclei templates, and nikto's Perl noise output is a pain
  for constructs." Old workhorse, supplanted.
- *Skip:* dirb, dirbuster — superseded by ffuf/feroxbuster.
- *Skip:* Burp Suite. GUI-first, scriptable but Java, hot-load
  cost is wrong for a default kit. Note its existence in the
  `web_pentester` profile addendum so a construct knows to
  recommend "open this in Burp" when the netrunner is doing
  manual triage.

**Borderline / opinionated:**

- **`caido`** is the modern Burp alternative — Rust, faster,
  CLI/web-API surface. Worth mentioning in profile addendums
  but the open-source version doesn't have the scanner depth
  to replace nuclei + sqlmap for automated work. Skip from
  default kit; flag for "watch this."
- **OWASP ZAP** as headless scanner. Can run via `zap-cli` /
  `zap.sh -daemon`. Useful when you need a more thorough
  scanner than nuclei but don't have Burp. Skip default —
  Java, big install. Document as "available if installed."

**Reach-for guidance examples:**
- "What subdomains exist for example.com" → `subfinder -d
  example.com -all -silent | dnsx -resp` then `httpx -title -tech-detect`.
- "Audit a known web URL for common vulns" → `nuclei -u
  https://target.com -severity medium,high,critical`.
- "Find hidden directories on a web app" → `feroxbuster -u
  https://target.com -w /usr/share/wordlists/dirb/common.txt`.
- "Test a parameter for SQLi" → `sqlmap -u 'https://target.com/?id=1'
  --batch --level 3`.
- "Fetch historical URLs to find old endpoints" → `gau
  example.com | tee gau.txt` then grep for interesting paths.

[nuclei]: https://github.com/projectdiscovery/nuclei
[ffuf-vs]: https://github.com/six2dez/pentest-book/blob/master/others/web-fuzzers-comparision.md
[secrets]: https://www.jit.io/resources/appsec-tools/trufflehog-vs-gitleaks-a-detailed-comparison-of-secret-scanning-tools

### 3.2 `ad/` — RECOMMEND (Active Directory + internal Windows)

Even larger absence than `web/` for many engagements. Internal
network pentesting against AD is a distinct discipline with a
distinct toolkit.

**Proposed kit:**

- **`netexec`** (formerly CrackMapExec). The Swiss Army knife.
  CME is dead; NetExec is the canonical successor and is
  Kali-default in 2024-2026. SMB/WinRM/LDAP/RDP/MSSQL/SSH/FTP
  protocol coverage, password spraying, pass-the-hash, lateral
  movement, BloodHound integration. See [NetExec successor
  article][netexec].
- **Impacket suite** — Python toolkit. Specifically:
  `secretsdump.py` (DPAPI/SAM/NTDS dump), `ntlmrelayx.py`
  (NTLM relay), `getTGT.py` / `getST.py` (Kerberos ticket
  ops), `psexec.py` / `wmiexec.py` / `smbexec.py` (lateral
  execution), `GetNPUsers.py` / `GetUserSPNs.py` (AS-REP
  roasting / Kerberoasting). One install, dozens of binaries.
- **`responder`** — LLMNR/NBT-NS/mDNS poisoning, captures
  hashes from broadcast.
- **`mitm6`** — IPv6 DHCPv6 poisoning, the modern complement
  to responder for environments that support IPv6.
- **`bloodhound-ce`** + **`bloodhound.py`**. CE is the
  community/free version; bloodhound.py is the remote
  collector (no need to drop SharpHound on a Windows box).
  Kali-shipped as `bloodhound.py-ng` typically.
- **`certipy-ad`** (ly4k/Certipy). ADCS enumeration and
  abuse. ESC1-ESC16 attack chains. The 2024-2025 attack
  surface here keeps growing — Certipy v5 added ESC9-16.
  See [Certipy GitHub][certipy].
- **`kerbrute`** — fast Kerberos pre-auth username enumeration
  and password spraying.
- **`coercer`** — automation around PetitPotam/PrintNightmare/
  DfsCoerce-style authentication coercion. Pairs with ntlmrelayx.
- **`ldapsearch`** + **`ldapdomaindump`** — passive enumeration.
- **`smbclient`** — assumed-available, document anyway.
- **`enum4linux-ng`** — Python rewrite of enum4linux. Still
  useful for SMB/RPC enumeration when not authenticated.
- **`evil-winrm`** — interactive WinRM shell with built-in
  upload/download/AMSI-bypass. Better than `winrs.py`.
- *Skip:* PowerView (PowerShell-only, runs on the target;
  out of band for a Linux deck unless via evil-winrm).
- *Skip:* Mimikatz. Runs on the target; not relevant to deck-
  side tooling. But note its existence in profile addendums
  so the construct knows when to recommend dropping it.

**Reach-for guidance:**
- "I have credentials, what's in the domain" → `netexec ldap
  <DC> -u user -p pass --groups`, then `bloodhound.py -u user
  -p pass -d domain.local -ns <DC>` and ingest into BloodHound.
- "Coerce a relay" → `responder -I eth0` in tab 1, `ntlmrelayx.py
  -t ldaps://<DC> --escalate-user user` in tab 2, `coercer coerce
  -t <victim> -u user -p pass -l <attacker_ip>` in tab 3.
- "Test for Kerberoastable accounts" → `GetUserSPNs.py
  domain/user:pass -dc-ip <DC> -request`.
- "Audit ADCS for misconfigs" → `certipy-ad find -u user@domain
  -p pass -dc-ip <DC>`.

[netexec]: https://www.johnvictorwolfe.com/2024/07/21/the-successor-to-crackmapexec/
[certipy]: https://github.com/ly4k/Certipy

### 3.3 `cloud/` — RECOMMEND

Cloud pentest tooling has matured a lot since 2022 and OffSec added
intro cloud content to the OSCP+ syllabus in late 2024. The deck
should reflect that.

**Proposed kit:**

- **`prowler`** — multi-cloud (AWS/Azure/GCP) CIS benchmark and
  best-practices scanner. Read-only, fast, comprehensive.
- **`scoutsuite`** — multi-cloud security posture review. Generates
  HTML reports. Read-only.
- **`cloudfox`** (Bishop Fox) — AWS+Azure exploitable-attack-path
  enumeration. Complementary to prowler/scoutsuite (those find
  misconfigs; CloudFox finds *paths*).
- **`pacu`** — AWS post-exploitation framework. Modular like
  Metasploit but for cloud. The active-attack tool.
- **`enumerate-iam`** — small AWS IAM permission enumerator.
- **`trivy`** (Aqua Security) — container/IaC/SBOM/secret/vuln
  scanner. Scans Docker images, k8s manifests, Terraform, etc.
  See [Stakater container scanning deep-dive][trivy].
- **`grype`** + **`syft`** (Anchore) — vulnerability scanner +
  SBOM generator. Trivy overlaps; some teams ship both because
  Grype's vuln matching outperforms Trivy's on certain images.
  **Recommend ship trivy default; grype/syft optional in
  pentester-cloud.**
- **`kubectl`** — assumed-available for cloud profile.
- **`kubescape`** — k8s posture scanner (replaces kube-bench
  + kube-hunter for most use cases; kube-hunter has been
  abandoned by Aqua). See [Mattermost k8s tools][k8s].
- **`kube-bench`** — still useful for CIS-Kubernetes-Benchmark
  specifically.
- **AWS CLI / az / gcloud** — assumed-available; document.
- *Skip:* kube-hunter (abandoned).
- *Skip:* enumerate-iam in lean kit; redundant with cloudfox/pacu.

[trivy]: https://www.stakater.com/post/open-source-container-security-a-deep-dive-into-trivy-clair-and-grype
[k8s]: https://mattermost.com/blog/the-top-7-open-source-tools-for-securing-your-kubernetes-cluster/

**Reach-for guidance:**
- "Audit my AWS account for CIS misconfigs" → `prowler aws --severity
  high,critical`.
- "What can this AWS access key actually do" → `pacu`, then run
  enumeration modules.
- "Find attack paths from this IAM role" → `cloudfox aws
  --profile <p> all-checks`.
- "Scan this Docker image for CVEs" → `trivy image <image:tag>`.

### 3.4 `passwords/` — RECOMMEND (split from crypto/)

v1 punts on hashcat citing GPU. That's wrong: shipping the manifest
is cheap and the construct should know the verb exists even if the
deck-local backend is CPU-only. The category is small but
self-contained and merits its own folder.

**Proposed kit:**

- **`hashcat`** — GPU-accelerated. CPU mode works but is slow.
  Document "if no GPU, you'll only crack short / well-targeted
  hashes." Still ships.
- **`john`** — John the Ripper. Better hash-format auto-detection
  and CPU-first. Faster off-the-shelf for common formats; loses
  on speed-per-watt to hashcat at scale.
- **`hash-identifier`** / **`hashid`** — identify hash type from
  string. Tiny, useful.
- **`cewl`** — custom wordlist generator from a website. Spider
  a target's site, build a domain-tailored wordlist. Bug bounty
  staple.
- **`crunch`** — pattern-based wordlist generator. Use sparingly
  (mask attacks via hashcat are usually better).
- **`mentalist`** GUI — skip.
- *Already-listed-elsewhere reminder:* `hcxpcapngtool` (in
  wireless) extracts hashes from pcaps for hashcat input.

### 3.5 `osint/` — RECOMMEND (replaces or augments osint_researcher)

v1's osint_researcher profile is thin (exiftool + whois + dig).
The actual OSINT field has rich tooling.

**Proposed kit:**

- **`theHarvester`** — email/subdomain/IP gathering from search
  engines, PGP, certs, etc. Staple.
- **`sherlock`** — username search across ~400 social platforms.
  One-shot lookup tool.
- **`maigret`** — sherlock-on-steroids. Deeper investigation,
  finds related accounts, checks ~3000 sites. Slower but
  thorough.
- **`recon-ng`** — modular recon framework. Marketplace of
  modules for various data sources.
- **`spiderfoot`** — automated OSINT platform. ~200 data
  sources. Web UI primary; has a CLI mode (`sf.py -s`).
  Heavy; ship in `osint`-tier profile only.
- **`exiftool`** — already in `media/`. Cross-list.
- **`whois`** / **`dig`** — already in `recon/`. Cross-list.
- **`dnstwist`** — domain typosquatting / phishing-domain
  detection.
- **`gallery-dl`** — image scraper from social platforms.
- **`yt-dlp`** — useful for archival.
- **`gron`** — JSON to grep-friendly format. Helpful for OSINT
  data manipulation. Optional.
- *Skip:* twint (Twitter scraper) — broken since 2023 due to
  Twitter API changes. There is no clean modern equivalent.
- *Skip:* SocialAnalyzer — overlap with maigret.

**Reach-for guidance:**
- "Find email addresses for example.com" → `theHarvester -d
  example.com -b all`.
- "Is this username on social platforms" → `sherlock <username>`
  for fast, `maigret <username>` for thorough.
- "Investigate a person across the web" → start `spiderfoot
  -s <target>` then triage.

### 3.6 `exploit/` — TENTATIVELY SKIP

Binary exploitation / RE is a specialty that doesn't fit the
hot-load model well. The relevant tools are:

- **`gdb`** + **`gef`** or **`pwndbg`** — gdb extensions. Both
  good; gef is more actively maintained, pwndbg is faster.
  Pick gef.
- **`pwntools`** — Python exploitation framework.
- **`ROPgadget`** — gadget finder.
- **`checksec`** (`pwntools` includes; also standalone). Binary
  protection inspector.
- **`radare2`** / **`r2`** — CLI RE framework.
- **`ghidra`** — GUI; CLI via headless mode (`analyzeHeadless`).
  Big install (Java).
- **`one_gadget`** — libc shortcut finder.
- **`xxd`** / **`objdump`** / **`readelf`** / **`nm`** /
  **`strings`** / **`file`** — binutils/coreutils, assumed-available.

**Recommendation: skip from default install.** Add an
`exploit_dev` profile addendum that *names these tools* without
their manifests bloating every spawn. Constructs working in
this niche should pull the tools on-demand. The audience for
this kit is small enough that opt-in install is correct.

If you do ship a category, keep it lean: `gef`, `pwntools`,
`ROPgadget`, `checksec`, `radare2`, plus an addendum for ghidra
("Ghidra is available; invoke via `analyzeHeadless` for
scripting"). 5 manifests max.

### 3.7 What about `dev/`?

v1 correctly drops it. Git/make/language toolchains are assumed.
The only debate is whether `gh` (GitHub CLI) belongs anywhere —
useful for the recon/data-leak side ("who has forked this repo,
what secrets are in old commits"). Recommend: ship in `osint/`
or as a standalone tool, not as a category.

---

## 4. Hot-load implications

The constraint reshapes everything. v1's seven categories model the
*kit* as a thing the deck installs once. The hot-load model says
the kit is a thing **assembled per-spawn from a library**.

### 4.1 Token budget reality

Rough numbers, conservative:
- A tool manifest is 300-800 tokens once you include name,
  description, args, output schema, side-effects, when-to-use.
- 10 manifests = 3-8K tokens. 20 manifests = 6-16K. 50 manifests
  = 15-40K — that's wholly impractical, and even 20 is steep when
  the construct's *actual task* is the part that matters.

So the right ceiling is roughly:
- **Lean profile (data_analyst, code_reviewer):** ~5-8 manifests.
- **Specialty profile (web_pentester, ad_operator):** ~10-15
  manifests. The bigger budget reflects bigger task surface.
- **Generalist profile (pentester):** ~15-20 manifests, hard cap.
  Beyond this, the construct's instruction-following degrades
  before its capability does.

### 4.2 Manifest density tactics

- **Tier descriptions by reach frequency.** First-reach tools get
  full manifests; second-reach tools get one-line entries
  ("`gau` — historical URL fetch from Wayback. See `gau --help`").
  The construct can read help on demand; the manifest just needs
  to put the tool *in the construct's vocabulary*.
- **Compress "when-to-use" into reach-for tables.** v1's per-
  category tables are excellent and the right compression. One
  table is more dense than 10 prose paragraphs.
- **Group related tools into combo manifests.** Instead of 6
  ProjectDiscovery manifests, ship one `projectdiscovery_suite.md`
  that documents the chain (`subfinder | dnsx | httpx | naabu |
  nuclei | katana`) with each binary's role in 2 lines. Probably
  saves 60% of tokens vs 6 individual manifests.
- **Make `default_scripts` the right granularity.** A script that
  composes 3 tools into a workflow is one manifest's worth of
  prompt cost but exposes the right verb. v1's `scan_subnet.sh`
  and `pdf_to_text.sh` examples are exactly right; widen this.

### 4.3 Profile-as-kit-assembly

v2 should let a profile declare:

```toml
[kit]
include = ["recon/discovery", "web/projectdiscovery_suite", "web/ffuf",
           "web/sqlmap", "web/secrets", "data/jq"]
```

…rather than `categories = ["recon", "web", "data"]`. The sets
referenced are named manifest groups, not folders. This lets
`web_pentester` and `ad_operator` share a `recon/discovery` group
without each pulling in the whole `recon/` folder.

---

## 5. Profile templates worth adding

v1 drafts: `data_analyst`, `osint_researcher`, `pentester`. Add:

### 5.1 `web_pentester`
**Tools:** subfinder, dnsx, httpx, naabu, nuclei, katana, ffuf,
feroxbuster, sqlmap, gau, gowitness, mitmproxy, gitleaks. (~13
manifests.)

**Addendum sketch:** "You operate against authorized web targets.
Reach for ProjectDiscovery's chain (subfinder → dnsx → httpx →
nuclei) for breadth. Use ffuf for parameter/header fuzzing,
feroxbuster for recursive directory walks. SQLMap for SQLi. Use
mitmproxy when you need to observe or modify live traffic. Always
confirm scope before scanning; rate-limit nuclei with `-rate-limit
30` against production targets."

### 5.2 `ad_operator`
**Tools:** netexec, impacket-suite, responder, mitm6, bloodhound.py,
certipy-ad, kerbrute, evil-winrm, ldapdomaindump. (~9 manifests but
impacket is multi-binary.)

**Addendum sketch:** "Authorized internal-network engagement.
NetExec is the swiss-army; reach for it first for protocol
enumeration, password spray, lateral movement. Pair Responder +
ntlmrelayx + coercer for relay attacks. Always document the path:
initial access → enumeration → BloodHound graph → privilege
escalation → DA. Avoid noisy actions during business hours unless
scope says otherwise."

### 5.3 `cloud_auditor`
**Tools:** prowler, scoutsuite, cloudfox, pacu, trivy, kubescape,
gitleaks. (~7 manifests.)

**Addendum sketch:** "Cloud-environment review. Read-only by
default; pacu for active actions only with explicit scope. Prowler
for CIS audit, ScoutSuite for posture review, CloudFox for attack
paths, Trivy for container/IaC scanning. Surface findings as
account-id + service + finding-ref + severity, not as raw tool
output."

### 5.4 `bug_hunter`
Lighter than full pentester; bug-bounty-shaped.

**Tools:** subfinder, dnsx, httpx, naabu, katana, gau, waybackurls,
nuclei, ffuf, gowitness, sqlmap, gitleaks, gf (pattern matcher),
anew. (~13 manifests.)

**Addendum sketch:** "Bug bounty engagement. Stay within scope on
the program's policy page. Prefer passive recon (subfinder, gau,
waybackurls) before active. Use nuclei templates that match the
program's tech stack. Document each finding with reproduction
steps and impact. Never DoS, never social-engineer, never test
out-of-scope assets."

### 5.5 `dfir_responder`
Defensive / forensics shape. Optional.

**Tools:** chainsaw, hayabusa, exiftool, strings, yara,
volatility3, plaso, wireshark/tshark, sleuthkit (`fls`/`icat`),
sqlite3. (~10 manifests.)

**Addendum sketch:** "DFIR / threat-hunting context. You analyze
artifacts, you do not interact with live attacker infrastructure.
Hayabusa + chainsaw for Windows event logs (Sigma rule support).
Plaso for timeline. Volatility for memory. exiftool for file
metadata. Always preserve original artifact integrity; work on
copies."

### 5.6 `exploit_dev` (lightweight)

**Tools:** gef, pwntools, ROPgadget, checksec, radare2,
strings/file/objdump (assumed-available, named in addendum).
(~5 manifests + assumed-available list.)

**Addendum sketch:** "Binary analysis / exploit development.
Always start with `checksec` to know what mitigations apply.
Use radare2 / `r2` for static, gdb+gef for dynamic. Pwntools for
exploit scaffolding. ROPgadget when NX is enabled. Document
each step: target binary, mitigations, attack primitive,
exploitation technique."

---

## 6. Modern tools likely missed (one-line pitches)

Tools rated "should consider" that didn't get into the per-category
audits:

- **`xsv`** — *correctly* skipped per v1; supplanted by `qsv`. ✓
- **`anew`** (tomnomnom) — append-unique stdin to file. Trivial
  but ubiquitous in bug-bounty pipelines. Ship in `web/`.
- **`gf`** (tomnomnom) — pattern grep with prebuilt patterns
  (XSS, SSRF, AWS keys, etc.). Ship in `web/`.
- **`unfurl`** (tomnomnom) — extract bits of URLs (host, path,
  parameters). Ship in `web/`.
- **`httprobe`** (tomnomnom) — older, supplanted by httpx; skip.
- **`assetfinder`** — older subfinder-shaped tool; supplanted
  but lightweight enough that some workflows still ship it. Skip.
- **`gospider`** — older crawler; supplanted by katana. Skip.
- **`hakrawler`** — alternative crawler; supplanted by katana.
  Skip.
- **`subzy`** — subdomain takeover scanner. Niche; ship in
  `bug_hunter` profile.
- **`puredns`** — fast DNS resolver/bruter. Heavy and faster than
  dnsx for pure resolution; ship in `bug_hunter` profile.
- **`amass intel`** — Amass mode for OSINT collection (orgs,
  ASNs, etc.). Useful in `osint/`.
- **`bbot`** — already covered above. Worth shipping in pentester.
- **`yamllint`** / **`shellcheck`** / **`hadolint`** — linting,
  not security; skip.
- **`semgrep`** — SAST. Ship for `code_reviewer` profile (its own
  category? "audit"?). Open question.
- **`codeql`** — also SAST; needs database build, heavy. Skip.
- **`xbow`** — autonomous AI pentester demoed at Black Hat 2025;
  proprietary, skip.
- **`pentest-gpt`** / **`pentagi`** — AI pentest agents. Watch this
  space (your deck *is* this space, basically). Skip from kit;
  worth a section in design docs about overlap.
- **`garak`** — NVIDIA's LLM vulnerability scanner. Ship in
  `llm_red_team` if/when that profile makes sense.
- **`pyrit`** — Microsoft AI red-teaming. Ship in `llm_red_team`.
- **`mssqlpwner`** (Kali 2024.4 default) — MSSQL pentest. Ship in
  `ad/` or its own `db/` micro-category.
- **`xsrfprobe`** (Kali 2024.4 default) — CSRF audit. Ship in `web/`.
- **`hexwalk`** (Kali 2024.4 default) — hex viewer/editor. Useful
  in `data/` or `exploit/`.
- **`linkedin2username`** (Kali 2024.4 default) — corp username
  generation from LinkedIn pages. Ship in `osint/` or `ad/`.

---

## 7. Tools in v1 that are losing favor or obsolete

Be unsparing. Things to reconsider:

- **`magick` / `convert` (ImageMagick)** — *not* obsolete, but
  kept on Kali; security-tooling perspective: still useful.
  No removal.
- **`hcitool`** — deprecated upstream by BlueZ since ~2017
  (technically still works on most distros via the
  bluez-deprecated-tools package on Debian). Replace with
  `bluetoothctl` and `btmgmt` from bluez-tools. **Drop hcitool.**
- **`telnet`** — correctly omitted. ✓
- **`ftp`** — correctly omitted. ✓
- **`netstat` / `ifconfig` / `route`** — correctly omitted. ✓
- **`nslookup`** — correctly omitted. ✓
- **`hexdump`** — correctly omitted in favor of xxd. ✓
- **`sox`** — correctly omitted. ✓
- **`pdftk`** — correctly omitted. ✓
- **`xsv`** — correctly omitted in favor of qsv. ✓
- **CrackMapExec** — *implicitly absent from v1; absolutely
  do not ship CME if you ever planned to.* It's dead. Use
  NetExec.
- **`kube-hunter`** — abandoned. If you build cloud kit, skip.
  Kubescape covers most use cases.
- **`wfuzz`** — not formally deprecated but supplanted by ffuf.
  Skip unless you want it for legacy parity.
- **`gobuster`** — actively maintained but ffuf and feroxbuster
  cover its turf better. Skip from default; mention in
  addendum as alt.
- **`dirb` / `dirbuster`** — superseded. Skip.
- **`nikto`** — old workhorse, but every check is in nuclei
  templates, and the Perl output is hostile to construct
  parsing. Skip from default; document as "available."
- **`twint`** — broken since 2023. Skip.
- **`enum4linux`** (original Perl) — replaced by enum4linux-ng
  (Python rewrite, actively maintained). Use the -ng version.
- **`amass`** for *passive subdomain enumeration alone* — slower
  than subfinder for that specific job. Keep amass for "deep
  mode" (active + brute), reach for subfinder otherwise. Not
  a "drop"; a "demote."

---

## 8. Open questions for the netrunner

These are calls that genuinely depend on workflow preference; v2
should pick *one* answer to each, not both.

1. **subfinder/naabu/dnsx/httpx — `recon/` or `web/`?** They're
   genuinely cross-cutting. Recommend: ship them in *both*
   category READMEs (cross-listed) but the manifest source-of-
   truth is `web/projectdiscovery_suite.md`. The category folder
   structure becomes a hint to humans browsing the Tools panel,
   not a constraint on profile composition.

2. **Default vs pentester gating for the heavier categories.**
   Should `desktop` ship `web/` and `ad/` and `cloud/`? Three
   options: (a) yes, full kit on every desktop install; (b) no,
   pentester-only; (c) prompt at install time. Recommend (a)
   for the netrunner persona — this is a hacker's deck. The
   *prompt* cost is per-spawn-per-profile, not per-install, so
   shipping more on the disk costs nothing the prompt-bloat
   model cares about.

3. **MCP servers as `web/`-tier capabilities.** ProjectDiscovery
   ships an MCP server for nuclei. Could a construct call
   `nuclei` via MCP rather than via Bash? Probably yes, but it
   changes the prompt-cost math (MCP tool descriptions are
   structured and might be cheaper). Worth a v3 conversation;
   keep v2 Bash-first.

4. **AI-augmented tooling: integrate or wait?** Tools like
   PentestGPT, Pentagi, XBOW are agentic pentest engines —
   they overlap with what the cyberdeck *is*. Recommend: skip
   from the default kit, but the deck's design doc should have
   a "competitor / sibling" section noting them. The cyberdeck
   differentiator is the keyboard-first supervision UI, not the
   underlying agentic engine.

5. **Wireless category install gating.** The form-factor cut
   (`pentester-base` vs `pentester-with-radio`) is the right
   axis but adds installer complexity. Alternative: ship
   wireless tools always, document that they require a
   monitor-mode-capable card to be useful. Constructs running
   on a deck without one will get clean "no monitor-mode
   interface available" errors. Recommend: ship by default
   in pentester profiles; let the failure mode be the
   gating.

6. **Manifest format granularity.** v1's per-binary TOML is fine,
   but a per-suite README (e.g. `web/projectdiscovery_suite.md`)
   with all the ProjectDiscovery binaries documented in one
   doc is *probably* the better unit for hot-load economy.
   Open question: does the deck's tools panel render
   per-manifest entries or per-README entries? They imply
   different file structures.

7. **Burp Suite stance.** GUI-first, but bug bounty workflow
   often requires it. Options: (a) skip entirely, document
   in profile addendum that Burp exists; (b) ship a thin
   `burp_launch.sh` script that surfaces it as a tool entry
   even though it's GUI-driven; (c) full integration via
   Burp's REST API (Burp Pro only). Recommend (a) — the deck
   is CLI-first, the GUI use is the netrunner's separate
   concern.

8. **`exploit/` category — ship or skip?** Recommend skip from
   default but include an `exploit_dev` profile addendum that
   names the tools without shipping their manifests. The
   audience is small and the prompt-bloat cost is real.

9. **What about a `mobile/` category?** APK/IPA reverse
   engineering: jadx, apktool, frida, objection. Niche but
   distinct from `exploit/`. Recommend: defer; add a
   `mobile_pentester` profile if a use case lands.

10. **Should the wearable form-factor get a different default
    set than desktop?** The v1 matrix has `wearable` ≈ desktop
    minus heavy media. Recommend: also drop `cloud/` and
    `exploit/` from wearable (heavy installs, Java for
    Ghidra, etc.). Wearable focus is recon + web + ad +
    wireless when hardware exists.

11. **Where does `gh` (GitHub CLI) belong?** Cross-cutting:
    OSINT (find leaked data), code review (PR ops), data
    analysis (repo metadata). Recommend: ship as a top-level
    tool (`/tools/gh.md`) outside any category, similar to
    how `git` is assumed.

---

## 9. Minor refactor suggestions

Mostly editorial / organizational.

- **The seven-category v1 axis is "verb-shaped".** The new
  categories I'm proposing are "domain-shaped" (web, ad, cloud).
  This is an ambiguity worth resolving in v2 — pick one axis,
  or be explicit that there are two axes (verb-categories like
  `data/`, `crypto/`; domain-categories like `web/`, `cloud/`).
  Recommend: own the duality. Tag manifests with both
  `verb_tags = ["network-fetch"]` and `domain_tags = ["web"]`
  so profiles can compose by either dimension.

- **The `requires` field in the manifest schema (v1 §4) is
  great** — let the deck warn at startup if binaries are
  missing. Extend to `requires_hardware = ["monitor_mode_wifi",
  "gpu"]` for tools that need physical capabilities. Wireless
  tools warn cleanly on a deck without a USB radio; hashcat
  warns on a CPU-only deck.

- **The `side_effects` field could be sharper.** v1 has
  "none/filesystem/network/destructive". Add `network_active`
  vs `network_passive` because the brake hook semantics are
  different — a paranoid brake should let `whois example.com`
  through but block `nuclei -u target.com`. Both are
  "network", but only one touches the target.

- **The `output_schema` field is excellent for constructs**
  but should probably also include a *sample output* line —
  one realistic example of what the construct will see.
  "Schema is for compile-time, sample is for inference-time."

---

## 10. Final recommendation: v2 shape

If I were rewriting the doc:

1. **Drop the install-profile / category coupling.** Categories
   become taxonomy on the tool side. Install profiles become
   axes (form-factor: desktop / wearable / minimal; specialty:
   pentester / dfir / cloud / web / data).

2. **The default desktop install is generous.** Disk is cheap;
   prompt budget is the constraint. Ship everything except the
   exploit/mobile/wireless niches by default.

3. **Profiles compose kits explicitly.** A profile names manifest
   groups, not categories. This is the central tactical change.

4. **Manifest groups are the unit of authoring.** Some are
   single-tool (`crypto/age.md`), some are multi-tool
   (`web/projectdiscovery_suite.md`). Pick the granularity that
   compresses the prompt best for that domain.

5. **Categories proposed for v2:** `recon/`, `net/`, `data/`,
   `crypto/`, `passwords/`, `media/`, `system/`, `web/`, `ad/`,
   `cloud/`, `osint/`, `wireless/`, plus an `assumed/` README
   that lists git/curl/python/etc. Twelve categories, but each
   is small enough that its README fits on one screen, and
   most profiles only pull from 3-5.

6. **Profile templates to ship:** `data_analyst`, `code_reviewer`,
   `osint_researcher`, `bug_hunter`, `web_pentester`,
   `ad_operator`, `cloud_auditor`, `pentester` (generalist),
   `dfir_responder`, optional `exploit_dev`.

The v1 draft is ~70% of the way to a strong design. The shape
fixes (web/ad/cloud categories, kit-as-composition, tightened
osint) are the remaining 30%. Internal `data/`, `crypto/`,
`media/`, `system/`, and `net/` choices are excellent and need
no rework.

---

## Sources

- [Kali Linux Metapackages | Kali Linux Documentation](https://www.kali.org/docs/general-use/metapackages/)
- [Major Metapackage Makeover | Kali Linux Blog](https://www.kali.org/blog/major-metapackage-makeover/)
- [Kali Linux 2024.4 release notes (BleepingComputer)](https://www.bleepingcomputer.com/news/security/kali-linux-20244-released-with-14-new-tools-deprecates-some-features/)
- [ProjectDiscovery / Nuclei (GitHub)](https://github.com/projectdiscovery/nuclei)
- [ProjectDiscovery suite guide (Vercel.land)](https://www.vercel.land/blog/projectdiscovery-security-toolkit-comprehensive-guide)
- [ProjectDiscovery / Naabu (GitHub)](https://github.com/projectdiscovery/naabu)
- [ProjectDiscovery / dnsx (GitHub)](https://github.com/projectdiscovery/dnsx)
- [ProjectDiscovery / Alterx blog](https://projectdiscovery.io/blog/introducing-alterx-simplifying-active-subdomain-enumeration-with-patterns)
- [NetExec successor article (John Victor Wolfe, 2024)](https://www.johnvictorwolfe.com/2024/07/21/the-successor-to-crackmapexec/)
- [NetExec / NXC successor (B00t2R00t)](https://h3ll-ka1ser.gitbook.io/boot2root/tools/active-directory/netexec/resources-and-credits)
- [NetExec cheat sheet (Route Zero, 2025)](https://routezero.security/2025/04/06/netexec-formerly-crackmapexec-cheat-sheet-for-penetration-testers/)
- [BloodHound CE practical guide (Hive Security)](https://hivesecurity.gitlab.io/blog/bloodhound-practical-guide-ad-attack-paths/)
- [Certipy GitHub (ly4k)](https://github.com/ly4k/Certipy)
- [ADCS abuse with Certipy (Hive Security 2025)](https://hivesecurity.gitlab.io/blog/adcs-abuse-certipy-esc1-esc8-attack-chains/)
- [Cloud pentesting tools 2025 (Deepstrike)](https://deepstrike.io/blog/best-tools-for-cloud-penetration-testing-in-2025)
- [BishopFox / cloudfox GitHub](https://github.com/BishopFox/cloudfox)
- [Stakater container scanning (Trivy/Grype/Clair)](https://www.stakater.com/post/open-source-container-security-a-deep-dive-into-trivy-clair-and-grype)
- [Mattermost K8s security tools](https://mattermost.com/blog/the-top-7-open-source-tools-for-securing-your-kubernetes-cluster/)
- [Web fuzzer comparison (six2dez pentest-book)](https://github.com/six2dez/pentest-book/blob/master/others/web-fuzzers-comparision.md)
- [Feroxbuster (Kali Tools)](https://www.kali.org/tools/feroxbuster/)
- [BBOT GitHub (Black Lantern Security)](https://github.com/blacklanternsecurity/bbot)
- [BBOT recursive scanner blog](https://blog.blacklanternsecurity.com/p/bbot)
- [TruffleHog vs Gitleaks comparison (Jit)](https://www.jit.io/resources/appsec-tools/trufflehog-vs-gitleaks-a-detailed-comparison-of-secret-scanning-tools)
- [Caido vs Burp Suite (AFINE)](https://afine.com/blogs/caido-vs-burp-suite-a-penetration-testers-comparison)
- [mitmproxy site](https://www.mitmproxy.org/)
- [mitmproxy GitHub](https://github.com/mitmproxy/mitmproxy)
- [OSINT tools 2025 (Axis Intelligence)](https://axis-intelligence.com/top-10-osint-tools-in-2025-review/)
- [OSINT playbook 2025 (Andrea Fortuna)](https://andreafortuna.org/2025/04/07/the-osint-playbook-essential-tools-and-tutorials-for-every-analyst/)
- [Hayabusa GitHub (Yamato Security)](https://github.com/Yamato-Security/hayabusa)
- [Chainsaw GitHub (WithSecure Labs)](https://github.com/WithSecureLabs/chainsaw)
- [Hashcat vs John the Ripper (Computingforgeeks)](https://computingforgeeks.com/password-cracking-hashcat-john-kali/)
- [OSCP+ exam guide (OffSec)](https://help.offsec.com/hc/en-us/articles/360040165632-OSCP-Exam-Guide)
- [OSCP curriculum 2025 (Axxim Info Solutions)](https://www.axximuminfosolutions.com/article/oscp-course-details-2025/)
- [C2 frameworks 2025-2026 (AlphaHunt)](https://blog.alphahunt.io/modular-c2-frameworks-quietly-redefine-threat-operations-for-2025-2026/)
- [RustScan vs Naabu (Medium)](https://medium.com/fmisec/rustscan-vs-naabu-9d7cfbd18424)
- [AI pentest tools 2025 (Mindgard)](https://mindgard.ai/blog/top-ai-pentesting-tools)
- [Bug bounty methodology 2025 (GitHub)](https://github.com/amrelsagaei/Bug-Bounty-Hunting-Methodology-2025)
- [Modern coreutils replacements (It's FOSS)](https://itsfoss.com/legacy-linux-commands-alternatives/)
- [dust GitHub / opensource.com guide](https://opensource.com/article/21/6/dust-linux)
- [Black Hills Impacket cheatsheet](https://www.blackhillsinfosec.com/impacket-cheatsheet/)
