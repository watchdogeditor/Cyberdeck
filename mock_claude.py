#!/usr/bin/env python3
"""
Mock 'claude' binary that emits stream-json resembling the real thing.

Used for testing the cyberdeck plumbing without depending on a real Claude
Code install or a live Max subscription. Accepts -p <task> and ignores
everything else. Emits a scripted sequence of events with realistic timing.

Drop this on PATH as 'claude' (or pass explicitly via --claude-bin) to
smoke-test the Construct class.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def main() -> int:
    # We only care about -p; everything else is accepted and ignored so
    # the mock stays compatible with the real CLI's arg surface.
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--print", dest="task", default="", nargs="?")
    args, _unknown = parser.parse_known_args()

    session_id = f"mock-{uuid.uuid4().hex[:12]}"
    # Prefer stdin if the caller piped a prompt that way (the cyberdeck
    # auto-routes multiline tasks through stdin to dodge Windows argv
    # mangling — same reason the daemon does it). Mirrors mock_daemon's
    # behavior so multiline-prompt tests work end-to-end.
    task = args.task or "unknown task"
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
            if stdin_data.strip():
                task = stdin_data.strip()
        except Exception:
            pass

    # Init
    emit({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "tools": ["Bash", "Read", "Edit", "WebSearch"],
        "cwd": ".",
    })
    time.sleep(0.25)

    # Assistant announces plan
    emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Working on: {task}. I'll start by listing the directory."},
            ],
        },
    })
    time.sleep(0.4)

    # Assistant calls a tool
    emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_001",
                    "name": "Bash",
                    "input": {"command": "ls -la"},
                },
            ],
        },
    })
    time.sleep(0.35)

    # Tool result (stream-json represents these as user turns)
    emit({
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu_001",
                    "content": "total 24\ndrwxr-xr-x 3 user user 4096 .\n-rw-r--r-- 1 user user  120 readme.md",
                },
            ],
        },
    })
    time.sleep(0.3)

    # Assistant continues
    emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Found a readme and one subdir. Task complete."},
            ],
        },
    })
    time.sleep(0.2)

    # Final result stat block
    emit({
        "type": "system",
        "subtype": "result",
        "session_id": session_id,
        "duration_ms": 1500,
        "num_turns": 3,
        "is_error": False,
    })

    return 0


if __name__ == "__main__":
    sys.exit(main())
