"""
Milestone Zero: spawn one construct, print its events, exit cleanly.

Usage:
    python main.py "list files in the current directory"           # uses real `claude`
    python main.py "some task" ./mock_claude.py                    # uses mock for testing

This is the smallest possible proof that the plumbing works. No daemon,
no watchdog, no TUI, no routing. Just: subprocess spawns, events flow,
shutdown is clean.
"""
from __future__ import annotations

import asyncio
import signal
import sys
from construct import Construct
from display import summarize


def run_prefix(c, event) -> str:
    return f"[{c.id} +{event.timestamp - c._started_at:5.2f}s]"


async def run_one(task: str, claude_bin: str, permission_mode: str) -> int:
    c = Construct(task=task, claude_bin=claude_bin, permission_mode=permission_mode)
    print(f"[deck] spawning {c.id}  bin={claude_bin}  mode={permission_mode}  task={task!r}")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _request_stop():
        if not stop.is_set():
            print("\n[deck] shutdown requested, killing construct...")
            stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass  # Windows

    try:
        await c.spawn()
    except FileNotFoundError:
        print(f"[deck] ERROR: could not find {claude_bin!r}. "
              f"Install Claude Code or pass a mock binary path.")
        return 2

    async def consume():
        try:
            async for event in c.events():
                prefix = f"[{c.id} +{event.timestamp - c._started_at:5.2f}s]"
                print(f"{prefix} {event.kind:14s} {summarize(event.raw)}")
        finally:
            # Always finalize, even if iteration was cancelled or we bailed
            # early. wait() is idempotent and safe to call repeatedly.
            await c.wait()

    consume_task = asyncio.create_task(consume())
    stop_task = asyncio.create_task(stop.wait())

    done, pending = await asyncio.wait(
        {consume_task, stop_task},
        return_when=asyncio.FIRST_COMPLETED,
    )

    if stop_task in done and not consume_task.done():
        await c.kill()
        consume_task.cancel()
        try:
            await consume_task
        except asyncio.CancelledError:
            pass

    for p in pending:
        p.cancel()

    # Belt-and-suspenders: make sure we've got terminal state before we
    # print the summary. wait() is idempotent so this is cheap if consume
    # already drove finalization.
    await c.wait()

    print(f"[deck] {c.id} ended: state={c.state.value} exit={c.exit_code} runtime={c.runtime:.2f}s")
    if c.stderr.strip():
        print(f"[deck] stderr:\n{c.stderr}")
    return 0 if c.state.value in ("done", "killed") else 1


if __name__ == "__main__":
    # Usage:
    #   python main.py "<task>"
    #   python main.py "<task>" <claude_bin>
    #   python main.py "<task>" <claude_bin> <permission_mode>
    # permission_mode: default | acceptEdits | bypassPermissions | plan
    # We default to acceptEdits because 'default' mode prompts for writes
    # interactively, which deadlocks in headless use.
    task = sys.argv[1] if len(sys.argv) > 1 else "list files in the current directory and briefly describe each"
    claude_bin = sys.argv[2] if len(sys.argv) > 2 else "claude"
    permission_mode = sys.argv[3] if len(sys.argv) > 3 else "bypassPermissions"
    sys.exit(asyncio.run(run_one(task, claude_bin, permission_mode)))
