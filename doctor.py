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
classification + remediation hints. DETECT + SUGGEST, NOT
AUTO-INSTALL — npm/pip auto-install is fragile across corp
firewalls, alternate Python distributions, environments where the
user can't write globally. Better to be the doctor than the surgeon.

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
  - `import mss` (screenshot plugin specific)
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


def check_mss() -> Result:
    """Soft check for the mss library — screenshot plugin's
    runtime dep. WARN-only; missing mss just means the screenshot
    plugin will refuse to load (graceful degradation in
    plugins.load_plugin's requires-check)."""
    if importlib.util.find_spec("mss") is not None:
        return Result(
            severity=Severity.PASS,
            label="mss",
            detail="mss library available (screenshot plugin OK)",
        )
    return Result(
        severity=Severity.WARN,
        label="mss",
        detail="mss library not found",
        hint=(
            "screenshot plugin will be unavailable. "
            "install: pip install mss (only needed if you want "
            "the screenshot plugin)"
        ),
    )


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
    results.append(check_mss())
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
