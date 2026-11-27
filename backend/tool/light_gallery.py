"""Execute a user-confirmed gallery remake from a saved production form."""

import asyncio
import json
from dataclasses import replace
from functools import partial
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.file_asset import FileAsset
from light.mode import LightError
from tool import image_gen, video_comfyui, video_compose, video_production, video_template
from tool.tool import ToolContext, ToolResult, define_tool
from tool.video_providers import VideoRequestError

TOOL_ID = "light_gallery"
PRODUCT = "form_product"
POSTERS = {"offer_poster", "brand_poster"}
STILLS = {"open": "gallery-open.jpg", "genie": "gallery-genie.jpg"}
FILM_WAIT = 65 * 60
_PROGRESS_COPY = {
    "zh": {
        "storyboard": "核对分镜与素材角色", "materials": "准备参考素材",
        "poster": "第 {number} 段宣传原画", "shot": "第 {number} 段",
        "compose": "阿里云 IMS 剪辑",
        "preparing": "处理中", "validating": "校验参数", "submitting": "提交中", "waiting": "等待结果",
        "resuming": "查询已有作业", "done": "已完成", "paused": "已暂停",
    },
    "en": {
        "storyboard": "Check storyboard and reference roles", "materials": "Prepare reference assets",
        "poster": "Shot {number} promotional artwork", "shot": "Shot {number}",
        "compose": "Aliyun IMS edit",
        "preparing": "In progress", "validating": "Validating", "submitting": "Submitting", "waiting": "Waiting for result",
        "resuming": "Checking existing job", "done": "Complete", "paused": "Paused",
    },
    "vi": {
        "storyboard": "Kiểm tra phân cảnh và vai trò tư liệu", "materials": "Chuẩn bị tư liệu tham chiếu",
        "poster": "Ảnh quảng cáo cảnh {number}", "shot": "Cảnh {number}",
        "compose": "Dựng phim trên Aliyun IMS",
        "preparing": "Đang xử lý", "validating": "Đang kiểm tra", "submitting": "Đang gửi", "waiting": "Đang chờ kết quả",
        "resuming": "Đang kiểm tra tác vụ đã gửi", "done": "Hoàn tất", "paused": "Đã tạm dừng",
    },
}


class _GalleryProgress:
    def __init__(self, ctx: ToolContext, language: str):
        self.ctx = ctx
        self.copy = _PROGRESS_COPY[language]
        self.lines: dict[int, str] = {}
        self.lock = asyncio.Lock()

    def render(self) -> str:
        return "\n".join(self.lines[index] for index in sorted(self.lines))

    async def update(self, index: int, label: str, state: str) -> None:
        async with self.lock:
            self.lines[index] = f"{label} · {self.copy[state]}"
            await self.ctx.update_output(self.render())


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration: int = Field(ge=4, le=15)
    reference: Literal[
        "motion", "motion_with_product", "gallery_first_frame", "gallery_first_and_last",
        "gallery_and_product", "product_first_frame", "offer_poster", "brand_poster",
    ]
    still: Literal["open", "genie"] | None = Field(
        default=None, description="Select a gallery still when using gallery_first_frame or gallery_and_product.",
    )
    prompt: str = Field(min_length=20, max_length=6000)
    poster_direction: str = Field(default="", max_length=800,
                                  description="Visual design and layout for a poster. Approved wording comes from the form.")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shots: Annotated[list[Shot], Field(min_length=5, max_length=5)]


def sources(shot: Shot, beat: dict) -> list[tuple[str, str]]:
    if shot.reference in {"motion", "motion_with_product"}:
        if not beat.get("reference_video"):
            raise ValueError("这一段没有可用的原片视频参考。")
        result = [("reference_video", beat["reference_video"])]
        if shot.reference == "motion_with_product":
            result.append(("reference_image", PRODUCT))
        return result
    if shot.reference == "product_first_frame" or shot.reference in POSTERS:
        return [("first_frame", PRODUCT)]
    if shot.reference == "gallery_first_and_last":
        if not all(still in beat["still_options"] for still in STILLS.values()):
            raise ValueError("这一段没有配对的画廊首尾帧。")
        return [("first_frame", STILLS["open"]), ("last_frame", STILLS["genie"])]
    still = STILLS.get(shot.still)
    if still not in beat["still_options"]:
        raise ValueError("请选择本段可用的画廊参考图。")
    if shot.reference == "gallery_and_product":
        return [("reference_image", still), ("reference_image", PRODUCT)]
    return [("first_frame", still)]


