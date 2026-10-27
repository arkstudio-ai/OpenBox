"""Bash tool: execute shell commands in the sandbox with real-time output streaming."""
import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from core.log import create_logger
from sandbox.client import IdleNotification
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.bash")


class _BashAborted(RuntimeError):
    """The owning Agent run stopped while its command stream was open."""


@dataclass(frozen=True)
class _IdleJudgment:
    notice: IdleNotification
    decision: str
    forced: bool = False


async def _iter_until_abort(
    stream: AsyncIterator[Any],
    abort: asyncio.Event,
    judge: Callable[[IdleNotification], Awaitable[str]],
):
    """Keep receiving output and Stop while the auxiliary idle judge runs.

    The generic tool scheduler deliberately drains already-dispatched external
    effects.  A shell process is locally cancellable, so waiting for it would
    make the Stop button appear broken.  Canceling the pending ``__anext__``
    closes SandboxClient's response; Action Server then kills the exact process
    group in its generator cleanup.

    A judge only describes the output observed when it started. New output or
    an exit invalidates it, even if its decision is ready in the same event-loop
    turn. At most one judge (including cancellation/billing settlement) exists;
    repeated idle notices do not queue more model calls behind a slow one.
    """
    iterator = stream.__aiter__()
    abort_task = asyncio.create_task(abort.wait())
    next_task: asyncio.Task | None = None
    judge_task: asyncio.Task | None = None
    judge_notice: IdleNotification | None = None
    judge_stale = False
    judge_forced = False
    idle_judge_count = 0
    command_exited = False

    def invalidate_judge():
        nonlocal judge_stale
        if judge_task is not None and not judge_stale:
            judge_stale = True
            # Cancel once: another cancellation could interrupt the provider's
            # shielded usage settlement. Its obsolete result is never applied.
            if not judge_task.done():
                judge_task.cancel()

    def check_judge_failure():
        if judge_task is not None and judge_task.done():
            if judge_task.cancelled() and judge_stale:
                return
            # Revocation must still reach the owning run when output made this
            # judgment stale, or when cancellation raced with the run fence.
            # A cancellation we did not request is also a run cancellation.
            judge_task.result()

    async def forced_kill():
        return "kill"

    try:
        while True:
            if abort.is_set():
                raise _BashAborted
            check_judge_failure()
            if judge_task is not None and judge_stale and judge_task.done():
                judge_task = None
            if next_task is None:
                next_task = asyncio.create_task(anext(iterator))
            pending = {next_task, abort_task}
            if judge_task is not None:
                pending.add(judge_task)
            await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if abort.is_set():
                raise _BashAborted
            check_judge_failure()
            # Check the live task state, not just asyncio.wait's snapshot: the
            # stream may become ready alongside the judge's completion callback.
            if next_task.done():
                try:
                    chunk = next_task.result()
                except StopAsyncIteration:
                    return
                next_task = None
                if isinstance(chunk, IdleNotification):
                    if not command_exited and judge_task is None:
                        idle_judge_count += 1
                        judge_notice = chunk
                        judge_stale = False
                        judge_forced = idle_judge_count > MAX_IDLE_JUDGES
                        judge_task = asyncio.create_task(
                            forced_kill() if judge_forced else judge(chunk)
                        )
                    continue
                invalidate_judge()
                if isinstance(chunk, int):
                    command_exited = True
                else:
                    idle_judge_count = 0
                yield chunk
                continue
            if judge_task is not None and judge_task.done():
                completed_judge = judge_task
                judge_task = None
                if not judge_stale and not completed_judge.cancelled():
                    assert judge_notice is not None
                    yield _IdleJudgment(judge_notice, completed_judge.result(), judge_forced)
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
        if next_task is not None:
            await asyncio.gather(next_task, return_exceptions=True)
        invalidate_judge()
        try:
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()
        finally:
            abort_task.cancel()
            await asyncio.gather(abort_task, return_exceptions=True)
            if judge_task is not None:
                await asyncio.gather(judge_task, return_exceptions=True)
                check_judge_failure()


class BashArgs(BaseModel):
    command: str = Field(description="The command to execute")
    timeout: int = Field(default=120, description="Optional timeout in seconds. Default 120s (2 minutes). Max 600s (10 minutes).")
    description: str = Field(default="", description="Description of what this command does")


MAX_STREAM_OUTPUT = 100_000  # Max characters to stream (prevent memory overflow)
IDLE_TIMEOUT = 60   # Seconds without output before triggering idle check
MAX_IDLE_JUDGES = 5  # Max LLM judgment calls before force kill (~5 min total idle)
MAX_TIMEOUT = 600   # Absolute safety cap sent to action server (10 min)

