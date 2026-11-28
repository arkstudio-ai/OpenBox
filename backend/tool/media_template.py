"""Save a reusable recipe from a finished work in the current conversation."""

from light.media_templates import Draft, save
from light.mode import LightError
from tool.tool import ToolContext, ToolResult, define_tool


async def execute(args: Draft, ctx: ToolContext) -> ToolResult:
    if not all((ctx.user_id, ctx.workspace_id, ctx.session_id)):
        return ToolResult(title="无法保存模板", output="需要已登录的专业版会话。")
    try:
        draft = await save(ctx.user_id, ctx.workspace_id, ctx.session_id, args)
    except LightError as exc:
        return ToolResult(title="模板未保存", output=str(exc), metadata={"blocked": True, "code": exc.code})
    url = f"/app/skills?section=media&tab=mine&template={draft['id']}"
    return ToolResult(title="模板草稿已保存",
                      output=f"已保存《{draft['title']}》v{draft['version']}。"
                             f"请打开[我的模板]({url})在精简版试做、确认成品并提交。"
                             f"修订此模板时将 revises_edition_id 设为 {draft['id']}。",
                      metadata={"edition_id": draft["id"], "version": draft["version"], "url": url})


media_template_tool = define_tool(
    "media_template",
    description=(
        "Save a private reusable media template from a ready final work the owner explicitly asks to turn into a template. "
        "Supports video, footage_edit, menu and poster. Read the media-template-author skill first. Use the real "
        "source asset attached to this professional conversation. For a revision pass revises_edition_id. "
        "This tool cannot generate a trial, submit a review or publish. Give the owner the returned link."
    ),
    parameters=Draft, execute=execute, sandbox_required=False, parallel_safe=False,
)