def source_plan(plan: Plan, form: dict) -> list[list[tuple[str, str]]]:
    refs = []
    for shot, beat in zip(plan.shots, video_template.recipe()["shots"], strict=True):
        try:
            video_comfyui.dialogue(shot.prompt)
            video_template.verify_third_wish_line(form, beat["shot"], shot.prompt)
            refs.append(sources(shot, beat))
        except (ValueError, VideoRequestError) as exc:
            raise LightError("LIGHT_GALLERY_PLAN", f"第 {beat['shot']} 段：{exc}", 422) from exc
    if not any(name == PRODUCT for refs_for_shot in refs for _, name in refs_for_shot):
        raise LightError("LIGHT_GALLERY_PLAN", "分镜需要使用本次选中的商品照片。", 422)
    return refs


def poster_prompt(shot: Shot, form: dict) -> str:
    promotion = form.get("promotion_brief") or {}
    if shot.reference == "offer_poster":
        fields = (form["product_name"], promotion.get("offer") or promotion.get("call_to_action"))
    else:
        fields = (form["shop"], form["product_name"], promotion.get("offer"), promotion.get("call_to_action"))
    text = json.dumps([value for value in fields if value], ensure_ascii=False)
    return (
        "设计9:16商品广告原画，作为后续图生视频的完整画面。参照上传商品图保留真实外观、材质、包装和颜色。"
        f"设计方向（只用来设计画面）：{shot.poster_direction or '正面视角，商品主体清楚，广告文案层级清晰。'}\n"
        f"只排印以下已确认的广告文字，逐字匹配，保持顺序，不添加新文案：{text}。"
        "用清晰正体和高对比的稳定平面排版。文字和商品留在竖屏安全区，保证左右内边距。"
        "把文字和版式画进原画的标牌、海报或图形区中。动态空间留给远离文字的商品、灯光或背景。"
    )


async def prepare_poster(ctx: ToolContext, prefix: str, index: int, shot: Shot, form: dict) -> str:
    from light.remake import RemakeStopped, _check, _digest

    key = _digest(f"{prefix}:poster:{index}")
    filename = f"gallery-poster-{key}.png"
    async with get_db_session() as db:
        existing = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(
            FileAsset.session_id == ctx.session_id, FileAsset.name == filename,
        ).order_by(FileAsset.created_at.desc()).limit(1))
    if existing:
        return existing.id
    await _check(ctx)
    image_ctx = replace(ctx, part_id=f"gallery-poster-{key}", sandbox=None, _on_output=None, transient_output=True)
    result = await image_gen.execute(image_gen.ImageGenArgs(
        prompt=poster_prompt(shot, form), input_images=[form["asset_ids"][0]],
        filename=filename, size="864x1536", output_format="png", quality="high",
    ), image_ctx)
    if not (asset := result.metadata.get("asset_id")):
        raise RemakeStopped(f"第 {index} 段的宣传原画尚未就绪，制作进度已保存。")
    return asset


def timeline_for(plan: Plan, assets: list[str], resolution: str) -> dict:
    shots = []
    for shot, asset in zip(plan.shots, assets, strict=True):
        frames = video_comfyui._canvas({"duration": shot.duration, "ratio": "9:16", "resolution": resolution})["length"]
        shots.append({"asset": asset, "duration_sec": round(frames / 24, 4), "fit": "cover", "volume": 1})
    return {"canvas": {"width": 720, "height": 1280, "fps": 25}, "shots": shots}


