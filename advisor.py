"""
Advisor: a narrowly-scoped Q&A oracle for ONE tool or plugin.

Spec framing (from the netrunner, 2026-05-05):
    "It is extremely specific — exclusively dedicated to informing
    you about that tool. It does nothing else and exclusively takes
    questions about the tool and its use cases. The most you can
    ask it is 'how can I use it to do X' and name other tools in
    the process. The Advisor can see the names of the full tool
    list if needed."

So this is the Watchdog's distant cousin: same `claude -p` one-shot
substrate (cheap, async, callback-on-answer), but the system prompt
and target shape are completely different.

Where the Watchdog observes everything, the Advisor observes ONE
named thing. Where the Watchdog reasons over a chatlog snapshot, the
Advisor reasons over a tool/plugin manifest + (when present) the
plugin's README. Where the Watchdog will answer pretty much any
question about fleet activity, the Advisor refuses anything that
isn't about the target tool — explicitly. The system prompt makes
that scope a hard constraint.

Why narrow? The deck's capability library grows with use (philosophy
thesis 2). A per-tool Advisor is a per-tool *instructional surface* —
a netrunner who's looking at `rg` in the Tools tab can press H and
get a focused "how do I use this for X" exchange without polluting
the daemon channel or blowing watchdog quota on infrastructure
questions. Narrow scope = high signal = cheap per-call.

Caliber: haiku + low. Tool advice is lookup-and-format, not deep
reasoning. The cost asymmetry (~30x Haiku → Opus per token) is
exactly what we want exploited here.

Substrate: `claude -p` one-shot per question. NOT streaming. Three
reasons:
  1. Multiple advisor sessions can stack (netrunner asks about
     ripgrep, then about jq, then about ripgrep again); a streaming
     subprocess per session would be heavyweight.
  2. Multi-turn context within a single advisor session is cheap to
     pass in the prompt itself (Q1, A1, Q2, A2, ..., Qn). No need for
     `--resume` machinery.
  3. The total Q&A volume per session is small enough that one-shot
     latency (a few seconds on cache hit) beats the complexity tax
     of streaming subprocess management for this use case.

Not on the bus: Advisor activity is netrunner-facing scratch work,
not load-bearing fleet event flow. The chatlog should stay focused
on the actual orchestration story. If we ever want a "show me what
I asked the advisor today" view, the modal can keep its own session
log; we don't need to spam bus subscribers.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


# Per-question hard timeout. Tool questions should resolve fast;
# a hung subprocess past this is almost always a wedged claude. Same
# 60s budget as Watchdog one-shot — symmetric for symmetric usage.
DEFAULT_TIMEOUT = 60.0


# Caliber: sonnet + medium. Originally haiku/low (tool advice is
# lookup-shape, ought to be cheap), but the first real-deck pass
# (2026-05-05) caught Haiku-low failing two ways:
#   (1) it lost track of its scope anchor and asked the netrunner
#       to clarify which plugin — already-told-you-which
#       information. Symptom of a small model not following a
#       narrowly-scoped system prompt reliably.
#   (2) it answered using context from the deck's project-root
#       CLAUDE.md — which we initially misdiagnosed as Read tool
#       use, but the docs (https://code.claude.com/docs/en/memory)
#       confirm CLAUDE.md is auto-loaded into every claude session
#       from cwd + walking up parent dirs, regardless of tool use.
#       The fix for that lives in the subprocess command (--bare +
#       env vars) — see _run_one. The caliber bump here is for the
#       scope-following half.
# Sonnet/medium follows the scope rule reliably. Reified as
# constants so the modal renders the caliber in its subtitle
# ("Advisor: ripgrep · sonnet·medium · …") — the netrunner sees
# what they're paying for.
ADVISOR_MODEL = "sonnet"
ADVISOR_EFFORT = "medium"


@dataclass
class AdvisorTarget:
    """The tool or plugin the Advisor is scoped to.

    Compact view-model for the system prompt: name + kind + the bits
    of metadata that help the model answer well. Caller (the modal)
    builds this from a Tool or Plugin dataclass.

    `name` is the user-visible identifier. The Advisor uses this as
    its sole subject — questions about anything else are out of scope.

    `kind_label` is a short human-shaped descriptor for the system
    prompt ("a binary CLI tool", "a deck plugin", etc.). Distinct
    from a registry kind constant because the Advisor's prompt
    should read like prose.

    `command` is the invocation surface. For binaries: the shell
    command (`rg`). For plugins: the bridge invocation
    (`python <bridge> <name>` or just `<name>` — we let the caller
    decide how to phrase it).

    `description` and `help_text` (when present) are the meaty
    content of the prompt; the model reasons over them.

    `extended_text` is the full README (for plugins) or any other
    long-form documentation. Optional. Goes in the prompt verbatim
    when present — Haiku has plenty of context for this.

    `available` + `unavailable_reason` matter because the Advisor
    should mention "this tool is currently unavailable: <reason>"
    when the user asks how to use a tool that won't run on their
    machine. Saves a "why isn't this working" round-trip.
    """

    name: str
    kind_label: str
    command: str
    description: str
    help_text: str = ""
    extended_text: str = ""
    available: bool = True
    unavailable_reason: Optional[str] = None
    source_path: Optional[str] = None


@dataclass
class AdvisorTurn:
    """One Q+A exchange within an Advisor session.

    The modal stacks these so follow-ups can include "you previously
    asked X, I answered Y" context. answer is None until resolved.
    failed/error mirror Watchdog's failure shape so the modal can
    render "advisor failed: <reason>" on a bad turn without crashing
    the session — the netrunner can re-ask.
    """

    question: str
    answer: Optional[str] = None
    asked_at: float = field(default_factory=time.time)
    answered_at: Optional[float] = None
    failed: bool = False
    error: Optional[str] = None
    turn_id: str = field(default_factory=lambda: f"adv-{uuid.uuid4().hex[:8]}")


# Static body of the advisor's system prompt — the parts that don't
# depend on which tool we're advising on. Extracted as a module-level
# constant (item 000 phase 2, 2026-05-11) so the roles registry can
# externalize it to <deck-source>/roles/advisor.md when role-injection
# is enabled. Uses str.format-style {placeholder} tokens for the per-
# target slots; build_system_prompt() flattens AdvisorTarget fields
# into format kwargs at spawn time.
#
# Placeholders:
#   {target_name}            target.name
#   {target_kind_label}      target.kind_label
#   {target_command}         target.command
#   {target_description}     target.description
#   {src_block}              pre-rendered SOURCE line (or empty)
#   {avail_line}             pre-rendered availability blurb
#   {help_block}             pre-rendered HELP TEXT section (or empty)
#   {extended_block}         pre-rendered EXTENDED DOCUMENTATION section
#   {sibling_block}          pre-rendered list of other tool names
ADVISOR_TEMPLATE = """\
You are the Advisor for ONE specific tool: **{target_name}**.

