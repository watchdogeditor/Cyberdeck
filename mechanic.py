"""
mechanic.py — Cyberdeck supervisor (v0, supervisor-only).

A sibling Python process to the deck. Watches the deck's PID, tracks
its claude subprocess PIDs from the per-launch NDJSON log file, and
kills the subprocesses on detected deck death so they don't orphan
when the deck dies (Task Manager kill, OOM, blue screen, etc.).

v0 scope (this file): supervisor only. No claude dependency, no LLM,
no UI. Cross-platform pure-Python — works on every platform Python
runs on without per-OS plumbing (no Windows Job Objects, no Linux
PR_SET_PDEATHSIG, no `psutil` dep). The deck publishes child-process
pids on its existing event bus; this process reads them out of the
file logger's NDJSON stream.

v1+ (future): an on-demand LLM session for triage when the deck dies
uncleanly, or summoned from the deck's UI. See
`Design Files/cyberdeck-maintbot-design.md`.

Usage:
    python mechanic.py
    python mechanic.py --log-dir <dir>
    python mechanic.py --watch-deck-pid 12345

When --watch-deck-pid is omitted, the supervisor discovers the deck
PID by reading the `pid` field of the most recent log file's header.
This is the everyday path — `launch.bat` spawns the supervisor
without an explicit PID and lets it self-discover.

What gets killed on deck death:
- Construct claude subprocesses (tracked via fleet.spawn events).

What does NOT get killed (yet — follow-up slices):
- The daemon's claude subprocess.
- The watchdog's claude Q&A subprocess.
- Watchdog tripwire-authoring one-shot subprocesses.
- Pool-warming subprocesses.

Those four sources don't publish their pids to the bus today; until
they do, they orphan the same way they did before this supervisor
existed. Filed for follow-up — the moment any spawn site adds a
`pid` to its bus event payload, this file picks it up automatically
(it tracks anything that comes through `fleet.spawn`-shaped events;
extending to other kinds is a small change to `_apply_record`).
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional


# ---- platform-aware pid helpers ---------------------------------------


def _pid_alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but isn't ours to signal — count as alive.
        # Treating a foreign process as alive is the safe direction:
        # the worst case is the supervisor never gives up watching.
        return True
    except OSError:
        return False


def _pid_alive_win(pid: int) -> bool:
    """Windows: open a query handle and read the exit code. STILL_ACTIVE
    (259) means the process is running; any other code means it exited
    with that value. OpenProcess returning NULL means the pid is gone
    (or we don't have rights, which we usually do for our own children).

    **Critical: explicit ctypes argtypes/restype.** Without them,
    ctypes defaults to `c_int` (32-bit signed) for the HANDLE return
    of OpenProcess. On 64-bit Windows, HANDLE is pointer-sized
    (8 bytes); the default truncates to 32 bits. The truncated value
    may "look" non-NULL to `if not h:` but is corrupt — passing it
    to GetExitCodeProcess fails (returns 0), and `_pid_alive_win`
    returns False even though the process is fully alive. Symptom:
    mechanic immediately reports the deck dead right after launch.
    Real-deck-confirmed 2026-05-06 on Python 3.14.3 + Windows 11.
    Fix: declare argtypes + restype with `ctypes.wintypes` so HANDLE
    is `c_void_p`-shaped on 64-bit and DWORD/BOOL stay correctly
    sized regardless of architecture.

    Filed in cyberdeck-state.md Async/subprocess gotchas — the
    "ctypes Windows-handle truncation" landmine. Generalizes
    beyond mechanic: any future ctypes call into kernel32 / user32
    / etc. needs explicit types."""
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32

    # OpenProcess(DWORD dwDesiredAccess, BOOL bInheritHandle, DWORD dwProcessId) -> HANDLE
    OpenProcess = kernel32.OpenProcess
    OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    OpenProcess.restype = wintypes.HANDLE

    # GetExitCodeProcess(HANDLE hProcess, LPDWORD lpExitCode) -> BOOL
    GetExitCodeProcess = kernel32.GetExitCodeProcess
    GetExitCodeProcess.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
    ]
    GetExitCodeProcess.restype = wintypes.BOOL

    # CloseHandle(HANDLE hObject) -> BOOL
    CloseHandle = kernel32.CloseHandle
    CloseHandle.argtypes = [wintypes.HANDLE]
    CloseHandle.restype = wintypes.BOOL

    h = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        exit_code = wintypes.DWORD()
        ok = GetExitCodeProcess(h, ctypes.byref(exit_code))
        return bool(ok) and exit_code.value == STILL_ACTIVE
    finally:
        CloseHandle(h)


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        return _pid_alive_win(pid)
    return _pid_alive_posix(pid)


def kill_pid(pid: int, *, grace: float = 2.0) -> bool:
    """Best-effort kill. SIGTERM first; on POSIX, escalates to SIGKILL
    after `grace` seconds if the process is still alive. On Windows,
    `os.kill(pid, SIGTERM)` maps to TerminateProcess (immediate, no
    graceful shutdown), so no escalation is needed.

    Returns True if the process is no longer alive after the call,
    False if we couldn't deliver the signal AND the pid is still
    around (e.g. PermissionError on a foreign process — shouldn't
    happen for our own children but the guard is cheap)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return not pid_alive(pid)

    if sys.platform == "win32":
        # TerminateProcess is synchronous-ish — usually dead by next call.
        return not pid_alive(pid)

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    return not pid_alive(pid)


# ---- log discovery + tail ---------------------------------------------

# Stale-file cutoff for `find_log_file`. The supervisor only cares about
# *the current launch's* log; anything older than this is yesterday's
# debris and we don't want to attach to it. 5 minutes is huge compared
# to a real launch sequence (sub-second) and tiny compared to "I left
# the machine on overnight." Tunable here, not exposed as a CLI flag.
_LOG_FRESHNESS_SECONDS = 300.0


def find_log_file(log_dir: Path) -> Optional[Path]:
    """Return the most-recently-modified `cyberdeck-*.log` in `log_dir`
    that's been touched within `_LOG_FRESHNESS_SECONDS`, or None.

    Skips `latest.log` deliberately — on Windows the file logger writes
    that as a one-shot copy at startup (symlinks require admin / dev
    mode), so its content is stale forever after. The timestamped
    per-launch files are the only correct surface to tail."""
    if not log_dir.is_dir():
        return None
    now = time.time()
    candidates = []
    for p in log_dir.glob("cyberdeck-*.log"):
        if not p.is_file():
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if now - mtime > _LOG_FRESHNESS_SECONDS:
            continue
        candidates.append((mtime, p))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


class LogTail:
    """Incremental NDJSON reader. Holds a byte offset into the file
    and catches up new lines on each `catch_up()` call. Tolerant of
    partial last-line writes — the deck flushes line-buffered, but a
    half-written line during a crash is still possible."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._pos = 0
        self._buf = ""

    def catch_up(self) -> list[dict]:
        """Read all new whole lines and parse as NDJSON. Returns a
        list of records (possibly empty). Bad JSON lines are silently
        dropped — forward progress is the goal, not perfection."""
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                fp.seek(self._pos)
                chunk = fp.read()
                self._pos = fp.tell()
        except OSError:
            return []
        if not chunk:
            return []
        self._buf += chunk
        records: list[dict] = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records


# ---- record processing ------------------------------------------------


def _apply_record(record: dict, tracked: dict[str, int]) -> Optional[str]:
    """Apply one NDJSON record to the tracked-pid set. Mutates
    `tracked` in place. Returns the log_footer's reason string if
    this record is a footer, else None.

    Bus event payload shape (the bit we care about):
        {
          "kind": "fleet.spawn",
          "construct_id": "cx-abc123",
          "payload": {                      # the FleetEvent dataclass
            "kind": "meta",
            "construct_id": "cx-abc123",
            "payload": {                    # the meta event dict
              "type": "spawned",
              "pid": 12345,
              ...
            }
          }
        }

    Two layers of nested `payload` because the bus envelope (DeckEvent)
    carries the full FleetEvent as its payload, and the FleetEvent
    itself has a `payload` dict for meta events. The file logger's
    `_serialize_payload` walks both via `__dict__` recursively, so
    the structure on disk matches the in-memory shape.
    """
    rtype = record.get("type")
    if rtype == "log_footer":
        return record.get("reason") or "unspecified"

    kind = record.get("kind", "")
    if kind == "fleet.spawn":
        cid = record.get("construct_id")
        outer = record.get("payload") or {}
        inner = outer.get("payload") or {}
        pid = inner.get("pid")
        if cid and isinstance(pid, int):
            tracked[cid] = pid
    elif kind == "fleet.finalize":
        cid = record.get("construct_id")
        if cid:
            tracked.pop(cid, None)
    elif kind in ("fleet.spawn_failed", "fleet.spawn_blocked"):
        # No subprocess came up — nothing to track.
        pass
    return None


# ---- v1.5 stale-heartbeat triage state machine ------------------------
#
# When the heartbeat goes stale, the supervisor doesn't immediately fire
# triage — the most common cause of stale heartbeat is the netrunner
# suspending their machine, NOT a wedged TUI. Auto-firing on every
# stale event would burn ~$0.10 + 2 minutes of triage time per suspend.
#
# Instead: prompt the netrunner interactively (via stderr) asking
# whether to triage. While the prompt is open, keep polling heartbeat.
# Three resolutions:
#
#   1. Heartbeat recovers WHILE prompt is open → print recovery notice,
#      ask the netrunner to dismiss the now-stale prompt. The deck is
#      back; no triage needed.
#   2. Netrunner answers "y" AND heartbeat is still stale → kill the
#      deck (graceful SIGTERM with grace, escalating to SIGKILL on
#      POSIX) so the log gets finalized cleanly, then run the same
#      finalize+triage flow that fires on natural deck death. The
#      triage gets a `wedge_kill_context` flag so the report knows
#      the supervisor force-killed (vs. natural crash).
#   3. Netrunner answers "n" / Enter → triage dismissed for this stale
#      event. State machine flips to DISMISSED; re-arms only when
#      heartbeat goes fresh again (so a future stale event re-prompts).
#
# `--auto-triage-on-stale` flag bypasses the prompt entirely, going
# straight to the kill-and-triage path. Useful for headless / wall-
# mount deployments where there's no netrunner to answer the prompt.


class _HeartbeatState:
    IDLE = "idle"            # heartbeat fresh, no prompt active
    PROMPTING = "prompting"  # prompt thread alive, waiting for response
    DISMISSED = "dismissed"  # netrunner said no; awaiting recovery before re-arming


def _ask_triage(response: dict, age_seconds: float) -> None:
    """Run from a daemon thread. Block on input(), record the answer.

    `response` is a dict shared with the main thread:
        {"value": Optional[str], "done": bool}
    The main thread polls `done` each tick and reads `value` when set.

    On EOFError (no stdin attached, e.g. headless launch with stdin
    closed) records empty string — the main thread treats that as
    "no answer / dismissed."
    """
    try:
        ans = input(
            f"\n[mechanic] Heartbeat stale ({age_seconds:.0f}s). "
            f"Trigger v1 triage? Will kill deck first. (y/N): "
        )
        response["value"] = ans
    except EOFError:
        response["value"] = ""
    except Exception as e:
        response["value"] = ""
        # Best-effort log; don't propagate (we're a daemon thread).
        try:
            print(
                f"[mechanic] triage prompt thread error: {e}",
                file=sys.stderr,
            )
        except Exception:
            pass
    finally:
        response["done"] = True


# ---- main loop --------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Cyberdeck Mechanic supervisor v0 — sibling process that "
            "kills orphan claude subprocesses on detected deck death."
        ),
    )
    parser.add_argument(
        "--watch-deck-pid",
        type=int,
        default=None,
        help=(
            "Deck PID to watch. If omitted, read from the log header "
            "of the most recent log file (the everyday path)."
        ),
    )
    parser.add_argument(
        "--log-dir",
        type=str,
        default=None,
        help=(
            "Directory containing the deck's per-launch log files. "
            "Default: $CYBERDECK_LOG_DIR or `<this script>/logs/`. "
            "Matches the deck's default."
        ),
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Heartbeat / log-tail interval in seconds (default 2.0).",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=30.0,
        help=(
            "Seconds to wait for a log file (and its header) to "
            "appear before giving up (default 30). Lets the "
            "supervisor launch slightly before the deck without "
            "missing the attach window."
        ),
    )
    parser.add_argument(
        "--heartbeat-path",
        type=str,
        default=None,
        help=(
            "Path to the deck's liveness heartbeat file. Default: "
            "$CYBERDECK_HOME/.cyberdeck/heartbeat (or "
            "<deck>/cyberdeck-home/.cyberdeck/heartbeat). The deck "
            "writes this file every 5s; if its mtime falls more "
            "than --heartbeat-stale-seconds behind wall-clock, the "
            "supervisor flags the deck as wedged (PID alive but "
            "TUI frozen) and logs a one-time diagnostic. v0+1 "
            "logs only — v1 LLM session (deferred) will decide "
            "what to do."
        ),
    )
    parser.add_argument(
        "--heartbeat-stale-seconds",
        type=float,
        default=20.0,
        help=(
            "How old the heartbeat file's mtime can get before "
            "the supervisor flags it as stale (default 20s). "
            "The deck writes every 5s, so 20s = ~4 missed ticks."
        ),
    )
    parser.add_argument(
        "--no-triage",
        action="store_true",
        help=(
            "Disable v1 LLM-session triage on unclean deck exit. "
            "Default behavior fires a `claude -p` triage call after "
            "subprocess cleanup whenever the deck dies without "
            "writing a clean shutdown/eject footer; the report "
            "lands at <log-basename>-triage.md next to the log. "
            "Disable this if you don't want claude burning tokens "
            "on every crash (e.g. while iterating on a known-flaky "
            "branch). Also suppresses v1.5 stale-heartbeat triage."
        ),
    )
    parser.add_argument(
        "--auto-triage-on-stale",
        action="store_true",
        help=(
            "v1.5 (2026-05-06): when the heartbeat goes stale, fire "
            "triage automatically without prompting. Default is "
            "interactive — supervisor prints a y/N prompt to stderr "
            "and waits for the netrunner to confirm before "
            "killing the deck and triaging (which avoids burning "
            "tokens on every machine-suspend false positive). Set "
            "this flag in headless / wall-mount deployments where "
            "no netrunner is sitting at the terminal to answer."
        ),
    )
    parser.add_argument(
        "--triage-timeout",
        type=float,
        default=180.0,
        help=(
            "Per-call hard timeout for the v1 triage subprocess "
            "(default 180s). Mechanic kills the claude process and "
            "writes a stub failure report if the LLM call doesn't "
            "complete in time."
        ),
    )
    parser.add_argument(
        "--claude-bin",
        type=str,
        default="claude",
        help=(
            "Path to the claude binary. Default 'claude' (uses PATH). "
            "Match the deck's CLAUDE_BIN env var if it's set "
            "elsewhere on disk."
        ),
    )
    return parser.parse_args()


def resolve_heartbeat_path(arg_path: Optional[str]) -> Path:
    """Resolve the deck heartbeat file path. Mirrors resolve_log_dir
    in shape — explicit arg wins, then $CYBERDECK_HOME, then the
    default <deck>/cyberdeck-home/.cyberdeck/heartbeat. The deck
    writes this file every 5s; the supervisor reads its mtime to
    detect wedged-TUI cases the PID-only watch misses."""
    if arg_path:
        return Path(arg_path)
    home_env = os.environ.get("CYBERDECK_HOME")
    if home_env:
        return Path(home_env) / ".cyberdeck" / "heartbeat"
    # Default: assume the deck home is alongside the mechanic source
    # (the launch.bat convention puts both in the same dir).
    return Path(__file__).resolve().parent / "cyberdeck-home" / ".cyberdeck" / "heartbeat"


def heartbeat_age_seconds(path: Path) -> Optional[float]:
    """Return seconds since the heartbeat file was last touched.
    None if the file doesn't exist (supervisor pre-attach window,
    or the deck was launched without the heartbeat writer for any
    reason)."""
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def resolve_log_dir(arg_dir: Optional[str]) -> Path:
    if arg_dir:
        return Path(arg_dir)
    env = os.environ.get("CYBERDECK_LOG_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "logs"


def _peek_log_header_pid(log_path: Path) -> Optional[int]:
    """Read just the first line of a log file. If it's a valid
    `log_header` record with a `pid` int, return that pid; else None.

    Cheap (one line read), no file lock, tolerant of partial /
    in-progress writes (returns None silently). Used by
    `wait_for_log_file` to validate liveness before committing to a
    log file as the supervision target."""
    try:
        with open(log_path, "r", encoding="utf-8") as fp:
            line = fp.readline()
        if not line:
            return None
        rec = json.loads(line)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    if rec.get("type") != "log_header":
        return None
    pid = rec.get("pid")
    if isinstance(pid, int):
        return pid
    return None


def wait_for_log_file(log_dir: Path, timeout: float) -> Optional[Path]:
    """Poll `log_dir` for a fresh `cyberdeck-*.log` whose deck pid
    is currently alive. Returns that log path, or None if no
    alive-deck log appears within `timeout`.

    **Liveness check is what differentiates this from a naive
    "newest fresh file" pick.** Filed 2026-05-06: when the
    netrunner kills the deck and relaunches quickly, the previous
    launch's log file is still within the 5-minute freshness
    window (`_LOG_FRESHNESS_SECONDS`) and may have a more recent
    mtime than the new deck's log file (which hasn't been written
    yet because Python is still starting up). Without the
    pid_alive check, mechanic would attach to the stale log,
    read the dead pid from its header, and immediately fire
    triage on a deck that's actually fine. The bug surfaced
    intermittently as "mechanic reports deck dead immediately
    after launch when I restart quickly" — symptom matches
    exactly. Fix: validate the header's pid is currently alive
    before committing.

    Stale logs (header pid dead) are silently skipped each poll;
    the loop continues waiting for either the new deck's log to
    be written OR the timeout to elapse. If timeout elapses with
    nothing alive, returns None — the supervisor exits cleanly
    rather than misattaching to a dead deck."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = find_log_file(log_dir)
        if log is not None:
            pid = _peek_log_header_pid(log)
            if pid is not None and pid_alive(pid):
                return log
            # else: stale log (no header yet, or header pid dead).
            # Keep polling — the new deck may still be starting up
            # and writing its log file.
        time.sleep(0.5)
    return None


