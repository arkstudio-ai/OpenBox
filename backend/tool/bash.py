"""Bash tool: execute shell commands in the sandbox with real-time output streaming."""
import re

from pydantic import BaseModel, Field

from core.log import create_logger
from sandbox.client import IdleNotification
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.bash")


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
) -> str:
    """Ask LLM whether an idle command should be killed or kept waiting.

    Returns: "kill", "wait", or "success".
    """
    import litellm
    from agent.llm import _get_provider_kwargs
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
        response = await litellm.acompletion(
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
    timeout = min(args.timeout, MAX_TIMEOUT)

    # The action server gets MAX_TIMEOUT as the hard safety cap.
    # Actual timeout decisions are made by the idle detection + LLM judgment system.
    # Timeline: idle check at IDLE_TIMEOUT intervals → LLM decides wait/kill/success
    #           MAX_TIMEOUT is only the absolute backstop.
    try:
        collected_output = ""
        exit_code = 0
        idle_judge_count = 0

        async for chunk in ctx.sandbox.execute_stream(
            command=args.command,
            timeout=MAX_TIMEOUT,
            idle_timeout=IDLE_TIMEOUT,
            workdir=ctx.workdir,
        ):
            if isinstance(chunk, IdleNotification):
                idle_judge_count += 1

                if idle_judge_count > MAX_IDLE_JUDGES:
                    # Too many idle checks — force kill
                    log.info(f"Force killing idle process (pid={chunk.pid}): {args.command[:80]}")
                    await ctx.sandbox.kill_command(chunk.pid)
                    collected_output += f"\n[Process killed: idle too long ({chunk.idle_seconds}s no output)]\n"
                    await ctx.update_output(collected_output)
                    continue

                # Ask LLM to judge
                log.info(f"Idle detected ({chunk.idle_seconds}s), asking LLM to judge: {args.command[:80]}")
                decision = await _judge_idle_command(
                    command=args.command,
                    recent_output=collected_output[-2000:],
                    idle_seconds=chunk.idle_seconds,
                    total_seconds=chunk.total_seconds,
                )
                log.info(f"LLM idle judgment: {decision}")

                if decision in ("kill", "success"):
                    await ctx.sandbox.kill_command(chunk.pid)
                    suffix = "completed successfully" if decision == "success" else "killed (idle)"
                    collected_output += f"\n[Process {suffix} after {chunk.total_seconds}s]\n"
                    await ctx.update_output(collected_output)
                else:
                    # decision == "wait" — continue waiting, show status
                    await ctx.update_output(
                        collected_output + f"\n[Waiting... no output for {chunk.idle_seconds}s]\n"
                    )

            elif isinstance(chunk, int):
                exit_code = chunk
            else:
                # OutputChunk with type and content
                collected_output += chunk.content
                idle_judge_count = 0  # Reset on new output
                # Push incremental update to frontend
                if len(collected_output) <= MAX_STREAM_OUTPUT:
                    await ctx.update_output(collected_output)

        output = collected_output
        if len(output) > MAX_STREAM_OUTPUT:
            output = output[:MAX_STREAM_OUTPUT] + "\n... (output truncated)"

        return ToolResult(
            title=f"exit code: {exit_code}",
            output=output,
            metadata={"exit_code": exit_code},
        )

    except Exception:
        # Fallback to non-streaming execution if streaming fails
        result = await ctx.sandbox.execute(
            command=args.command,
            timeout=timeout,
            workdir=ctx.workdir,
        )

        output = result.stdout
        if result.stderr:
            output += f"\nSTDERR:\n{result.stderr}"

        return ToolResult(
            title=f"exit code: {result.exit_code}",
            output=output,
            metadata={"exit_code": result.exit_code},
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
