"""Task tool: spawn sub-agent sessions."""
import asyncio
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from core.log import create_logger
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.task")


class TaskArgs(BaseModel):
    action: Literal["spawn", "fork", "follow_up", "interrupt", "report", "list"] = Field(
        default="spawn",
        description="Subagent operation",
    )
    description: str = Field(
        default="", max_length=255, description="Short task label",
    )
    prompt: str = Field(
        default="", max_length=65_536, description="Self-contained child prompt",
    )
    subagent_type: str = Field(default="explore", description="Child agent preset")
    lifecycle: Literal["one_shot", "continuable"] = Field(
        default="one_shot",
        description="One-shot or continuable child",
    )
    task_id: str = Field(
        default="",
        max_length=64,
        description="Task id for control actions",
    )
    model: str | None = Field(
        default=None,
        max_length=128,
        description="Exact configured child model",
    )
    reasoning: str | None = Field(
        default=None,
        max_length=32,
        description="Declared reasoning variant",
    )
    persona: str | None = Field(
        default=None,
        max_length=8_192,
        description="Persona overlay",
    )
    tools: list[str] | None = Field(
        default=None,
        description="Exact child tool allowlist",
    )
    output_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema for the result",
    )

    @field_validator("tools")
    @classmethod
    def _bounded_tools(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if len(value) > 4_096:
            raise ValueError("tools exceeds the Task composition limit")
        if any(not item or len(item) > 1_024 for item in value):
            raise ValueError("tools contains an invalid tool id")
        if len(set(value)) != len(value):
            raise ValueError("tools contains duplicate tool ids")
        return value


async def execute(args: TaskArgs, ctx: ToolContext) -> ToolResult:
    """Operate the durable subagent protocol."""
    from session import session as session_mod
    parent_session = await session_mod.get_session(ctx.session_id, user_id=ctx.user_id or "default")
    if parent_session is None:
        raise LookupError("parent Session not found")
    user_id = ctx.user_id or "default"
    project_id = parent_session.project_id

    from agent.subagent_runtime import (
        accept_follow_up,
        accept_spawn,
        interrupt_subagent,
        list_subagent_reports,
        report_subagent,
    )

    if args.action == "list":
        reports = await list_subagent_reports(
            user_id=user_id,
            parent_session_id=ctx.session_id,
            project_id=project_id,
        )
        return ToolResult(
            title="Subagents",
            output=json.dumps(reports, ensure_ascii=False, indent=2),
            metadata={"subagent_count": len(reports)},
        )
    if not args.task_id and args.action in {"follow_up", "interrupt", "report"}:
        raise ValueError(f"task_id is required for {args.action}")
    if args.action == "report":
        report = await report_subagent(
            args.task_id,
            user_id=user_id,
            parent_session_id=ctx.session_id,
            project_id=project_id,
        )
        return ToolResult(
            title=f"Subagent {args.task_id}",
            output=json.dumps(report, ensure_ascii=False, indent=2),
            metadata={"subagent_id": args.task_id, "subagent_outcome": report["outcome"]},
        )
    if args.action == "interrupt":
        result = await interrupt_subagent(
            args.task_id,
            user_id=user_id,
            parent_session_id=ctx.session_id,
            project_id=project_id,
        )
        return ToolResult(
            title=f"Interrupt {args.task_id}",
            output=json.dumps(result, ensure_ascii=False, indent=2),
            metadata={"subagent_id": args.task_id},
        )

    if not args.prompt:
        raise ValueError(f"prompt is required for {args.action}")
    if not args.description and args.action in {"spawn", "fork"}:
        raise ValueError(f"description is required for {args.action}")
    from agent.driver import current_run_fence

    parent_fence = current_run_fence()
    if parent_fence is None or parent_fence[0] != ctx.session_id:
        raise RuntimeError("Task activation cannot be accepted without a parent run fence")
    _, parent_run_id, parent_generation = parent_fence
    if ctx.run_id and ctx.run_id != parent_run_id:
        raise RuntimeError("Task parent context no longer matches its run fence")
    if not ctx.message_id or not ctx.part_id:
        raise RuntimeError("Task activation requires an exact parent ToolPart")

    if args.action in {"spawn", "fork"}:
        from agent.agent import get_agent, list_subagents
        from agent.subagent_authority import (
            authority_for_spawn,
            parse_subagent_authority,
            with_subagent_composition,
        )
        from agent.subagent_composition import build_subagent_composition
        from core.config import get_config

        agent_def = get_agent(args.subagent_type)
        if agent_def.mode not in {"subagent", "all"}:
            raise ValueError(
                f"'{args.subagent_type}' is not a subagent. Available: "
                + ", ".join(sorted(a.name for a in list_subagents()))
            )
        parent_authority = parse_subagent_authority(authority_for_spawn(
            getattr(ctx, "_subagent_authority_snapshot", None)
        ))
        composition = build_subagent_composition(
            agent_def=agent_def,
            parent_tool_ids=parent_authority.tool_ids,
            config=get_config(),
            inherited_model=parent_session.model,
            requested_model=args.model,
            reasoning=args.reasoning,
            persona=args.persona,
            requested_tools=args.tools,
            output_schema=args.output_schema,
            seed_mode="fork" if args.action == "fork" else "fresh",
        )
        authority_snapshot = with_subagent_composition(
            parent_authority,
            composition,
        ).to_json()
        fork_seed = None
        if args.action == "fork":
            from session.event_range import freeze_fork_event_range

            fork_seed = await freeze_fork_event_range(
                ctx.session_id,
                user_id=user_id,
                up_to_message_id=None,
            )
        activation = await accept_spawn(
            user_id=user_id,
            parent_session_id=ctx.session_id,
            parent_message_id=ctx.message_id,
            parent_part_id=ctx.part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
            task_title=args.description,
            prompt=args.prompt,
            subagent_type=args.subagent_type,
            child_model=composition.model,
            lifecycle=args.lifecycle,
            authority_snapshot=authority_snapshot,
            fork_seed=fork_seed,
        )
    else:
        from agent.subagent_authority import authority_for_spawn

        activation = await accept_follow_up(
            descriptor_id=args.task_id,
            user_id=user_id,
            parent_session_id=ctx.session_id,
            parent_message_id=ctx.message_id,
            parent_part_id=ctx.part_id,
            parent_run_id=parent_run_id,
            parent_generation=parent_generation,
            task_title=args.description,
            prompt=args.prompt,
            authority_snapshot=authority_for_spawn(
                getattr(ctx, "_subagent_authority_snapshot", None)
            ),
            requested_model=args.model,
            reasoning=args.reasoning,
            persona=args.persona,
            requested_tools=args.tools,
            output_schema=args.output_schema,
        )
    return await _dispatch_activation(ctx, activation, project_id=project_id)


async def _dispatch_activation(ctx: ToolContext, activation, *, project_id: str) -> ToolResult:
    """Foreground dispatcher; losing the exact claim becomes an outbox wait."""
    from agent.driver import DriverBusyError, DriverRecoveryRequiredError, reserve_run
    from agent.subagent_runtime import (
        abandon_claim,
        activation_completion_disposition,
        bind_claimed_activation,
        claim_activation,
        claim_is_dispatchable,
        complete_activation_from_transcript,
        interrupt_subagent,
        wait_for_outbox,
    )

    user_id = ctx.user_id or "default"

    async def wait_for_owner() -> ToolResult:
        try:
            raw = await wait_for_outbox(
                activation.id,
                user_id=user_id,
                abort=ctx.abort,
            )
        except asyncio.CancelledError:
            await interrupt_subagent(
                activation.descriptor_id,
                user_id=user_id,
                parent_session_id=ctx.session_id,
                project_id=project_id,
            )
            raise
        return ToolResult.model_validate(raw)

    claim = await claim_activation(activation.id, user_id=user_id)
    if claim is None:
        return await wait_for_owner()
    if not await claim_is_dispatchable(claim):
        await abandon_claim(claim)
        return await wait_for_owner()
    try:
        lease = await reserve_run(
            claim.child_session_id,
            claim.user_id,
            trigger_message_id=claim.child_trigger_message_id,
        )
    except (DriverBusyError, DriverRecoveryRequiredError):
        await abandon_claim(claim)
        return await wait_for_owner()
    except BaseException:
        await abandon_claim(claim)
        raise
    try:
        bound = await bind_claimed_activation(claim, lease)
        if not bound:
            await _release_failed_start(lease, claim.child_session_id)
            return await wait_for_owner()
        await _run_child(ctx, claim.child_session_id, lease)
    except BaseException:
        await _release_failed_start(lease, claim.child_session_id)
        raise
    disposition = await activation_completion_disposition(
        activation.id,
        child_run_id=lease.run_id,
        child_generation=lease.generation,
    )
    if disposition in {"replay", "wait"}:
        return await wait_for_owner()
    raw_result = await complete_activation_from_transcript(
        activation.id,
        child_run_id=lease.run_id,
        child_generation=lease.generation,
        forced_outcome=(
            "outcome_unknown" if disposition == "outcome_unknown" else None
        ),
        recovery_code=(
            "subagent_child_outcome_unknown"
            if disposition == "outcome_unknown" else None
        ),
    )
    return ToolResult.model_validate(raw_result)


async def _release_failed_start(lease, child_id: str) -> None:
    """Best-effort release for a generation that never reached ``run_loop``."""
    try:
        await lease.release(session_status="error")
    except Exception:
        # The row remains fenced by this generation and will expire into the
        # normal startup recovery path. Do not hide the original start error.
        log.exception("could not release unstarted child driver %s", child_id)


async def _run_child(ctx: ToolContext, child_id: str, lease) -> None:
    """Run one reserved subagent, forwarding the parent's stop durably."""
    import asyncio

    from agent.loop import run_loop
    from agent.driver import request_abort, wait_for_idle

    user_id = ctx.user_id or "default"

    async def drive() -> None:
        try:
            await run_loop(child_id, user_id=user_id, lease=lease)
        finally:
            # ``run_loop`` normally releases in its own finally block. This
            # idempotent fallback covers failures before that block is entered
            # (for example, a failed initial Session read).
            await _release_failed_start(lease, child_id)

    async def persist_abort() -> None:
        try:
            await request_abort(
                child_id,
                user_id,
                expected_run_id=lease.run_id,
                expected_generation=lease.generation,
            )
        except Exception:
            # The exact durable CAS could not be written. Stop only this
            # already-owned local lease, then let expiry recovery handle the
            # temporarily unavailable state store.
            lease.abort.set()
            log.exception("could not persist child abort %s", child_id)

    async def cancel_local_after_abort() -> None:
        await persist_abort()
        if not child.done():
            child.cancel()
            await asyncio.gather(child, return_exceptions=True)

    try:
        child = asyncio.create_task(
            drive(),
            name=f"subagent:{child_id}:{lease.generation}",
        )
    except BaseException:
        await _release_failed_start(lease, child_id)
        raise
    if ctx.abort is None:
        try:
            # Do not let a scheduler cancellation reach the local child until
            # the stop is also visible to another possible worker.
            await asyncio.shield(child)
        except asyncio.CancelledError:
            await cancel_local_after_abort()
            raise
        return

    watch = asyncio.create_task(ctx.abort.wait())
    try:
        done, _ = await asyncio.wait(
            {child, watch},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if child in done:
            await child
            return

        # Persist first: a child may be owned by another worker after a
        # handoff, and an in-memory Task.cancel() cannot reach that owner.
        await persist_abort()
        try:
            await asyncio.wait_for(asyncio.shield(child), timeout=10)
        except (asyncio.TimeoutError, TimeoutError):
            # Give the durable generation one final chance to report idle. If
            # the local coroutine is still wedged, cancellation is only a
            # process-local cleanup after the database stop was requested.
            await wait_for_idle(child_id, timeout=1.0)
            if not child.done():
                child.cancel()
                await asyncio.gather(child, return_exceptions=True)
    except asyncio.CancelledError:
        # Scheduler/process-local cancellation follows the same ordering as a
        # UI abort: durable request first, local cancellation second.
        await cancel_local_after_abort()
        raise
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


TASK_DESCRIPTION = """\
Create and control durable subagents. `spawn` (default) starts a fresh child conversation and context.
`fork` copies only the parent's last closed logical
turn prefix, excluding an open turn. Set lifecycle=`continuable` for later
`follow_up`; use `interrupt`, `report`, or `list` to control direct children.

Use `explore` for discovery/research and `general` for multi-step work, or a
configured spawnable preset. The child shares the parent's project sandbox/worktree,
so concurrent edits can conflict.

Give a self-contained prompt with objective, scope, paths, write permission,
constraints, checks, and expected result. Run only independent tasks concurrently;
run dependent or overlapping work sequentially. Model,
reasoning, persona, tools, and output_schema are accepted only when explicitly
supported and remain frozen across follow-ups. Interrupted provider/tool work
is outcome-unknown and is not replayed automatically."""

task_tool = define_tool(
    "task",
    description=TASK_DESCRIPTION,
    parameters=TaskArgs,
    execute=execute,
    sandbox_required=False,
)
