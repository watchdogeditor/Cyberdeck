"""
models_registry.py — disk-backed catalog of models the daemon picks
from at spawn time.

Build-plan item 13 follow-on (2026-05-11; first step of the local-model
substrate migration that became urgent after Anthropic's 2026-06-15
SDK-credit-pool change). Pairs with quota_reader / cost tracking
(near-future) / hardware-resource awareness (tomorrow's slice).

The catalog is the source of truth for:
  - Which models the daemon may reference in spawn actions
  - Each model's provider (which backend adapter handles it)
  - Each model's COST in dollars per million tokens (informs the
    CREDIT signal + daemon's caliber decisions)
  - Each model's RESOURCE requirements (informs spawn-time guardrails
    for local-provider models — tomorrow's slice will enforce these
    against the deck's live resource snapshot)
  - Each model's POWER rating — netrunner's calibrated capability
    rank, used by the daemon to compare similar-tier models when
    constraints make one preferable
  - Per-effort sub-tables: when a model supports effort levels,
    each gets its own description + power value + api_effort string

Lifecycle: load-once-at-startup, no hot reload. Editing the catalog
mid-flight would produce confusing half-applied behavior; the
netrunner edits the file, restarts, picks it up. Same discipline as
roles/*.md and general.toml.

Default-restoration: if `models.toml` is missing OR effectively empty
(only comments + whitespace), the registry rewrites it from
DEFAULT_MODELS_TOML. The bundled default content seeds the file
fresh; the netrunner edits from there. The comment-only-equals-empty
detection means the netrunner can wipe the file's content to reset
without losing the schema reference.

Location: `<deck-source>/roles/models.toml` — same folder as
general.toml + the role .md files. "All prompt-affecting configs in
one place" was the netrunner's call (2026-05-11).

Public surface:
  ModelEffort       Frozen dataclass: effort name + power + description + api_effort.
  ModelRequirements Frozen dataclass: hardware requirements for local providers.
  Model             Frozen dataclass: one catalog entry + its effort tables.
  ModelLoadError    Raised on catastrophic load failure.
  ModelsRegistry    The loader; load() / get(name) / all() / has_provider(name).
"""
from __future__ import annotations

