"""
Fleet: run multiple constructs concurrently and multiplex their event
streams into a unified log.

Milestone One. Still no TUI, no daemon, no watchdog — just proving that
N constructs can run side by side cleanly and that we can observe them
in a single stream without the plumbing getting garbled.

Architecture notes:

- Single-process for now. Each construct is still its own OS subprocess
  (inherited from M0), but the orchestration lives in one Python process
  using asyncio primitives. ZeroMQ gets introduced later when we split
  the daemon and watchdog into their own processes.

- Per-construct consumer tasks push events into a shared asyncio.Queue.
  A single drain loop reads from the queue, writes to console, and
  appends to the NDJSON log. This is the same shape the Textual UI will
  use in M2 (the TUI becomes the queue consumer).

- Log format: NDJSON, one line per entry. Entries are fleet-level
  envelopes: {run_id, ts, construct_id, kind, payload}. `kind` is
  'event' for Construct events or 'meta' for fleet-level annotations
  (spawn, finalize). This is the foundation for the spec's "annotated
  append-only run logs."
"""
from __future__ import annotations

import asyncio
import json
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    # Profile is referenced by signature only on Fleet.spawn. Avoid a
    # runtime cycle since profiles.py is upstream of the rest of the
    # deck and imports cleanly without us.
    from profiles import Profile

from construct import Construct
from display import summarize
from session_manager import SessionManager, SessionPool
from brake_state import (
    BrakeState,
    make_spawn_settings,
    cleanup_spawn_settings,
)


@dataclass
class FleetEvent:
    """One item in the multiplexed stream.

    kind='event'  → payload is {'event_kind': str, 'raw': dict}
    kind='meta'   → payload is {'type': 'spawned'|'finalized'|..., ...}
    """
    timestamp: float
    kind: str
    construct_id: str
    payload: dict


def _terminal_manifest_state(construct_state: str) -> str:
    """Map a Construct's terminal state name to the SessionManager
    state vocabulary. Construct states are: STARTING, RUNNING, DONE,
    FAILED, KILLED. We only call this on terminal states; non-terminal
    fall-through to 'failed' as a safe default."""
    mapping = {
        "done":   "done",
        "failed": "failed",
        "killed": "killed",
    }
    return mapping.get(construct_state.lower(), "failed")


