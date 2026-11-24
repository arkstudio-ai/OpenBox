"""One-pass short film recipe for the soda commercial on the mobile production form."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from dataclasses import replace

import httpx
from PIL import Image, ImageOps
from sqlalchemy import select

from assets.service import owned_ready_query
from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light import wan_story
from light.reference_film import lettering, speech_window, timeline_for
from light.remake import RemakeStopped, _check, _digest, _job, _reference_asset
from light.wan_remake import _on_set, _still
from tool import video_compose, video_production as vp
from tool.footage_analyze import snapshot_url
from tool.video_runninghub import MODEL
from video.media_probe import ensure_meta

REVIEW = "soda-first-take-v1"
DIRECTING = (
    "你是手机「做一条视频」的广告导演。苏打水样片已经拆解完毕，直接使用这些拍法："
    "贴桌低机位拍道具的开场动作，短切人物反应，正反打解围，暖光下亮商品，"
    "切冷色场景接反转或回扣，最后给招牌特写和后期题字。只参考镜头语言，人物、事件、台词、"
    "道具和剪点由本片决定。用户的文案是创作参考。真实店名、菜名和菜品外观按当前素材。"
    "直接写完整的约25至30秒竖屏广告分镜，建议6至8个镜头，每镜设3至5秒的目标剪辑长度。"
    "同空间写同一个scene，所有人写固定着装和辨识特征。每个frame写清开拍那一刻实际入镜的人、"
    "从哪个方向看、道具在哪、菜品是否已经入镜。action用一个主要动作，填可拍的起点和终点。"
    "商品亮相如需上菜，起始画面保持桌面中央为空、一只托盘悬在上方，落桌后再穿插商品特写。"
    "对白用短句，一镜一人，不把题字写进拍摄画面。片尾kind=product，菜名headline与店名后期排版。"
    "无需分析样片或读取原版文件。首帧同批产出，使用表单选定的视频模型每镜一遍，按实际台词挑剪点，IMS合成。"
)


async def _materials(ctx, ids):
    async with get_db_session() as db:
        rows = {a.id: a for a in (await db.scalars(owned_ready_query(ctx.user_id, ctx.workspace_id)
                                                .where(FileAsset.id.in_(ids)))).all()}
    oss = get_oss()
    async with httpx.AsyncClient(timeout=15) as client:
        async def take(asset_id):
            row = rows.get(asset_id)
            if row is None:
                return None
            if row.mime.startswith("image/"):
                return row.id
            if not row.mime.startswith("video/"):
                return None
            try:
                response = await client.get(snapshot_url(oss, row.oss_key, 1.05, width=720, exact=True))
                response.raise_for_status()
                image = Image.open(io.BytesIO(response.content))
                image.verify()
            except (httpx.HTTPError, OSError, ValueError):
                return None
            return await _reference_asset(ctx, f"soda-input-{_digest(row.id)}", response.content)
        ids = await asyncio.gather(*(take(asset_id) for asset_id in ids[:8]))
    if not any(ids):
        raise RemakeStopped("未读取到商品或店内素材。")
    return [asset_id for asset_id in ids if asset_id]


def _board_prompt(plan, columns):
    slots = []
    for i, shot in enumerate(plan.shots, 1):
        product = ("商品状态严格参考实际菜品图。" if shot.product_in_first_frame is not False
                   else "桌上没有商品，保持剧情道具实际位置。")
        slots.append(
            f"第{(i - 1) // columns + 1}行第{(i - 1) % columns + 1}列，竖画幅#{i}："
            f"{shot.scene}。{_on_set(shot.frame)}。{product}"
        )
    if len(slots) % 2:
        slots.append("最后一格复用上一格的无文字画面。")
    return (
        f"影视摄影素材板，精确等分为两行、每行{columns}列，每个格子独立9:16，足幅到边。"
        "不要网格线、格号、拼贴穿帮、文字、字幕、边框或水印。摄影构图都在各自格子内。"
        "所有格子使用相同虚构成年演员与指定服装，准确连贯；素材中的人物仅作为环境、灯光参考。"
        f"环境与定妆：{_on_set(plan.setting_and_cast)}。{_on_set(plan.identity_frame)}。"
        f"真实菜品特征：{_on_set(plan.appearance)}。所附图片用于菜品摆盘、器皿与餐厅装修。"
        "准确保留暖灯夜景和冷色白天的对照。每一格只画动作开始前的瞬间。\n" + "\n".join(slots)
    )


def split_board(raw, columns, count):
    with Image.open(io.BytesIO(raw)) as image:
        width, height = image.size
        cells = []
        for i in range(count):
            col, row = i % columns, i // columns
            x0, x1 = round(col * width / columns), round((col + 1) * width / columns)
            y0, y1 = round(row * height / 2), round((row + 1) * height / 2)
            tile = image.crop((x0 + 2, y0 + 2, x1 - 2, y1 - 2)).convert("RGB")
            tile = ImageOps.fit(tile, (720, 1280), Image.Resampling.LANCZOS)
            buf = io.BytesIO()
            tile.save(buf, "JPEG", quality=92)
            cells.append(buf.getvalue())
        return cells


async def _shooting_frames(ctx, prefix, plan, materials):
    columns = (len(plan.shots) + 1) // 2
    asset = await _still(ctx, prefix, "soda-board-v1", _board_prompt(plan, columns), materials,
                         size=f"{432 * columns}x1536", quality="medium")
    async with get_db_session() as db:
        board = await db.get(FileAsset, asset)
        key = board.oss_key
    raw = await get_oss().get_object(key)
    crops = await asyncio.to_thread(split_board, raw, columns, len(plan.shots))
    return await asyncio.gather(*(
        _reference_asset(ctx, f"soda-shot-{_digest(prefix)}-{i}", crop) for i, crop in enumerate(crops, 1)
    ))


def shot_request(prefix, index, shot, first, *, model=MODEL, resolution="768p"):
    speech = (
        f"只有{shot.speaker}开口，约0.3秒开讲，简短自然地讲完「{shot.line}」，全片只说这句。"
        if shot.line else "所有人保持闭口，不生成对白、哼唱或旁白。"
    )
    native_ratio = "9:16" if model == wan_story.MODEL else "adaptive"
    return vp.VideoGenerateArgs(
        action="submit", model=model, resolution=resolution, ratio=native_ratio,
        duration=max(5, shot.seconds), generate_audio=model == MODEL or bool(shot.line), watermark=False,
        idempotency_key=f"{prefix}:shot{index}", shot=index,
        input_assets=[vp.VideoInputRef(asset_id=first, role="first_frame")],
        spoken_line=shot.line or None, no_speech=not shot.line,
        prompt=("写实竖屏餐饮广告，一个连续实拍镜头。严格保持首帧人物、空间、服装、光线和出现的道具。"
                + _on_set(shot.action) + "。" + speech + "只保留微弱现场声，无配乐、字幕、字样或水印。"),
    )


def cut_for(shot, index, asset, meta, transcript):
    duration = float(meta.get("duration_sec") or 0)
    fps = float(meta.get("fps") or 0)
    if fps <= 0 or duration <= 0.5:
        raise RemakeStopped(f"第 {index} 镜的文件还未取得可用时长。")
    cues = []
    length = min(duration, shot.seconds)
    start = min(0.12, duration - length)
    if shot.line:
        window = speech_window(shot.line, transcript, duration)
        if window:
            cues, left, right = window
        else:
            # A model can paraphrase the approved dialogue. Subtitle the clearly
            # recognized take and retain it once, preserving the single-pass run.
            sentences = [s for s in transcript.get("sentences") or []
                         if s.get("text") and 0 <= s["from_sec"] < s["to_sec"] <= duration + 0.08]
            cues, left, right = sentences, 0.0, duration
        if cues:
            first = cues[0]["from_sec"]
            end = min(duration, cues[-1]["to_sec"])
            length = min(right - left, max(length, end - first + 0.18))
            start = max(left, min(first - 0.12, end + 0.30 - length, right - length))
            if end > start + length:
                start = min(first, end - length)
            cues = [{**cue, "to_sec": min(cue["to_sec"], duration, start + length)}
                    for cue in cues]
    return {"shot": index, "source": asset, "in_sec": round(start, 4), "duration_sec": round(length, 4),
            "fps": fps, "first": math.floor(start * fps),
            "last": max(0, math.ceil((start + length) * fps) - 1), "cues": cues,
            "mute_audio": not cues, "review_version": REVIEW, "take": 1}


async def _transcript(ctx, asset, *, language=None):
    from tool.video_production import VideoTranscribeArgs, execute_transcribe

    result = await execute_transcribe(VideoTranscribeArgs(
        action="submit", asset_id=asset, language=language,
        idempotency_key=f"soda-asr:{asset}" + (f":{language}" if language else "")), ctx)
    if result.metadata.get("status") != "completed":
        return {}
    async with get_db_session() as db:
        job = await db.get(VideoJob, result.metadata["job_id"])
        return (job.result_data or {}).get("transcript") or {}


async def _review(ctx, shot, index, asset, meta):
    if not meta:
        raise RemakeStopped(f"第 {index} 镜仍在保存，已记录生成作业。")
    transcript = await _transcript(ctx, asset) if shot.line and meta.get("has_audio") else {}
    return cut_for(shot, index, asset, meta, transcript)


async def render(ctx, form, run, plan):
    from light import soda_visual_edit
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_soda_visual_review, save_wan_edit

    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, REVIEW)
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    remaining = [(i, shot) for i, shot in enumerate(plan.shots, 1) if i not in cuts]
    needs_visual_review = not (run.get("soda_visual_review") or run.get("wan_final_key"))
    samples = {}
    if needs_visual_review:
        samples = {
            i: asyncio.create_task(soda_visual_edit.sample_existing(task, cut, plan.shots[i - 1].seconds))
            for i, cut in cuts.items()
        }
    await remake_stage(ctx, prefix, "remake_frames")

    async def title():
        art = await asyncio.to_thread(lettering, plan.headline, form["shop"])
        digest = hashlib.sha256(art).hexdigest()[:20]
        return await _reference_asset(ctx, f"soda-lettering-{digest}", art, mime="image/png")

    async def capture():
        if not remaining:
            return
        materials = await _materials(task, form["asset_ids"])
        frames = await _shooting_frames(task, prefix, plan, materials)
        await remake_stage(ctx, prefix, "wan_shots")

        async def make(i, shot):
            from light.shot_editor import publish_request
            await _check(task)
            request = shot_request(prefix, i, shot, frames[i - 1],
                                   model=run.get("video_model") or MODEL,
                                   resolution=run.get("video_resolution") or "768p")
            await publish_request(ctx, prefix, request)
            asset = await _job(task, request, kind="segment", label=f"剧情镜头 {i}", run=run)
            meta = await ensure_meta(asset, user_id=task.user_id)
            if needs_visual_review:
                samples[i] = asyncio.create_task(soda_visual_edit.sample(task, asset, meta, shot.seconds))
            cut = await _review(task, shot, i, asset, meta)
            await save_reference_cut(ctx, prefix, cut)
            cuts[i] = cut

        results = await asyncio.gather(*(make(i, shot) for i, shot in remaining), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result

    try:
        collected = await asyncio.gather(title(), capture(), return_exceptions=True)
        for result in collected:
            if isinstance(result, BaseException):
                raise result
        art = collected[0]
        await remake_stage(ctx, prefix, "wan_edit")
        ordered = [cuts[i] for i in range(1, len(plan.shots) + 1)]
        if needs_visual_review:
            revised, report = await soda_visual_edit.revise(task, plan, ordered, samples)
            run = await save_soda_visual_review(ctx, prefix, original=ordered, cuts=revised, report=report)
            ordered = run["wan_cuts"]
    finally:
        for pending in samples.values():
            if not pending.done():
                pending.cancel()
        await asyncio.gather(*samples.values(), return_exceptions=True)
    timeline = timeline_for(ordered, art, music_start=0)
    key = f"{prefix}:final:{_digest(json.dumps(timeline, sort_keys=True))}"
    run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
    await remake_stage(ctx, prefix, "compose")
    output = await _job(task, video_compose.VideoComposeArgs(
        action="submit", idempotency_key=key, timeline=timeline,
        filename=f"{plan.title}-{plan.product}.mp4"), kind="compose", label="完整广告成片", run=run)
    async with get_db_session() as db:
        job = await db.scalar(select(VideoJob).where(VideoJob.session_id == ctx.session_id,
                                                    VideoJob.kind == "compose", VideoJob.output_asset_id == output))
    meta = await ensure_meta(output, user_id=ctx.user_id)
    expected = sum(c["duration_sec"] for c in ordered)
    if (not job or not wan_story.is_complete_cut(run, job) or not meta or
            abs(float(meta.get("duration_sec") or 0) - expected) > 0.20):
        raise RemakeStopped("完整成片尚未通过镜头清单和实际时长核对。")
    if not await vp._attach_completed(job, ctx):
        raise RemakeStopped("合成完成后暂未能附到这条对话。")
    return output, expected
