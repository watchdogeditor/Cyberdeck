#!/usr/bin/env python3
"""
Mock 'claude' binary for testing the Daemon.

Handles BOTH modes:
- One-shot (default, used by non-streaming Daemon): reads prompt from
  -p arg OR stdin, emits a single scripted response as stream-json, exits.
- Streaming (--input-format stream-json): stays alive, reads one JSON
  message per stdin line, emits a scripted response per message, loops
  until stdin closes.

Response logic (identical in both modes):
  - If message contains "GOAL:", decompose into 3 subtasks (status=waiting)
  - If message contains "OUTCOMES:", mark done (status=done)
  - Otherwise emit a default waiting response

Accepts and ignores all other Claude Code flags. Drop-in for `claude`
for testing purposes only.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def emit_init(session_id: str) -> None:
    emit({
        "type": "system",
        "subtype": "init",
        "session_id": session_id,
        "tools": [],
        "cwd": ".",
    })


def emit_assistant_text(text: str) -> None:
    emit({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
        },
    })


def emit_result(session_id: str, duration_ms: int) -> None:
    emit({
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "duration_ms": duration_ms,
        "is_error": False,
    })


def decide_response(prompt: str) -> dict:
    if "GOAL:" in prompt:
        return {
            "thinking": "Breaking the goal into three concrete subtasks",
            "chat": "I'll handle this as three parallel workstreams.",
            "actions": [
                {"type": "spawn", "task": "subtask alpha (mock)"},
                {"type": "spawn", "task": "subtask beta (mock)"},
                {"type": "spawn", "task": "subtask gamma (mock)"},
            ],
            "status": "waiting",
        }
    if "OUTCOMES:" in prompt:
        return {
            "thinking": "All three subtasks completed; goal is satisfied",
            "chat": "All three constructs finished. Goal achieved.",
            "actions": [],
            "status": "done",
        }
    return {
        "thinking": "Awaiting further input",
        "chat": None,
        "actions": [],
        "status": "waiting",
    }


def answer_prompt(prompt: str, session_id: str) -> None:
    """Emit one full turn's worth of events for a single prompt."""
    response = decide_response(prompt)
    assistant_text = f"```json\n{json.dumps(response, indent=2)}\n```"
    emit_assistant_text(assistant_text)
    time.sleep(0.05)
    emit_result(session_id, 200)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--print", dest="prompt", default="", nargs="?")
    parser.add_argument("--resume", dest="resume", default=None)
    parser.add_argument("--input-format", dest="input_format", default="text")
    args, _unknown = parser.parse_known_args()

    session_id = args.resume or f"mock-dm-{uuid.uuid4().hex[:12]}"
    emit_init(session_id)
    time.sleep(0.1)

    if args.input_format == "stream-json":
        # Streaming mode: one JSON message per line, loop until EOF.
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = msg.get("message", {}).get("content", "")
            if isinstance(content, list):
                # content can be a list of blocks; concat text ones
                content = "".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            answer_prompt(str(content), session_id)
        return 0

    # One-shot mode: prompt from stdin if piped, else -p arg
    prompt = args.prompt or ""
    if not sys.stdin.isatty():
        try:
            stdin_data = sys.stdin.read()
            if stdin_data.strip():
                prompt = stdin_data
        except Exception:
            pass
    answer_prompt(prompt, session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

