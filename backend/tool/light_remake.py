"""Apply the chosen production template in its owning Agent turn."""

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from core.log import create_logger
from light import commercial_plan, product_story_plan, production, remake_case, shortcut_plan, silk_road, wan_story
from light.mode import LightError
from tool.tool import ToolContext, ToolResult, define_tool


_PREPARED = {"zh": "已写好剧情分镜", "en": "Storyboard ready", "vi": "Đã viết xong kịch bản phân cảnh"}
_STOPPED = {"zh": "制作已暂停", "en": "Production paused", "vi": "Đã tạm dừng sản xuất"}
log = create_logger("light.guided_remake")


def _delivered(title, language, asset, duration, shots):
    summary = {
        "zh": f"{shots} 个镜头已剪成 {duration:.1f} 秒成片，视频已附在本对话。",
        "en": f"The finished {duration:.1f}-second video is attached here. It contains {shots} shots.",
        "vi": f"Video hoàn chỉnh dài {duration:.1f} giây gồm {shots} cảnh đã được đính kèm tại đây.",
    }[language]
    return ToolResult(title=title, output=f"{summary}\nasset_id={asset}, duration_sec={duration:.4f}.",
                      metadata={"asset_id": asset, "duration_sec": duration, "shots": shots})


async def execute(args: remake_case.Adaptation, ctx: ToolContext) -> ToolResult:
    from light.remake import RemakeStopped, render

    try:
        form, run, adaptation = await production.begin_remake(ctx, args)
        if run["action"] == "prepare":
            return ToolResult(
                title=_PREPARED[adaptation.language],
                output=form["script"],
                metadata={"revision": form["revision"], "rendered": False},
            )
        try:
            asset_id = await render(ctx, form, run, adaptation)
        except RemakeStopped as exc:
            reason = exc.for_language(adaptation.language)
            await production.remake_stage(ctx, run["job_prefix"], error=reason)
            return ToolResult(
                title=_STOPPED[adaptation.language], output=reason, metadata={"error": True, "do_not_retry": True}
            )
        return _delivered(remake_case.title(adaptation.language), adaptation.language, asset_id, 30, 7)
    except LightError as exc:
        return ToolResult(title="复刻制作已暂停", output=str(exc), metadata={"error": True})


light_remake_tool = define_tool(
    "light_remake",
    description=(
        "Apply the user's selected 30-second, seven-beat remake template. Supply only the real product name, "
        "its appearance and brief optional reveal, tasting and end-card copy from the user's reference. The current App "
        "action determines whether this saves a preview screenplay or runs native first-frame generation, "
        "RunningHub MiniMax H3 Turbo shots and IMS composition. Call once per confirmed action."
    ),
    parameters=remake_case.Adaptation,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
    body_timeout=lambda _args, _ctx: 45 * 60,
)

class SilkRoadPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: silk_road.Plan

    @model_validator(mode="after")
    def require_endpoints(self):
        world = self.plan.world
        if not world or any(not (take.first_frame and take.last_frame) for take in world.takes):
            raise ValueError("填写 world，并为每段提供独立的 first_frame 和 last_frame 静帧描述。")
        return self