You answer the netrunner's questions about how to use {target_name}.
That is your entire job. You do nothing else.

================================================================
HARD RULES — read these before doing anything else
================================================================

1. YOU ALREADY KNOW WHICH TOOL YOU ARE ADVISING ON.
   It is **{target_name}**. Do NOT ask the netrunner to clarify
   which tool — they pressed `h` on this tool's info modal, that
   is how you got opened. If their question is vague (e.g. "what
   is this?", "how do I use it?", "what does it do?"), they mean
   {target_name} — answer about {target_name}.

2. EVERYTHING YOU KNOW IS IN THIS SYSTEM PROMPT.
   You have no tools and no file access — your only knowledge
   about {target_name} is what's written below in the "WHAT YOU
   KNOW ABOUT {target_name}" section. If a question would
   require information not in this prompt (a flag's exact
   behavior, a feature you can't see documented), say so
   honestly: "That's not in what I know about {target_name} —
   try `{target_name} --help` or the project docs."

3. STAY IN SCOPE. If the netrunner asks about something other
   than {target_name} — the Cyberdeck itself, AI models, their
   broader project, another tool's internals — refuse politely
   in one sentence and redirect: "That's outside my scope; I
   only know about {target_name}. For <X>, try the Advisor on
   <sibling-name> or ask the daemon." You may NAME other tools
   (cross-references like "you'd pipe this into jq") but you do
   NOT explain their internals — you don't know them.

================================================================
WHAT YOU KNOW ABOUT {target_name}
================================================================

KIND: {target_kind_label}
INVOCATION: {target_command}{src_block}
DESCRIPTION: {target_description}

{avail_line}{help_block}{extended_block}

================================================================
OTHER TOOLS REGISTERED ON THE DECK (names only)
================================================================

You may mention these by name when answering "how can I combine
{target_name} with X" questions. You do NOT know their internals
— if the netrunner wants details on one of these, they'd open
its own Advisor.

{sibling_block}

================================================================
STYLE
================================================================

- Concise. A few sentences plus an optional fenced code block.
- Practical. Lead with the command they'd run.
- Honest. Don't invent {target_name} flags or behaviors that
  aren't in this prompt — if you don't know, say so.
- No preamble. No "Great question!" — answer directly.
- Total answer under ~400 words unless they asked for depth.
- Markdown fine; backtick code spans render nicely.
"""


def build_system_prompt(
    target: AdvisorTarget,
    sibling_tool_names: tuple[str, ...] = (),
    *,
    template: Optional[str] = None,
) -> str:
    """Compose the Advisor's system prompt for a given target.

    The prompt is rebuilt per session, not per question — the modal
    keeps a single Advisor object alive for the duration of a Q&A
    chain on one tool. Rebuilding per question would mean the
    one-shot cache misses on every single turn (system prompt drift,
    same shape as the 2026-05-02 cache-fix problem). Stable system
    prompt + variable user message = warm cache + fast turns.

    Sibling list: names only. The Advisor is allowed to mention
    other tools by name when answering "how would I combine X with
    Y" questions, but it must not pretend to know how those siblings
    work — its expertise is bounded to the target. Names give it
    enough vocabulary to redirect ("for that, you'd want jq — open
    the Advisor on jq for details") without putting unfounded claims
    in its mouth.

    Role-injection support (item 000 phase 2, 2026-05-11): pass
    `template=` to override the bundled ADVISOR_TEMPLATE with content
    from `<deck-source>/roles/advisor.md`. The template must use the
    same {placeholder} tokens — build_system_prompt populates them
    from `target` + `sibling_tool_names`. When `template` is None
    (default), uses the in-module ADVISOR_TEMPLATE constant for
    backward compatibility.
    """
    sibling_block = (
        "\n".join(f"  - {n}" for n in sibling_tool_names)
        if sibling_tool_names else "  (no other tools registered)"
    )

    avail_line = (
        "AVAILABILITY: available on the netrunner's machine."
        if target.available else
        f"AVAILABILITY: UNAVAILABLE — {target.unavailable_reason}. "
        "Mention this if the netrunner asks how to run the tool; "
        "they may need to install or configure it first."
    )

    extended_block = (
        f"\n\nEXTENDED DOCUMENTATION:\n{target.extended_text}"
        if target.extended_text else ""
    )

    help_block = (
        f"\n\nHELP TEXT:\n{target.help_text}"
        if target.help_text else ""
    )

    src_block = (
        f"\nSOURCE: {target.source_path}"
        if target.source_path else ""
    )

    # Resolve template: caller-supplied wins; otherwise use the
    # in-module constant. Role-injection path passes the role-file
    # text; default path uses the bundled constant.
    tpl = template if template is not None else ADVISOR_TEMPLATE
    return tpl.format(
        target_name=target.name,
        target_kind_label=target.kind_label,
        target_command=target.command,
        target_description=target.description,
        src_block=src_block,
        avail_line=avail_line,
        help_block=help_block,
        extended_block=extended_block,
        sibling_block=sibling_block,
    )


def build_user_prompt(
    new_question: str,
    prior_turns: tuple[AdvisorTurn, ...] = (),
) -> str:
    """Compose the user-side prompt for one ask().

    Multi-turn context: prior Q&A in the same modal session get
    serialized and prepended so the model can answer follow-ups
    coherently ("OK, and how would I combine that with --json?").
    Token cost: bounded by modal session length, which is bounded
    by netrunner attention. Not worth a session-resume mechanism.

    Failed prior turns are excluded — there's no point feeding
    "Q: ... A: <error>" back to the model; it'd just re-ask
    confused. The new turn proceeds as if the failed one didn't
    happen.
    """
    parts: list[str] = []
    for t in prior_turns:
        if t.failed or t.answer is None:
            continue
        parts.append(f"PRIOR Q: {t.question}\nPRIOR A: {t.answer}")
    parts.append(f"NEW Q: {new_question}")
    return "\n\n".join(parts)


class Advisor:
    """Per-tool Advisor — one instance per modal session.

    Lifecycle:
      adv = Advisor(target=AdvisorTarget(...), sibling_names=("jq", "fd"))
      turn = await adv.ask("how do I search case-insensitively?")
      # turn.answer is set; or turn.failed=True with turn.error

    Concurrency: one question in flight at a time per Advisor
    instance. The modal serializes through this; if the netrunner
    spams Enter, subsequent questions wait their turn. Same posture
    as the Watchdog queue — the cost of letting them race
    (double-spent tokens for overlapping context, interleaved UI
    updates) outweighs the benefit (the netrunner's slow enough
    that the perceived latency hit is negligible).
    """

    def __init__(
        self,
        target: AdvisorTarget,
        sibling_tool_names: tuple[str, ...] = (),
        claude_bin: str = "claude",
        cwd: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        *,
        template: Optional[str] = None,
    ) -> None:
        self.id = f"adv-{uuid.uuid4().hex[:8]}"
        self.target = target
        self.sibling_tool_names = sibling_tool_names
        self.claude_bin = claude_bin
        self.cwd = cwd
        self.timeout = timeout
        # Item 000 phase 2 (2026-05-11): role-injection support.
        # `template`, when provided, overrides the bundled ADVISOR_TEMPLATE
        # with content from <deck-source>/roles/advisor.md. The TUI passes
        # this in when `prefs.role_injection` is True; None preserves
        # current behavior. build_system_prompt handles the substitution
        # either way.
        self.system_prompt = build_system_prompt(
            target, sibling_tool_names, template=template,
        )
        self._lock = asyncio.Lock()
        self._turns: list[AdvisorTurn] = []

    @property
    def turns(self) -> tuple[AdvisorTurn, ...]:
        """Snapshot of resolved + in-flight turns. The modal reads
        this when re-rendering the scrollback after a turn lands."""
        return tuple(self._turns)

    async def ask(self, question: str) -> AdvisorTurn:
        """Send one question through `claude -p`, return the turn.

        Serialized via self._lock so concurrent callers queue. The
        returned AdvisorTurn is the same object the caller can see
        in self._turns — mutating it on resolve is intentional, the
        modal reads attributes off the live turn to update the UI.
        """
        turn = AdvisorTurn(question=question)
        # Append eagerly so the modal can render "Q: ... · thinking"
        # before the subprocess finishes. The lock below ensures
        # only one turn is *in flight* at a time, but the caller
        # gets the turn handle immediately.
        self._turns.append(turn)

        async with self._lock:
            await self._run_one(turn)
        return turn

    async def _run_one(self, turn: AdvisorTurn) -> None:
        """Spawn one `claude -p` subprocess for one turn.

        Mirrors Watchdog._process_oneshot's failure shape:
          - claude binary missing
          - subprocess spawn failed
          - timeout
          - non-zero exit
          - empty stdout

        **Clean-spawn recipe (verified against
        https://code.claude.com/docs/ on 2026-05-05, refined on
        round-3 after `--bare` broke OAuth auth).** First
        real-deck Advisor pass caught the model answering with
        content from the deck's project-root CLAUDE.md — turns out
        Claude Code auto-loads CLAUDE.md from cwd + walks up parent
        dirs concatenating every CLAUDE.md it finds, AND auto-loads
        ~/.claude/projects/<repo>/memory/MEMORY.md (first 200 lines
        / 25KB), AND user-level ~/.claude/CLAUDE.md, AND any
        managed-policy CLAUDE.md.

        Round-2 tried `--bare` for one-flag suppression. Real-deck
        round-3 caught it: `claude --help` reveals `--bare`
        "Anthropic auth is strictly ANTHROPIC_API_KEY or
        apiKeyHelper via --settings (OAuth and keychain are never
        read)". The netrunner uses Claude Max via OAuth/keychain;
        `--bare` made every Advisor turn exit 1 silently because
        auth never resolved. Dropped --bare; rely on env vars for
        the CLAUDE.md/auto-memory suppression (they're independently
        documented to do exactly that without breaking auth).

        Round-4 caught a critical bug in the round-3 recipe:
        `--system-prompt <text>` (passing the prompt as an argv
        value) **truncates at the first newline on Windows**. The
        Advisor's prompt is ~5800 chars including the plugin
        README; the first newline is right after the opening
        line, so the model was receiving only "You are the
        Advisor for ONE specific tool: <name>." and absolutely
        nothing else. The model honestly reported "I don't have
        a README" — it didn't. Diagnosed by asking the model to
        verbatim-quote its own EXTENDED DOCUMENTATION block; it
        replied "NO EXTENDED DOCUMENTATION SECTION." Repro'd
        with a synthetic 3-line prompt: lines 2 and 3 silently
        clipped under `--system-prompt`; both survive intact
        under `--system-prompt-file`.

        Fix: write the composed system prompt to a temp file and
        pass `--system-prompt-file <path>` instead. Cleanup runs
        in finally so the file is removed even on exception.
        Filed as a deck gotcha: any subprocess passing
        multi-line content via argv on Windows is a tripwire.

        Final recipe:

          --system-prompt-file <path>: REPLACES the default system
                                    prompt with the contents of a
                                    file. Avoids the argv newline
                                    truncation that breaks
                                    --system-prompt with multi-
                                    line content.
          --tools "":               disables every built-in tool —
                                    verbatim, 'Use "" to disable all'
          --disable-slash-commands: skips skills + slash commands
                                    (covers what --bare was hitting
                                    for those layers)
          --no-session-persistence: -p mode only; don't write a
                                    transcript to disk for each turn
          env CLAUDE_CODE_DISABLE_CLAUDE_MDS=1: kills CLAUDE.md
                                    auto-load — verbatim, "prevent
                                    loading any CLAUDE.md memory
                                    files into context, including
                                    user, project, and auto-memory
                                    files"
          env CLAUDE_CODE_DISABLE_AUTO_MEMORY=1: belt-and-suspenders
                                    against MEMORY.md auto-load
          env CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1: removes git
                                    workflow instructions + git status
                                    snapshot from system prompt

        The ONLY thing that can still leak past all of this is a
        managed-policy CLAUDE.md, which the docs explicitly say
        "cannot be excluded." Not a concern for the netrunner's
        solo deck — no IT pushing managed policies.
        """
        bin_path = shutil.which(self.claude_bin) or self.claude_bin

        prior = tuple(t for t in self._turns if t is not turn)
        user_prompt = build_user_prompt(turn.question, prior)

        # Write the system prompt to a temp file. `--system-prompt`
        # truncates at the first newline when the prompt is passed
        # as an argv value (Windows argv parsing or claude code's
        # parser; verified empirically 2026-05-05). `--system-
        # prompt-file` reads the file and preserves multi-line
        # content correctly. Use a per-turn temp file so concurrent
        # turns (shouldn't happen — we serialize via _lock — but be
        # robust) don't collide.
        sysprompt_path: Optional[str] = None
        try:
            fd, sysprompt_path = tempfile.mkstemp(
                suffix=".txt", prefix=f"advisor-{self.id}-",
            )
            os.close(fd)
            Path(sysprompt_path).write_text(
                self.system_prompt, encoding="utf-8",
            )
        except Exception as e:
            turn.failed = True
            turn.error = f"failed to write system-prompt file: {e}"
            turn.answered_at = time.time()
            if sysprompt_path:
                try:
                    os.unlink(sysprompt_path)
                except Exception:
                    pass
            return

        cmd = [
            bin_path,
            "-p",
            # FULL system-prompt replacement via FILE (not argv —
            # argv mode silently truncates at the first newline).
            # The default Claude Code system prompt is tool-
            # oriented; replacing it entirely keeps the model
            # focused on the scope rule.
            "--system-prompt-file", sysprompt_path,
            # Disable every built-in tool. The Advisor answers from
            # its system prompt alone — no Read/Bash/Edit/etc.
            "--tools", "",
            # Skip skills + slash commands. Covers what --bare was
            # doing for those layers without breaking OAuth.
            "--disable-slash-commands",
            # Caliber.
            "--model", ADVISOR_MODEL,
            "--effort", ADVISOR_EFFORT,
            # Don't persist the Q&A transcript to disk. Per-question
            # Advisor turns are scratch work, not load-bearing fleet
            # event flow. Avoids cluttering ~/.claude/projects/...
            # with hundreds of Advisor session files over time.
            "--no-session-persistence",
        ]

        # Belt-and-suspenders env vars for context-leak suppression.
        # Each one independently kills a different auto-load path.
        # Per-subprocess scope (passed via env= kwarg below); does
        # NOT mutate the deck's own env or anything else on the
        # system.
        env = {
            **os.environ,
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        }

        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.cwd,
                    env=env,
                )
            except FileNotFoundError:
                turn.failed = True
                turn.error = f"claude binary not found: {self.claude_bin}"
                turn.answered_at = time.time()
                return
            except Exception as e:
                turn.failed = True
                turn.error = f"subprocess spawn failed: {e}"
                turn.answered_at = time.time()
                return

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(input=user_prompt.encode("utf-8")),
                    timeout=self.timeout,
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                turn.failed = True
                turn.error = f"timed out after {self.timeout}s"
                turn.answered_at = time.time()
                return

            if proc.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace").strip()
                turn.failed = True
                turn.error = (
                    f"claude exited {proc.returncode}: "
                    f"{err_text[:200] if err_text else '(no stderr)'}"
                )
                turn.answered_at = time.time()
                return

            answer = stdout.decode("utf-8", errors="replace").strip()
            if not answer:
                turn.failed = True
                turn.error = "claude returned empty output"
                turn.answered_at = time.time()
                return

            turn.answer = answer
            turn.answered_at = time.time()
        finally:
            # Always clean up the temp prompt file. Survives
            # subprocess crashes, timeouts, exceptions during
            # decode. Best-effort — a leaked temp file is harmless,
            # but we'd rather not have them.
            if sysprompt_path:
                try:
                    os.unlink(sysprompt_path)
                except Exception:
                    pass


# ---- target builders -------------------------------------------------------
#
# Helpers that translate the deck's Tool / Plugin dataclasses into the
# Advisor's view-model. Kept here (not in tools.py / plugins.py) so the
# data layer stays unaware of the Advisor; this module owns the
# coupling.

def target_from_tool(tool: Any) -> AdvisorTarget:
    """Build an AdvisorTarget from a tools.Tool dataclass.

    Duck-typed (Any) to avoid an import cycle through tools.py — this
    module is callable from the modal and the modal is callable from
    tools-aware code. The Tool fields used here are stable contract.
    """
    kind_label = (
        "a binary CLI tool installed on the netrunner's PATH"
        if getattr(tool, "kind", "") == "binary"
        else "a script tool registered in tools.toml"
    )
    src = getattr(tool, "source_path", None)
    return AdvisorTarget(
        name=tool.name,
        kind_label=kind_label,
        command=tool.command,
        description=tool.description,
        help_text=getattr(tool, "help_text", "") or "",
        available=getattr(tool, "available", True),
        unavailable_reason=getattr(tool, "unavailable_reason", None),
        source_path=str(src) if src is not None else None,
    )


def target_from_plugin(plugin: Any, readme_text: str = "") -> AdvisorTarget:
    """Build an AdvisorTarget from a plugins.Plugin dataclass.

    `readme_text` is read by the caller (the modal) — keeps file I/O
    out of this module so it stays sync-pure for testing. README is
    the LLM-facing interface doc per the plugin spec; including it
    in extended_text gives the Advisor real depth on plugin usage.
    """
    src_dir = getattr(plugin, "source_dir", None)
    return AdvisorTarget(
        name=plugin.name,
        kind_label=f"a deck plugin in the '{plugin.category}' category",
        command=f"python <bridge> {plugin.name} [args...]",
        description=plugin.description,
        extended_text=readme_text,
        available=getattr(plugin, "available", True),
        unavailable_reason=getattr(plugin, "unavailable_reason", None),
        source_path=str(src_dir) if src_dir is not None else None,
    )