import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# Bundled default catalog. Replicates the deck's pre-catalog
# behavior — three Anthropic entries (haiku / sonnet / opus) with
# their effort variants and rough cost estimates as of 2026-05-11.
# Cost values may drift as Anthropic adjusts pricing; the netrunner
# updates them by editing the file.
#
# Commented-out templates for the three pending adapters (ollama,
# openrouter, private) live below. The netrunner uncomments + tunes
# when the corresponding backend ships.
#
# Power values are calibration, not science. The netrunner adjusts
# them as real-deck data informs better defaults.
DEFAULT_MODELS_TOML: str = """\
# Cyberdeck — models catalog.
#
# This file is the source of truth for every model the daemon may
# pick from when spawning constructs. Edit freely between launches;
# restart picks up changes. (Hot reload is intentionally off — a
# half-applied model catalog mid-flight produces confusing behavior.)
#
# Save this file with everything below this comment block deleted
# (or delete the file entirely) and the deck will restore the
# bundled defaults on the next launch.
#
# ============================================================
# SCHEMA
# ============================================================
#
# Each [[model]] entry defines one model the daemon may reference
# by `name` in spawn actions. Required fields:
#
#   name              str  slug daemon uses (e.g., "haiku")
#   power             num  capability rank summary — netrunner's
#                          calibrated opinion. 1 = fastest/cheapest,
#                          higher = more capable. Each effort below
#                          ALSO has a power value — the daemon ranks
#                          (model, effort) pairs by effort.power.
#                          model.power exists for at-a-glance summaries
#                          and sanity-check the netrunner can scan.
#   provider          str  backend adapter. Day-1 supported:
#                            "anthropic-sdk" (current claude -p path)
#                          Future adapters (when backends/*.py ship):
#                            "ollama" / "openrouter" / "private"
#   api_model         str  what the adapter passes to the provider's API
#   description       str  one-line summary shown to the daemon
#   use_cases         str  pipe-separated task shapes; daemon's main
#                          relevance-matching signal alongside power
#   cost_per_1m_input num  USD per 1M input tokens. 0.0 for local.
#   cost_per_1m_output num USD per 1M output tokens. 0.0 for local.
#   network_required  bool true for cloud; false for local. Daemon
#                          consults CONNECTION line in user message
#                          before spawning network-required models.
#
# [model.requirements] subtable — hard checks at spawn time for
# local-provider models. Cloud models set requirements to zero/false.
#
#   min_ram_free_gb         num GB free RAM needed at spawn time
#   min_disk_free_gb        num GB free on the deck-source mount
#   needs_gpu               bool true if model requires a GPU
#   min_gpu_vram_free_gb    num required free VRAM (when needs_gpu=true)
#   max_concurrent_local    int max simultaneous spawns of this model.
#                               0 = unbounded (cloud); >0 = local cap
#   typical_tokens_per_sec  int rough output speed for latency expectations
#
# [model.effort.<level>] subtable — one per supported effort level
# for this model. Daemon picks (model, effort) jointly.
#
#   power         num  total effective capability at this effort.
#                      This is the value the daemon ranks against
#                      when comparing models — encodes both the
#                      model's base capability AND the effort's
#                      contribution to it.
#   description   str  natural-language explanation of WHEN to pick
#                      this effort on this model
#   api_effort    str  what the adapter passes as the effort flag
#
# ============================================================

[[model]]
name = "haiku"
power = 1.0
provider = "anthropic-sdk"
api_model = "claude-haiku-4"
description = "Anthropic Haiku — fast, cheap, narrow tasks."
use_cases = "parallel recon · classification · single-file lookups · text transforms · cheap parallel work"
cost_per_1m_input = 1.00
cost_per_1m_output = 5.00
network_required = true

[model.requirements]
min_ram_free_gb = 0.0
min_disk_free_gb = 0.0
needs_gpu = false
min_gpu_vram_free_gb = 0.0
max_concurrent_local = 0
typical_tokens_per_sec = 60

[model.effort.low]
power = 1.0
description = "Default on haiku. Most efficient; minimal reasoning. Use for tightly-scoped tasks paired with explicit checklists."
api_effort = "low"

[model.effort.medium]
power = 1.2
description = "Balanced. Use when haiku is the right tier but the task has more than one step."
api_effort = "medium"

[model.effort.high]
power = 1.5
description = "Haiku ceiling. Use when haiku is the right model but the work needs care; if quality matters more, bump to sonnet+low instead."
api_effort = "high"


[[model]]
name = "sonnet"
power = 2.0
provider = "anthropic-sdk"
api_model = "claude-sonnet-4-6"
description = "Anthropic Sonnet 4.6 — versatile, everyday default for routine work."
use_cases = "multi-file recon · structured reports · routine implementation · code review · most agentic work"
cost_per_1m_input = 3.00
cost_per_1m_output = 15.00
network_required = true

[model.requirements]
min_ram_free_gb = 0.0
min_disk_free_gb = 0.0
needs_gpu = false
min_gpu_vram_free_gb = 0.0
max_concurrent_local = 0
typical_tokens_per_sec = 50

[model.effort.low]
power = 2.0
description = "Sonnet with minimal reasoning. Cheaper variant for routine work that needs sonnet's breadth but not depth."
api_effort = "low"

[model.effort.medium]
power = 2.3
description = "Default routine sonnet work. Balanced cost/capability."
api_effort = "medium"

[model.effort.high]
power = 2.7
description = "Sonnet sweet spot. Strong reasoning + token efficiency. Default if you can't decide between caliber tiers."
api_effort = "high"

[model.effort.max]
power = 2.9
description = "Sonnet maximum. Rarely the right pick — at this effort opus+high is usually a better cost/capability ratio. Reserve for cases where sonnet's specific behaviors matter."
api_effort = "max"


[[model]]
name = "opus"
power = 3.0
provider = "anthropic-sdk"
api_model = "claude-opus-4-7"
description = "Anthropic Opus 4.7 — heavy reasoning, synthesis, code review of complex logic."
use_cases = "synthesis · multi-step coding · hard reasoning · code review of complex logic · the daemon's own subprocess"
cost_per_1m_input = 15.00
cost_per_1m_output = 75.00
network_required = true

[model.requirements]
min_ram_free_gb = 0.0
min_disk_free_gb = 0.0
needs_gpu = false
min_gpu_vram_free_gb = 0.0
max_concurrent_local = 0
typical_tokens_per_sec = 40

[model.effort.low]
power = 3.0
description = "Opus with minimal reasoning. Use when opus's training is what matters but the task itself is bounded."
api_effort = "low"

[model.effort.high]
power = 3.5
description = "API default for opus. Strong reasoning + token efficiency. Default for synthesis-class work."
api_effort = "high"

[model.effort.xhigh]
power = 3.8
description = "Extended capability for long-horizon agentic work. Anthropic's recommended starting point for complex coding. Expect significantly higher token usage than high."
api_effort = "xhigh"

[model.effort.max]
power = 4.0
description = "Maximum capability, no token-spending constraints. Reserve for genuinely frontier problems where eval evidence justifies the cost. Don't default — most workloads see small quality gains for large cost increases."
api_effort = "max"


# ============================================================
# UNCOMMENT WHEN ADAPTERS SHIP
# ============================================================
# The templates below are schema-correct placeholders for future
# providers. Backend adapters for these providers don't exist yet.
# When an adapter ships (e.g., backends/ollama.py), the netrunner
# uncomments the relevant template + adjusts api_model and power
# values for their specific local install.

# [[model]]
# name = "qwen3-local-7b"
# power = 1.0
# provider = "ollama"
# api_model = "qwen3:7b-instruct"
# description = "Local Qwen 3 7B Instruct — free after electricity, slower than cloud."
# use_cases = "watchdog Q&A · routine recon · summaries · tripwire authoring · offline ops"
# cost_per_1m_input = 0.0
# cost_per_1m_output = 0.0
# network_required = false
#
# [model.requirements]
# min_ram_free_gb = 8.0
# min_disk_free_gb = 12.0
# needs_gpu = false
# min_gpu_vram_free_gb = 0.0
# max_concurrent_local = 1
# typical_tokens_per_sec = 30
#
# [model.effort.low]
# power = 1.0
# description = "Default for local 7B work. Roughly haiku-tier in English; varies by domain."
# api_effort = "low"


# [[model]]
# name = "deepseek-openrouter"
# power = 2.5
# provider = "openrouter"
# api_model = "deepseek/deepseek-chat"
# description = "DeepSeek Chat via OpenRouter — cheap alternative for code synthesis."
# use_cases = "code synthesis · structured reasoning · routine multi-file work · cost-efficient sonnet alternative"
# cost_per_1m_input = 0.14
# cost_per_1m_output = 0.28
# network_required = true
#
# [model.requirements]
# min_ram_free_gb = 0.0
# min_disk_free_gb = 0.0
# needs_gpu = false
# min_gpu_vram_free_gb = 0.0
# max_concurrent_local = 0
# typical_tokens_per_sec = 40
#
# [model.effort.medium]
# power = 2.5
# description = "Default for DeepSeek work. Roughly equivocal to sonnet+medium for English tasks; cheaper."
# api_effort = "medium"


# [[model]]
# name = "private-server"
# power = 2.5
# provider = "private"
# api_model = "configured-on-server"
# description = "Self-hosted model on a private server (LAN or VPN)."
# use_cases = "privacy-sensitive work · LAN-resident only · custom fine-tunes"
# cost_per_1m_input = 0.0
# cost_per_1m_output = 0.0
# network_required = true
#
# [model.requirements]
# min_ram_free_gb = 0.0
# min_disk_free_gb = 0.0
# needs_gpu = false
# min_gpu_vram_free_gb = 0.0
# max_concurrent_local = 0
# typical_tokens_per_sec = 30
#
# [model.effort.medium]
# power = 2.5
# description = "Default for private-server work. Calibrate power based on the actual model deployed there."
# api_effort = "medium"
"""


