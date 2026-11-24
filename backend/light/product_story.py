"""Film and checkpoint the 16 live shots for the 54-second product story."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from sqlalchemy import select

from assets.service import owned_ready_query
from core.identifier import ascending
from core.oss import OssError, get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light import product_story_plan as recipe
from light.wan_edit import _words
from light.reference_film import speech_window
from light.remake import RemakeStopped, _check, _digest, _existing_job, _job, _material_refs, _parallel, _reference_asset
from light.soda_story import _transcript
from light.wan_remake import _on_set, _still
from tool import video_production as vp
from tool.video_runninghub import MODEL
from video.media_probe import ensure_meta

REVIEW = "product-story-dialogue-v1"
SPEAKERS = {1: "女儿", 2: "爸爸", 14: "女儿"}


async def _product_frames(ctx, prefix, plan, form):
    refs = await _material_refs(ctx, form["asset_ids"])
    is_original = re.sub(r"\s+", "", plan.product) == "小米18" and not plan.appearance
    if refs and is_original:
        material = await _asset(ctx, refs[0])
        ref = await get_oss().get_object(material.oss_key)
        is_original = hashlib.sha256(ref).hexdigest() == recipe.case()["product_image_sha256"]
    if not (is_original or refs):
        raise RemakeStopped("请先选一张能辨认这款手机外观的产品图，再生成。")

    async def prepare(shot):
        identifier = shot["id"]
        raw = (recipe.ASSETS / f"{identifier}.jpg").read_bytes()
        plate = await _reference_asset(ctx, f"{recipe.FORMAT}-{identifier}", raw)
        if is_original or not shot["product_visible"]:
            return int(identifier), plate
        composition = (
            "保持手机屏幕在输入图1完全相同的像素边界和圆角，保持空白屏幕。"
            if identifier == "18" else ""
        )
        adapted = await _still(
            ctx, prefix, f"product-story-{identifier}",
            "保持图1摄影角度、演员、衣服、站位、手势、取景和光线。"
            f"只把手机替换成图2的真实机型「{_on_set(plan.product)}」。{_on_set(plan.appearance)}。"
            "只有原镜头中的同一台手机，后摄、屏幕方向和边框按实际手机保留。"
            f"{composition}输出横屏16:9单张剧照。无字幕、宣传文字和新水印。",
            [plate, *refs[:2]], size="1536x864", quality="high",
        )
        return int(identifier), adapted

    plates = [shot for shot in recipe.shots(plan) if shot["id"] != "17"]
    return dict(await _parallel(plates, prepare, concurrency=5))


async def _asset(ctx, asset_id):
    async with get_db_session() as db:
        item = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id))
    if item is None:
        raise RemakeStopped("一个制作素材尚未保存，重试会接着使用原任务。")
    return item


def shot_request(prefix, index, shot, frame, take):
    line = shot["spoken_line"]
    performance = (
        f"仅{SPEAKERS[index]}按指定中文原话自然说一遍，其他人不说话，"
        "留一点表情反应和室内原声。"
        if line else "无人说话，不出现旁白、哼唱或背景音乐。"
    )
    return vp.VideoGenerateArgs(
        action="submit", model=MODEL, resolution="768p", ratio="adaptive",
        duration=shot["generate_seconds"], generate_audio=True, watermark=False,
        shot=index, input_assets=[vp.VideoInputRef(asset_id=frame, role="first_frame")],
        spoken_line=line, no_speech=not line,
        idempotency_key=f"{prefix}:shot{index}" + (":retake:2" if take > 1 else ""),
        prompt=f"{shot['prompt']} 连续横屏电影镜头，保持首帧人物、道具位置和服装。{performance}"
               + ("口型清楚，先完整说出台词再停顿。" if take > 1 and line else ""),
    )


def choose_cut(shot, index, asset, meta, transcript):
    duration = float(meta.get("duration_sec") or 0)
    available = math.floor(duration * recipe.FPS + .02)
    target = shot["frames"]
    if available < target - 4 or not meta.get("width") or not meta.get("height"):
        raise RemakeStopped(f"第 {index} 镜素材时长不足。")
    start, end = shot["preferred"]
    count = min(end - start, available)
    start = min(start, available - count)
    cues = []
    if line := shot["spoken_line"]:
        window = speech_window(line, transcript, duration)
        if not window:
            # Sentence boundaries are sufficient when the recognizer returned
            # the complete line but split a compound word differently.
            sentences = transcript.get("sentences") or []
            if not sentences or _words("".join(s["text"] for s in sentences)) != _words(line):
                raise RemakeStopped(f"第 {index} 镜未核对到完整的指定对白。")
            window = ([{"text": line, "from_sec": sentences[0]["from_sec"],
                        "to_sec": sentences[-1]["to_sec"]}], 0, duration)
        cues, clear_from, clear_to = window
        line_first = math.floor(cues[0]["from_sec"] * recipe.FPS)
        line_end = math.ceil(cues[0]["to_sec"] * recipe.FPS)
        left = max(0, math.ceil(clear_from * recipe.FPS), line_end - count)
        right = min(line_first, math.floor(clear_to * recipe.FPS) - count, available - count)
        if left > right:
            raise RemakeStopped(f"第 {index} 镜没有可保留完整对白的剪点。")
        start = min(max(start, left), right)
    return {"shot": index, "source": asset, "start": start, "frames": count, "output_frames": target,
            "speed": count / target, "has_audio": bool(meta.get("has_audio")),
            "cues": cues, "review_version": REVIEW}


async def _film(ctx, prefix, run, index, shot, frame):
    from light.shot_editor import publish_request

    for take in (1, 2):
        req = shot_request(prefix, index, shot, frame, take)
        await publish_request(ctx, prefix, req)
        try:
            source = await _job(ctx, req, kind="segment", label=f"品牌剧情第 {index} 镜", run=run)
        except RemakeStopped:
            prior = await _existing_job(ctx, "segment", req.idempotency_key)
            ambiguous = ((prior.result_data or {}).get("submit_error") or {}).get("submission_outcome") if prior else None
            if take == 1 and prior and prior.status == "failed" and prior.provider_task_id and ambiguous != "ambiguous":
                continue
            raise
        meta = await ensure_meta(source, user_id=ctx.user_id)
        if not meta:
            raise RemakeStopped(f"第 {index} 镜还在保存，重试会沿用这个作业。")
        transcript = (await _transcript(ctx, source, language="zh")
                      if shot["spoken_line"] and meta.get("has_audio") else {})
        try:
            return {**choose_cut(shot, index, source, meta, transcript), "take": take}
        except RemakeStopped:
            if take != 1:
                raise


async def _download(ctx, asset_id, path):
    asset = await _asset(ctx, asset_id)
    with path.open("wb") as file:
        async for block in get_oss().get_object_chunks(asset.oss_key):
            file.write(block)


async def _publish(ctx, key, path, plan, cuts, measured):
    from session.agent_event_log import prepare_agent_event_write
    from trajectory.jobs import record_job_in_tx

    asset_id = f"asset_product_{_digest(key)}"
    name = (re.sub(r'[^\w\-\u4e00-\u9fff]+', "-", f"{plan.product}-{plan.title}").strip("-")
            or "product-story") + ".mp4"
    oss_key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await _check(ctx)
    await get_oss().put_object_file(oss_key, path, content_type="video/mp4", forbid_overwrite=True)
    request = recipe.final_request(cuts)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        row = await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id,
                                              run_fence=ctx.run_fence)
        existing = await db.scalar(select(VideoJob).where(
            VideoJob.user_id == ctx.user_id, VideoJob.kind == "compose", VideoJob.idempotency_key == key,
        ))
        if existing:
            return existing
        db.add(FileAsset(id=asset_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                         session_id=row.id, project_id=row.project_id, name=name, mime="video/mp4",
                         size=path.stat().st_size, oss_key=oss_key, created_at=now, status="ready",
                         source="agent", transient=False, meta={
                             "width": 1280, "height": 720, "fps": recipe.FPS, "duration_sec": recipe.DURATION,
                             "has_audio": True,
                         }))
        await db.flush()
        job = VideoJob(id=ascending("video"), user_id=ctx.user_id, session_id=row.id, project_id=row.project_id,
                       kind="compose", model="ffmpeg", status="completed", idempotency_key=key,
                       request_data=request, request_hash=hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(),
                       result_data=measured, output_asset_id=asset_id, created_at=now, updated_at=now, completed_at=now)
        db.add(job)
        await record_job_in_tx(db, job, submitted=True)
        await record_job_in_tx(db, job)
    return job


async def render(ctx, form, run, plan):
    from light import product_story_post as post
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_wan_edit

    post.require_tools()
    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, REVIEW)
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    story = recipe.shots(plan)
    remaining = [i for i in range(1, recipe.FILMED_COUNT + 1) if i not in cuts]

    try:
        await remake_stage(ctx, prefix, "remake_frames")
        plates = await _product_frames(task, prefix, plan, form)
        if remaining:
            await remake_stage(ctx, prefix, "wan_shots")

            async def film(index):
                cut = await _film(task, prefix, run, index, story[index - 1], plates[index])
                await save_reference_cut(ctx, prefix, cut)
                cuts[index] = cut

            await _parallel(remaining, film, concurrency=5)

        ordered = [cuts[index] for index in range(1, recipe.FILMED_COUNT + 1)]
        key = f"{prefix}:final:{_digest(json.dumps(recipe.final_request(ordered), sort_keys=True))}"
        await remake_stage(ctx, prefix, "wan_edit")
        run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
        await remake_stage(ctx, prefix, "compose")
        job = await _existing_job(ctx, "compose", key)
        if not job:
            with TemporaryDirectory(prefix="openbox-product-story-") as directory:
                folder = Path(directory)
                media = [(i, cut["source"], "mp4") for i, cut in cuts.items()]
                media.extend((i, plates[i], "jpg") for i in range(18, 22))

                async def download(item):
                    index, asset, extension = item
                    await _download(task, asset, folder / f"source-{index:02d}.{extension}")

                await _parallel(media, download, concurrency=6)
                output, verified = await post.assemble(folder, plan, ordered)
                job = await _publish(task, key, output, plan, ordered, verified)
    except OssError as exc:
        raise RemakeStopped("制作素材暂时无法读写，重试会接着使用已核对的镜头。") from exc
    if not recipe.is_complete_cut(run, job) or not await vp._attach_completed(job, ctx):
        raise RemakeStopped("成片尚未通过完整交付核对，已保留剪辑。")
    return job.output_asset_id, recipe.DURATION