class Fleet:
    def __init__(
        self,
        claude_bin: str = "claude",
        permission_mode: str = "bypassPermissions",
        log_path: Optional[Path] = None,
        on_event: Optional[Callable[["FleetEvent"], None]] = None,
        quiet: bool = False,
        install_signal_handlers: bool = True,
        max_concurrent: int = 5,
        session_manager: Optional[SessionManager] = None,
        session_pool: Optional["SessionPool"] = None,
        cwd: Optional[str] = None,
        deck_addendum: Optional[str] = None,
        brake_state_provider: Optional[Callable[[], "BrakeState"]] = None,
        home_dir: Optional[Path] = None,
    ):
        self.run_id = f"run-{uuid.uuid4().hex[:8]}"
        self.claude_bin = claude_bin
        self.permission_mode = permission_mode
        self.log_path = log_path
        self.quiet = quiet
        self.install_signal_handlers = install_signal_handlers
        self.max_concurrent = max_concurrent
        # Working directory for every construct this fleet spawns. None
        # means "inherit from python's cwd" (legacy behavior). Set to an
        # explicit path to soft-sandbox constructs into a home dir —
        # they'll default-search/write there instead of the deck source.
        self.cwd = cwd
        # Deck-wide system-prompt addendum applied to every construct
        # spawned through this fleet, regardless of profile. Used by
        # the TUI to advertise the cyberdeck dispatcher script
        # (<home>/tools/deck/cyberdeck.py) so constructs know they
        # can call back to the deck UI.
        self.deck_addendum = deck_addendum
        # SessionManager is optional. When provided, every Construct
        # spawned through this Fleet will be recorded in the manifest
        # — session_id captured on system_init, state transitioned on
        # finalization. Pool-aware behavior lands in M5.3b+.
        self.session_manager = session_manager
        # SessionPool is optional. When provided, spawn() pulls a warm
        # session from the pool and uses --resume. When unavailable
        # or empty, falls back to a fresh spawn (current behavior).
        self.session_pool = session_pool

        # Brake-state callable + home dir for per-spawn hook settings.
        # When `brake_state_provider` is wired, every spawn calls it
        # to read the current deck-global brake at spawn time, then
        # generates a transient claude --settings file in
        # <home>/.cyberdeck/spawns/. The construct's lifecycle owns
        # that file: written on spawn, deleted on finalize.
        # When the provider is None (e.g., headless console runs of
        # fleet.py without a TUI), no hook is installed — equivalent
        # to YOLO. home_dir defaults to cwd because pre-brake-refactor
        # callers passed cwd as their soft-sandbox dir; that's the
        # natural place for the .cyberdeck/spawns/ directory too.
        self.brake_state_provider = brake_state_provider
        self.home_dir = (
            Path(home_dir) if home_dir is not None
            else (Path(cwd) if cwd is not None else None)
        )

        # Listener list. The legacy `on_event` kwarg becomes the first
        # listener for backward compat; new code should use add_listener().
        self._listeners: list[Callable[[FleetEvent], None]] = []
        if on_event is not None:
            self._listeners.append(on_event)

        self._constructs: list[Construct] = []
        self._consumers: list[asyncio.Task] = []
        self._queue: asyncio.Queue[FleetEvent] = asyncio.Queue()
        self._log_fp = None
        self._started_at = time.time()
        self._stop = asyncio.Event()
        self._pending_count = 0
        self._keep_alive = False

        # Concurrency gate. Acquired in spawn() before the subprocess is
        # actually started; released in _consume()'s finally after the
        # subprocess exits. This caps peak in-flight constructs without
        # making fleet.spawn() itself fail — callers just wait their turn.
        self._slot_sema = asyncio.Semaphore(max_concurrent)

        # Observability: running totals for the whole fleet run.
        # - total_cost_usd is summed from the `total_cost_usd` field
        #   on result events. For Max subscribers this is an estimate,
        #   not a bill, but it's still the right signal for "am I
        #   burning through my quota?"
        # - total_tokens_in/out are a more reliable proxy than cost
        #   because they're always reported and never zero for real
        #   work. Cost shows $0.00 in some configurations; tokens
        #   always tell you if the fleet actually did anything.
        self.total_spawned = 0
        self.total_finalized = 0
        self.total_cost_usd = 0.0
        self.total_tokens_in = 0
        self.total_tokens_out = 0

    def add_listener(self, callback: Callable[["FleetEvent"], None]) -> None:
        """Register an event listener. Listeners are called in registration
        order for every event (both construct events and meta events).
        Exceptions in one listener don't stop others."""
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[["FleetEvent"], None]) -> None:
        """Unregister a previously-added listener. No-op if not registered."""
        try:
            self._listeners.remove(callback)
        except ValueError:
            pass

    # ---- external control ----------------------------------------------

    def shutdown(self) -> None:
        """Trigger graceful shutdown from outside run() (e.g. TUI quit)."""
        if not self._stop.is_set():
            if not self.quiet:
                print("\n[fleet] shutdown requested, killing all constructs...")
            self._stop.set()

    # ---- context management ---------------------------------------------

    async def __aenter__(self) -> "Fleet":
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fp = open(self.log_path, "a", encoding="utf-8")
            self._write_log({
                "run_id": self.run_id,
                "ts": time.time(),
                "construct_id": "fleet",
                "kind": "meta",
                "payload": {"type": "run_start"},
            })
        return self

    async def __aexit__(self, *exc) -> None:
        if self._log_fp is not None:
            self._write_log({
                "run_id": self.run_id,
                "ts": time.time(),
                "construct_id": "fleet",
                "kind": "meta",
                "payload": {"type": "run_end"},
            })
            self._log_fp.close()
            self._log_fp = None

    # ---- output ---------------------------------------------------------

    def _write_log(self, entry: dict) -> None:
        if self._log_fp is not None:
            self._log_fp.write(json.dumps(entry) + "\n")
            self._log_fp.flush()

    def _render(self, fevent: FleetEvent) -> None:
        """Emit a fleet event to console (unless quiet), NDJSON log, and
        callback (if registered)."""
        rel = fevent.timestamp - self._started_at
        cid = fevent.construct_id

        if not self.quiet:
            if fevent.kind == "meta":
                mtype = fevent.payload.get("type", "?")
                if mtype == "spawned":
                    task = fevent.payload.get("task", "")
                    print(f"[{cid} +{rel:5.2f}s] META   spawned: {task!r}")
                elif mtype == "finalized":
                    state = fevent.payload.get("state")
                    exit_code = fevent.payload.get("exit_code")
                    runtime = fevent.payload.get("runtime")
                    print(f"[{cid} +{rel:5.2f}s] META   finalized: state={state} "
                          f"exit={exit_code} runtime={runtime:.2f}s")
                else:
                    print(f"[{cid} +{rel:5.2f}s] META   {fevent.payload}")
            else:
                event_kind = fevent.payload.get("event_kind", "?")
                raw = fevent.payload.get("raw", {})
                print(f"[{cid} +{rel:5.2f}s] {event_kind:14s} {summarize(raw)}")

        self._write_log({
            "run_id": self.run_id,
            "ts": fevent.timestamp,
            "construct_id": cid,
            "kind": fevent.kind,
            "payload": fevent.payload,
        })

        if self._listeners:
            for listener in self._listeners:
                try:
                    listener(fevent)
                except Exception as e:
                    if not self.quiet:
                        print(f"[fleet] listener {listener!r} raised: {e!r}")

    # ---- per-construct consumer ----------------------------------------

    async def _consume(self, c: Construct) -> None:
        """Pump one construct's events into the shared queue, then finalize."""
        # Track whether we've already recorded this session in the
        # manifest. session_id arrives on the first system_init event,
        # which can land at any time during the stream; we want to
        # record exactly once.
        recorded_session = False
        # Per-construct accumulator for brake-hook denials. Populated
        # from the `permission_denials` field on result events; rolled
        # into the finalize meta event payload so listeners (chatlog,
        # watchdog) can see what got blocked. Empty list means "no
        # denials this run" (which is the common case).
        permission_denials: list[dict] = []
        try:
            async for event in c.events():
                # If we've now seen the session_id and haven't recorded
                # it yet, do so. This is idempotent in SessionManager
                # but the local guard avoids redundant disk writes.
                if (not recorded_session
                        and self.session_manager is not None
                        and c.session_id is not None):
                    self.session_manager.record(
                        session_id=c.session_id,
                        kind="construct",
                        state="in_use",
                        task_preview=c.task,
                    )
                    recorded_session = True

                # Opportunistically scrape cost, tokens, and brake-hook
                # permission_denials from result events. All three live
                # on the top-level result event in Claude Code's
                # stream-json. permission_denials is the catalog of
                # what the brake hook blocked this turn — surfaced in
                # the finalize meta event so the chatlog and watchdog
                # see what got caught.
                if (event.kind == "result"
                        or event.raw.get("type") == "result"):
                    cost = event.raw.get("total_cost_usd")
                    if isinstance(cost, (int, float)):
                        self.total_cost_usd += float(cost)
                    pd = event.raw.get("permission_denials")
                    if isinstance(pd, list):
                        permission_denials.extend(
                            d for d in pd if isinstance(d, dict)
                        )
                    usage = event.raw.get("usage") or {}
                    # Sum all input flavors: fresh input + cache reads
                    # + cache creation. For a user watching the sidebar,
                    # this is the "how much did the model see" number.
                    tin = (
                        int(usage.get("input_tokens") or 0)
                        + int(usage.get("cache_read_input_tokens") or 0)
                        + int(usage.get("cache_creation_input_tokens") or 0)
                    )
                    tout = int(usage.get("output_tokens") or 0)
                    self.total_tokens_in += tin
                    self.total_tokens_out += tout

                await self._queue.put(FleetEvent(
                    timestamp=event.timestamp,
                    kind="event",
                    construct_id=c.id,
                    payload={"event_kind": event.kind, "raw": event.raw},
                ))
        finally:
            try:
                # 30s upper bound. Normal exits take <1s. If the
                # subprocess is wedged (Windows orphan, etc.), this
                # escalates to force-kill inside wait() rather than
                # hanging in _consume's finally and blocking Ctrl-C.
                await c.wait(timeout=30.0)
                self._pending_count = max(0, self._pending_count - 1)
                self.total_finalized += 1

                # Update SessionManager with terminal state. If
                # session_id never landed (e.g., subprocess died before
                # system_init), there's nothing to record — that's fine.
                # If we never recorded the session because system_init
                # came late, do a last-chance record here so terminal
                # state is at least captured.
                if self.session_manager is not None and c.session_id is not None:
                    if not recorded_session:
                        self.session_manager.record(
                            session_id=c.session_id,
                            kind="construct",
                            state=_terminal_manifest_state(c.state.value),
                            task_preview=c.task,
                        )
                    else:
                        self.session_manager.update(
                            c.session_id,
                            state=_terminal_manifest_state(c.state.value),
                        )

                # Truncate final_output — if a construct produces a
                # massive report, we don't want the full thing wedged
                # into daemon context every outcome turn. ~2KB handles
                # summaries, file lists, short reports. Constructs that
                # produce bigger artifacts should write to disk anyway.
                final = c.final_output or ""
                if len(final) > 2048:
                    final = final[:2045] + "..."
                await self._queue.put(FleetEvent(
                    timestamp=time.time(),
                    kind="meta",
                    construct_id=c.id,
                    payload={
                        "type": "finalized",
                        "state": c.state.value,
                        "exit_code": c.exit_code,
                        "runtime": c.runtime,
                        "final_output": final,
                        "files_written": c.files_written,
                        # Brake-hook denials. Empty list when nothing
                        # was blocked. Each entry is a dict with
                        # tool_name, tool_use_id, tool_input — the
                        # shape Claude Code's result event provides.
                        # Listeners use this to render chatlog notes
                        # ("blocked: Write -> C:/Windows/...") and to
                        # feed the watchdog's brake-awareness layer.
                        "permission_denials": list(permission_denials),
                    },
                ))
            finally:
                # Always release the slot, even on crash. If we didn't,
                # one buggy construct could starve the fleet permanently.
                self._slot_sema.release()
                # Clean up the per-spawn brake-settings file. Best-
                # effort; cleanup_spawn_settings is None-safe and
                # idempotent. Done here rather than in spawn's
                # finally because we want it to run on the *consumer*
                # side, after the subprocess actually exits — keeping
                # the settings file alive for the construct's full
                # lifetime is the simple semantic.
                cleanup_spawn_settings(
                    Path(c.settings_path) if c.settings_path else None,
                )

    # ---- construct management ------------------------------------------

    async def spawn(
        self,
        task: str,
        resume_session_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        profile: Optional["Profile"] = None,
        origin: str = "daemon",
    ) -> Optional[Construct]:
        """Spawn a single construct and wire its consumer into the fleet.

        If max_concurrent in-flight constructs are already running, this
        call waits (via semaphore) until a slot frees. Release happens
        automatically when the construct finalizes in _consume(), so
        callers don't need to worry about it.

        When a SessionPool is wired up and has warm sessions available,
        the new construct is spawned via --resume to reuse a pre-warmed
        session. Falls back to fresh spawn when pool is empty/disabled.

        `origin` records who initiated the spawn. Values:
          - "daemon"    — daemon's plan dispatched it (default; safest
                          fallback for any caller that hasn't been
                          updated yet).
          - "netrunner" — direct human action (n key, action_new_construct).
          - "inject"    — netrunner injection on a running construct
                          (q/Q queue/interrupt). The new construct
                          continues an existing session, so semantically
                          it's still netrunner-initiated, but the
                          parent_id linkage carries the additional
                          context. Distinguishing inject from a fresh
                          netrunner spawn matters because inject
                          follow-ups have a different mental model
                          (continuation vs. new task).
        Threaded into the 'spawned' meta event so listeners (chatlog,
        watchdog, future telemetry) can attribute spawns at source
        rather than reverse-engineering it from log timing. Solves the
        case where the watchdog had to infer "this was netrunner-
        initiated because no daemon thinking line preceded it" — now
        it's right there in the event.

        Pool reuse is GATED on profile: only spawns under the default
        profile (or no profile) consult the pool. Other profiles
        always spawn fresh — the pool's warm sessions don't carry the
        profile's system-prompt addendum, and a profile-bound spawn
        resuming a default-warmed session would silently get the wrong
        steering. (We considered per-profile pools; concluded the
        warming cost wasn't worth the complexity.)

        When resume_session_id is explicitly provided, the pool is
        bypassed and the spawn resumes that specific session — used by
        injection to continue an existing construct's conversation.
        Profile is also ignored in that case: the resumed session has
        whatever system prompt it was started with.

        When parent_id is provided, it's threaded into the 'spawned' meta
        event so listeners (the TUI in particular) can link the new pane
        to its origin. Used by inject — the new construct is a turn-N+1
        continuation of the parent's session, not an independent task.
        """
        # Gate on the concurrency semaphore BEFORE creating the Construct
        # object, so queued spawns don't pile up as half-initialized
        # objects in memory. If spawn_exec fails, we release in the
        # error path below.
        await self._slot_sema.acquire()

        # Resolve the session_id we'll resume (if any). Explicit caller
        # request wins; otherwise try the pool (only when profile is
        # default-or-absent); otherwise fresh spawn.
        resume_id: Optional[str] = resume_session_id
        pool_eligible = (
            profile is None
            or profile.name == "default"
        )
        if (resume_id is None
                and self.session_pool is not None
                and pool_eligible):
            warm_entry = await self.session_pool.pull()
            if warm_entry is not None:
                resume_id = warm_entry.session_id

        # Build the construct first (we need its id for the settings
        # filename), then generate the per-spawn brake settings file
        # and attach. Doing it in this order keeps the construct id
        # canonical (Construct mints one in __init__ if not given).
        c = Construct(
            task=task,
            claude_bin=self.claude_bin,
            permission_mode=self.permission_mode,
            resume_session_id=resume_id,
            cwd=self.cwd,
            profile=profile,
            deck_addendum=self.deck_addendum,
        )

        # Generate the brake-hook --settings file for this spawn, if
        # the deck has wired up a brake-state provider and a home dir
        # to write into. YOLO brake (and missing config) yields None,
        # which means no --settings on the claude command line and
        # therefore no hook installed for this construct.
        if (self.brake_state_provider is not None
                and self.home_dir is not None):
            try:
                settings_path = make_spawn_settings(
                    brake=self.brake_state_provider(),
                    home_dir=self.home_dir,
                    construct_id=c.id,
                )
                if settings_path is not None:
                    c.settings_path = str(settings_path)
            except Exception as exc:
                # Spawn-settings generation must never break a spawn.
                # Surface the issue and run the construct without a
                # hook (effectively YOLO for this one). Caller will
                # see the warning in stderr; the deck stays usable.
                if not self.quiet:
                    print(
                        f"[fleet] brake settings generation failed: "
                        f"{exc!r}",
                        file=sys.stderr,
                    )

        self._constructs.append(c)
        self._pending_count += 1
        self.total_spawned += 1
        try:
            await c.spawn()
        except FileNotFoundError as e:
            self._pending_count -= 1
            self.total_spawned -= 1
            self._constructs.remove(c)
            self._slot_sema.release()  # never got to _consume, release here
            # Settings file will go un-cleaned-up by _consume since
            # we never reached it. Best-effort cleanup here.
            cleanup_spawn_settings(
                Path(c.settings_path) if c.settings_path else None,
            )
            # Surface the error via the event stream so TUIs and logs
            # see it; don't silently drop.
            await self._queue.put(FleetEvent(
                timestamp=time.time(),
                kind="meta",
                construct_id=c.id,
                payload={"type": "spawn_failed", "task": task, "error": str(e)},
            ))
            return None

        await self._queue.put(FleetEvent(
            timestamp=time.time(),
            kind="meta",
            construct_id=c.id,
            payload={
                "type": "spawned",
                "task": task,
                "resumed_from": resume_id,  # None if fresh spawn
                "parent_id": parent_id,  # None unless this is an inject followup
                # Profile name (or None). Lets the TUI show a profile
                # badge on the pane header without reaching back into
                # construct internals. None means no profile applied.
                "profile_name": c.profile_name,
                # Spawn provenance: "daemon" / "netrunner" / "inject".
                # Surfaced in the chatlog so a glance tells the
                # netrunner who dispatched this work, and so the
                # watchdog stops having to reverse-engineer it from
                # log timing.
                "origin": origin,
            },
        ))
        consumer = asyncio.create_task(self._consume(c))
        self._consumers.append(consumer)
        return c

    async def kill_construct(self, construct_id: str) -> bool:
        """Kill one construct by ID. Returns True if found and killed."""
        for c in self._constructs:
            if c.id == construct_id:
                await c.kill()
                return True
        return False

    # ---- orchestration --------------------------------------------------

    async def run(self, tasks: list[str], keep_alive: bool = False) -> None:
        """Spawn initial tasks and drain events.

        keep_alive=False (default): drain exits when all pending constructs
            finalize. Suits one-shot console runs.
        keep_alive=True: drain runs until shutdown() is called. Suits TUIs
            where the user may spawn more constructs interactively.
        """
        self._keep_alive = keep_alive

        for task in tasks:
            await self.spawn(task)

        # Signal handling: SIGINT → kill all constructs, let consumers finalize.
        # Skipped when install_signal_handlers=False (e.g. under a TUI that
        # owns signals and drives shutdown via fleet.shutdown()).
        if self.install_signal_handlers:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, self.shutdown)
                except NotImplementedError:
                    pass  # Windows

        async def drain() -> None:
            while True:
                if self._stop.is_set():
                    break
                try:
                    fevent = await asyncio.wait_for(
                        self._queue.get(), timeout=0.1,
                    )
                    self._render(fevent)
                except asyncio.TimeoutError:
                    # Queue idle. If we're in one-shot mode and nothing
                    # is pending, we're done.
                    if not self._keep_alive and self._pending_count == 0:
                        break

        drain_task = asyncio.create_task(drain())
        stop_task = asyncio.create_task(self._stop.wait())

        done, pending = await asyncio.wait(
            {drain_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if stop_task in done and not drain_task.done():
            # Kill everything in parallel. Consumers will emit finalized
            # meta events, drain_task will catch them and exit on the
            # pending-zero check (in non-keep-alive mode) or stop check
            # (in keep-alive mode, which set the stop event to get here).
            await asyncio.gather(
                *(c.kill() for c in self._constructs),
                return_exceptions=True,
            )
            await drain_task

        # Clean up any leftover tasks
        for p in pending:
            p.cancel()
        for t in self._consumers:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._consumers, return_exceptions=True)


# ---- entry point ---------------------------------------------------------

async def _main(tasks: list[str], claude_bin: str, permission_mode: str,
                log_path: Optional[Path]) -> int:
    async with Fleet(
        claude_bin=claude_bin,
        permission_mode=permission_mode,
        log_path=log_path,
    ) as fleet:
        print(f"[fleet] run_id={fleet.run_id}  bin={claude_bin}  mode={permission_mode}  "
              f"tasks={len(tasks)}"
              + (f"  log={log_path}" if log_path else ""))
        await fleet.run(tasks)
    return 0


if __name__ == "__main__":
    # Usage:
    #   python fleet.py "task 1" "task 2" [...]
    # Env overrides:
    #   CLAUDE_BIN=./mock_claude.py  python fleet.py "task A" "task B"
    #   CLAUDE_MODE=bypassPermissions python fleet.py "..."
    #   CYBERDECK_LOG=/path/to/log.ndjson python fleet.py "..."
    import os

    tasks = sys.argv[1:]
    if not tasks:
        print('usage: python fleet.py "task 1" "task 2" [...]')
        print("env:   CLAUDE_BIN=./mock_claude.py  (default: claude)")
        print("       CLAUDE_MODE=acceptEdits      (default: acceptEdits)")
        print("       CYBERDECK_LOG=cyberdeck.log  (default: cyberdeck.log)")
        sys.exit(2)

    claude_bin = os.environ.get("CLAUDE_BIN", "claude")
    permission_mode = os.environ.get("CLAUDE_MODE", "bypassPermissions")
    log_env = os.environ.get("CYBERDECK_LOG", "cyberdeck.log")
    log_path = Path(log_env) if log_env else None

    sys.exit(asyncio.run(_main(tasks, claude_bin, permission_mode, log_path)))
