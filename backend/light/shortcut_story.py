"""Generate, review and compose the seven-shot shortcut remake."""

import hashlib
import json
from dataclasses import replace
from difflib import SequenceMatcher

from light import shortcut_review, soda_story, wan_edit, wan_story
from light.reference_film import speech_window, timeline_for
from light.remake import RemakeStopped, _digest, _existing_job, _job, _parallel, _reference_asset
from light.remake_lettering import end_card
from light.shortcut_plan import HANDOFF, OPENING, PRODUCT, REVEAL, REVIEWED_SHOTS, SETUP_ROLES
from light.wan_remake import _on_set, _still
from tool import video_compose, video_production as vp
from tool.video_runninghub import MODEL
from video.media_probe import ensure_meta

REVIEW = "shortcut-continuity-v3"
APPROVED_SETUPS = f"{REVIEW}:setups"
SETUP_REPAIRS = 2


def _opening_exchange(shot):
    if shot.line:
        listener = shot.listener or "他身边画内正在交谈的同伴"
        exchange = f"{shot.speaker}面对{listener}说话；{listener}自然看回说话人。"
    else:
        exchange = "有来往的角色彼此看向画内对方或手上道具。"
    return (f"{exchange}第三人关注画内同伴或自己的道具。"
            "交流结束只留一个自然反应，眼睛仍朝画内；谁也不朝镜头、画外或未来的路线齐转头。")


async def _anchor(ctx, prefix, plan):
    actors = plan.shots[0].visible_roles
    order = "，从左到右依次为" + "、".join(actors) if actors else ""
    return await _still(
        ctx, prefix, "shortcut-cast-v3",
        "三位虚构成年演员的身份参考照。摄影棚中性背景，三人并排半身正面、彼此之间留空，"
        f"脸、发型和衣着清楚可辨{order}。只按以下设定辨认脸、发型和服装："
        f"{_on_set(plan.setting_and_cast)}。保留每人区分性的装束，"
        "画面始终是中性摄影棚身份参考，无桌椅、餐馆、菜品、文字或水印。",
        [], quality="medium",
    )


async def _frame_pair(ctx, prefix, plan, index, shot, anchor, material, *, correction=""):
    product = (f"本镜能见到实际商品{plan.product}，保持{_on_set(plan.appearance)}。"
               if shot.product_in_first_frame else "本镜没有解决问题的商品成品，只拍准备中的人物和普通道具。")
    camera = f"机位与景别：{_on_set(shot.camera)}。" if shot.camera else ""
    if index == OPENING:
        camera += "摄影机从侧前方看已经在交谈的三人，构图使说话人与接话人的脸存在明确视线交点。"
    if index == HANDOFF:
        camera += "只取两名辛苦者的双人中近景，能看清两双眼睛，取景在揭晓桌之前截止。"
    if index == REVEAL:
        camera += "切到桌子另一侧的单人反打，用新的背景和取景方向，只拍从容者与整份成品。"
    cast = ""
    if shot.visible_roles is not None:
        cast = ("本镜只允许出现：" + "、".join(shot.visible_roles) + "。"
                if shot.visible_roles else "本镜没有人物或人手。")
    refs = [anchor] if shot.visible_roles != [] else []
    if shot.product_in_first_frame:
        refs.extend(material[:3])
    identities = ("图1仅提供脸与服装，按本镜名单取人，完全重新设计机位、背景与站位。"
                  if shot.visible_roles != [] else "")
    repair = f"依据本次验收，按这条新取景替代旧描述中冲突的构图：{_on_set(correction)}。" if correction else ""
    suffix = f"v3-{_digest(correction)}" if correction else "v3"
    exchange = _opening_exchange(shot) if index == OPENING else ""
    first = await _still(
        ctx, prefix, f"shortcut-{index}-first-{suffix}",
        "输出一张真实电影剧照，足幅9:16，无字幕、题字、标题和水印。"
        f"{identities}商品参考只用于商品外观。{cast}{camera}{product}"
        f"本镜段落：{_on_set(shot.scene)}。起始静态构图：{_on_set(shot.frame)}。{exchange}{repair}",
        refs, quality="medium",
    )
    end = shot.last_frame
    if index == OPENING:
        end = ("原地说完后的反应瞬间，演员仍在图1同样位置，脸留在交谈姿态。"
               "从容者的道具保持归属，转身去办事在下一镜。"
               f"{exchange}")
    if index == HANDOFF and not end:
        end = "两位演员已停下，双眼一同转向下一镜安静人物所在的画外同一侧，桌面和成品仍在画外。"
    if not end:
        return first, None
    last = await _still(
        ctx, prefix, f"shortcut-{index}-last-{suffix}",
        "局部编辑图1成为同一连续镜头的结束画面，9:16。只改本镜主动作完成后的姿态、手或视线。"
        "锁住图1的取景边界、灯光和景别，保持图1演员脸、衣着、道具数量及商品位置。"
        f"{cast}{product}结束静态状态：{_on_set(end)}。无文字、字幕、画框或水印。",
        [first], quality="medium",
    )
    return first, last


