"""share_file: hand a workspace file back to the user as a chat attachment.

`view_image` exists so the MODEL can see pixels; this tool exists so the
USER can receive a file. Same transport (sandbox → OSS via `obx-file put`,
no tunnel bytes — see sandbox/assets.attach_sandbox_image), same file part
on the assistant message: the chat renders images and videos as playable
previews and anything else as a file chip with a download URL.
"""
import shlex

from pydantic import BaseModel, Field

from core.log import create_logger
from tool.tool import ToolResult, ToolContext, define_tool

log = create_logger("tool.share_file")

#: Matches the assets API ceiling — bytes go sandbox→OSS directly, so the
#: backend never buffers them.
_MAX_BYTES = 512 * 1024 * 1024


class ShareFileArgs(BaseModel):
    file_path: str = Field(description="Absolute path to the file in the workspace to send to the user")


async def execute(args: ShareFileArgs, ctx: ToolContext) -> ToolResult:
    from core.oss import OssNotConfigured, get_oss
    from sandbox.assets import attach_sandbox_image

    try:
        path = ctx.resolve_file_path(args.file_path, allow_user_scope=True)
    except ValueError as exc:
        return ToolResult(title=f"Cannot read {args.file_path}", output=str(exc))

    try:
        get_oss()
    except OssNotConfigured as e:
        return ToolResult(title="share_file unavailable", output=f"OSS transfer is not configured: {e}")

    probe = await ctx.sandbox.execute(
        f"stat -c %s {shlex.quote(path)} && file --brief --mime-type {shlex.quote(path)}",
        timeout=15,
        workdir=ctx.workdir,
    )
    if probe.exit_code != 0:
        return ToolResult(
            title=f"Cannot read {args.file_path}",
            output=probe.stderr.strip() or "No such file",
        )
    lines = probe.stdout.strip().splitlines()
    size = int(lines[0]) if lines and lines[0].isdigit() else 0
    mime = (lines[1].strip() if len(lines) > 1 else "") or "application/octet-stream"
    if size > _MAX_BYTES:
        return ToolResult(
            title=f"File too large: {args.file_path}",
            output=f"{size} bytes (max {_MAX_BYTES})",
        )

    name = path.split("/")[-1]
    try:
        asset_id, verified = await attach_sandbox_image(
            ctx,
            path,
            mime,
            size,
            name=name,
            relation_kind="shared_file",
            relation_role="result",
            relation_label=name,
        )
    except Exception as e:
        return ToolResult(title=f"Upload failed: {args.file_path}", output=str(e)[:300])

    return ToolResult(
        title=name,
        output=f"File attached to this reply for the user: {args.file_path} ({mime}, {verified} bytes).",
        metadata={"asset_id": asset_id, "mime": mime, "path": path, "size": verified},
    )


SHARE_FILE_DESCRIPTION = """\
Send a file from the workspace back to the user as an attachment on your \
reply. The chat shows images and videos as playable previews and other \
files as a downloadable chip. Use when the user asks you to return, send, \
or share a file (report, screenshot, video, archive...). Any file type, \
up to 512 MB. This does NOT let you see the file's content — use \
view_image for that."""

share_file_tool = define_tool(
    "share_file",
    description=SHARE_FILE_DESCRIPTION,
    parameters=ShareFileArgs,
    execute=execute,
)
