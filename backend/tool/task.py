"""Task tool: spawn sub-agent sessions."""
from pydantic import BaseModel, Field
import time

from core.log import create_logger
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.task")


class TaskArgs(BaseModel):
    description: str = Field(description="Short description of the task")
    prompt: str = Field(description="Detailed prompt for the sub-agent")
    subagent_type: str = Field(default="explore", description="Agent type: explore, general")


async def execute(args: TaskArgs, ctx: ToolContext) -> ToolResult:
    """Spawn a sub-agent to handle a task."""
    from session import session as session_mod
    from agent.agent import get_agent, list_subagents

    # Resolve model: agent override > parent session's model (matching opencode)
    agent_def = get_agent(args.subagent_type)
    # A primary agent is not spawnable. build and plan carry the whole
    # conversational contract — plan mode's review handshake, build's
    # todo bookkeeping — none of which means anything in a child session
    # that answers one prompt and exits. opencode draws the same line.
    if agent_def.mode == "primary":
        raise ValueError(
            f"'{args.subagent_type}' is not a subagent. Available: "
            + ", ".join(sorted(a.name for a in list_subagents()))
        )
    parent_session = await session_mod.get_session(ctx.session_id, user_id=ctx.user_id or "default")
    child_model = agent_def.model or (parent_session.model if parent_session else "")

    # Create a child session linked to parent (won't appear in sidebar)
    child = await session_mod.create_session(
        agent=args.subagent_type,
        title=f"{args.description} (@{args.subagent_type} subagent)",
        parent_id=ctx.session_id,
        model=child_model,
        user_id=ctx.user_id or "default",
        workspace_id=(parent_session.workspace_id if parent_session else ctx.workspace_id),
        project_id=(parent_session.project_id if parent_session else None),
    )

    # Share parent's sandbox with the child session so tools can access it
    from sandbox import sandbox_manager
    parent_project = sandbox_manager._session_project.get(ctx.session_id)
    if parent_project:
        sandbox_info = sandbox_manager._project_map.get(parent_project)
        if sandbox_info:
            sandbox_info.session_ids.add(child.id)
            sandbox_manager._session_project[child.id] = parent_project

    from agent.trajectory import context_for_tool
    from core.identifier import ascending
    from trajectory import bind, record
    parent_trace = await context_for_tool(ctx)
    child_trace = None
    if parent_trace is not None:
        child_trace = parent_trace.derive(source_session_id=child.id, agent_id=ascending("agent"),
            parent_agent_id=parent_trace.agent_id, parent_call_id=parent_trace.call_id,
            run_id=None, generation=None, step_id=None, request_id=None, call_id=None,
            message_id=None, part_id=None)
    await record("agent.spawned", {"agent": args.subagent_type, "description": args.description,
        "prompt": args.prompt, "child_session_id": child.id, "model": child_model}, context=child_trace)
    child_started = time.monotonic()

    try:
        with bind(child_trace):
            # Send the prompt
            await session_mod.create_user_message(
                session_id=child.id,
                text=args.prompt,
                agent=args.subagent_type,
                synthetic=True,
                user_id=ctx.user_id or "default",
            )

            # Point this tool call at its child BEFORE the child runs. The UI follows
            # the child's own parts to show what the subagent is doing; without the
            # pointer it has nothing to follow, and the pointer is useless if it only
            # arrives with the result — by then there is nothing left to watch. This
            # is why the parent's row read "task · running" and nothing else.
            await _announce_child(ctx, child.id, args.subagent_type)

            # Run the agent loop, and let the parent's stop reach it. The child has
            # its own abort signal, so aborting the parent alone left the subagent
            # running to completion after the user had already stopped the run.
            await _run_child(ctx, child.id)

    except BaseException as exc:
        import asyncio
        await record("agent.finished", {"status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            "error": {"type": type(exc).__name__, "message": str(exc)}, "child_session_id": child.id,
            "duration_ms": (time.monotonic() - child_started) * 1000,
            "timing_source": "producer_monotonic"}, context=child_trace)
        raise

    # Collect output: only the LAST text part (matching opencode's findLast)
    messages = await session_mod.get_messages(child.id, user_id=ctx.user_id or "default")
    text = ""
    last_assistant = None
    for msg in reversed(messages):
        role = msg.role if isinstance(msg.role, str) else msg.role.value
        if role == "assistant":
            if last_assistant is None:
                last_assistant = msg
            parts = msg.parts if isinstance(msg.parts, list) else []
            for part in reversed(parts):
                p = part if isinstance(part, dict) else (part.model_dump() if hasattr(part, "model_dump") else {})
                if isinstance(p, dict) and p.get("type") == "text":
                    text = p.get("text", "")
                    break
            if text:
                break

    # Wrap output in <task_result> tags (matching opencode format)
    output = "\n".join([
        f"task_id: {child.id}",
        "",
        "<task_result>",
        text,
        "</task_result>",
    ]) if text else "Task completed with no text output."

    await record("agent.message", {"direction": "child_to_parent", "output": text,
        "model_output": output, "child_session_id": child.id}, context=child_trace)
    finish = getattr(last_assistant, "finish", None)
    error = getattr(last_assistant, "error", None)
    status = ("cancelled" if ctx.abort is not None and ctx.abort.is_set() else
        "failed" if error else "waiting" if finish == "waiting_input" else
        "completed" if finish == "stop" else "unknown")
    await record("agent.finished", {"status": status, "finish_reason": finish, "error": error,
        "output": text, "model_output": output, "child_session_id": child.id,
        "duration_ms": (time.monotonic() - child_started) * 1000,
        "timing_source": "producer_monotonic"}, context=child_trace)
    return ToolResult(
        title=args.description,
        output=output,
        # Carried on the finished part too, so a reloaded conversation can
        # still link the row to the child session it spawned.
        metadata={"child_session_id": child.id, "subagent_type": args.subagent_type},
    )


