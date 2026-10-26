"""Batch tool: parallel tool execution."""
import asyncio
from pydantic import BaseModel, Field

from tool.tool import ToolResult, ToolContext, define_tool


class Invocation(BaseModel):
    tool: str
    parameters: dict = {}


class BatchArgs(BaseModel):
    invocations: list[Invocation] = Field(description="List of tool calls to execute in parallel (max 25)")


def image_batch_limit() -> int:
    import os
    value = int(os.environ.get("IMAGE_BATCH_MAX_CALLS", "5"))
    if not 1 <= value <= 25:
        raise ValueError("IMAGE_BATCH_MAX_CALLS must be between 1 and 25")
    return value


def image_call_count(tool_id: str, args: dict, lookup) -> int:
    info = lookup.get(tool_id) if lookup is not None else None
    canonical = getattr(info, "id", tool_id)
    if canonical == "image_gen":
        return 1
    if canonical == "batch":
        invocations = args.get("invocations") or []
        if isinstance(invocations, list):
            # Recursion is rejected by the dispatcher, never traversed here.
            return sum((getattr(lookup.get(i.get("tool")), "id", i.get("tool"))
                        if lookup is not None else i.get("tool")) == "image_gen"
                       for i in invocations if isinstance(i, dict))
    return 0


def image_batch_rejection(count: int) -> ToolResult | None:
    limit = image_batch_limit()
    if count <= limit:
        return None
    return ToolResult(title="Image batch limit exceeded",
        output=f"本批包含 {count} 次图片生成或修改调用，单批最多允许 {limit} 次。整批未执行、未提交付费请求；请减少数量后重新提交。",
        metadata={"error": True, "blocked": True, "failure_code": "image_batch_limit_exceeded",
                  "max_batch_calls": limit, "requested_calls": count})


