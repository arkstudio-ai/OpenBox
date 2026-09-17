"""Glob tool: file name pattern search in the sandbox."""
from pydantic import BaseModel, Field

from tool.sensitive_paths import explicitly_requests_sensitive
from tool.tool import ToolResult, ToolContext, define_tool


class GlobArgs(BaseModel):
    pattern: str = Field(description="Glob pattern to match files (e.g. '**/*.py')")
    path: str = Field(default="/workspace", description="Directory to search in")


async def execute(args: GlobArgs, ctx: ToolContext) -> ToolResult:
    """Find files matching a glob pattern."""
    try:
        search_path = ctx.resolve_file_path(args.path, allow_user_scope=True)
    except ValueError as exc:
        return ToolResult(title=f"glob: {args.pattern}", output=str(exc))
    files = await ctx.sandbox.glob(
        pattern=args.pattern,
        path=search_path,
        include_sensitive=explicitly_requests_sensitive(search_path, args.pattern),
    )

    if not files:
        return ToolResult(
            title=f"No matches for {args.pattern}",
            output="No files found matching the pattern.",
        )

    return ToolResult(
        title=f"Found {len(files)} files",
        output="\n".join(files),
    )


GLOB_DESCRIPTION = """\
Fast file pattern matching tool that works with any codebase size.

- Supports glob patterns like "**/*.js" or "src/**/*.ts"
- Returns matching file paths sorted by modification time
- Use this tool when you need to find files by name patterns
- When you are doing an open-ended search that may require multiple rounds of globbing and grepping, use the Task tool instead
- You can call multiple tools in a single response. It is always better to speculatively perform multiple searches as a batch that are potentially useful."""

glob_tool = define_tool(
    "glob",
    description=GLOB_DESCRIPTION,
    parameters=GlobArgs,
    execute=execute,
    parallel_safe=True,
)
