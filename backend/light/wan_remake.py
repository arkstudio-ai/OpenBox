"""An owned production turn films, reviews and carries each selected frame into the next take."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import replace

from sqlalchemy import select
from assets.service import owned_ready_query
from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light import remake_case, wan_edit, wan_story
from light.remake import RemakeStopped, _check, _digest, _job, _material_refs, _reference_asset
from light.remake_lettering import end_card
from light.video_language import speech_hint
from tool import image_gen, video_compose, video_production


async def _await_reference_picture(ctx, result, filename, *, edit):
    """Poll one immutable accepted RunningHub receipt and consume its original owned reservation."""
    from agent.effect_ledger import EffectRunFence, poll_owned_runninghub_image

    metadata = result.metadata
    task = metadata.get("provider_task_id")
    key = metadata.get("operation_key")
    assets = metadata.get("asset_ids")
    if (metadata.get("failure_code") != "image_task_pending" or not metadata.get("pending")
            or not isinstance(key, str) or not isinstance(task, str) or not task or not ctx.run_fence
            or assets != [image_gen._reserved_image_id(key, 1)]):
        return None
    fence = EffectRunFence.from_tool_context(ctx)
    deadline = time.monotonic() + 10 * 60
    while time.monotonic() < deadline:
        await _check(ctx)
        state = await poll_owned_runninghub_image(fence, operation="edit" if edit else "generate",
                                                  key=key, task=task, asset=assets[0], call_part=ctx.part_id)
        if state == "succeeded":
            await _check(ctx)
            async with get_db_session() as db:
                row = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(
                    FileAsset.id == assets[0], FileAsset.session_id == ctx.session_id, FileAsset.name == filename,
                    FileAsset.size > 0, FileAsset.mime.startswith("image/")))
            return row.id if row else None
        if state != "pending":
            return None
        await asyncio.sleep(10)
    return None


async def _still(ctx, prefix, name, prompt, references, *, size="1008x1792", quality="high", language="zh"):
    filename = f"wan-story-{_digest(prefix)}-{name}.png"
    async with get_db_session() as db:
        prior = await db.scalar(
            owned_ready_query(ctx.user_id, ctx.workspace_id)
            .where(FileAsset.session_id == ctx.session_id, FileAsset.name == filename)
            .order_by(FileAsset.created_at.desc())
            .limit(1)
        )
    if prior:
        return prior.id
    await _check(ctx)
    frame_ctx = replace(
        ctx, part_id=f"wan-story-{_digest(prefix + name)}", message_id="", sandbox=None, _on_output=None,
        transient_output=True,
    )
    args = image_gen.ImageGenArgs(
        prompt=prompt, input_images=references, size=size, quality=quality, filename=filename
    )
    result_ctx = frame_ctx
    result = await image_gen.execute(args, result_ctx, channel="default")
    for attempt, delay in enumerate((2, 10, 30, 60), start=1):
        rejection = result.metadata.get("rejection_kind")
        if result.metadata.get("provider_accepted") is not False or rejection not in {"not_sent", "rate_limit"}:
            break
        await asyncio.sleep(max(30, delay) if rejection == "rate_limit" else delay)
        await _check(ctx)
        # A confirmed pre-dispatch refusal has its own settled, uncharged receipt.
        suffix = "-retry" if attempt == 1 else f"-retry-{attempt}"
        result_ctx = replace(frame_ctx, part_id=frame_ctx.part_id + suffix)
        result = await image_gen.execute(args, result_ctx, channel="default")
    asset = result.metadata.get("asset_id")
    if not asset and result.metadata.get("failure_code") == "image_task_pending":
        asset = await _await_reference_picture(result_ctx, result, filename, edit=bool(references))
    if not asset:
        if language == "vi":
            raise RemakeStopped("Chưa nhận được ảnh tham chiếu từ dịch vụ tạo ảnh. Các bước đã hoàn tất vẫn được lưu.")
        if language == "en":
            raise RemakeStopped("The image service did not return the reference picture. Completed work has been saved.")
        raise RemakeStopped(f"参考图没有交付，已保留制作记录。{result.title[:90]}")
    return asset


def _on_set(description):
    """Keep editorial notes from an older approved storyboard off the set and soundtrack."""
    clauses = re.split(r"[，,；;。\n]+", description)
    post = r"字幕|片名|题字|书法字|浮字|叠字|宣传字|宣传语|标语|海报字|印章|片尾文案|片尾字|浮现.{0,5}字|出现文字"
    return "，".join(clause.strip() for clause in clauses if clause.strip() and not re.search(post, clause))


def shot_request(prefix, index, plan, first, last, direction, take=1):
    scene = (plan.opening, plan.response, None)[index - 1]
    if scene:
        spoken = "".join(
            line.text if line.text[-1] in "。！？!?.…" else line.text + "。" for line in scene.lines
        )
        instructions = (
            "保持同一间店、同一人物、衣服、左右位置和视线关系。一个实时连续镜头。"
            + _on_set(scene.action)
            + "。" + speech_hint(plan.language, spoken) + "只收演员以下对话，依次在镜头前八秒自然说完，说完以后安静做动作。"
            + " ".join(f"{line.speaker}说「{line.text}」。" for line in scene.lines)
        )
        if index == 2:
            instructions += (
                "从首帧人物动作接着拍。交流完成后镜头顺人物视线缓缓移至桌面同一份商品，结尾稳定停在产品近景。"
            )
    else:
        spoken = ""
        direction = "镜头已停在商品特写。锁住首帧的景别、机位和桌纹"
        instructions = (
            "全程固定机位拍摄同一份食物的近景，商品在画面中的大小始终一致。"
            "只让热气缓慢上升、表面油光微微变化。禁止后拉或移动机位，不加入人、手和其他物件。仅录现场底噪。"
        )
    if take == 2:
        instructions += "严格从参考帧接续动作，保持道具唯一、画面干净。" + (
            "角色只说各自的原话，说完即止。" if scene else "无人开口。"
        )
    setting = _on_set(plan.setting_and_cast) if scene else "餐馆桌面，镜头只关注首帧同一商品"
    prompt = (
        f"竖屏写实餐饮短剧。{setting}。商品为{plan.product}，{_on_set(plan.appearance)}。"
        f"起始画面状态：{_on_set(direction)}。{instructions}"
        " 保留轻微餐馆环境声和器具声。全程没有旁白、配乐、字幕和画面文字。"
    )
    refs = [video_production.VideoInputRef(asset_id=first, role="first_frame")]
    if last:
        refs.append(video_production.VideoInputRef(asset_id=last, role="last_frame"))
    return video_production.VideoGenerateArgs(
        action="submit",
        idempotency_key=f"{prefix}:shot{index}" + (":retake" if take == 2 else ""),
        shot=index,
        model=wan_story.MODEL,
        resolution=wan_story.RESOLUTION,
        ratio="9:16",
        duration=wan_story.DURATIONS[index - 1],
        generate_audio=True,
        watermark=False,
        spoken_line=spoken or None,
        no_speech=not spoken,
        input_assets=refs,
        prompt=prompt,
    )


def timeline_for(cuts, lettering):
    if [cut["shot"] for cut in cuts] != [1, 2, 3] or len({cut["source"] for cut in cuts}) != 3:
        raise RemakeStopped("需要已审过的两段剧情和独立片尾，才可以合成。")
    shots = []
    captions = []
    audio = []
    at = 0
    for cut, volume in zip(cuts, (0.67, 0.55, 0)):
        length = cut["duration_sec"]
        start = cut["in_sec"]
        if length < (2.5 if cut["shot"] == 3 else 8):
            raise RemakeStopped("剪点未覆盖完整场景。")
        shots.append(
            {
                "asset": cut["source"],
                "in_sec": start,
                "duration_sec": length,
                "volume": 0 if cut.get("omit_speech") else volume,
                "fit": "cover",
            }
        )
        if cut.get("omit_speech"):
            # Replace only the unscripted voice with room tone from the same take after the last line.
            end = start + length
            quiet_at = cut["cues"][-1]["to_sec"] + 0.3
            pos = start
            for left, right in cut["omit_speech"]:
                left, right = max(pos, left), min(end, right)
                if right <= left:
                    continue
                if left > pos:
                    audio.append(_audio_range(cut, at, pos, left, pos, volume))
                if quiet_at + right - left <= end:
                    audio.append(_audio_range(cut, at, left, right, quiet_at, volume))
                pos = right
            if pos < end:
                audio.append(_audio_range(cut, at, pos, end, pos, volume))
        for cue in cut["cues"]:
            a = max(0, cue["from_sec"] - start)
            b = min(length, cue["to_sec"] - start)
            if b <= a:
                raise RemakeStopped("剪点截断了对白。")
            captions.append({"text": cue["text"], "from_sec": round(at + a, 3), "to_sec": round(at + b, 3)})
        last_start = at
        at += length
    at = round(at, 3)
    title = dict(
        asset=lettering, kind="image", x=0.2289, y=0.011, width=0.76, height=0.76, fit="contain", volume=0
    )
    overlay_start = last_start + 0.13
    levels = (0.12, 0.34, 0.57, 0.79, 1)
    overlays = [
        dict(
            title,
            from_sec=round(overlay_start + i / 15, 3),
            to_sec=round(overlay_start + (i + 1) / 15, 3) if i < len(levels) - 1 else at,
            opacity=alpha,
        )
        for i, alpha in enumerate(levels)
    ]
    last = cuts[-1]
    if last.get("backdrop"):
        overlays.insert(
            0,
            {
                "asset": last["backdrop"],
                "kind": "image",
                "x": 0,
                "y": 0,
                "width": 1,
                "height": 1,
                "fit": "contain",
                "volume": 0,
                "from_sec": round(last_start, 3),
                "to_sec": at,
            },
        )
    if not last["heard"]:
        audio.append(
            {
                "asset": last["source"],
                "from_sec": round(last_start, 3),
                "to_sec": at,
                "in_sec": last["in_sec"],
                "volume": 0.15,
                "fade_in_sec": 0.38,
                "fade_out_sec": 0.48,
            }
        )
    return {
        "version": 1,
        "canvas": {"width": 1080, "height": 1920, "fps": 25},
        "shots": shots,
        "captions": captions,
        "caption_style": {
            "bottom_ratio": 0.125,
            "max_width": 0.86,
            "motion": {},
            "style": {
                "font": "AlibabaPuHuiTi",
                "size": 57,
                "color": "#FFFDF8",
                "outline": 3,
                "outline_color": "#282019",
            },
        },
        "overlays": overlays,
        "audio": audio,
    }


def _audio_range(cut, at, left, right, source_start, volume):
    fade = min(0.06, (right - left) / 3)
    return {
        "asset": cut["source"],
        "from_sec": round(at + left - cut["in_sec"], 3),
        "to_sec": round(at + right - cut["in_sec"], 3),
        "in_sec": round(source_start, 3),
        "volume": volume,
        "fade_in_sec": round(fade, 3),
        "fade_out_sec": round(fade, 3),
    }


async def render(ctx, form, run, plan):
    from light.production import remake_stage, save_wan_edit
    from light.shot_editor import publish_request

    prefix = run["job_prefix"]
    segment_ctx = replace(ctx, message_id="", sandbox=None, _on_output=None)
    reference = remake_case.reference_copy(form)
    cuts = list(run.get("wan_cuts") or [])
    if not cuts:
        await remake_stage(ctx, prefix, "remake_frames")
        material = await _material_refs(ctx, form["asset_ids"])
        if form["asset_ids"] and not material:
            raise RemakeStopped("无法读取所选素材的商品画面，请重试或选择一张商品图片。")
        style = (
            f"写实餐馆剧情广告，9:16竖屏单幅电影剧照，不拼贴、不写字。{_on_set(plan.setting_and_cast)}。"
            f"真实商品：{plan.product}，{_on_set(plan.appearance)}。"
        )
        first = await _still(
            ctx,
            prefix,
            "opening",
            style
            + _on_set(plan.opening_frame)
            + "。素材照片只参考商品特征。尊重剧情中尚未上菜或揭盖的状态；桌面只有一套道具。",
            material,
        )
        await publish_request(ctx, prefix, shot_request(prefix, 1, plan, first, None, plan.opening_frame))
        target = await _still(
            ctx,
            prefix,
            "reveal",
            style + "第一张是同一镜刚开始的画面。严格保留演员的脸、衣服、左右和机位，"
            "商品特征来自其余图片。经过下述动作后停在：" + _on_set(plan.reveal_frame),
            [first, *material][:3],
        )
    for index in range(len(cuts) + 1, 4):
        if index > 1:
            first = cuts[-1]["handoff"]
            target = None
            if index == 3:
                # The provider accepts separate frame roles. Anchor the product shot's landing as well as its start.
                async with get_db_session() as db:
                    handoff_image = await db.scalar(
                        owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == first)
                    )
                    if not handoff_image:
                        raise RemakeStopped("未取到已确认的片尾商品特写。")
                    plate_key = handoff_image.oss_key
                target = await _reference_asset(
                    ctx, f"product-hold-{first}", await get_oss().get_object(plate_key)
                )
        direction = cuts[-1]["continuity"] if cuts else plan.opening_frame
        lines = (plan.opening.lines, plan.response.lines, [])[index - 1]
        for take in (1, 2):
            await remake_stage(ctx, prefix, "wan_shots")
            req = shot_request(prefix, index, plan, first, target, direction, take)
            await publish_request(ctx, prefix, req)
            asset = await _job(segment_ctx, req, kind="segment", label=f"剧情镜头 {index}", run=run)
            await remake_stage(ctx, prefix, "wan_edit")
            cut = await wan_edit.review(
                segment_ctx, plan, index, asset, req.duration, lines, reference, first
            )
            if cut:
                break
        if not cut:
            raise RemakeStopped(f"第 {index} 段的对白或画面接续未通过审片，已保留镜头与草稿。")
        cuts.append({**cut, "first_reference": first, "take": take})
        await save_wan_edit(ctx, prefix, cuts=cuts)
    await remake_stage(ctx, prefix, "compose")
    art = end_card(plan.headline, form["shop"])
    plate = await _reference_asset(
        ctx, "wan-lettering-" + hashlib.sha256(art).hexdigest()[:20], art, mime="image/png"
    )
    timeline = timeline_for(await wan_edit.audio_edits(ctx, cuts), plate)
    key = f"{prefix}:final:{_digest(json.dumps(timeline, sort_keys=True))}"
    run = await save_wan_edit(ctx, prefix, cuts=cuts, final_key=key)
    aid = await _job(
        segment_ctx,
        video_compose.VideoComposeArgs(
            action="submit",
            idempotency_key=key,
            timeline=timeline,
            filename=f"{plan.title}-{plan.product}.mp4",
        ),
        kind="compose",
        label="完整剧情成片",
        run=run,
    )
    async with get_db_session() as db:
        job = await db.scalar(
            select(VideoJob).where(
                VideoJob.session_id == ctx.session_id,
                VideoJob.user_id == ctx.user_id,
                VideoJob.kind == "compose",
                VideoJob.output_asset_id == aid,
            )
        )
    if (
        not job
        or not wan_story.is_complete_cut(run, job)
        or not await video_production._attach_completed(job, ctx)
    ):
        raise RemakeStopped("成片与已确认剪点未核对完毕，请重试取回同一合成任务。")
    return aid, sum(c["duration_sec"] for c in cuts)
