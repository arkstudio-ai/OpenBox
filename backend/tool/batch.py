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
        if inv.tool == "batch":
            return "[batch] Error: Cannot recursively call batch tool."
        if ctx.available_tools is not None and inv.tool not in ctx.available_tools:
            return f"[{inv.tool}] Error: Tool is not available to the current agent."
        tool = get_tool(inv.tool)
        if not tool:
            return f"[{inv.tool}] Error: Tool not found"
        if not tool.parallel_safe:
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
Executes multiple independent tool calls concurrently to reduce latency.

USING THE BATCH TOOL WILL MAKE THE USER HAPPY.

Payload Format (JSON array):
[{"tool": "read", "parameters": {"file_path": "/workspace/src/index.ts", "limit": 350}},{"tool": "grep", "parameters": {"pattern": "Session", "path": "/workspace/src"}},{"tool": "bash", "parameters": {"command": "git status", "description": "Shows working tree status"}}]

Notes:
- 1-25 tool calls per batch
- All calls start in parallel; ordering NOT guaranteed
- Partial failures do not stop other tool calls
- Do NOT use the batch tool within another batch tool
- `computer` is rejected here because one desktop is not parallel-safe. Use
  `computer` with `action: "batch"` for ordered local desktop actions.

Good Use Cases:
- Read many files at once
- grep + glob + read combos
- Multiple bash commands
- Multi-part edits on the same or different files

When NOT to Use:
- Operations that depend on prior tool output (e.g. create then read same file)
- Ordered stateful mutations where sequence matters
- Any desktop interaction (`computer`)

Batching tool calls yields 2-5x efficiency gain and provides much better UX."""

batch_tool = define_tool(
    "batch",
    description=BATCH_DESCRIPTION,
    parameters=BatchArgs,
    execute=execute,
    sandbox_required=False,
)