async def _announce_child(ctx: ToolContext, child_id: str, subagent_type: str) -> None:
    """Record the child session on this tool part, while it still matters."""
    from session.session import get_messages, update_part_data

    if not ctx.part_id:
        return
    try:
        messages = await get_messages(ctx.session_id, user_id=ctx.user_id or "default")
        for msg in reversed(messages):
            for part in reversed(msg.parts or []):
                p = part if isinstance(part, dict) else (
                    part.model_dump() if hasattr(part, "model_dump") else {}
                )
                if isinstance(p, dict) and p.get("id") == ctx.part_id:
                    meta = dict(p.get("metadata") or {})
                    meta.update({"child_session_id": child_id, "subagent_type": subagent_type})
                    p["metadata"] = meta
                    await update_part_data(
                        ctx.part_id, p, publish=True, user_id=ctx.user_id or "default"
                    )
                    return
    except Exception as e:  # never fail the task over a progress pointer
        from trajectory import TrajectoryError
        if isinstance(e, TrajectoryError):
            raise
        log.debug(f"could not announce child session {child_id}: {e}")


async def _run_child(ctx: ToolContext, child_id: str) -> None:
    """Run the subagent, forwarding the parent's stop to it."""
    import asyncio

    from agent.loop import run_loop
    from session.status import trigger_abort

    user_id = ctx.user_id or "default"
    child = asyncio.create_task(run_loop(child_id, user_id=user_id))
    if ctx.abort is None:
        await child
        return

    watch = asyncio.create_task(ctx.abort.wait())
    done, _ = await asyncio.wait({child, watch}, return_when=asyncio.FIRST_COMPLETED)
    watch.cancel()
    if child in done:
        await child
        return
    # The parent was stopped. Signal the child and give it a moment to wind
    # down on its own before abandoning the wait.
    trigger_abort(child_id)
    try:
        await asyncio.wait_for(child, timeout=10)
    except (asyncio.TimeoutError, TimeoutError):
        child.cancel()


TASK_DESCRIPTION = """\
Delegate a bounded, non-trivial research or implementation task to a subagent.
Use `explore` for codebase discovery and focused research, or `general` for
multi-step work; configured spawnable agent types may also be selected. Do not
delegate a simple lookup in one known file.

Each invocation gets a fresh child conversation and context, isolated from the
parent conversation. It still shares the parent's project sandbox/worktree, so
concurrent agents can observe or conflict with each other's file changes.

Make the prompt self-contained: state the objective and scope, relevant paths,
whether writes are allowed, constraints, verification commands, and the exact
result to return. Run only independent tasks concurrently; run dependent or
overlapping edits sequentially.

The subagent returns one result to this parent; inspect it and summarize the
user-facing outcome. The child session is linked to this tool call and is
stopped when the parent run is stopped."""

task_tool = define_tool(
    "task",
    description=TASK_DESCRIPTION,
    parameters=TaskArgs,
    execute=execute,
    sandbox_required=False,
)