# HTML comment block extractor for empty-detection. Same shape as
# roles_registry — strip TOML comments + whitespace, see if anything's
# left.
_TOML_COMMENT_LINE_RE = re.compile(r"^\s*#.*$", re.MULTILINE)


def _is_effectively_empty(content: str) -> bool:
    """Return True if `content` has nothing meaningful — just TOML
    comments and whitespace.

    Mirrors roles_registry's empty-detection: the "save blank to
    restore" UX requires letting the netrunner wipe the file to
    just-comments and have the registry treat that as a reset
    request. We strip all TOML comment lines + whitespace, then
    check if anything actionable remains."""
    without_comments = _TOML_COMMENT_LINE_RE.sub("", content)
    return without_comments.strip() == ""


class ModelLoadError(Exception):
    """Raised when the registry can't create the parent directory.
    Catastrophic — deck startup treats this as fatal. Per-entry
    load failures don't raise; they're logged + the entry is
    skipped (registry continues with whatever loaded cleanly)."""


@dataclass(frozen=True)
class ModelEffort:
    """One effort level on one model.

    `power` is the total effective capability at this effort —
    encodes both the model's base capability and the effort's
    contribution. Daemon ranks (model, effort) pairs by THIS value
    when picking caliber.

    `api_effort` is what the backend adapter passes to the provider's
    API (e.g., "low" / "medium" / "high" / "xhigh" / "max" for
    Anthropic).

    `description` is the daemon-visible explanation of when to pick
    this effort on this model."""
    level: str            # effort level name (low / medium / high / etc.)
    power: float
    description: str
    api_effort: str


