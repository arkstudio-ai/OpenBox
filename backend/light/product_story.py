"""Prepare, film and checkpoint the approved brand story."""

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
from light.video_language import NAMES
from light.wan_edit import _words
from light.reference_film import speech_window
from light.remake import RemakeStopped, _check, _digest, _existing_job, _job, _material_refs, _parallel, _reference_asset
from light.soda_story import _transcript
from light.wan_remake import _on_set, _still
from tool import video_production as vp
from tool.video_runninghub import MODEL
from video.media_probe import ensure_meta

REVIEW = "product-story-dialogue-v1"
BRAND_REVIEW = "product-story-dialogue-v2"
SPEAKERS = {1: "女儿", 2: "爸爸", 14: "女儿"}
VIETNAMESE_PERFORMANCE = {
    1: "Cô bé chỉ vào bức ảnh trong album rồi ngước nhìn ba. Cô bé nói với ba, hơi ngạc nhiên.",
    2: "Người ba nhìn con gái, mỉm cười và trả lời. Nói xong, anh đưa tay lấy điện thoại và chìa khóa.",
    14: "Cô bé nhìn về phía ba và nhẹ nhàng gọi ba đến chụp ảnh cùng gia đình.",
}


async def _product_frames(ctx, prefix, plan, form):
    refs = await _material_refs(ctx, form["asset_ids"])
    is_original = re.sub(r"\s+", "", plan.product).casefold() in {"小米18", "xiaomi18"} and not plan.appearance
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


async def _brand_frames(ctx, prefix, plan, story, remaining):
    refs = await _material_refs(ctx, plan.product_assets)
    if not refs:
        raise RemakeStopped("请先选择一张清晰商品照片，或一段开头能看清商品的实拍。")
    identity = None
    if remaining and plan.identity_frame:
        identity = await _still(
            ctx, prefix, "brand-cast",
            f"横屏电影定妆照。{_on_set(plan.identity_frame)}。清楚展示实际选定的演员、脸和服装。不要商品、叠字或水印。",
            [], size="1536x864", quality="high",
        )

    async def prepare(index):
        shot = story[index - 1]
        hero = index == len(story)
        references = []
        if identity and not hero:
            references.append(identity)
        if shot["product_visible"]:
            references.extend(refs)
        product = (
            f"画面里的商品是「{plan.product}」。按商品素材一致地保留形状、比例、材质、颜色与可见标识。"
            f"外观要点：{plan.appearance}。" if shot["product_visible"] else "此时画面里还看不到商品。"
        )
        context = (
            "商品完整摆在左半画面，右半画面为均匀的暗色留白，无人物。"
            if hero else f"场景和造型连续性：{_on_set(plan.setting_and_cast)}。"
            "按本镜构图取景，定妆参考只用于识别本镜明确入画的演员；其他人留在画外。只写到手的特写，仅拍手和道具。"
        )
        image = await _still(
            ctx, prefix, f"brand-{index:02d}",
            f"横屏16:9电影广告首帧。{_on_set(shot['frame'])}。{product}{context}"
            "输出单张剧照，保持物件数量与摆位，无字幕、叠字或新增水印。",
            references, size="1536x864", quality="high",
        )
        return index, image

    return dict(await _parallel([*remaining, len(story)], prepare, concurrency=5))


async def _asset(ctx, asset_id):
    async with get_db_session() as db:
        item = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id))
    if item is None:
        raise RemakeStopped("一个制作素材尚未保存，重试会接着使用原任务。",
                            vi="Một tư liệu sản xuất chưa được lưu. Thử lại sẽ tiếp tục yêu cầu ban đầu.",
                            en="A production asset has not been saved yet. Retrying will continue the original job.")
    return item


