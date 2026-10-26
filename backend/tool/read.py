"""Read tool: read files from the sandbox."""
import shlex
from pathlib import PurePosixPath

from pydantic import BaseModel, Field

from core.log import create_logger
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.read")

_VIEWABLE_IMAGES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_BINARY_EXTENSIONS = _VIEWABLE_IMAGES | {
    ".pdf", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".ico",
    ".mp4", ".mov", ".webm", ".mp3", ".wav", ".m4a", ".ogg",
    ".zip", ".7z", ".gz", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}


def _binary_result(path, *, image=False):
    guidance = ("Use view_image with this file_path to inspect the actual pixels. "
                "If it is not available, discover view_image first."
                if image else "Use a format-appropriate reader or extract text to a new file first.")
    return ToolResult(title=f"Text reader cannot read {path}",
        output=f"This is not a plain-text file. No file content or attachment was returned. {guidance}",
        metadata={"error": True, "failure_code": "READ_REQUIRES_BINARY_TOOL", "validation_failed": True,
                  **({"suggested_tool": "view_image"} if image else {})})


def _binary_content(content):
    # Old Action Servers decode arbitrary bytes with errors='replace'. Keep
    # their output out of model context even when the extension is misleading.
    first = content.partition("\n")[0]
    if "\t" in first and first.partition("\t")[0].strip().isdigit():
        first = first.partition("\t")[2]
    return ("\x00" in content or first.startswith(("\x89PNG", "\ufffdPNG", "%PDF-", "PK\x03\x04"))
            or first.startswith("\ufffd\ufffd") and ("JFIF" in first or "Exif" in first))


def project_text_read_part(part):
    """Exclude old binary text results from model replay; retain the audit row."""
    if (not isinstance(part, dict) or part.get("type") != "tool"
            or (part.get("canonical_tool_id") or part.get("tool")) != "read"):
        return part
    state = part.get("state") or {}
    outputs = [part.get("output"), state.get("output") if isinstance(state, dict) else None]
    if not any(isinstance(value, str) and _binary_content(value) for value in outputs):
        return part
    from copy import deepcopy
    projected = deepcopy(part)
    notice = ("[Binary bytes from an earlier text read omitted from model context; "
              "the original audit record is retained. Use view_image for image pixels, "
              "or an appropriate reader for other binary files.]")
    projected["output"] = notice
    if isinstance(projected.get("state"), dict) and "output" in projected["state"]:
        projected["state"]["output"] = notice
    return projected


class ReadArgs(BaseModel):
    file_path: str = Field(description="Absolute path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from")
    limit: int = Field(default=2000, description="Maximum number of lines to read")


async def execute(args: ReadArgs, ctx: ToolContext) -> ToolResult:
    """Read a file from the sandbox with line numbers."""
    try:
        execution_path = ctx.resolve_file_path(
            args.file_path,
            allow_user_scope=True,
        )
    except ValueError as exc:
        return ToolResult(
            title=f"Error reading {args.file_path}",
            output=str(exc),
        )

    suffix = PurePosixPath(execution_path).suffix.lower()
    if suffix in _BINARY_EXTENSIONS:
        return _binary_result(args.file_path, image=suffix in _VIEWABLE_IMAGES)

    try:
        content = await ctx.sandbox.read_file(
            path=execution_path,
            offset=args.offset,
            limit=args.limit,
        )
        if _binary_content(content):
            return _binary_result(args.file_path)

        # F1: Inject directory-level instruction files (AGENTS.md / CLAUDE.md)
        try:
            from session.instruction import instruction_resolve
            instructions = await instruction_resolve(
                execution_path,
                ctx.message_id,
            )
            if instructions:
                extra = "\n\n".join(
                    f"[Directory instructions from: {inst['filepath']}]\n{inst['content']}"
                    for inst in instructions
                )
                content = content + "\n\n" + extra
        except Exception as e:
            log.debug(f"Instruction resolve skipped: {e}")

        # F9: Record file mtime for stale edit detection
        try:
            from tool.filetime import get_tracker
            stat_result = await ctx.sandbox.execute(
                f"stat -c %Y -- {shlex.quote(execution_path)} 2>/dev/null",
                timeout=5,
                workdir=ctx.workdir,
            )
            if stat_result.exit_code == 0 and stat_result.stdout.strip():
                mtime = float(stat_result.stdout.strip())
                get_tracker(ctx.session_id).record(execution_path, mtime)
        except Exception:
            pass  # Best-effort

        return ToolResult(
            title=args.file_path,
            output=content,
        )
    except Exception as e:
        return ToolResult(
            title=f"Error reading {args.file_path}",
            output=str(e),
        )


READ_DESCRIPTION = """\
Read a plain-text file from the sandbox filesystem. If the path does not exist, an error is returned.

Usage:
- The file_path parameter should be an absolute path.
- By default, this tool returns up to 2000 lines from the start of the file.
- The offset parameter is the line number to start from (0-indexed).
- To read later sections, call this tool again with a larger offset.
- Use the Grep tool to find specific content in large files or files with long lines.
- If you are unsure of the correct file path, use the Glob tool to look up filenames by glob pattern.
- Contents are returned with each line prefixed by its line number.
- Any line longer than 2000 characters is truncated.
- Call this tool in parallel when you know there are multiple files you want to read.
- Avoid tiny repeated slices (30 line chunks). If you need more context, read a larger window.
- This tool does not read images, PDFs, office files, audio or video and never attaches files.
- For images use view_image (discover it first if needed). For other binary formats use an appropriate reader or extract text to a new file first."""

read_tool = define_tool(
    "read",
    description=READ_DESCRIPTION,
    parameters=ReadArgs,
    execute=execute,
    parallel_safe=True,
)
