#!/usr/bin/env python3
"""screenshot plugin — capture the screen as a PNG via mss.

See README.md for the full interface. Brief:

    python run.py [output_path] [monitor_index]

Stdout: absolute path of the captured PNG (one line, on success).
Stderr: ERROR: <reason> (on failure, with non-zero exit code).

Exit codes:
    0  capture written
    1  mss not installed
    2  capture failed at runtime (mss raised, disk error, etc.)
    3  bad arguments

Self-contained: depends only on stdlib + mss. No imports from the
deck source — the plugin is its own subprocess and shouldn't
intermingle with deck process state.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path


def _usage() -> str:
    return (
        "usage: python run.py [output_path] [monitor_index]\n"
        "  output_path     where to write the PNG (default: cwd/screenshot-<ts>.png)\n"
        "  monitor_index   which monitor (0=all monitors, 1+=specific; default: 0)"
    )


def main(argv: list[str]) -> int:
    # Lazy import so we can return a clean error code if mss isn't
    # installed (the deck's `requires` check should catch this at
    # registry scan time, but defense in depth is cheap).
    try:
        import mss  # type: ignore[import-not-found]
    except ImportError as exc:
        print(f"ERROR: mss not installed: {exc}", file=sys.stderr)
        print("install: pip install mss", file=sys.stderr)
        return 1

    args = argv[1:]
    if any(a in ("-h", "--help") for a in args):
        print(_usage())
        return 0

    out_path: Path
    monitor: int

    if len(args) == 0:
        ts = time.strftime("%Y%m%d-%H%M%S")
        out_path = Path.cwd() / f"screenshot-{ts}.png"
        monitor = 0
    elif len(args) == 1:
        out_path = Path(args[0]).expanduser().resolve()
        monitor = 0
    elif len(args) == 2:
        out_path = Path(args[0]).expanduser().resolve()
        try:
            monitor = int(args[1])
        except ValueError:
            print(
                f"ERROR: monitor_index must be an integer, got {args[1]!r}",
                file=sys.stderr,
            )
            return 3
        if monitor < 0:
            print(
                f"ERROR: monitor_index must be >= 0, got {monitor}",
                file=sys.stderr,
            )
            return 3
    else:
        print(f"ERROR: too many arguments\n{_usage()}", file=sys.stderr)
        return 3

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(
            f"ERROR: could not create output directory {out_path.parent}: {exc}",
            file=sys.stderr,
        )
        return 2

    try:
        with mss.mss() as sct:
            # Validate monitor index against available displays.
            # mss.monitors[0] is the union of all monitors;
            # mss.monitors[1:] are individual monitors. Out-of-range
            # = clear error rather than mss's less-friendly IndexError.
            if monitor >= len(sct.monitors):
                print(
                    f"ERROR: monitor_index {monitor} out of range "
                    f"(have {len(sct.monitors)} entries: 0=all, "
                    f"1..{len(sct.monitors) - 1}=individual)",
                    file=sys.stderr,
                )
                return 3
            sct.shot(mon=monitor, output=str(out_path))
    except Exception as exc:
        # mss can raise platform-specific errors (X11 connection
        # failures, Quartz permission denied on macOS, etc.). Surface
        # the message rather than swallowing.
        print(f"ERROR: capture failed: {exc}", file=sys.stderr)
        return 2

    if not out_path.is_file():
        print(
            f"ERROR: capture appeared to succeed but {out_path} doesn't exist",
            file=sys.stderr,
        )
        return 2

    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