async def execute_wan(args: wan_story.StoryRequest, ctx: ToolContext) -> ToolResult:
    from light.remake import RemakeStopped
    from light.wan_remake import render

    language = getattr(args.plan, "language", "zh")
    try:
        form, run, plan = await production.begin_remake(ctx, args.plan, requested_action=args.action)
        language = run.get("video_language", language)
        try:
            if form["template"] == product_story_plan.TEMPLATE and plan is None:
                if run["action"] == "render":
                    await production.remake_stage(ctx, run["job_prefix"], "copy")
                plan = await commercial_plan.adapt(ctx, form, run["video_language"])
                form, run, plan = await production.begin_remake(ctx, plan, requested_action=args.action)
            if run["action"] == "prepare":
                return ToolResult(title=_PREPARED[plan.language], output=form["script"], metadata={"rendered": False})
            if form["template"] == wan_story.SODA_TEMPLATE:
                from light.soda_story import render
            elif isinstance(plan, commercial_plan.Plan):
                from light.commercial_film import render
            elif isinstance(plan, (product_story_plan.BrandPlan, product_story_plan.Plan)):
                from light.product_story import render
            elif isinstance(plan, shortcut_plan.ShortcutPlan):
                from light.shortcut_story import render
            elif isinstance(plan, silk_road.Plan):
                from light.silk_road import render
            elif isinstance(plan, wan_story.ShotPlan):
                from light.reference_film import render
            asset, duration = await render(ctx, form, run, plan)
        except (RemakeStopped, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                if form["template"] != product_story_plan.TEMPLATE:
                    raise
                fields = [".".join(map(str, item["loc"])) for item in exc.errors(include_input=False)]
                log.warning("Commercial production validation session=%s fields=%s", ctx.session_id, fields)
                reason = {"zh": "生成内容未通过格式校验，制作记录已保存。", "en": "The generated content failed validation. Your production record was saved.",
                          "vi": "Nội dung vừa tạo chưa đúng định dạng. Tiến trình sản xuất đã được lưu."}[language]
            else:
                reason = exc.for_language(language)
            await production.remake_stage(ctx, run["job_prefix"], error=reason,
                                          feedback=getattr(exc, "review_feedback", None))
            return ToolResult(
                title=_STOPPED[language], output=reason, metadata={"error": True, "do_not_retry": True}
            )
        shot_count = (len(product_story_plan.shots(plan))
                      if isinstance(plan, (product_story_plan.BrandPlan, product_story_plan.Plan))
                      else wan_story.shot_count(run))
        return _delivered(plan.title, plan.language, asset, duration, shot_count)
    except LightError as exc:
        return ToolResult(title=_STOPPED[language], output=str(exc), metadata={"error": True})


light_wan_story_tool = define_tool(
    "light_wan_story",
    description="The single tool for the App's selected Wan remake. With action=prepare, supply the storyboard in plan to save the preview. With action=render after confirmation, omit plan to film the saved storyboard, review actual frames and dialogue, select cuts and attach the full composition. The action must match the owner's App button.",
    parameters=wan_story.StoryRequest,
    execute=execute_wan,
    sandbox_required=False,
    parallel_safe=False,
    body_timeout=lambda _args, _ctx: 55 * 60,
)


@lru_cache(maxsize=10)
def wan_tool_for_action(action: str, template: str | None = None):
    """Expose only the operation selected by the owner's App button."""
    if action not in {"prepare", "render"}:
        raise ValueError("Unknown guided production action")

    async def execute_owned(args, ctx):
        result = await execute_wan(wan_story.StoryRequest(action=action, plan=getattr(args, "plan", None)), ctx)
        # This receipt comes only from the tool bound to the owner's App action,
        # not model arguments. Validation failures never reach this executor.
        result.metadata["guided_action_finished"] = action
        return result

    parameters = wan_story.RenderRequest
    if action == "prepare":
        parameters = {
            silk_road.TEMPLATE: SilkRoadPrepareRequest,
            shortcut_plan.TEMPLATE: shortcut_plan.PrepareRequest,
            product_story_plan.TEMPLATE: wan_story.ProductPreviewRequest,
        }.get(template, wan_story.PrepareRequest)
    if template == product_story_plan.TEMPLATE:
        description = (f"The owner selected {action} for the commercial reference. Call once with action={action}. "
                       "The executor reads the uploaded materials and adapts the reference storyboard. "
                       "Prepare saves a preview. Render autonomously films, reviews, mixes and attaches the finished cut.")
    elif action == "prepare":
        description = "The App has requested a new preview screenplay. Supply the complete plan; this does not generate paid media."
    else:
        description = ("The owner pressed Generate or Retry in the App. Call once with action=render to film "
                       "the saved plan, reconcile jobs, review actors and dialogue, and attach the full composition.")
    # The 28-shot commercial includes provider queues, endpoint checks and bounded automatic retakes.
    minutes = 90 if template == product_story_plan.TEMPLATE and action == "render" else 55
    return define_tool(
        "light_wan_story",
        description=description,
        parameters=parameters,
        execute=execute_owned,
        sandbox_required=False,
        parallel_safe=False,
        body_timeout=lambda _args, _ctx: minutes * 60,
    )