@dataclass(frozen=True)
class ModelRequirements:
    """Hardware requirements for spawning this model.

    Cloud models set everything to zero/false — no local resource
    constraints to check. Local models declare real requirements;
    the construct spawn-time guardrail (tomorrow's slice) refuses
    spawns when the deck's live resources don't meet these.

    `max_concurrent_local`: 0 = unbounded (cloud); >0 = local cap.
    Daemon shouldn't spawn more than `max_concurrent_local`
    instances of a local model simultaneously."""
    min_ram_free_gb: float
    min_disk_free_gb: float
    needs_gpu: bool
    min_gpu_vram_free_gb: float
    max_concurrent_local: int
    typical_tokens_per_sec: int


@dataclass(frozen=True)
class Model:
    """One catalog entry.

    `efforts` is a dict keyed by effort level name. Empty when the
    model declares no effort variants (rare; current Anthropic
    models all support multiple efforts)."""
    name: str
    power: float                  # base summary rank (informational)
    provider: str
    api_model: str
    description: str
    use_cases: str
    cost_per_1m_input: float
    cost_per_1m_output: float
    network_required: bool
    requirements: ModelRequirements
    efforts: dict[str, ModelEffort] = field(default_factory=dict)
    source_path: Optional[Path] = None


def _parse_requirements(raw: dict) -> ModelRequirements:
    """Parse a [model.requirements] table. Missing fields fall back
    to safe defaults (cloud-shaped: zero / false)."""
    if not isinstance(raw, dict):
        raw = {}
    return ModelRequirements(
        min_ram_free_gb=_as_float(raw.get("min_ram_free_gb"), 0.0),
        min_disk_free_gb=_as_float(raw.get("min_disk_free_gb"), 0.0),
        needs_gpu=bool(raw.get("needs_gpu", False)),
        min_gpu_vram_free_gb=_as_float(raw.get("min_gpu_vram_free_gb"), 0.0),
        max_concurrent_local=int(_as_float(
            raw.get("max_concurrent_local"), 0.0,
        )),
        typical_tokens_per_sec=int(_as_float(
            raw.get("typical_tokens_per_sec"), 0.0,
        )),
    )


