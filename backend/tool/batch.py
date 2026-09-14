"""Batch tool: parallel tool execution."""
import asyncio
from pydantic import BaseModel, Field

from tool.tool import ToolResult, ToolContext, define_tool


class Invocation(BaseModel):
    tool: str
    parameters: dict = {}


class BatchArgs(BaseModel):
    invocations: list[Invocation] = Field(description="List of tool calls to execute in parallel (max 25)")


async def execute(args: BatchArgs, ctx: ToolContext) -> ToolResult:
    """Execute multiple tool calls in parallel."""
    if len(args.invocations) > 25:
        return ToolResult(title="Error", output="Maximum 25 parallel invocations allowed.")

    from tool.registry import get_tool

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
            elif not tool.parallel_safe:
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
                from trajectory.types import TrajectoryError
                if isinstance(e, TrajectoryError):
                    raise
                return f"[{inv.tool}] Error: {e}"

    tasks = [run_one(inv) for inv in args.invocations]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    from trajectory.types import TrajectoryError
    for result in results:
        if isinstance(result, TrajectoryError):
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
Run 1-25 independent tool calls concurrently. Ordering is not guaranteed, and
one failure does not stop the other calls.

Do not nest `batch`, include dependent operations, or parallelize ordered or
overlapping state mutations. `computer` is rejected because the desktop is
stateful and not parallel-safe; use `computer(action='batch', ...)` for ordered
desktop actions that need no intermediate screenshot."""

batch_tool = define_tool(
    "batch",
    description=BATCH_DESCRIPTION,
    parameters=BatchArgs,
    execute=execute,
    sandbox_required=False,
)