# Protected command patterns — prevent agents from killing the action_server
# (the only communication channel between backend and container).
_PROTECTED_CMD_PATTERNS: list[tuple[str, str]] = [
    (r'\bkill\b[^|;]*\s+(-\w+\s+)*1\b',
     "Blocked: cannot kill PID 1 (container init process)"),
    (r'\bkill\b[^|;]*\s+-1\b',
     "Blocked: cannot send signals to all processes"),
    (r'\b(pkill|killall)\b[^|;]*\b(python[23]?|uvicorn|action_server)\b',
     "Blocked: killing python/uvicorn/action_server would destroy the sandbox execution interface"),
    (r'\bfuser\b[^|;]*-k[^|;]*\b8000\b',
     "Blocked: cannot kill processes on port 8000 (sandbox communication port)"),
    (r'\bfuser\b[^|;]*\b8000\b[^|;]*-k',
     "Blocked: cannot kill processes on port 8000 (sandbox communication port)"),
    (r'\blsof\b[^|]*\b8000\b.*\|\s*(xargs\s+)?kill',
     "Blocked: cannot kill processes on port 8000 (sandbox communication port)"),
    (r'\b(rm|mv)\b[^|;]*/opt/action_server',
     "Blocked: cannot modify /opt/action_server (sandbox execution interface)"),
    (r'\b(shutdown|poweroff|halt)\b',
     "Blocked: cannot shut down the container"),
]


def _check_protected_command(command: str) -> str | None:
    """Return error message if command would damage sandbox communication, None if safe."""
    for pattern, msg in _PROTECTED_CMD_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return msg
    return None


async def _judge_idle_command(
    command: str,
    recent_output: str,
    idle_seconds: int,
    total_seconds: int,
    ctx: ToolContext | None = None,
) -> str:
    """Ask LLM whether an idle command should be killed or kept waiting.

    Returns: "kill", "wait", or "success".
    """
    from agent.llm import _get_provider_kwargs, metered_completion
    from core.config import get_config

    config = get_config()
    model_id = config.model
    provider_kwargs = _get_provider_kwargs(model_id)

    prompt = f"""A shell command has been running with no output for {idle_seconds} seconds (total runtime: {total_seconds}s).

Command: {command}

Last output (tail):
{recent_output[-1000:] if recent_output else "(no output)"}

Classify this situation. Reply with ONLY one word:
- "wait" — command is likely still working (compiling, installing, downloading), just slow
- "kill" — command appears stuck, hung, or is in a state that won't progress
- "success" — command is a long-running server/service that has already started successfully

Reply with one word only: wait, kill, or success"""

    log.info(f"[LLM Judge] Calling model={model_id} for command='{command[:60]}' idle={idle_seconds}s total={total_seconds}s")

    try:
        response = await metered_completion(
            ctx=ctx or ToolContext(), billing_kind="bash_judge",
            model=model_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=10,
            temperature=0.0,
            **provider_kwargs,
        )
        raw_answer = response.choices[0].message.content.strip()
        answer = re.sub(r"<think>[\s\S]*?</think>\s*", "", raw_answer).strip().lower()
        log.info(f"[LLM Judge] Raw response: '{raw_answer}' → parsed: '{answer}'")
        if answer in ("kill", "wait", "success"):
            return answer
        log.warning(f"[LLM Judge] Unrecognized response '{answer}', defaulting to kill")
        return "kill"
    except Exception as e:
        from agent.driver import LeaseLostError
        from question.runtime import RunRevoked
        if isinstance(e, (LeaseLostError, RunRevoked)):
            raise
        log.warning(f"[LLM Judge] LLM call failed: {e}")
        return "kill"


