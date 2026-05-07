"""
First-run onboarding check + on-demand diagnostics.

Build-plan item 0a (filed 2026-05-03; shipped 2026-05-04).

Today's deck self-bootstraps file artifacts (`<home>/profiles/`,
dispatcher script, plugin bridge, tools.toml, state.json) but does
NOT verify external prerequisites. A netrunner who clones the repo to
a fresh machine and runs `python tui.py` gets either:

  - Cryptic ImportError on Textual missing
  - `[Errno 2] No such file or directory: 'claude'` deep in async
    setup
  - Silent failure modes when claude is on PATH but not logged in

`doctor.py` runs cheap detection at startup with PASS / WARN / FAIL
classification + remediation hints.

Policy (post-2026-05-07 split):
  - HARD PREREQS (Python, textual, claude binary, claude --version)
    are DETECT + SUGGEST, NOT AUTO-INSTALL. npm install for the
    claude CLI and a working Python with textual are environment-
    level concerns where auto-install is fragile across corp
    firewalls, alternate Python distributions, and locked-down
    environments. FAIL → print hint + exit 1; netrunner fixes
    manually.
  - PLUGIN DEPS (each plugin.toml's `requires.python_imports`) are
    DETECT + OPTIONAL-PROMPT-TO-INSTALL. Missing deps NEVER block
    deck launch — affected plugins simply appear in the Tools panel
    as `unavailable: missing python imports: <pkg>`, the same way
    binary tools that aren't on PATH show up. If stdin is a TTY and
    deps are missing, the doctor offers a one-shot prompt: "install
    via pip? [y/N]". On yes → run `<python> -m pip install <pkgs>`
    + re-check (any still-missing plugins remain unavailable;
    install failures don't block launch either). On no / non-TTY /
    Ctrl+D → skip cleanly, deck launches with affected plugins
    marked unavailable. NO download is ever attempted without
    explicit y/yes from the prompt.

    Policy history: pre-2026-05-07 was WARN-and-continue (no
    prompt). 2026-05-07 morning was PROMPT-TO-INSTALL-OR-ABORT.
    2026-05-07 afternoon (current) is PROMPT-TO-INSTALL-OR-SKIP —
    netrunner direction: plugins should degrade gracefully like
    tools, not gate launch.

Sentinel: `<home>/.cyberdeck/first_run_complete`. After a run where
no FAIL is encountered, write the sentinel; subsequent runs skip
the diagnostics output (still run the checks; only display on FAIL
or via --doctor flag).

Hard prereqs (FAIL = exit before TUI mount):
  - Python >= 3.11
  - `import textual`
  - claude binary on PATH (or $CLAUDE_BIN points at one)
  - `claude --version` runs cleanly within 5s

Soft prereqs (WARN only):
  - Plugin python_imports — walked from each <deck-source>/plugins/
    <name>/plugin.toml manifest's `requires.python_imports` field.
    One WARN per missing import. Pre-2026-05-07 this was a
    hardcoded `import mss` check; now plugin-derived so adding a
    new plugin auto-extends the dep matrix without touching
    doctor.py. (The deck still works without these — the missing
    plugin just refuses to load via plugins.load_plugin's
    requires-check.)
  - `claude --help` looks healthy (auth check; can't verify
    without a real API call which we don't want to spend tokens on)

Public surface:
  Result(severity, label, detail, hint)
  run_checks(claude_bin) -> list[Result]
  format_report(results, *, color) -> str
  is_first_run(home_dir) -> bool
  mark_first_run_complete(home_dir) -> None

The runner is intentionally NOT integrated with the bus — the deck's
event bus doesn't exist yet at the point this runs. Stdout + stderr
are the only output channels.
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Severity classification. PASS items don't block; WARN items don't
# block but degrade specific functionality (e.g., screenshot plugin
# without mss); FAIL items block deck launch — the netrunner needs
# to fix before the deck can run.
class Severity:
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True)
class Result:
    """One prerequisite check result."""
    severity: str  # PASS | WARN | FAIL
    label: str  # short human-readable identifier (e.g. "python")
    detail: str  # what was actually observed
    hint: str = ""  # remediation suggestion (empty for PASS)


# Minimum Python version. Cyberdeck uses 3.11+ features (tomllib in
# stdlib, structural pattern matching in places, etc.). The deck
# tests on 3.14 in development; older versions are likely fine but
# unverified.
MIN_PYTHON = (3, 11)


def check_python_version() -> Result:
    """Verify Python >= 3.11."""
    cur = sys.version_info[:2]
    cur_str = f"{cur[0]}.{cur[1]}.{sys.version_info[2]}"
    min_str = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    if cur >= MIN_PYTHON:
        return Result(
            severity=Severity.PASS,
            label="python",
            detail=f"Python {cur_str} >= {min_str}",
        )
    return Result(
        severity=Severity.FAIL,
        label="python",
        detail=f"Python {cur_str} < {min_str}",
        hint=(
            f"Install Python {min_str}+ and re-run. The deck uses "
            f"3.11+ stdlib features (tomllib in particular)."
        ),
    )


def check_textual() -> Result:
    """Verify the textual library is importable."""
    if importlib.util.find_spec("textual") is not None:
        return Result(
            severity=Severity.PASS,
            label="textual",
            detail="textual library available",
        )
    return Result(
        severity=Severity.FAIL,
        label="textual",
        detail="textual library not found",
        hint="install: pip install textual",
    )


def check_plugin_dependencies(plugins_dir: Path) -> list[Result]:
    """Walk <deck-source>/plugins/, read each plugin.toml's
    `requires.python_imports`, return one Result per declared import.

    Soft prereqs (WARN on missing) — a missing plugin dep just means
    that plugin will refuse to load (graceful degradation in
    `plugins.load_plugin`'s requires-check). The deck itself still
    works.

    Pre-2026-05-07 this was a hardcoded `import mss` check tied to
    the screenshot plugin specifically. Adding a new plugin with
    its own dependency required editing doctor.py — easy to forget,
    and a divergence between manifest and doctor would silently
    miss the dep in the first-run check. Plugin-derived means new
    plugins auto-extend the dep matrix.

    Best-effort: missing/malformed plugins.toml entries are skipped
    silently. Plugin registry's own scan-time validation surfaces
    those errors; doctor.py shouldn't double-report them at a
    pre-TUI layer where the bus isn't even up yet."""
    results: list[Result] = []
    if not plugins_dir.is_dir():
        return results
    try:
        import tomllib
    except ImportError:
        # Pre-3.11 — would have FAILed the python check above
        # already, so getting here is unexpected. Fall through
        # silently rather than masquerade as a plugin issue.
        return results

    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        manifest = entry / "plugin.toml"
        if not manifest.is_file():
            continue
        try:
            with manifest.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        plugin_name = data.get("name", entry.name)
        if not isinstance(plugin_name, str) or not plugin_name:
            plugin_name = entry.name
        requires = data.get("requires") or {}
        if not isinstance(requires, dict):
            continue
        imports = requires.get("python_imports") or []
        if not isinstance(imports, list):
            continue
        for imp in imports:
            if not isinstance(imp, str) or not imp.strip():
                continue
            imp = imp.strip()
            label = f"plugin:{plugin_name}:{imp}"
            if importlib.util.find_spec(imp) is not None:
                results.append(Result(
                    severity=Severity.PASS,
                    label=label,
                    detail=f"{imp} available ({plugin_name} OK)",
                ))
            else:
                results.append(Result(
                    severity=Severity.WARN,
                    label=label,
                    detail=f"{imp} not found",
                    hint=(
                        f"plugin '{plugin_name}' will be unavailable. "
                        f"install: pip install {imp}"
                    ),
                ))
    return results


def check_claude_bin(claude_bin: str) -> Result:
    """Verify the claude CLI is reachable. Hard fail — the deck
    spawns claude as every construct + the daemon + the watchdog;
    without it nothing works.

    Resolution rules:
      1. shutil.which(claude_bin) — finds installed binaries on PATH
      2. Path(claude_bin).is_file() — accepts explicit relative or
         absolute paths to a script (e.g., the mock_claude.py used
         for offline development)
    Either passes; only when both miss do we FAIL.
    """
    resolved = shutil.which(claude_bin)
    if resolved is not None:
        return Result(
            severity=Severity.PASS,
            label="claude_bin",
            detail=f"{claude_bin} -> {resolved}",
        )
    # Fallback: explicit file path. Lets development setups using
    # `CLAUDE_BIN=./mock_claude.py` pass the doctor without needing
    # PATHEXT tweaks or shebang lines that Windows would interpret.
    candidate = Path(claude_bin)
    if candidate.is_file():
        return Result(
            severity=Severity.PASS,
            label="claude_bin",
            detail=f"{claude_bin} -> {candidate.resolve()} (script)",
        )
    return Result(
        severity=Severity.FAIL,
        label="claude_bin",
        detail=f"could not locate {claude_bin!r} on PATH or as file",
        hint=(
            "install: npm install -g @anthropic-ai/claude-code\n"
            "  (then restart your terminal so PATH picks up "
            "the new binary)\n"
            "for development with a mock, pass --no-doctor or "
            "use --claude-bin <abs-path>"
        ),
    )


def check_claude_version(claude_bin: str) -> Result:
    """Run `claude --version` to verify the binary is healthy.
    5s timeout — should be near-instant. Auth state isn't
    verifiable without a real API call (which we don't want to
    spend tokens on at startup); a successful --version is the
    cheapest health signal."""
    resolved = shutil.which(claude_bin)
    if resolved is None:
        # Could be a development mock script (e.g. ./mock_claude.py).
        # Skip the version probe — those scripts aren't directly
        # executable on Windows without `python` prefix, and the
        # deck has its own handling for that path. The claude_bin
        # check above already FAILed if the file doesn't exist
        # at all; getting here means it's a script.
        candidate = Path(claude_bin)
        if candidate.is_file():
            return Result(
                severity=Severity.PASS,
                label="claude_version",
                detail=(
                    "skipped - claude_bin is a script "
                    "(development mock); deck handles invocation"
                ),
            )
        return Result(
            severity=Severity.WARN,
            label="claude_version",
            detail="skipped - binary not found",
        )
    try:
        proc = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except subprocess.TimeoutExpired:
        return Result(
            severity=Severity.FAIL,
            label="claude_version",
            detail=f"{claude_bin} --version timed out (5s)",
            hint=(
                "claude binary is slow to start or hung. Try "
                "running `claude --version` manually. If it hangs "
                "there too, reinstall: npm install -g "
                "@anthropic-ai/claude-code"
            ),
        )
    except OSError as exc:
        return Result(
            severity=Severity.FAIL,
            label="claude_version",
            detail=f"{claude_bin} --version failed: {exc}",
            hint="check the binary is executable + on PATH",
        )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if len(stderr) > 200:
            stderr = stderr[:197] + "..."
        return Result(
            severity=Severity.FAIL,
            label="claude_version",
            detail=(
                f"{claude_bin} --version exited "
                f"{proc.returncode}: {stderr or '(no stderr)'}"
            ),
            hint="check the install + your account login state",
        )
    version = (proc.stdout or "").strip().splitlines()[0] if proc.stdout else "?"
    return Result(
        severity=Severity.PASS,
        label="claude_version",
        detail=f"{claude_bin} version: {version}",
    )


def run_checks(claude_bin: str = "claude") -> list[Result]:
    """Run the full prereq battery. Order matters — Python first
    (everything else depends on it), then library imports, then
    binary checks. Results returned in run order so the report
    reads top-down by dependency.

    `claude_bin` defaults to "claude" (the canonical install
    name). Pass an override (typically from $CLAUDE_BIN env var
    or --claude-bin CLI flag) when the netrunner has a non-default
    install location.
    """
    results: list[Result] = []
    results.append(check_python_version())
    results.append(check_textual())
    # Plugin python_imports — derived from each plugin.toml's
    # `requires.python_imports`. One Result per declared import.
    # Plugins live at <deck-source>/plugins/<name>/, where deck
    # source = the directory containing this file (doctor.py sits
    # next to tui.py / brake_hook.py / etc.).
    plugins_dir = Path(__file__).resolve().parent / "plugins"
    results.extend(check_plugin_dependencies(plugins_dir))
    results.append(check_claude_bin(claude_bin))
    # Only run the version probe when the binary check passed —
    # otherwise we'd report two redundant FAILs for the same
    # underlying issue.
    if results[-1].severity == Severity.PASS:
        results.append(check_claude_version(claude_bin))
    return results


# Severity → terminal color codes. ANSI escapes; works in modern
# terminals (Windows Terminal, iTerm2, gnome-terminal, etc.).
# Falls back to plain text when stdout isn't a TTY (piped output,
# CI logs).
_COLORS = {
    Severity.PASS: "\033[32m",  # green
    Severity.WARN: "\033[33m",  # yellow
    Severity.FAIL: "\033[31m",  # red
}
_COLOR_RESET = "\033[0m"


def format_report(results: list[Result], *, color: bool = True) -> str:
    """Render a check list as a readable multi-line string.

    Format:
        cyberdeck: prerequisite check
        ─────────────────────────────
        [PASS] python          Python 3.14.3 >= 3.11
        [PASS] textual         textual library available
        [WARN] mss             mss library not found
                               → screenshot plugin will be unavailable.
                                 install: pip install mss (...)
        [PASS] claude_bin      claude → C:/.../claude.exe
        [PASS] claude_version  claude version: 2.1.126

    Hints render indented under their respective lines. Color is on
    by default; pass color=False for piped / log output.
    """
    if not results:
        return "cyberdeck: no checks ran"

    # Compute column widths so labels align cleanly. Cap label width
    # at 16 chars — anything longer wraps awkwardly.
    label_w = min(16, max(len(r.label) for r in results))

    # ASCII divider — Windows cp1252 stdout encoding can't render
    # box-drawing characters when stdout is piped (the encoding
    # picks cp1252 not utf-8 in that case). The check is meant to
    # run on fresh installs where utf-8 reliability is unknowable;
    # ASCII keeps the output portable.
    lines: list[str] = [
        "cyberdeck: prerequisite check",
        "-" * 32,
    ]
    for r in results:
        col = _COLORS.get(r.severity, "") if color else ""
        rst = _COLOR_RESET if color else ""
        lines.append(
            f"{col}[{r.severity:<4}]{rst} "
            f"{r.label:<{label_w}}  {r.detail}"
        )
        if r.hint:
            for hint_line in r.hint.splitlines():
                # ASCII arrow — same cp1252 reasoning as the divider.
                lines.append(
                    f"  {' ' * (label_w + 8)}-> {hint_line}"
                )
    return "\n".join(lines)


# Sentinel filename. Lives in <home>/.cyberdeck/ alongside state.json
# + spawn_settings.json; existence is the "first run is done" flag.
# Rationale for a separate file (vs a key in state.json): doctor.py
# runs BEFORE state.json's loader is wired (we're pre-tui-construction
# at this point), and threading state.json's load through here would
# couple the doctor to brake_state's API.
_FIRST_RUN_SENTINEL = "first_run_complete"


def is_first_run(home_dir: Path) -> bool:
    """True if the sentinel doesn't exist — the netrunner hasn't
    completed a successful first-run check yet."""
    sentinel = Path(home_dir) / ".cyberdeck" / _FIRST_RUN_SENTINEL
    return not sentinel.is_file()


def mark_first_run_complete(home_dir: Path) -> None:
    """Write the sentinel. Best-effort — if we can't write it, the
    diagnostics will run again next launch (annoying, not broken)."""
    sentinel_dir = Path(home_dir) / ".cyberdeck"
    sentinel = sentinel_dir / _FIRST_RUN_SENTINEL
    try:
        sentinel_dir.mkdir(parents=True, exist_ok=True)
        sentinel.write_text(
            "# First-run prerequisite check passed. Delete this "
            "file to re-run diagnostics on next launch, or pass "
            "--doctor.\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def has_failure(results: list[Result]) -> bool:
    """Convenience: True if any check returned FAIL."""
    return any(r.severity == Severity.FAIL for r in results)


def has_warning(results: list[Result]) -> bool:
    """Convenience: True if any check returned WARN (and no FAIL)."""
    return any(r.severity == Severity.WARN for r in results)


# ---- plugin-dep prompt-to-install (post-2026-05-07 policy) ---------


def get_missing_plugin_packages(
    results: list[Result],
) -> list[tuple[str, str]]:
    """Extract (plugin_name, package) pairs from `plugin:*` WARN
    entries. Returns deduplicated list preserving discovery order.

    Used by `run_doctor_or_exit` to drive the prompt-to-install
    flow: show the list, prompt y/N, install on yes, exit 1 on no.
    """
    missing: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for r in results:
        if r.severity != Severity.WARN:
            continue
        if not r.label.startswith("plugin:"):
            continue
        # Label format: "plugin:<name>:<import>"
        parts = r.label.split(":", 2)
        if len(parts) != 3:
            continue
        _, name, pkg = parts
        key = (name, pkg)
        if key in seen:
            continue
        seen.add(key)
        missing.append(key)
    return missing


def prompt_install_plugin_deps(
    missing: list[tuple[str, str]],
) -> bool:
    """Show the missing-packages list and prompt for install consent.

    Reads from stdin via `input()`. Returns True on affirmative
    ("y" / "yes", case-insensitive). Returns False on anything else
    (empty enter, "n", "no", "skip", etc.).

    Non-TTY behavior: stdin not interactive (piped input, CI run,
    detached process) → returns False without prompting. The caller
    treats False as "skip install, launch with affected plugins
    marked unavailable" (post-2026-05-07 policy). No download is
    attempted without explicit consent.
    """
    if not sys.stdin.isatty():
        print(
            "cyberdeck: stdin is not a TTY; skipping plugin-dep "
            "install prompt. Affected plugins will be marked "
            "unavailable.",
            file=sys.stderr,
        )
        return False
    # Group by plugin for legibility — same package required by
    # multiple plugins is rare but possible.
    by_plugin: dict[str, list[str]] = {}
    for plugin, pkg in missing:
        by_plugin.setdefault(plugin, []).append(pkg)
    print("cyberdeck: missing plugin dependencies:")
    for plugin, pkgs in by_plugin.items():
        joined = ", ".join(pkgs)
        print(f"  - {plugin}: {joined}")
    print()
    pkg_set = sorted({pkg for _, pkg in missing})
    print(
        f"Install via pip?  ({' '.join(pkg_set)})  "
        f"Skipping launches the deck with these plugins unavailable."
    )
    try:
        response = input("[y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # Ctrl+D / Ctrl+C during the prompt — treat as skip.
        print()
        return False
    return response in ("y", "yes")


def install_packages(packages: list[str]) -> tuple[bool, str]:
    """Run `<python> -m pip install <packages>`. Returns (success,
    output) — output is captured stdout+stderr suitable for the
    netrunner to see when something went wrong.

    Uses `sys.executable` to install into the same Python that's
    running the deck. Avoids picking up the wrong pip when multiple
    Python distributions are on PATH (the alternative — bare `pip`
    on PATH — frequently targets the wrong environment on Windows
    with multiple Python installs).

    5-minute timeout: pip installs over slow corporate firewalls
    sometimes take a while; 5min is generous but bounded so a
    network hang doesn't sit forever.
    """
    if not packages:
        return True, ""
    cmd = [sys.executable, "-m", "pip", "install", *packages]
    print(f"cyberdeck: running: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300.0,
        )
    except subprocess.TimeoutExpired:
        return False, "pip install timed out (5min)"
    except OSError as exc:
        return False, f"pip invocation failed: {exc}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        return False, output.strip() or "pip exited non-zero (no output)"
    return True, output.strip()


def run_doctor_or_exit(
    claude_bin: str,
    home_dir: Path,
    *,
    doctor_flag: bool = False,
) -> None:
    """Top-level orchestration. Either returns cleanly (deck can
    launch) or calls sys.exit(N).

    Flow:
      1. Run checks.
      2. If first run / `--doctor` / any FAIL: print the report.
      3. If `--doctor` flag: exit (0 on PASS-only, 1 on any FAIL).
         Observational mode — no install prompt.
      4. If any FAIL on hard prereqs (Python, textual, claude): exit 1.
         These are environment-level issues; the deck cannot launch
         without them.
      5. If any plugin dep WARN: optional prompt to install (TTY
         only). Yes → pip install + re-check (still-missing plugins
         remain unavailable). No / non-TTY / Ctrl+D / install
         failure → continue. Affected plugins surface in the Tools
         panel as `unavailable: missing python imports: <pkg>`,
         same way binary tools that aren't on PATH show up.
      6. Mark first-run done, return.

    Plugin-dep handling never blocks launch (post-2026-05-07
    netrunner direction). Pre-policy this exited 1 on decline /
    install failure; current policy treats those as graceful
    degradation — the plugin registry already marks unavailable
    plugins with a reason, so the netrunner sees the consequence
    in the Tools panel without the doctor needing to gate launch.

    Caller is expected to skip this entirely when the netrunner
    passes `--no-doctor` (escape hatch for development with mock
    Claude binaries / unusual environments).
    """
    results = run_checks(claude_bin)
    failed = has_failure(results)
    force_show = doctor_flag or is_first_run(home_dir)
    if force_show or failed:
        color = sys.stdout.isatty()
        print(format_report(results, color=color))
        print()

    if doctor_flag:
        # --doctor is observational. Exit 0 on PASS-only, 1 on
        # any FAIL so the netrunner can use it in shell scripts
        # (`python tui.py --doctor && python tui.py --goal ...`).
        # Plugin WARNs don't affect exit code here; they'll be
        # picked up by a non-doctor launch's prompt flow.
        sys.exit(1 if failed else 0)

    if failed:
        print(
            "cyberdeck: hard prerequisites failed. Fix the FAIL "
            "items above and re-run.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Plugin-dep optional prompt-to-install (post-2026-05-07 policy).
    # Never blocks launch — decline and install failure both fall
    # through to the deck launching with affected plugins marked
    # unavailable in the Tools panel.
    missing = get_missing_plugin_packages(results)
    if missing:
        # Make sure the netrunner sees the report before answering.
        # No-op if we already printed it above (first_run / FAIL).
        if not (force_show or failed):
            color = sys.stdout.isatty()
            print(format_report(results, color=color))
            print()
        packages = sorted({pkg for _, pkg in missing})
        if prompt_install_plugin_deps(missing):
            # Affirmative → run pip install. Failures don't gate
            # launch; the plugin just stays unavailable.
            ok, output = install_packages(packages)
            if not ok:
                print(
                    f"cyberdeck: pip install failed:\n{output}\n"
                    f"Continuing — affected plugins will be "
                    f"unavailable in the Tools panel.",
                    file=sys.stderr,
                )
            else:
                # Re-check to confirm everything resolved. Pip can
                # exit 0 but leave imports unfindable (version
                # mismatch, namespace package, install into wrong
                # site-packages, etc.). Still-missing plugins just
                # stay unavailable; we don't gate launch on them.
                results = run_checks(claude_bin)
                still_missing = get_missing_plugin_packages(results)
                if still_missing:
                    pkgs = ", ".join(
                        pkg for _, pkg in still_missing
                    )
                    print(
                        f"cyberdeck: install completed but the "
                        f"following are still missing: {pkgs}. "
                        f"Affected plugins will be unavailable.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "cyberdeck: plugin dependencies installed."
                    )
        else:
            # Skip path: no install attempted. Print the manual-
            # install hint so the netrunner has the command if they
            # change their mind later. Deck launches normally.
            print(
                f"cyberdeck: skipping install. Affected plugins "
                f"will be marked unavailable in the Tools panel. "
                f"To install later: pip install "
                f"{' '.join(packages)}",
            )

    # PASS (or WARN with plugin deps either installed or skipped)
    # → mark first-run done so future launches stay quiet. Best-
    # effort; a write failure means the diagnostics show again
    # next launch.
    mark_first_run_complete(home_dir)