def _parse_effort(level: str, raw: dict) -> Optional[ModelEffort]:
    """Parse one [model.effort.<level>] table. Returns None when
    the level is malformed (missing required fields)."""
    if not isinstance(raw, dict):
        return None
    power = _as_float(raw.get("power"), None)
    if power is None:
        return None
    description = raw.get("description", "")
    if not isinstance(description, str):
        description = ""
    api_effort = raw.get("api_effort", level)
    if not isinstance(api_effort, str) or not api_effort:
        api_effort = level
    return ModelEffort(
        level=level,
        power=power,
        description=description,
        api_effort=api_effort,
    )


def _as_float(value, default):
    """Tolerant numeric coercion: ints, floats, numeric strings all
    accepted. Booleans rejected (would coerce to 0/1 silently). Any
    other non-numeric returns `default`."""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _parse_model(raw: dict, source_path: Path) -> Optional[Model]:
    """Parse one [[model]] entry. Returns None when required fields
    are missing or malformed — logs the error to stderr; caller skips
    this entry and continues with the rest of the catalog."""
    if not isinstance(raw, dict):
        return None

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        print(
            f"models_registry: skipping entry with missing/invalid "
            f"`name` field (source: {source_path})",
            file=sys.stderr,
        )
        return None

    provider = raw.get("provider")
    if not isinstance(provider, str) or not provider.strip():
        print(
            f"models_registry: skipping model {name!r}: missing "
            f"`provider` field",
            file=sys.stderr,
        )
        return None

    api_model = raw.get("api_model")
    if not isinstance(api_model, str) or not api_model.strip():
        print(
            f"models_registry: skipping model {name!r}: missing "
            f"`api_model` field",
            file=sys.stderr,
        )
        return None

    power = _as_float(raw.get("power"), 1.0)
    description = raw.get("description", "")
    if not isinstance(description, str):
        description = ""
    use_cases = raw.get("use_cases", "")
    if not isinstance(use_cases, str):
        use_cases = ""
    cost_in = _as_float(raw.get("cost_per_1m_input"), 0.0)
    cost_out = _as_float(raw.get("cost_per_1m_output"), 0.0)
    network_required = bool(raw.get("network_required", True))

    requirements = _parse_requirements(raw.get("requirements", {}))

    efforts: dict[str, ModelEffort] = {}
    effort_table = raw.get("effort", {})
    if isinstance(effort_table, dict):
        for level, effort_raw in effort_table.items():
            parsed = _parse_effort(level, effort_raw)
            if parsed is not None:
                efforts[level] = parsed

    return Model(
        name=name,
        power=power,
        provider=provider,
        api_model=api_model,
        description=description,
        use_cases=use_cases,
        cost_per_1m_input=cost_in,
        cost_per_1m_output=cost_out,
        network_required=network_required,
        requirements=requirements,
        efforts=efforts,
        source_path=source_path,
    )