def shot_request(prefix, index, shot, frame, take, *, last=None, correction="", revision=0):
    line = shot["spoken_line"]
    adaptive = shot.get("adaptive", False)
    speaker = shot.get("speaker") or SPEAKERS.get(index, "指定角色")
    performance = (
        f"仅{speaker}用 {NAMES[shot['language']]} 逐字说指定台词一次，其他人不说话，"
        "留一点表情反应和环境原声。"
        if line else "无人说话，不出现旁白、哼唱或背景音乐。"
    )
    prompt = f"{shot['prompt']} 连续横屏电影镜头，保持首帧人物、道具位置和服装。{performance}"
    if adaptive:
        prompt += "保持首帧商品的品牌、结构、颜色与材质。只做指定动作，不添加字幕、旁白、水印或背景音乐。"
        if not line:
            prompt += "保留符合画面动作的自然环境声和道具声。"
    if last:
        prompt += (
            f"从输入首帧开始，保持同一机位、景别、照明和时间。只做一次上述动作，最后自然停在输入尾帧。"
            f"尾帧状态：{_on_set(shot['last_frame'])}。镜内无剪切、闪回、插镜、推拉或重演。"
            "分镜未指定的动态反光、镜头光晕、星芒和闪光一律不加。固定照明中的反光保持静止。"
        )
        if correction:
            prompt += (f"补拍必须执行这条修正。按它限定本镜的动作、道具和照明：{_on_set(correction)}。"
                       "按首尾帧保留画幅。修正提到的画外物品留在原处并保持在画外。")
    if take == 2 and line:
        prompt += "口型清楚，先完整说出台词再停顿。"
    if take >= 3 and adaptive:
        prompt += " Nói chậm, phát âm rõ từng tiếng và đúng dấu thanh. Giữ nguyên lời thoại."
    elif take >= 3:
        pace = "Nói chậm, phát âm rõ từng tiếng và đúng dấu thanh." if take == 3 else (
            "Nói với giọng tiếng Việt miền Nam tự nhiên. Ngắt nhẹ ở dấu phẩy và nói trọn câu."
        )
        prompt = (
            "Cảnh quảng cáo gia đình chân thực, ngang 16:9, bắt đầu từ ảnh đã cho. "
            "Giữ nguyên khuôn mặt, trang phục và đồ vật. "
            f"{VIETNAMESE_PERFORMANCE[index]} {pace} "
            "Chỉ nhân vật này nói đúng một lần. Khớp khẩu hình, không chữ, không nhạc."
        )
    inputs = [vp.VideoInputRef(asset_id=frame, role="first_frame")]
    if last:
        inputs.append(vp.VideoInputRef(asset_id=last, role="last_frame"))
    return vp.VideoGenerateArgs(
        action="submit", model=MODEL, resolution="768p", ratio="adaptive",
        duration=shot["generate_seconds"], generate_audio=True, watermark=False,
        shot=index, input_assets=inputs,
        spoken_line=line, no_speech=not line,
        idempotency_key=f"{prefix}:shot{index}" + (f":repair:{revision}" if revision else "")
        + (f":retake:{take}" if take > 1 else ""),
        prompt=prompt,
    )


def choose_cut(shot, index, asset, meta, transcript):
    duration = float(meta.get("duration_sec") or 0)
    available = math.floor(duration * recipe.FPS + .02)
    target = shot["frames"]
    if available < target - 4 or not meta.get("width") or not meta.get("height"):
        raise RemakeStopped(f"第 {index} 镜素材时长不足。")
    start, end = shot["preferred"]
    count = available if shot.get("locked") else min(end - start, available)
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
            "cues": cues, "review_version": (recipe.VISUAL_REVIEW if shot.get("locked") else
                                             BRAND_REVIEW if shot.get("adaptive") else REVIEW)}