async def _confirm_setups(ctx, prefix, run, plan, pairs, anchor, material):
    from light.production import save_director_decision

    saved = (run.get("director_decisions") or {}).get(APPROVED_SETUPS)
    if saved:
        return saved
    for attempt in range(SETUP_REPAIRS + 1):
        result = await shortcut_review.key_frames(ctx, plan, pairs, anchor, material)
        report = shortcut_review.KeyFrames.model_validate(result["report"])
        repairs = [(index, getattr(report, role)) for index, role in SETUP_ROLES.items()
                   if not getattr(report, role).ok]
        if not repairs:
            return await save_director_decision(ctx, prefix, REVEAL, APPROVED_SETUPS,
                                                {**result, "frames": [pairs[i] for i in SETUP_ROLES]})
        if attempt == SETUP_REPAIRS:
            detail = "；".join(f"第{index}镜：{item.reason}" for index, item in repairs)
            raise RemakeStopped(f"关键镜头首尾帧暂未通过，已保留生成素材。{detail}")

        async def repair(item):
            index, decision = item
            return index, await _frame_pair(
                ctx, prefix, plan, index, plan.shots[index - 1], anchor, material,
                correction=f"{attempt + 1}. {decision.correction}",
            )

        pairs.update(await _parallel(repairs, repair, concurrency=2))
    raise AssertionError("The setup check must resolve or stop")


def shot_request(prefix, index, shot, frames, take=1, *, correction="", visible_setup=""):
    first, last = frames
    refs = [vp.VideoInputRef(asset_id=first, role="first_frame")]
    if last:
        refs.append(vp.VideoInputRef(asset_id=last, role="last_frame"))
    speech = (f"只有{shot.speaker}在合适的动作点自然说出逐字台词一次，其他角色始终不说话。"
              "不添加语气词、额外对白或画外话音。" if shot.line else "无人说话、哼唱或旁白。")
    camera = f"机位与景别：{_on_set(shot.camera)}。" if shot.camera and not visible_setup else ""
    visible = ("画内只保留" + "、".join(shot.visible_roles) + "。" if shot.visible_roles else "")
    performer = f"下面的主动作只由{shot.performer}完成，其他演员保持自然静止或反应。" if shot.performer else ""
    motion = _on_set(correction or shot.action)
    if index == OPENING:
        motion = (
            f"{_opening_exchange(shot)}全镜专注画内交流，双脚原地。"
            f"仅保留以下安排中的画内对话、自然反应和已有道具小动作：{motion}"
        )
    boundary = "严格从首帧开拍自然变到尾帧。" if last else "基于首帧开始并连续拍完一个动作。"
    return vp.VideoGenerateArgs(
        action="submit", model=MODEL, resolution="768p", ratio="adaptive",
        duration=max(5, shot.seconds), generate_audio=True, watermark=False,
        idempotency_key=f"{prefix}:shot{index}:{REVIEW}" + (":retake:2" if take == 2 else ""), shot=index,
        input_assets=refs, spoken_line=shot.line or None, no_speech=not shot.line,
        prompt=("竖屏相机直出的连续实拍素材，单机位。"
                f"{boundary}{camera}{visible}{performer}"
                f"开拍画面：{_on_set(visible_setup or shot.frame)}。{motion}。{speech}"
                + ("先把句子说完整，最后留一点自然停顿。" if take == 2 and shot.line else "")
                + "保留实际发生的动作同期声和现场底噪。画面和音轨均无配乐、说明或旁白。"
                  "保持演员、道具和商品外观，无文字、字幕、标题和水印。"),
    )


def accepted_cut(shot, index, asset, meta, transcript):
    if shot.line and not speech_window(shot.line, transcript, float(meta.get("duration_sec") or 0)):
        lines = transcript.get("sentences") or []
        heard = lines[0].get("text", "") if len(lines) == 1 else ""
        # ASR may render a spoken particle or regional pronunciation differently.
        if not heard or SequenceMatcher(None, wan_edit._words(shot.line), wan_edit._words(heard)).ratio() < 0.84:
            raise RemakeStopped(f"第 {index} 镜未听清指定对白。")
    cut = soda_story.cut_for(shot, index, asset, meta, transcript)
    if shot.line and (not cut["cues"] or cut["duration_sec"] < 2.6):
        raise RemakeStopped(f"第 {index} 镜的剪点没有留足对白。")
    cut.update(mute_audio=not meta.get("has_audio"), review_version=REVIEW)
    return cut