class ModelsRegistry:
    """One-shot loader for the models catalog.

    Lifecycle:
      registry = ModelsRegistry(roles_dir, bus=bus)
      registry.load()                 # synchronous; reads models.toml
      catalog = registry.all()        # list[Model]
      model = registry.get("haiku")   # Optional[Model]

    No hot reload (consistent with roles_registry's discipline). The
    netrunner edits models.toml; the change applies on next deck
    launch.

    Default-restoration runs during load(): if the file is missing
    OR effectively empty (comments + whitespace only), the registry
    writes DEFAULT_MODELS_TOML to disk + uses that content. The
    bundled defaults always include the three Anthropic entries
    (haiku / sonnet / opus); future-provider templates ship
    commented-out for the netrunner to enable when adapters ship.

    Errors during load() are best-effort: bad TOML falls back to
    bundled defaults; bad per-entry shapes are logged + skipped.
    Only catastrophic directory failure raises ModelLoadError.
    """

    FILENAME = "models.toml"

    def __init__(
        self,
        roles_dir: Path,
        *,
        bus: Optional[object] = None,
    ) -> None:
        self.roles_dir = Path(roles_dir)
        self.bus = bus
        self._by_name: dict[str, Model] = {}
        self._loaded = False

    def load(self) -> None:
        """Read models.toml, restoring defaults where needed.

        Idempotent. Catastrophic dir failure raises ModelLoadError;
        per-entry failures fall back to defaults silently (with
        stderr warnings).
        """
        try:
            self.roles_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ModelLoadError(
                f"could not create roles dir {self.roles_dir}: {e!r}"
            ) from e

        path = self.roles_dir / self.FILENAME

        # Read or seed.
        content = self._read_or_seed(path)

        # Parse TOML. Malformed → fall back to bundled defaults
        # (don't crash startup; surface the issue to stderr).
        try:
            data = tomllib.loads(content)
        except tomllib.TOMLDecodeError as e:
            print(
                f"models_registry: TOML parse error in {path}: {e!r} — "
                f"falling back to bundled defaults for this session "
                f"(file content preserved on disk; netrunner must fix)",
                file=sys.stderr,
            )
            try:
                data = tomllib.loads(DEFAULT_MODELS_TOML)
            except tomllib.TOMLDecodeError as inner:
                # Bundled defaults are malformed — shouldn't happen,
                # but failsafe to empty catalog rather than crash.
                print(
                    f"models_registry: BUNDLED DEFAULTS ALSO MALFORMED: "
                    f"{inner!r} — running with empty catalog",
                    file=sys.stderr,
                )
                data = {}

        self._by_name.clear()
        models_raw = data.get("model", []) if isinstance(data, dict) else []
        if not isinstance(models_raw, list):
            models_raw = []
        for entry in models_raw:
            model = _parse_model(entry, path)
            if model is None:
                continue
            # Last entry wins on name collision — log + keep going.
            if model.name in self._by_name:
                print(
                    f"models_registry: duplicate model name "
                    f"{model.name!r} in {path} — last entry wins",
                    file=sys.stderr,
                )
            self._by_name[model.name] = model

        self._loaded = True
        self._emit_loaded(path, len(self._by_name))

    def _read_or_seed(self, path: Path) -> str:
        """Read models.toml. Seed bundled defaults if missing or
        effectively empty. Returns the content to parse.
        """
        if not path.is_file():
            try:
                path.write_text(DEFAULT_MODELS_TOML, encoding="utf-8")
            except OSError as e:
                print(
                    f"models_registry: could not seed {path}: {e!r} — "
                    f"using bundled defaults in memory only",
                    file=sys.stderr,
                )
            return DEFAULT_MODELS_TOML

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            print(
                f"models_registry: could not read {path}: {e!r} — "
                f"using bundled defaults in memory",
                file=sys.stderr,
            )
            return DEFAULT_MODELS_TOML

        if _is_effectively_empty(content):
            try:
                path.write_text(DEFAULT_MODELS_TOML, encoding="utf-8")
            except OSError as e:
                print(
                    f"models_registry: could not restore {path}: {e!r} — "
                    f"using bundled defaults in memory only",
                    file=sys.stderr,
                )
            return DEFAULT_MODELS_TOML

        return content

    def _emit_loaded(self, path: Path, count: int) -> None:
        """Publish a 'models.loaded' bus event. Best-effort."""
        if self.bus is None:
            return
        try:
            from event_bus import DeckEvent, Severity
            self.bus.publish(DeckEvent(
                kind="models.loaded",
                source="models_registry",
                severity=Severity.INFO,
                payload={
                    "source_path": str(path),
                    "model_count": count,
                    "names": sorted(self._by_name.keys()),
                },
            ))
        except Exception as e:
            print(
                f"models_registry: bus publish error: {e!r}",
                file=sys.stderr,
            )

    # ---- read API --------------------------------------------------------

    def get(self, name: str) -> Optional[Model]:
        """Look up a model by name. None if not loaded or not present."""
        return self._by_name.get(name)

    def all(self) -> list[Model]:
        """All loaded models, sorted by ascending power, then name.

        Sort order matters for catalog rendering: presenting models
        in capability-ascending order gives the daemon a natural
        cheap-to-expensive scan.
        """
        return sorted(
            self._by_name.values(),
            key=lambda m: (m.power, m.name),
        )

    def by_provider(self, provider: str) -> list[Model]:
        """All models from a given provider, sorted by power."""
        matched = [m for m in self._by_name.values() if m.provider == provider]
        matched.sort(key=lambda m: (m.power, m.name))
        return matched

    def providers(self) -> list[str]:
        """All distinct provider names referenced in the catalog,
        sorted. Useful for adapter-availability checks at deck
        startup."""
        return sorted({m.provider for m in self._by_name.values()})

    def is_loaded(self) -> bool:
        """True after load() has been called. Lets callers verify
        startup completed before reading."""
        return self._loaded