async def _film(ctx, prefix, run, index, shot, frame, *, last=None, authored=None, materials=()):
    from light.product_story_continuity import ReviewRejected, approved_hold, review_take
    from light.shot_editor import publish_request

    last_take = 4 if shot["language"] == "vi" and shot["spoken_line"] else 2
    repair = recipe.repair_for(run, index)
    revision = repair.get("revision", 0)
    advice = repair.get("report") or {}
    correction = advice.get("correction") or advice.get("reason", "")
    if last and repair.get("kind") == "motion":
        frames = shot["frames"]
        hold = await approved_hold(ctx, index, authored, (frame, last), frames, _download)
        if hold:
            cut = {"shot": index, "source": hold, "start": 0, "frames": frames, "output_frames": frames,
                   "speed": 1, "has_audio": False, "cues": [], "review_version": recipe.VISUAL_REVIEW}
            evidence, _ = await review_take(ctx, cut, authored, (frame, last), materials, _download,
                                            language=shot["language"])
            if evidence:
                return {**cut, **evidence, "repair_revision": revision, "take": 1}
    for take in range(1, last_take + 1):
        req = shot_request(prefix, index, shot, frame, take, last=last, correction=correction, revision=revision)
        await publish_request(ctx, prefix, req)
        try:
            source = await _job(ctx, req, kind="segment", label=f"品牌剧情第 {index} 镜", run=run)
        except RemakeStopped:
            prior = await _existing_job(ctx, "segment", req.idempotency_key)
            ambiguous = ((prior.result_data or {}).get("submit_error") or {}).get("submission_outcome") if prior else None
            if take < last_take and prior and prior.status == "failed" and prior.provider_task_id and ambiguous != "ambiguous":
                continue
            raise
        meta = await ensure_meta(source, user_id=ctx.user_id)
        if not meta:
            raise RemakeStopped(f"第 {index} 镜还在保存，重试会沿用这个作业。")
        transcript = (await _transcript(ctx, source, language=shot["language"])
                      if shot["spoken_line"] and meta.get("has_audio") else {})
        try:
            cut = choose_cut(shot, index, source, meta, transcript)
        except RemakeStopped:
            if take < last_take:
                continue
            if shot["language"] != "vi" or not transcript.get("sentences"):
                raise
            cut = await _dub(ctx, index, shot, source, meta, transcript)
        if last:
            evidence, problem = await review_take(
                ctx, cut, authored, (frame, last), materials, _download, language=shot["language"],
            )
            if problem:
                if take == last_take:
                    raise ReviewRejected(index, problem.reason, problem.correction, kind="motion")
                correction = problem.correction
                continue
            cut.update(**evidence, repair_revision=revision)
        return {**cut, "take": take}


async def _dub(ctx, index, shot, source, meta, transcript):
    from light import production_voice

    # Keep the take's visible speaking interval. Replace its mispronounced voice
    # with the approved words, and use the duration of that replacement for the cue.
    line = shot["spoken_line"]
    preset = "system:steady" if shot.get("adaptive") or index == 2 else "system:bright"
    model, voice = await production_voice.resolve(ctx.user_id, preset, "vi")
    key = await production_voice.render_clip(user=ctx.user_id, workspace=ctx.workspace_id,
                                               session=ctx.session_id, model=model, voice=voice,
                                               text=line, locale="vi")
    _, duration = production_voice._canonical_wav(await get_oss().get_object(key))
    sentence = transcript["sentences"][0]
    start = sentence["from_sec"]
    end = min(float(meta["duration_sec"]), transcript["sentences"][-1]["to_sec"])
    if not 0 <= start < end or duration <= 0:
        raise RemakeStopped(f"第 {index} 镜无法对齐越南语对白。")
    tempo = max(.75, min(2.0, duration / (end - start)))
    stop = start + duration / tempo
    if stop > float(meta["duration_sec"]):
        raise RemakeStopped(f"第 {index} 镜不能保留完整越南语对白。")
    cut = choose_cut(shot, index, source, meta, {"sentences": [
        {"text": line, "from_sec": start, "to_sec": stop},
    ]})
    return {**cut, "dub": {"key": key, "tempo": tempo}}


async def _download(ctx, asset_id, path):
    asset = await _asset(ctx, asset_id)
    with path.open("wb") as file:
        async for block in get_oss().get_object_chunks(asset.oss_key):
            file.write(block)