async def execute(args: Plan, ctx: ToolContext) -> ToolResult:
    from core.oss import get_oss
    from light import production, remake
    from tool.light_remake import _delivered

    language = "zh"
    progress = None
    plan_rejected = False
    try:
        form, run, plan = await production.begin_gallery(ctx, args)
        language = run["video_language"]
        prefix = run["job_prefix"]
        progress = _GalleryProgress(ctx, language)
        copy = progress.copy
        refs = source_plan(plan, form)
        await progress.update(0, copy["storyboard"], "done")
        await progress.update(1, copy["materials"], "preparing")
        names = sorted({name for refs_for_shot in refs for _, name in refs_for_shot if name != PRODUCT})
        oss = get_oss()
        registered = await asyncio.gather(*(video_template._asset(name, ctx, ctx.project_id, oss=oss) for name in names))
        ids = dict(zip(names, registered, strict=True))
        ids[PRODUCT] = form["asset_ids"][0]
        await progress.update(1, copy["materials"], "done")
        job_ctx = replace(ctx, _on_output=None)

        async def film(item):
            number, (shot, shot_refs) = item
            inputs = [video_production.VideoInputRef(asset_id=ids[name], role=role) for role, name in shot_refs]
            if shot.reference in POSTERS:
                report_poster = partial(progress.update, 2 * number, copy["poster"].format(number=number))
                await report_poster("preparing")
                try:
                    poster = await prepare_poster(ctx, prefix, number, shot, form)
                except (remake.RemakeStopped, LightError):
                    await report_poster("paused")
                    raise
                await report_poster("done")
                inputs = [video_production.VideoInputRef(asset_id=poster, role=role)
                          for role in ("first_frame", "last_frame")]
            mode = ("R2V" if inputs[0].role.startswith("reference_")
                    else "FL2V" if len(inputs) == 2 else "I2V")
            label = f"ComfyUI · {copy['shot'].format(number=number)} · {mode} {shot.duration}s"
            report_shot = partial(progress.update, 2 * number + 1, label)
            request = video_production.VideoGenerateArgs(
                action="submit", idempotency_key=f"{prefix}:shot{number}", shot=number, prompt=shot.prompt,
                ratio="9:16", duration=shot.duration, generate_audio=True, watermark=False,
                no_speech=not video_comfyui.dialogue(shot.prompt),
                h3_profile="poster" if shot.reference in POSTERS else None,
                input_assets=inputs,
            )
            try:
                asset = await remake._job(job_ctx, request, kind="segment", label=f"第 {number} 段",
                                          run=run, wait_budget=FILM_WAIT, on_progress=report_shot)
            except (remake.RemakeStopped, LightError):
                await report_shot("paused")
                raise
            await report_shot("done")
            return asset

        await production.remake_stage(ctx, prefix, "video")
        assets = await remake._parallel(list(enumerate(zip(plan.shots, refs, strict=True), 1)),
                                       film, concurrency=len(plan.shots), stop_on_error=True)
        await production.remake_stage(ctx, prefix, "compose")
        timeline = timeline_for(plan, assets, run["video_resolution"])
        report_edit = partial(progress.update, 2 * len(plan.shots) + 2, copy["compose"])
        try:
            asset = await remake._job(job_ctx, video_compose.VideoComposeArgs(
                action="submit", idempotency_key=f"{prefix}:final", timeline=timeline,
                filename=f"{form['shop']}-{form['product_name']}.mp4",
            ), kind="compose", label="画廊成片", run=run, on_progress=report_edit)
        except (remake.RemakeStopped, LightError):
            await report_edit("paused")
            raise
        await report_edit("done")
        duration = sum(shot["duration_sec"] for shot in timeline["shots"])
        result = _delivered(form["product_name"], language, asset, duration, len(assets))
    except remake.RemakeStopped as exc:
        reason = exc.for_language(language)
        await production.remake_stage(ctx, prefix, error=reason)
        result = ToolResult(title="制作已暂停", output=reason, metadata={"error": True})
    except LightError as exc:
        result = ToolResult(title="暂时无法继续制作", output=str(exc), metadata={"error": True})
        plan_rejected = exc.code == "LIGHT_GALLERY_PLAN"
    if not plan_rejected:
        result.metadata["guided_action_finished"] = "render"
    if progress and (details := progress.render()):
        result.output = f"{details}\n\n{result.output}"
    return result


gallery_tool = define_tool(
    TOOL_ID,
    description="Film the five scenes from the App's saved gallery form, then attach an Aliyun IMS edit. Supply the scenes in playback order. Reference choices are constrained to the current template and uploaded product.",
    parameters=Plan,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
    body_timeout=lambda _args, _ctx: 75 * 60,
)