async def execute(args: BatchArgs, ctx: ToolContext) -> ToolResult:
    """Execute multiple tool calls in parallel."""
    if len(args.invocations) > 25:
        return ToolResult(title="Error", output="Maximum 25 invocations allowed.", metadata={"error": True, "blocked": True})
    rejected = image_batch_rejection(image_call_count("batch", args.model_dump(), ctx._tool_execution_lookup))
    if rejected is not None:
        return rejected

    if ctx._nested_tool_runtime is not None:
        results = await ctx._nested_tool_runtime.execute_batch([
            (inv.tool, inv.parameters) for inv in args.invocations
        ])
        return ToolResult(
            title=f"Batch: {len(args.invocations)} tools executed",
            output="\n\n---\n\n".join(
                f"[{inv.tool}] {result.title}\n{result.output}"
                for inv, result in zip(args.invocations, results)
            ),
            metadata={"error": any(result.metadata.get("error") or result.metadata.get("blocked")
                for result in results),
                "completed_count": sum(not (r.metadata.get("error") or r.metadata.get("blocked")) for r in results),
                "failed_count": sum(bool(r.metadata.get("error") or r.metadata.get("blocked")) for r in results),
                "remaining_count": 0},
        )
    if ctx.run_fence is not None:
        return ToolResult(title="Batch unavailable",
            output="The current run has no durable nested tool dispatcher.",
            metadata={"error": True, "blocked": True})

    from tool.registry import get_tool as registry_tool
    from memory.transient_tools import memory_operation

    def get_tool(tool_id):
        lookup = ctx._tool_execution_lookup
        return lookup.get(tool_id) if lookup is not None else registry_tool(tool_id)

    async def run_one(inv: Invocation) -> str:
        import copy
        from agent.hooks import ToolHooks
        from agent.trajectory import context_for_tool, public_value, tool_schema
        from core.identifier import ascending
        from trajectory import bind, record
        child = copy.copy(ctx)
        child._capability_revealed_ids = set(ctx._capability_revealed_ids)
        child.part_id = ascending("call")
        child._on_output = None
        child._authorized_tool_id = ""
        child._authorized_tool_args_key = ""
        trace = await context_for_tool(ctx)
        if trace is not None:
            trace = trace.derive(parent_call_id=trace.call_id, call_id=child.part_id, part_id=None)
        child.trace_context = trace
        tool = get_tool(inv.tool)
        with bind(trace):
            await record("tool.requested", {
                "tool": inv.tool, "requested_arguments": public_value(inv.parameters),
                "schema": tool_schema(tool, inv.tool),
                "schema_source": "executor_registry",
            }, context=trace)
            rejection = None
            if inv.tool == "batch":
                rejection = "Cannot recursively call batch tool."
            elif ctx.available_tools is not None and inv.tool not in ctx.available_tools:
                rejection = "Tool is not available to the current agent."
            elif not tool:
                rejection = "Tool not found"
            elif memory_operation(inv.tool, {"input": inv.parameters}):
                rejection = "Call memory and task reads directly so their temporary results retain authorization and version checks."
            elif tool.parallel_safe is False and not getattr(tool, "batch_serial_safe", False):
                rejection = "Tool is not safe for parallel execution."
                if inv.tool == "computer":
                    rejection += " Use computer(action='batch', actions=[...]) for ordered desktop actions."
            if rejection:
                await record("tool.finished", {"tool": inv.tool, "status": "denied", "reason": rejection}, context=trace)
                return f"[{inv.tool}] Error: {rejection}"
            hooks = getattr(ctx._authorize_tool, "__self__", None)
            if not isinstance(hooks, ToolHooks):
                hooks = ToolHooks(session_id=ctx.session_id, user_id=ctx.user_id)
                if ctx._authorize_tool is not None:
                    hooks.authorize_tool = ctx._authorize_tool
            try:
                result = await hooks.wrap_execute(inv.tool, tool.execute, inv.parameters, child,
                    part_id=child.part_id, tool_info=tool, requested_recorded=True)
                return f"[{inv.tool}] {result.title}\n{result.output}"
            except asyncio.CancelledError:
                raise
            except Exception as e:
                from agent.driver import LeaseLostError
                from question.runtime import RunRevoked
                from assistant.scheduling import TaskSchedulingHeld
                from assistant.budget import AssistantBudgetExceeded
                if isinstance(e, (RunRevoked, LeaseLostError, TaskSchedulingHeld, AssistantBudgetExceeded)):
                    raise
                return f"[{inv.tool}] Error: {e}"

    # Old tool definitions did not declare parallel safety. Keep them usable
    # in Batch, while overlapping only tools that explicitly opt in.
    async def run_parallel(group):
        tasks = [asyncio.create_task(run_one(inv)) for inv in group]
        try:
            return await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

    results = []
    pending = []
    for inv in args.invocations:
        tool = get_tool(inv.tool)
        if tool is not None and tool.parallel_safe is True:
            pending.append(inv)
            continue
        if pending:
            results.extend(await run_parallel(pending))
            pending.clear()
        results.append(await run_one(inv))
    if pending:
        results.extend(await run_parallel(pending))

    from question.runtime import RunRevoked
    for result in results:
        if isinstance(result, RunRevoked):
            raise result
    output_parts = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            output_parts.append(f"[{args.invocations[i].tool}] Error: {result}")
        else:
            output_parts.append(str(result))

    return ToolResult(
        title=f"Batch: {len(args.invocations)} tools executed",
        output="\n\n---\n\n".join(output_parts),
    )


BATCH_DESCRIPTION = """\
Run 1-25 independent tool calls concurrently. Ordering is not guaranteed;
failures are independent. Unmarked tools run serially.
Image generation/editing runs serially, saves each result, and shares a limit
of 5 calls by default (1-4 images each); excess rejects the whole batch before dispatch.
Call memory/task reads directly. No nesting, dependent calls or overlapping mutations.
`computer` is rejected; use computer(action='batch', ...) for ordered desktop actions."""

def _batch_body_timeout(args: dict, ctx: ToolContext) -> float:
    from tool.tool import tool_body_timeout
    lookup = ctx._tool_execution_lookup
    invocations = args.get("invocations") or []
    if not isinstance(invocations, list) or len(invocations) > 25:
        return 600.0
    # An outer composite must not cancel a later image merely because its
    # earlier serial siblings consumed the ordinary single-tool deadline.
    return 60.0 + sum(tool_body_timeout(
        lookup.get(inv.get("tool")) if lookup is not None and isinstance(inv, dict) else None,
        inv.get("parameters") or {} if isinstance(inv, dict) else {}, ctx,
    ) for inv in invocations)


batch_tool = define_tool(
    "batch",
    description=BATCH_DESCRIPTION,
    parameters=BatchArgs,
    execute=execute,
    sandbox_required=False,
    body_timeout=_batch_body_timeout,
)