async def _publish(ctx, key, path, plan, request, measured):
    from session.agent_event_log import prepare_agent_event_write
    from trajectory.jobs import record_job_in_tx

    asset_id = f"asset_product_{_digest(key)}"
    name = (re.sub(r'[^\w\-\u4e00-\u9fff]+', "-", f"{plan.product}-{plan.title}").strip("-")
            or "product-story") + ".mp4"
    oss_key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await _check(ctx)
    await get_oss().put_object_file(oss_key, path, content_type="video/mp4", forbid_overwrite=True)
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
                             "width": 1280, "height": 720, "fps": measured["fps"], "duration_sec": measured["duration_sec"],
                             "has_audio": True, "subtitles_srt": measured["subtitles_srt"],
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
    from light import product_story_continuity as continuity
    from light import product_story_post as post
    from light.production import begin_reference_edit, remake_stage, reopen_brand_cut, save_reference_cut, save_wan_edit

    post.require_tools()
    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    adaptive = isinstance(plan, recipe.BrandPlan)
    locked = isinstance(plan, recipe.ContinuityPlan)
    review = recipe.VISUAL_REVIEW if locked else BRAND_REVIEW if adaptive else REVIEW
    run = await begin_reference_edit(ctx, prefix, review)
    story = recipe.shots(plan)
    filmed = range(1, recipe.filmed_count(plan) + 1)

    try:
        while True:
            cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
            remaining = [i for i in filmed if i not in cuts]
            await remake_stage(ctx, prefix, "remake_frames")
            if locked:
                pairs, materials = await continuity.prepare(task, prefix, run, plan)
                plates = {index: frames[0] for index, frames in pairs.items()}
            elif adaptive:
                plates = await _brand_frames(task, prefix, plan, story, remaining)
            else:
                plates = await _product_frames(task, prefix, plan, form)
            if remaining:
                await remake_stage(ctx, prefix, "wan_shots")

                async def film(index):
                    kwargs = {"last": pairs[index][1], "authored": plan.shots[index - 1], "materials": materials} if locked else {}
                    cut = await _film(task, prefix, run, index, story[index - 1], plates[index], **kwargs)
                    await save_reference_cut(ctx, prefix, cut)
                    cuts[index] = cut

                await _parallel(remaining, film, concurrency=5)

            ordered = [cuts[index] for index in filmed]
            await remake_stage(ctx, prefix, "wan_edit")
            if locked:
                try:
                    await continuity.approve_joins(task, prefix, run, plan, ordered, plates[len(story)], materials)
                except continuity.JoinRejected as exc:
                    repaired = await reopen_brand_cut(
                        ctx, prefix, cuts=ordered, target=exc.check.shot, report=exc.check.model_dump(),
                    )
                    if len(repaired["brand_repairs"]) == len(run.get("brand_repairs") or []):
                        raise
                    run = repaired
                    continue
            break
        request = recipe.final_request(ordered, plan.language, plan=plan,
                                       end_card=plates[len(story)] if adaptive else None)
        key = f"{prefix}:final:{_digest(json.dumps(request, sort_keys=True))}"
        run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
        await remake_stage(ctx, prefix, "compose")
        job = await _existing_job(ctx, "compose", key)
        if not job:
            with TemporaryDirectory(prefix="openbox-product-story-") as directory:
                folder = Path(directory)
                media = [(i, cut["source"], "mp4") for i, cut in cuts.items()]
                media.extend((i, plates[i], "jpg") for i in ([len(story)] if adaptive else range(18, 22)))

                async def download(item):
                    index, asset, extension = item
                    await _download(task, asset, folder / f"source-{index:02d}.{extension}")

                await _parallel(media, download, concurrency=6)
                for cut in ordered:
                    if dub := cut.get("dub"):
                        (folder / f"voice-{cut['shot']:02d}.wav").write_bytes(await get_oss().get_object(dub["key"]))
                output, verified = await post.assemble(folder, plan, ordered)
                job = await _publish(task, key, output, plan, request, verified)
    except OssError as exc:
        raise RemakeStopped("制作素材暂时无法读写，重试会接着使用已核对的镜头。") from exc
    if not recipe.is_complete_cut(run, job) or not await vp._attach_completed(job, ctx):
        raise RemakeStopped("成片尚未通过完整交付核对，已保留剪辑。")
    return job.output_asset_id, recipe.DURATION
