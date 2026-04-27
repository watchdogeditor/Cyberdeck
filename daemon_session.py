"""
DaemonSession: coordinates a Daemon with a Fleet.

The Daemon decides, the Fleet executes. This module is the glue.

Flow:
  1. User provides a goal → session.run(goal)
  2. Daemon turn 1: receive goal, decompose, emit spawn actions
  3. Session executes each action → fleet.spawn(task)
  4. Fleet runs constructs, emits finalized events (via listener)
  5. Session collects outcomes, batches them with a short debounce
  6. Daemon turn 2: receive outcomes, assess, decide next steps
  7. Repeat until daemon emits status=done/failed or session shuts down
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from profiles import Profile

from daemon import Daemon, DaemonEvent
from fleet import Fleet, FleetEvent


@dataclass
class PendingOutcome:
    """A construct outcome we haven't yet reported to the daemon."""
    construct_id: str
    task: str
    state: str
    summary: str = ""


class DaemonSession:
    """One Daemon + one Fleet, running a single goal to completion."""

    def __init__(
        self,
        daemon: Daemon,
        fleet: Fleet,
        on_daemon_event: Optional[Callable[[DaemonEvent], None]] = None,
        outcome_batch_delay: float = 0.5,
        max_total_spawns: int = 20,
        default_profile: Optional["Profile"] = None,
        profile_lookup: Optional[Callable[[str], Optional["Profile"]]] = None,
    ) -> None:
        self.daemon = daemon
        self.fleet = fleet
        self.on_daemon_event = on_daemon_event
        self.outcome_batch_delay = outcome_batch_delay
        # Hard cap on constructs per session. This is the wood-chipper
        # shutoff: if the daemon loops or fans out pathologically, we
        # stop before the token bill gets scary. Concurrency is capped
        # separately in Fleet; this caps cumulative total.
        self.max_total_spawns = max_total_spawns
        # Profile applied to every daemon-spawned construct in this
        # session. C1c-era stand-in: the daemon doesn't yet pick
        # profiles per-spawn (that lands in C1e). Until then, every
        # spawn from this session uses the same profile (or none).
        self.default_profile = default_profile
        # Resolver for daemon-requested profiles by name. Typically
        # backed by the ProfileRegistry (C1b). When None, daemon
        # profile-switching is disabled — every spawn uses
        # default_profile regardless of what the daemon asks for.
        # This is the right default for tests and for the headless
        # CLI; the TUI wires this up to point at the registry.
        self.profile_lookup = profile_lookup

        # Track task-per-construct so we can report useful outcomes
        # (the finalized event doesn't carry the original task text).
        self._construct_tasks: dict[str, str] = {}
        self._pending_outcomes: list[PendingOutcome] = []
        self._outcome_event = asyncio.Event()
        self._goal_done = asyncio.Event()
        self._total_spawns = 0
        self._cap_hit = False

        # Fingerprint → count. A "fingerprint" is a hash of the first 80
        # chars of a task, lowercased. If the daemon keeps spawning the
        # same-shaped task over and over (as happened in the DSL-design
        # run: "Design a watchdog-tripwire DSL in X style" repeated with
        # X rotated), this counter ticks up. When a fingerprint hits 3,
        # we inject a warning into the next outcome message telling the
        # daemon it's in a loop.
        self._task_fingerprints: dict[str, int] = {}
        self._respawn_warnings: list[str] = []

        # Goal updates pending propagation to the daemon. Set via
        # set_pending_goal_update from the TUI when the netrunner
        # edits the goal mid-flight (`e`). The next outcome turn
        # picks this up and prepends a "GOAL UPDATE:" preamble so
        # the daemon sees the new wording alongside the construct
        # results — natural break-point per spec.
        #
        # Force-push (apply now, pre-emptively interrupt the in-
        # flight daemon turn) is M5+ — for now, updates wait for the
        # next natural break. If no constructs are currently running
        # and the daemon is idle, the update just sits here until
        # SOMETHING finalizes; that's an acceptable edge for v1.
        self._pending_goal_update: Optional[tuple[str, str, str]] = None
        # tuple shape: (new_goal, classification, old_goal)

        # Netrunner messages pending delivery to the daemon. Set via
        # set_pending_netrunner_message from the TUI when the
        # netrunner uses daemon chat (`T`). FIFO list — multiple
        # messages can stack between turns and all get delivered
        # together with a "NETRUNNER MESSAGES:" preamble. This
        # differs from goal-updates (which overwrite, since the
        # latest wording supersedes) because each chat message is
        # its own thought worth surfacing.
        self._pending_netrunner_messages: list[str] = []

        # Subscribe to fleet events so we can observe finalizes
        self.fleet.add_listener(self._on_fleet_event)

    # ---- fleet event handling ------------------------------------------

    def _on_fleet_event(self, fevent: FleetEvent) -> None:
        """Called for every fleet event. We care about spawns (to track
        task metadata) and finalizes (to feed outcomes back)."""
        if fevent.kind != "meta":
            return
        ptype = fevent.payload.get("type")
        if ptype == "spawned":
            self._construct_tasks[fevent.construct_id] = fevent.payload.get("task", "")
        elif ptype == "finalized":
            task = self._construct_tasks.get(fevent.construct_id, "")
            self._pending_outcomes.append(PendingOutcome(
                construct_id=fevent.construct_id,
                task=task,
                state=fevent.payload.get("state", "?"),
                summary=fevent.payload.get("final_output", ""),
            ))
            self._outcome_event.set()

    # ---- main loop ------------------------------------------------------

    async def run(self, goal: str) -> None:
        """Drive the decide→execute→observe loop until the daemon
        declares done/failed or shutdown() is called."""
        try:
            # Turn 1: send the initial goal
            initial = (
                f"GOAL: {goal}\n\n"
                "Decompose and delegate. Respond with the JSON action block."
            )
            await self._process_turn(initial)

            # Subsequent turns: wait for construct outcomes, feed back
            while not self._goal_done.is_set():
                outcomes = await self._wait_for_outcome_batch()
                if outcomes is None:
                    # shutdown fired while waiting
                    return
                if self._goal_done.is_set():
                    return
                message = _format_outcomes(
                    outcomes,
                    self._respawn_warnings,
                    goal_update=self._pending_goal_update,
                    netrunner_messages=self._pending_netrunner_messages,
                )
                self._respawn_warnings = []  # clear after surfacing
                self._pending_goal_update = None  # consumed
                self._pending_netrunner_messages = []  # consumed
                await self._process_turn(message)
        finally:
            self.fleet.remove_listener(self._on_fleet_event)

    def set_pending_goal_update(
        self,
        new_goal: str,
        classification: str,
        old_goal: str,
    ) -> None:
        """Stash a goal edit for delivery on the next outcome turn.

        classification is "clarification" / "scope-change" / "pivot"
        per spec — a human-meaningful label that helps the daemon
        decide how aggressively to revise its plan. The classifier
        lives in the TUI (cheap heuristic for now; spec calls for
        a model-driven version later).

        If a goal update is already pending (rapid successive edits
        before any outcome lands), this overwrites — the netrunner's
        latest wording wins, the daemon doesn't see intermediate
        stages. Old goal in the stored tuple is the one BEFORE this
        update so the daemon sees the diff against the goal it was
        actually working from.
        """
        self._pending_goal_update = (new_goal, classification, old_goal)
        # Wake the outcome-wait loop so the update propagates even if
        # no constructs are currently in flight. _format_outcomes
        # handles empty-outcome+goal-update specially. If a daemon
        # turn is currently running, this signal just queues — the
        # next loop iteration sees the pending update.
        self._outcome_event.set()

    def set_pending_netrunner_message(self, text: str) -> None:
        """Stash a daemon-chat message for delivery on the next
        outcome turn. Multiple messages stack — they're all delivered
        together as a numbered list rather than overwriting (unlike
        goal updates). The reasoning: each chat message is a discrete
        thought that the daemon should see, even if the netrunner
        sent three rapid-fire questions before the daemon got a
        chance to respond.

        Empty / whitespace-only messages are silently dropped — the
        UI shouldn't be sending them anyway, but defensive belt-and-
        suspenders here means the formatter never has to deal with
        a list of empty strings.

        Like goal updates, this fires the wake event so the daemon
        picks the message up promptly even if no constructs are
        currently running.
        """
        if not text or not text.strip():
            return
        self._pending_netrunner_messages.append(text.strip())
        self._outcome_event.set()

    async def _process_turn(self, user_message: str) -> None:
        """Run one daemon turn, executing actions as they arrive and
        setting goal_done if daemon declares a terminal status."""
        async for event in self.daemon.run_turn(user_message):
            if self.on_daemon_event is not None:
                try:
                    self.on_daemon_event(event)
                except Exception:
                    pass  # observer errors shouldn't break the session

            if event.kind == "action":
                action = event.payload.get("action", {})
                await self._execute_action(action)
            elif event.kind == "status":
                status = event.payload.get("status")
                if status in ("done", "failed"):
                    self._goal_done.set()

    async def _execute_action(self, action: dict) -> None:
        """Carry out a single daemon-issued action against the fleet."""
        atype = action.get("type")
        if atype == "spawn":
            task = action.get("task", "").strip()
            if not task:
                return
            if self._total_spawns >= self.max_total_spawns:
                # Wood-chipper shutoff. Stop the session cleanly and
                # surface the reason via the on_daemon_event channel so
                # the TUI can show it.
                if not self._cap_hit:
                    self._cap_hit = True
                    if self.on_daemon_event is not None:
                        import time as _time
                        self.on_daemon_event(DaemonEvent(
                            timestamp=_time.time(),
                            kind="error",
                            payload={
                                "text": (
                                    f"spawn cap ({self.max_total_spawns}) "
                                    "reached — halting session to prevent "
                                    "runaway token use. Increase with "
                                    "--max-spawns if intentional."
                                ),
                            },
                        ))
                    self._goal_done.set()
                return

            # Track task shape to detect respawn loops. A fingerprint is
            # the first 80 chars lowercased — catches "Design a X in A
            # style" / "Design a X in B style" as distinct but
            # "Design a X in A style" respawned verbatim as a repeat.
            fp = task[:80].lower().strip()
            count = self._task_fingerprints.get(fp, 0) + 1
            self._task_fingerprints[fp] = count
            if count == 3:
                # Fire a warning once at the 3rd repetition. The next
                # outcome message will include this so the daemon
                # knows it's been flailing.
                warning_text = (
                    f'"{task[:60]}..." has been spawned {count} times. '
                    "If previous outcomes looked empty, the constructs "
                    "may have succeeded but produced output you didn't "
                    "recognize — check `(files written: ...)` lines and "
                    "tool output fallbacks. Do not spawn this pattern "
                    "a 4th time."
                )
                self._respawn_warnings.append(warning_text)
                # Also surface to the TUI directly so the netrunner
                # sees it in the daemon pane, not just the daemon.
                if self.on_daemon_event is not None:
                    import time as _time
                    self.on_daemon_event(DaemonEvent(
                        timestamp=_time.time(),
                        kind="error",
                        payload={
                            "text": f"⚠ respawn loop: \"{task[:50]}...\" "
                                    f"spawned {count}× — check outcomes",
                        },
                    ))

            # Profile resolution for this spawn. The daemon may include
            # a `profile` field on a spawn action to ask for a specific
            # profile (de-escalation: "this subtask only needs read
            # access, run it under recon_specialist"). Resolution rules:
            #
            #   - No `profile` field             → use active default
            #   - Field set, profile not loaded  → log warning, use default
            #   - Field set, profile would PRIVESC against active → reject,
            #     log a security event, use active default. The netrunner
            #     is the ONLY one who can elevate; the daemon can never
            #     hand itself broader capabilities than the run started
            #     with.
            #   - Field set, profile is OK       → use it
            chosen_profile = self.default_profile
            requested_name = action.get("profile")
            if requested_name and isinstance(requested_name, str):
                chosen_profile = self._resolve_spawn_profile(
                    requested_name, task,
                )

            self._total_spawns += 1
            # origin="daemon" so the chatlog spawn line goes
            # un-badged (daemon spawns are the baseline, what's not
            # badged is routine). Explicit rather than relying on
            # the default to keep intent obvious to anyone reading
            # the call site.
            await self.fleet.spawn(
                task,
                profile=chosen_profile,
                origin="daemon",
            )
        # Future action types: kill, wire, inject, etc.

    def _resolve_spawn_profile(
        self, requested_name: str, task: str,
    ) -> Optional["Profile"]:
        """Validate and return the profile a daemon spawn should run
        under. Falls back to the active default with a logged reason
        when the requested name doesn't resolve. Never raises.

        With brake state moved out of profiles into the deck-global
        layer, profiles no longer have a privesc dimension — they're
        purely prescriptive templates (instructions + recommended
        tool lists). Any registered profile is fair game for daemon
        selection; the brake hook handles runtime constraint
        independently of which profile the construct spawned with.
        """
        if self.profile_lookup is None:
            return self.default_profile

        candidate = self.profile_lookup(requested_name)
        if candidate is None:
            self._emit_daemon_event(
                "error",
                f"daemon requested unknown profile {requested_name!r} "
                f"for task \"{task[:60]}...\" — using active default",
            )
            return self.default_profile

        return candidate

    def _emit_daemon_event(self, kind: str, text: str) -> None:
        """Local helper for emitting an event to on_daemon_event with
        a current timestamp. No-op if no listener is wired."""
        if self.on_daemon_event is None:
            return
        import time as _time
        self.on_daemon_event(DaemonEvent(
            timestamp=_time.time(),
            kind=kind,
            payload={"text": text},
        ))

    async def _wait_for_outcome_batch(self) -> Optional[list[PendingOutcome]]:
        """Block until at least one outcome arrives, debounce briefly,
        then return and clear the batch. Returns None if shutdown."""
        outcome_task = asyncio.create_task(self._outcome_event.wait())
        done_task = asyncio.create_task(self._goal_done.wait())
        try:
            done, pending = await asyncio.wait(
                {outcome_task, done_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            if done_task in done:
                return None
        except Exception:
            return None

        # Debounce — let other constructs finalize in the same wave
        await asyncio.sleep(self.outcome_batch_delay)
        self._outcome_event.clear()

        batch, self._pending_outcomes = self._pending_outcomes, []
        return batch

    async def shutdown(self) -> None:
        """Stop the session. In-flight daemon turns will cancel when
        the iterator is cancelled by control flow."""
        self._goal_done.set()
        await self.daemon.shutdown()


def _format_outcomes(
    outcomes: list[PendingOutcome],
    respawn_warnings: Optional[list[str]] = None,
    goal_update: Optional[tuple[str, str, str]] = None,
    netrunner_messages: Optional[list[str]] = None,
) -> str:
    """Render a batch of construct outcomes as a daemon-input message.

    Empty summaries are flagged explicitly rather than omitted — if the
    daemon doesn't see a `result:` line at all, it doesn't know whether
    the construct produced nothing or whether our capture missed it.

    Respawn warnings (when the same task fingerprint has been spawned
    3+ times) are prepended at the top of the message so the daemon
    sees them BEFORE the outcomes. This breaks loops where the daemon
    thinks empty outcomes mean "try again with different wording."

    Goal updates (when the netrunner edited the goal mid-flight) are
    prepended ABOVE everything else with high visual weight so the
    daemon notices the change before it reads outcomes. Classification
    is included so the daemon can calibrate response: a "clarification"
    means keep the current decomposition with refined detail; a
    "pivot" means tear up the plan and start over.

    Netrunner messages (from daemon chat `T`) sit below goal updates
    but above respawn warnings and outcomes. These are direct
    plan-affecting communication — questions, course corrections,
    additional context — that the daemon should weigh as it decides
    next steps. Multiple messages render as a numbered list with the
    netrunner's exact wording preserved.
    """
    if (not outcomes and not respawn_warnings
            and not goal_update and not netrunner_messages):
        return ""
    lines: list[str] = []

    if goal_update is not None:
        new_goal, classification, old_goal = goal_update
        lines.append("⚠ GOAL UPDATE FROM NETRUNNER:")
        lines.append(f"  classification: {classification}")
        lines.append(f"  previous: {old_goal}")
        lines.append(f"  current:  {new_goal}")
        if classification == "clarification":
            lines.append(
                "  → Keep your current decomposition; the netrunner "
                "added detail or refined wording. Adjust subtasks "
                "where the new wording disambiguates them."
            )
        elif classification == "scope-change":
            lines.append(
                "  → Material change in scope. Review your plan: "
                "subtasks already in flight may still be useful, but "
                "what comes next should reflect the new wording."
            )
        else:  # pivot
            lines.append(
                "  → This is a pivot. The previous plan is likely "
                "obsolete. Decide which (if any) in-flight work is "
                "still worth completing and re-decompose from here."
            )
        lines.append("")

    if respawn_warnings:
        lines.append("⚠ RESPAWN LOOP DETECTED:")
        for w in respawn_warnings:
            lines.append(f"  - {w}")
        lines.append("")

    if netrunner_messages:
        # High visual weight (≫) signals "human is talking to you,
        # weight this above mechanical signals." Numbered for easy
        # back-reference if the daemon's response needs to call out
        # which message it's addressing first.
        if len(netrunner_messages) == 1:
            lines.append("≫ NETRUNNER MESSAGE:")
            lines.append(f"  {netrunner_messages[0]}")
        else:
            lines.append("≫ NETRUNNER MESSAGES (in order received):")
            for i, msg in enumerate(netrunner_messages, start=1):
                lines.append(f"  {i}. {msg}")
        lines.append(
            "  → Address the netrunner directly in your `chat` field; "
            "treat plan-affecting content as authoritative input on "
            "next steps."
        )
        lines.append("")

    if outcomes:
        lines.append("CONSTRUCT OUTCOMES:")
        for o in outcomes:
            lines.append(f"- [{o.construct_id}] {o.state}: {o.task}")
            if o.summary and o.summary.strip():
                lines.append(f"  result: {o.summary}")
            else:
                lines.append(
                    "  result: (no text output captured — construct may have "
                    "ended on a tool call. If you need the result, respawn "
                    "with 'End with a one-paragraph summary of findings.' "
                    "appended to the task.)"
                )
        lines.append(
            "\nAssess the outcomes and decide next steps. "
            "Respond with the JSON action block."
        )
    else:
        # No construct outcomes this turn — the message is a goal
        # update, netrunner chat, or both. Daemon needs a different
        # prompt — we're not asking it to assess outcomes, we're
        # asking it to react to the human input above. The
        # classification preamble + chat block already steered the
        # response shape; this just closes the message.
        if netrunner_messages and goal_update:
            closer = (
                "Address the netrunner messages above and "
                "adjust your plan based on the goal update. "
                "Respond with the JSON action block."
            )
        elif netrunner_messages:
            closer = (
                "Address the netrunner messages above. "
                "Respond with the JSON action block."
            )
        else:
            closer = (
                "Adjust your plan based on the goal update above. "
                "Respond with the JSON action block."
            )
        lines.append(closer)
    return "\n".join(lines)