# ---- catalog rendering for daemon prompts ---------------------------------


def render_catalog_for_daemon(registry: ModelsRegistry) -> str:
    """Render the catalog as a daemon-prompt-friendly block.

    Returns a Markdown-ish text block listing every model + its
    effort variants, sorted by ascending power. The daemon receives
    this in its system prompt and consults it when picking caliber
    per spawn.

    Empty catalog returns an empty string — caller should omit the
    section from the system prompt entirely. The deck always seeds
    bundled defaults so this is a degenerate case (only happens on
    catastrophic load failure).
    """
    models = registry.all()
    if not models:
        return ""

    lines: list[str] = []
    lines.append("MODELS CATALOG — every model you may pick for a spawn:")
    lines.append("")

    for m in models:
        # Header line: name + power + provider + network requirement.
        network_label = (
            "network-required" if m.network_required else "local (offline-OK)"
        )
        lines.append(
            f"  {m.name} (power {m.power}, provider={m.provider}, "
            f"{network_label})"
        )
        if m.description:
            lines.append(f"    {m.description}")
        if m.use_cases:
            lines.append(f"    Use cases: {m.use_cases}")
        cost_label = (
            f"${m.cost_per_1m_input:.2f}/M in · ${m.cost_per_1m_output:.2f}/M out"
            if (m.cost_per_1m_input > 0 or m.cost_per_1m_output > 0)
            else "free (local)"
        )
        lines.append(f"    Cost: {cost_label}")

        # Resource requirements summary (local-relevant only).
        req = m.requirements
        if (req.min_ram_free_gb > 0 or req.min_disk_free_gb > 0
                or req.needs_gpu):
            req_parts = []
            if req.min_ram_free_gb > 0:
                req_parts.append(f"{req.min_ram_free_gb:.1f}GB RAM free")
            if req.min_disk_free_gb > 0:
                req_parts.append(f"{req.min_disk_free_gb:.1f}GB disk free")
            if req.needs_gpu:
                vram = (
                    f", {req.min_gpu_vram_free_gb:.1f}GB VRAM"
                    if req.min_gpu_vram_free_gb > 0 else ""
                )
                req_parts.append(f"GPU required{vram}")
            if req.max_concurrent_local > 0:
                req_parts.append(
                    f"max {req.max_concurrent_local} concurrent local"
                )
            lines.append(f"    Requirements: {' · '.join(req_parts)}")

        # Effort tables, sorted by power.
        if m.efforts:
            sorted_efforts = sorted(
                m.efforts.values(), key=lambda e: e.power,
            )
            lines.append("    Efforts:")
            for eff in sorted_efforts:
                lines.append(
                    f"      {eff.level} (power {eff.power}): "
                    f"{eff.description}"
                )
        lines.append("")

    return "\n".join(lines)
