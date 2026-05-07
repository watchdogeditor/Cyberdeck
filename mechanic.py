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
    (or we don't have rights, which we usually do for our own children)."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid,
    )
    if not h:
        return False
    try:
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code))
        return bool(ok) and exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(h)


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
            "branch)."
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


def wait_for_log_file(log_dir: Path, timeout: float) -> Optional[Path]:
    """Poll `log_dir` for any fresh `cyberdeck-*.log`. Returns the
    newest one, or None if the timeout elapses with nothing found."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        log = find_log_file(log_dir)
        if log is not None:
            return log
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
    # this catches deck-frozen. Logs a one-time diagnostic when
    # detected; v1 LLM session (deferred) will decide what to do
    # about it. Variables track the "have we already warned?" state
    # so we don't spam stderr every tick on a long wedge.
    heartbeat_path = resolve_heartbeat_path(args.heartbeat_path)
    print(
        f"[mechanic] heartbeat file: {heartbeat_path}",
        file=sys.stderr,
    )
    heartbeat_warned = False  # one-shot warn-then-quiet
    heartbeat_recovered_at: Optional[float] = None

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

            # Heartbeat-staleness check. Only meaningful AFTER
            # we've seen at least one fresh heartbeat — a missing
            # file early in the deck's startup is normal (the
            # writer hasn't ticked yet). Fresh-once-seen tracks
            # this implicitly via heartbeat_recovered_at.
            age = heartbeat_age_seconds(heartbeat_path)
            if age is not None and age > args.heartbeat_stale_seconds:
                if not heartbeat_warned:
                    print(
                        f"[mechanic] STALE HEARTBEAT detected: "
                        f"file is {age:.1f}s old "
                        f"(threshold={args.heartbeat_stale_seconds}s); "
                        f"deck pid={deck_pid} is alive but TUI may "
                        f"be wedged. v1 LLM session would triage "
                        f"here; v0+1 logs only.",
                        file=sys.stderr,
                    )
                    heartbeat_warned = True
            elif age is not None and heartbeat_warned:
                # Heartbeat came back. Reset the warn flag so a
                # future stale event prints again. Also note the
                # recovery so the netrunner sees the deck unfroze.
                print(
                    f"[mechanic] heartbeat recovered ({age:.1f}s "
                    f"old); deck appears responsive again",
                    file=sys.stderr,
                )
                heartbeat_warned = False
                heartbeat_recovered_at = time.time()

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