def main() -> int:
    args = parse_args()
    log_dir = resolve_log_dir(args.log_dir)

    print(
        f"[mechanic] starting; log_dir={log_dir} "
        f"poll={args.poll_interval}s",
        file=sys.stderr,
    )

    log_path = wait_for_log_file(log_dir, args.startup_timeout)
    if log_path is None:
        print(
            f"[mechanic] no fresh cyberdeck-*.log appeared in "
            f"{log_dir} within {args.startup_timeout}s; nothing to "
            f"supervise. Exiting.",
            file=sys.stderr,
        )
        return 1
    print(f"[mechanic] tailing {log_path.name}", file=sys.stderr)

    tail = LogTail(log_path)
    deck_pid: Optional[int] = args.watch_deck_pid
    tracked: dict[str, int] = {}  # construct_id → pid
    clean_close_reason: Optional[str] = None

    # Read forward until we've seen the header (for deck pid) and any
    # spawn events that landed before we got here. Header is the
    # very first line written, so this loop usually exits after one
    # tick.
    header_deadline = time.monotonic() + args.startup_timeout
    while deck_pid is None and time.monotonic() < header_deadline:
        for record in tail.catch_up():
            _apply_record(record, tracked)
            if record.get("type") == "log_header":
                pid = record.get("pid")
                if isinstance(pid, int):
                    deck_pid = pid
        if deck_pid is None:
            time.sleep(0.5)

    if deck_pid is None:
        print(
            f"[mechanic] log file present but no header pid found "
            f"within {args.startup_timeout}s; can't supervise without "
            f"a deck pid. Exiting.",
            file=sys.stderr,
        )
        return 1
    print(f"[mechanic] watching deck pid={deck_pid}", file=sys.stderr)

    # Mechanic v0→v1 bridge (2026-05-04): liveness heartbeat. The
    # deck writes <home>/.cyberdeck/heartbeat every 5s; we read its
    # mtime each tick. PID alive + heartbeat stale = wedged TUI
    # (event loop stuck, redraw cycle frozen). v0 catches deck-died;
    # this catches deck-frozen.
    #
    # v1.5 (2026-05-06): on stale heartbeat, prompt the netrunner
    # interactively (or auto-fire under --auto-triage-on-stale).
    # Most-common stale cause is machine suspend, NOT a wedged TUI;
    # auto-firing would burn ~$0.10 + ~2 minutes per suspend. The
    # state machine + prompt-thread pattern lets us listen for
    # heartbeat recovery while waiting for the netrunner's answer.
    # See _ask_triage and _HeartbeatState above for the full design.
    heartbeat_path = resolve_heartbeat_path(args.heartbeat_path)
    print(
        f"[mechanic] heartbeat file: {heartbeat_path}",
        file=sys.stderr,
    )
    hb_state = _HeartbeatState.IDLE
    prompt_response: dict = {"value": None, "done": False}
    prompt_thread: Optional[threading.Thread] = None
    recovery_announced_during_prompt = False
    # Set when a kill-and-triage flow should fire after the loop
    # breaks. Carries the wedge context (last seen heartbeat age)
    # so the triage prompt can mention "deck PID was alive but
    # heartbeat stale Xs; supervisor force-killed for diagnosis."
    wedge_kill_context: Optional[str] = None

    # Heartbeat loop. Catch up the log on every tick, check deck
    # liveness, check heartbeat freshness, sleep. Ctrl+C on the
    # supervisor terminal breaks out cleanly without killing tracked
    # subprocesses — interrupting the supervisor is not the same
    # signal as deck-died.
    try:
        while True:
            for record in tail.catch_up():
                reason = _apply_record(record, tracked)
                if reason is not None:
                    clean_close_reason = reason
            if not pid_alive(deck_pid):
                break

            age = heartbeat_age_seconds(heartbeat_path)
            is_stale = (
                age is not None
                and age > args.heartbeat_stale_seconds
            )

            # State machine:
            #
            #   IDLE       → on stale: detect, then either
            #                  (a) auto-fire if --auto-triage-on-stale
            #                  (b) spawn prompt thread, → PROMPTING
            #   PROMPTING  → on heartbeat recovery: print recovery
            #                  notice (one-shot); the prompt thread
            #                  may still be blocked on input(), but
            #                  the answer will be ignored when it
            #                  arrives if heartbeat is fresh.
            #              → on prompt completion ('y'): if still
            #                  stale → kill deck + break (drops to
            #                  finalize); if recovered → notify, reset
            #                  to IDLE.
            #              → on prompt completion ('n'/empty): notify,
            #                  → DISMISSED.
            #   DISMISSED  → on heartbeat fresh: → IDLE (re-arms
            #                  prompt for any future stale event).

            if hb_state == _HeartbeatState.IDLE and is_stale:
                # First detection of a fresh stale event.
                print(
                    f"[mechanic] STALE HEARTBEAT detected: "
                    f"file is {age:.1f}s old "
                    f"(threshold={args.heartbeat_stale_seconds}s); "
                    f"deck pid={deck_pid} is alive but TUI may be "
                    f"wedged. Common false-positive: machine "
                    f"suspended (heartbeat resumes when netrunner "
                    f"unsuspends).",
                    file=sys.stderr,
                )
                if args.no_triage:
                    print(
                        f"[mechanic] --no-triage set; not "
                        f"prompting for triage. Will log only.",
                        file=sys.stderr,
                    )
                    hb_state = _HeartbeatState.DISMISSED
                elif args.auto_triage_on_stale:
                    print(
                        f"[mechanic] --auto-triage-on-stale set; "
                        f"killing deck for triage without prompt",
                        file=sys.stderr,
                    )
                    wedge_kill_context = (
                        f"Heartbeat went stale ({age:.1f}s old at "
                        f"detection). --auto-triage-on-stale set, "
                        f"so the supervisor force-killed the deck "
                        f"for diagnosis without prompting."
                    )
                    if kill_pid(deck_pid):
                        print(
                            f"[mechanic] deck pid={deck_pid} killed; "
                            f"proceeding to triage",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"[mechanic] kill_pid({deck_pid}) "
                            f"reported failure; proceeding anyway",
                            file=sys.stderr,
                        )
                    break
                else:
                    # Spawn prompt thread; loop continues.
                    prompt_response = {"value": None, "done": False}
                    recovery_announced_during_prompt = False
                    prompt_thread = threading.Thread(
                        target=_ask_triage,
                        args=(prompt_response, age),
                        daemon=True,
                    )
                    prompt_thread.start()
                    hb_state = _HeartbeatState.PROMPTING

            elif hb_state == _HeartbeatState.PROMPTING:
                # While prompt thread is blocked on input(), watch
                # for heartbeat recovery. If it comes back, print
                # a one-shot recovery notice — the netrunner can
                # see the prompt is moot. We don't try to kill the
                # input thread; once the netrunner hits Enter or
                # types something, the answer will be ignored
                # because is_stale will be False at decision time.
                if not is_stale and not recovery_announced_during_prompt:
                    age_str = f"{age:.1f}s" if age is not None else "?"
                    print(
                        f"[mechanic] heartbeat RECOVERED ({age_str}) "
                        f"while triage prompt is open. Press Enter "
                        f"or type N to dismiss the prompt above; "
                        f"the deck appears responsive again, no "
                        f"triage needed.",
                        file=sys.stderr,
                    )
                    recovery_announced_during_prompt = True

                # Did the prompt thread complete? If yes, route
                # based on answer + current heartbeat state.
                if prompt_response.get("done"):
                    raw = prompt_response.get("value") or ""
                    ans = raw.strip().lower()
                    accept = ans in ("y", "yes")

                    if accept and is_stale:
                        # Netrunner confirmed AND heartbeat still
                        # stale — kill the deck and drop to
                        # finalize+triage path with wedge context.
                        wedge_kill_context = (
                            f"Heartbeat went stale ({age:.1f}s old "
                            f"at confirmation). Netrunner approved "
                            f"force-kill via the supervisor's "
                            f"interactive prompt; the supervisor "
                            f"killed the deck so the triage runs "
                            f"against a finalized log."
                        )
                        print(
                            f"[mechanic] netrunner confirmed; "
                            f"killing deck pid={deck_pid}",
                            file=sys.stderr,
                        )
                        if kill_pid(deck_pid):
                            print(
                                f"[mechanic] deck killed; "
                                f"proceeding to triage",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"[mechanic] kill_pid({deck_pid}) "
                                f"reported failure; proceeding anyway",
                                file=sys.stderr,
                            )
                        break
                    elif accept and not is_stale:
                        # Netrunner confirmed but heartbeat
                        # recovered first. Don't triage — the deck
                        # is back. Reset to IDLE so a future stale
                        # event re-prompts.
                        print(
                            f"[mechanic] netrunner confirmed "
                            f"triage but heartbeat recovered "
                            f"before kill; not triaging. Returning "
                            f"to BAU.",
                            file=sys.stderr,
                        )
                        hb_state = _HeartbeatState.IDLE
                        prompt_thread = None
                    else:
                        # Dismiss. Will re-arm when heartbeat
                        # recovers (DISMISSED → IDLE transition).
                        print(
                            f"[mechanic] triage dismissed. Will "
                            f"re-prompt if heartbeat goes stale "
                            f"again after recovery.",
                            file=sys.stderr,
                        )
                        hb_state = _HeartbeatState.DISMISSED
                        prompt_thread = None

            elif hb_state == _HeartbeatState.DISMISSED and not is_stale:
                # Recovery after a dismissed prompt — re-arm.
                age_str = f"{age:.1f}s" if age is not None else "?"
                print(
                    f"[mechanic] heartbeat recovered ({age_str}); "
                    f"re-arming v1.5 stale-heartbeat prompt for "
                    f"future stale events",
                    file=sys.stderr,
                )
                hb_state = _HeartbeatState.IDLE

            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        print(
            f"[mechanic] interrupted; releasing watch on pid={deck_pid} "
            f"without cleanup",
            file=sys.stderr,
        )
        return 0

    # Deck is dead. One final tail pass — pick up any spawn/finalize
    # events that landed between our last catch_up and the death.
    for record in tail.catch_up():
        reason = _apply_record(record, tracked)
        if reason is not None:
            clean_close_reason = reason

    # Cleanup. Even when the deck wrote a clean log_footer
    # (shutdown / eject), kill the tracked pids belt-and-suspenders.
    # The deck's own finalization paths usually already killed them,
    # so this is mostly a no-op then. Cost of a redundant kill is
    # zero; cost of skipping a real orphan is one stuck claude
    # session burning quota.
    print(
        f"[mechanic] deck pid={deck_pid} died "
        f"(clean_reason={clean_close_reason!r}); "
        f"cleaning up {len(tracked)} tracked subprocess(es)",
        file=sys.stderr,
    )

    killed = 0
    skipped = 0
    failed = 0
    for cid, pid in list(tracked.items()):
        if not pid_alive(pid):
            skipped += 1
            continue
        if kill_pid(pid):
            killed += 1
            print(
                f"[mechanic] killed pid={pid} ({cid})",
                file=sys.stderr,
            )
        else:
            failed += 1
            print(
                f"[mechanic] failed to kill pid={pid} ({cid})",
                file=sys.stderr,
            )

    print(
        f"[mechanic] done - killed={killed} already_dead={skipped} "
        f"failed={failed}",
        file=sys.stderr,
    )

    # Mechanic v1: diagnose-only LLM session (build-plan 0e). Fires
    # only on UNCLEAN exit — when the deck recorded a clean
    # `shutdown` or `eject` close_reason in its log_footer, the
    # netrunner closed the deck deliberately and triage adds noise.
    # Anything else (Python traceback, OOM, kill -9, blue screen,
    # network-driven hang) → fire triage. Synchronous: the
    # supervisor blocks until the report is written. ~10-30s on
    # cache-warm, longer on cold. Result + summary go to stderr;
    # full report writes to <log-basename>-triage.md next to the
    # original log. Best-effort throughout — triage failure
    # produces a stub report explaining what failed; the
    # supervisor never panics over an LLM call.
    if args.no_triage:
        print(
            f"[mechanic] triage disabled by --no-triage; skipping",
            file=sys.stderr,
        )
        return 0
    is_unclean = clean_close_reason not in ("shutdown", "eject")
    if not is_unclean:
        print(
            f"[mechanic] clean close ({clean_close_reason!r}); "
            f"skipping triage",
            file=sys.stderr,
        )
        return 0
    try:
        # Local import so the mechanic doesn't pay module-load cost
        # on the common clean-shutdown path. The LLM session module
        # has its own dependencies (tempfile, subprocess.run for the
        # synchronous claude call); cleaner to lazy-load.
        from mechanic_triage import TriageRequest, run_triage
    except Exception as e:
        print(
            f"[mechanic] triage module import failed: {e}; "
            f"skipping triage",
            file=sys.stderr,
        )
        return 0

    # Locate deck source — the directory containing tui.py +
    # Design Files/. The triage LLM uses Read/Glob/Grep against it
    # for gotcha lookups. Heuristic: walk up from log_dir looking
    # for tui.py. Falls back to None on miss; triage runs without
    # source-tree access (still useful — it has the log itself).
    deck_source_dir = _locate_deck_source(log_path.parent)

    print(
        f"[mechanic] firing v1 triage (unclean exit; "
        f"reason={clean_close_reason!r}); claude -p will write "
        f"a report alongside the log",
        file=sys.stderr,
    )
    triage_req = TriageRequest(
        log_path=log_path,
        clean_close_reason=clean_close_reason,
        deck_pid=deck_pid,
        tracked_subprocesses=len(tracked),
        subprocesses_killed=killed,
        deck_source_dir=deck_source_dir,
        claude_bin=args.claude_bin,
        timeout=args.triage_timeout,
        # v1.5: when set, the supervisor force-killed the deck on a
        # stale-heartbeat detection (interactive prompt approved or
        # --auto-triage-on-stale flag). Triage's user prompt
        # mentions this so the report can distinguish "deck wedged
        # then we killed it" from "deck crashed naturally."
        wedge_kill_context=wedge_kill_context,
    )
    triage_result = run_triage(triage_req)
    if triage_result.success:
        print(
            f"[mechanic] triage written ({triage_result.elapsed_s:.1f}s) "
            f"-> {triage_result.report_path}",
            file=sys.stderr,
        )
        print(
            f"[mechanic] summary: {triage_result.summary_line}",
            file=sys.stderr,
        )
    else:
        print(
            f"[mechanic] triage FAILED ({triage_result.elapsed_s:.1f}s): "
            f"{triage_result.error}",
            file=sys.stderr,
        )
        if triage_result.report_path:
            print(
                f"[mechanic]   stub written -> {triage_result.report_path}",
                file=sys.stderr,
            )
    return 0


def _locate_deck_source(log_dir: Path) -> Optional[Path]:
    """Walk up from `log_dir` looking for the directory containing
    tui.py. Returns the first hit or None.

    Logs typically live at `<deck-source>/logs/`, so we usually find
    deck source one parent up. Cap the walk at 5 levels to avoid
    pathological filesystem structures.
    """
    candidate = log_dir.resolve()
    for _ in range(5):
        if (candidate / "tui.py").is_file():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None


if __name__ == "__main__":
    sys.exit(main())