async def _film(ctx, prefix, run, plan, index, frames, *, incoming=None, visible_setup=""):
    from light.production import save_director_decision
    from light.shot_editor import publish_request

    shot = plan.shots[index - 1]
    correction = ""
    for take in (1, 2):
        request = shot_request(prefix, index, shot, frames, take, correction=correction,
                               visible_setup=visible_setup)
        await publish_request(ctx, prefix, request)
        asset = await _job(ctx, request, kind="segment", label=f"剧情镜头 {index}", run=run)
        meta = await ensure_meta(asset, user_id=ctx.user_id)
        if not meta:
            raise RemakeStopped(f"第 {index} 镜仍在保存，已记录生成作业。")
        transcript = await soda_story._transcript(ctx, asset) if shot.line and meta.get("has_audio") else {}
        try:
            cut = accepted_cut(shot, index, asset, meta, transcript)
        except RemakeStopped:
            if take == 2 or not shot.line:
                raise
            continue
        if index in REVIEWED_SHOTS:
            decision_key = f"{REVIEW}:cut:{asset}"
            checked = (run.get("director_decisions") or {}).get(decision_key)
            if checked is None:
                checked = await shortcut_review.filmed_cut(
                    ctx, plan, index, cut, float(meta["duration_sec"]), frames[0], incoming,
                )
                checked = await save_director_decision(ctx, prefix, index, decision_key, checked)
            cut = checked["cut"]
            if cut is None:
                report = checked["report"]
                if take == 2:
                    raise RemakeStopped(f"第{index}镜补拍后仍需修正，原素材已保留。{report['reason']}")
                correction = report["correction"]
                continue
        return {**cut, "take": take, "first_reference": frames[0], "last_reference": frames[1]}


async def render(ctx, form, run, plan):
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_wan_edit

    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, REVIEW)
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    remaining = [index for index in range(1, len(plan.shots) + 1) if index not in cuts]
    if remaining:
        await remake_stage(ctx, prefix, "remake_frames")
        material = await soda_story._materials(task, form["asset_ids"]) if form["asset_ids"] else []
        anchor = await _anchor(task, prefix, plan)
        needs_setups = any(i in SETUP_ROLES for i in remaining)
        scheduled = set(remaining)
        approved = {}
        if needs_setups:
            if (run.get("director_decisions") or {}).get(APPROVED_SETUPS):
                scheduled.difference_update(SETUP_ROLES)
            else:
                scheduled.update(SETUP_ROLES)

        async def prepare(index):
            return index, await _frame_pair(task, prefix, plan, index, plan.shots[index - 1], anchor, material)

        frames = dict(await _parallel(sorted(scheduled), prepare, concurrency=4))
        if needs_setups:
            decision = await _confirm_setups(task, prefix, run, plan, frames, anchor, material)
            frames.update(zip(SETUP_ROLES, decision["frames"], strict=True))
            approved = {i: decision["report"][key]["visible_setup"] for i, key in SETUP_ROLES.items()}
        await remake_stage(ctx, prefix, "wan_shots")

        async def film(index):
            incoming = None
            if index in (OPENING, HANDOFF):
                following = frames.get(index + 1)
                incoming = following[0] if following else (cuts.get(index + 1) or {}).get("first_reference")
            cut = await _film(task, prefix, run, plan, index, frames[index],
                              incoming=incoming, visible_setup=approved.get(index, ""))
            await save_reference_cut(ctx, prefix, cut)
            cuts[index] = cut

        await _parallel(remaining, film, concurrency=4)

    ordered = [cuts[i] for i in range(1, len(plan.shots) + 1)]
    await remake_stage(ctx, prefix, "wan_edit")
    artwork = end_card(plan.headline, form["shop"])
    digest = hashlib.sha256(artwork).hexdigest()[:20]
    art = await _reference_asset(task, f"shortcut-lettering-{digest}", artwork, mime="image/png")
    duration = sum(cut["duration_sec"] for cut in ordered)
    reveal = sum(cut["duration_sec"] for cut in ordered[:REVEAL - 1])
    timeline = timeline_for(ordered, art, hero_shot=PRODUCT, music_start=reveal)
    key = f"{prefix}:final:{_digest(json.dumps(timeline, sort_keys=True))}"
    run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
    await remake_stage(ctx, prefix, "compose")
    asset = await _job(task, video_compose.VideoComposeArgs(
        action="submit", idempotency_key=key, timeline=timeline,
        filename=f"{plan.title}-{plan.product}.mp4",
    ), kind="compose", label="完整广告成片", run=run)
    job = await _existing_job(ctx, "compose", key)
    meta = await ensure_meta(asset, user_id=ctx.user_id)
    if (not job or not wan_story.is_complete_cut(run, job) or not meta or not meta.get("has_audio")
            or abs(float(meta.get("duration_sec") or 0) - duration) > 0.2):
        raise RemakeStopped("成片尚未通过镜头、时长和音轨核对。")
    if not await vp._attach_completed(job, ctx):
        raise RemakeStopped("合成完成后暂未能附到这条对话。")
    return asset, duration