async def execute(args: BashArgs, ctx: ToolContext) -> ToolResult:
    """Execute a shell command in the sandbox with real-time output streaming."""
    blocked = _check_protected_command(args.command)
    if blocked:
        return ToolResult(
            title="command blocked",
            output=blocked,
            metadata={"exit_code": 1, "blocked": True},
        )

    # The action server gets MAX_TIMEOUT as the hard safety cap.
    # Actual timeout decisions are made by the idle detection + LLM judgment system.
    # Timeline: idle check at IDLE_TIMEOUT intervals → LLM decides wait/kill/success
    #           MAX_TIMEOUT is only the absolute backstop.
    try:
        collected_output = ""
        exit_code = 0
        stream_output_complete = None
        output_incomplete_reason = None

        async def judge(notice: IdleNotification) -> str:
            log.info(f"Idle detected ({notice.idle_seconds}s), asking LLM to judge: {args.command[:80]}")
            return await _judge_idle_command(
                command=args.command,
                recent_output=collected_output[-2000:],
                idle_seconds=notice.idle_seconds,
                total_seconds=notice.total_seconds,
                ctx=ctx,
            )

        stream = ctx.sandbox.execute_stream(
            command=args.command,
            timeout=MAX_TIMEOUT,
            idle_timeout=IDLE_TIMEOUT,
            workdir=ctx.workdir,
        )
        async with aclosing(_iter_until_abort(stream, ctx.abort, judge)) as events:
            async for chunk in events:
                if isinstance(chunk, _IdleJudgment):
                    notice, decision = chunk.notice, chunk.decision
                    if chunk.forced:
                        log.info(f"Force killing idle process (pid={notice.pid}): {args.command[:80]}")
                        await ctx.sandbox.kill_command(notice.pid)
                        collected_output += f"\n[Process killed: idle too long ({notice.idle_seconds}s no output)]\n"
                        await ctx.update_output(collected_output)
                    elif decision in ("kill", "success"):
                        log.info(f"LLM idle judgment: {decision}")
                        await ctx.sandbox.kill_command(notice.pid)
                        suffix = "completed successfully" if decision == "success" else "killed (idle)"
                        collected_output += f"\n[Process {suffix} after {notice.total_seconds}s]\n"
                        await ctx.update_output(collected_output)
                    else:
                        log.info(f"LLM idle judgment: {decision}")
                        await ctx.update_output(
                            collected_output + f"\n[Waiting... no output for {notice.idle_seconds}s]\n"
                        )
                elif isinstance(chunk, int):
                    exit_code = int(chunk)
                    stream_output_complete = getattr(chunk, "output_complete", None)
                    output_incomplete_reason = getattr(chunk, "output_incomplete_reason", None)
                else:
                    collected_output += chunk.content
                    # Push incremental update to frontend
                    if len(collected_output) <= MAX_STREAM_OUTPUT:
                        await ctx.update_output(collected_output)
                    else:
                        # The chat preview has a size budget, but observed stdout
                        # remains part of the execution history after that point:
                        # the call's output stream records each further chunk as
                        # appended text, never the whole output again.
                        from trajectory.tool_output import output_stream
                        await output_stream(ctx, "bash").append(chunk.content, output=collected_output)

        ctx._trajectory_full_tool_output = collected_output
        output = collected_output
        output_truncated = len(output) > MAX_STREAM_OUTPUT
        if len(output) > MAX_STREAM_OUTPUT:
            output = output[:MAX_STREAM_OUTPUT] + "\n... (output truncated)"
        if stream_output_complete is False:
            output += (
                "\n[Command exited, but its output is incomplete: the output pipes did not "
                "close before the drain deadline. Do not infer missing files or rerun the "
                "command from this result. Read the specific expected file with the read "
                "tool, or inspect the process/log of an intentionally backgrounded job.]"
            )

        return ToolResult(
            title=f"exit code: {exit_code}",
            output=output,
            metadata={"exit_code": exit_code,
                      "stream_output_complete": stream_output_complete,
                      "output_complete": False if output_truncated else stream_output_complete,
                      "output_truncated": output_truncated,
                      "output_incomplete_reason": output_incomplete_reason},
        )

    except _BashAborted:
        output = collected_output + "\n[Process stopped before completion]\n"
        await ctx.update_output(output)
        return ToolResult(
            title="command stopped",
            output=output,
            metadata={
                "exit_code": -9,
                "error": True,
                "failure_code": "tool_aborted",
            },
        )
    except Exception as exc:
        from agent.driver import LeaseLostError
        from question.runtime import RunRevoked
        from sandbox.client import refused_before_send
        if isinstance(exc, (LeaseLostError, RunRevoked)):
            raise
        refused = refused_before_send(exc) or ""
        if refused.startswith("POST ") and refused.endswith("/execute_stream"):
            # Authorization, admission or a precondition refused the request
            # before it left this process: the command was never sent, so its
            # outcome is known. Only a stream lost after sending is unknown.
            code = getattr(exc, "code", None) or type(exc).__name__
            return ToolResult(
                title="command not sent",
                output=f"The command was not sent to the sandbox, so this call did not run it "
                       f"({code}: {str(exc)[:300]}). Resolve that before trying again.",
                metadata={"error": True, "failure_code": "command_not_sent", "refusal_code": str(code)[:64]},
            )
        # A broken stream is not evidence that the remote command never ran.
        # Reissuing it through /execute can duplicate an irreversible action.
        return ToolResult(
            title="command outcome unknown",
            output=collected_output + "\nThe command stream was interrupted. Inspect the current state before retrying; the command was not sent again.",
            metadata={"error": True, "outcome_unknown": True, "failure_code": "command_stream_interrupted"},
        )


BASH_DESCRIPTION = """\
Execute a shell command in the current sandbox project directory and stream its
output. Use this for terminal-only operations; use the dedicated file tools for
reading, searching, editing, or writing files, and respond directly instead of
using shell output for conversation.

Quote paths containing spaces. The timeout defaults to 120 seconds and is
capped at 600 seconds. Long output may be truncated; inspect the full artifact
with the file tools. Run independent commands as separate parallel calls, and
join dependent commands with `&&` so failure stops the sequence.

Sandbox safety: never stop, replace, or modify the action server, its port, or
PID 1; protected commands are blocked. Do not expose secrets in commands or
output.

Git safety: commit or push only when the user explicitly requests it. Review
status and diffs first and include only intended files. Never change git config,
skip hooks, use interactive flags, or run destructive/force operations without
explicit authorization. Prefer a new commit; amend only when explicitly asked,
the commit is yours, and it has not been pushed. Never force-push a protected
branch."""

bash_tool = define_tool(
    "bash",
    description=BASH_DESCRIPTION,
    parameters=BashArgs,
    execute=execute,
)
