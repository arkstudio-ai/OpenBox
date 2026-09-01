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

    # Production ToolHooks install the canonical staged nested runtime. It
    # persists one ToolPart/Event per invocation, prepares permission in model
    # order, overlaps only reviewed bodies, and commits in the same order.
    runtime = ctx._nested_tool_runtime
    if runtime is not None:
        results = await runtime.execute_batch([
            (inv.tool, inv.parameters) for inv in args.invocations
        ])
        return ToolResult(
            title=f"Batch: {len(args.invocations)} tools executed",
            output="\n\n---\n\n".join(
                f"[{inv.tool}] {result.title}\n{result.output}"
                for inv, result in zip(args.invocations, results, strict=True)
            ),
        )

    from tool.registry import get_tool

    async def run_one(inv: Invocation) -> str:
        if inv.tool == "batch":
            return "[batch] Error: Cannot recursively call batch tool."
        if ctx.available_tools is not None and inv.tool not in ctx.available_tools:
            return f"[{inv.tool}] Error: Tool is not available to the current agent."
        tool = get_tool(inv.tool)
        if not tool:
            return f"[{inv.tool}] Error: Tool not found"
        if tool.parallel_safe is not True:
            guidance = (
                " Use computer(action='batch', actions=[...]) for ordered desktop actions."
                if inv.tool == "computer" else ""
            )
            return f"[{inv.tool}] Error: Tool is not safe for parallel execution.{guidance}"
        if ctx._authorize_tool is not None:
            blocked = await ctx._authorize_tool(inv.tool, inv.parameters)
            if blocked is not None:
                return f"[{inv.tool}] {blocked.title}\n{blocked.output}"
        try:
            result = await tool.execute(inv.parameters, ctx)
            return f"[{inv.tool}] {result.title}\n{result.output}"
        except Exception as e:
            return f"[{inv.tool}] Error: {e}"

    tasks = [run_one(inv) for inv in args.invocations]
    results = await asyncio.gather(*tasks, return_exceptions=True)

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
