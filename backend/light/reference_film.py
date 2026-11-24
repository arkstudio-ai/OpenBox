"""Execute an Agent-authored short-shot film and review the actual takes before IMS."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import math
from contextlib import asynccontextmanager
from dataclasses import replace

import httpx
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select

from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light.video_language import for_text, speech_hint
from light import wan_edit, wan_story
from light.remake import (
    RemakeStopped,
    _digest,
    _existing_job,
    _job,
    _material_refs,
    _parallel,
    _reference_asset,
)
from light.wan_remake import _on_set, _still
from tool import video_compose
from tool import video_production as vp
from video.media_probe import ensure_meta

REVIEW_VERSION = "cast-v3"
STILL_STYLE = "精致写实商业广告，9:16竖屏单幅电影剧照，真实肤质摄影光线，无可读文字、无拼贴。"
# Keys 1–3 can already identify a paid job in an older session.
COVERAGE_TAKE = 4


def coverage_prompt(plan, shot):
    return (STILL_STYLE + "。按新机位只画下文列出的入镜主体，参考图用于识别身份与商品。"
            + f"新机位的首帧：{shot.frame}")


def submitted_first_frame(job, fallback):
    request = job.request_data or {}
    return next((asset for asset, role in zip(request.get("input_asset_ids") or [], request.get("roles") or [])
                 if role == "first_frame"), fallback)


def speech_window(line, transcript, duration):
    """Locate the intended line inside actual ASR, excluding ad-libbed prefix/suffix words."""
    if not line:
        return (
            ([], 0.0, duration)
            if not str(transcript.get("text") or "").strip()
            else None
        )
    chars = []
    for word in transcript.get("words") or transcript.get("sentences") or []:
        text = wan_edit._words(word["text"])
        a, b = word["from_sec"], word["to_sec"]
        for i, char in enumerate(text):
            chars.append(
                (
                    char,
                    a + (b - a) * i / len(text),
                    a + (b - a) * (i + 1) / len(text),
                    i == 0,
                    i == len(text) - 1,
                )
            )
    heard = "".join(c[0] for c in chars)
    wanted = wan_edit._words(line)
    start = heard.find(wanted)
    if not wanted or start < 0:
        return None
    stop = start + len(wanted)
    # Sub-word timing is interpolation, not measured evidence. Never cut a
    # spoken compound word in half just because its prefix matches the script.
    if not chars[start][3] or not chars[stop - 1][4]:
        return None
    a, b = chars[start][1], chars[stop - 1][2]
    left = chars[start - 1][2] + 0.05 if start else 0.0
    right = chars[stop][1] - 0.05 if stop < len(chars) else duration
    if left > a or right < b:
        return None
    return (
        [{"text": line, "from_sec": round(a, 3), "to_sec": round(b, 3)}],
        left,
        right,
    )


def _take_key(prefix, index, take):
    key = f"{prefix}:shot{index}"
    if take > 1:
        key += f":retake:{REVIEW_VERSION}"
    if take == COVERAGE_TAKE:
        key += ":coverage-v1"
    elif take > 2:
        key += f":take{take}"
    return key


def shot_request(prefix, index, shot, first, take=1, direction=None, *, language=None):
    speaker = direction.speaker if direction else shot.speaker
    action = direction.action if direction else shot.action
    spoken = (
        f"只有{speaker}说「{shot.line}」，其他人闭口。"
        if shot.line
        else "无人说话，无旁白。"
    )
    performance = (
        "开头半秒后自然开始对白，说完留表情反应；严格只说指定原话，不能添加其他话语。"
        "只有清晰现场原声，没有背景音乐、字幕或画面文字。"
        if shot.line
        else "全程闭口，只做指定的无声动作。不要张嘴交谈，不生成任何人声、背景音乐、字幕或画面文字。"
    )
    prompt = (
        "9:16写实电影广告。从所附首帧继续，一个实时镜头，不切换场景。保持脸、服装、光线、道具和机位。"
        + _on_set(action)
        + "。"
        + spoken
        + performance
        + speech_hint(language, shot.line)
    )
    return vp.VideoGenerateArgs(
        action="submit",
        idempotency_key=_take_key(prefix, index, take),
        shot=index,
        model=wan_story.MODEL,
        resolution=wan_story.RESOLUTION,
        ratio="9:16",
        duration=direction.seconds if direction else shot.seconds,
        generate_audio=bool(shot.line),
        watermark=False,
        spoken_line=shot.line or None,
        no_speech=not shot.line,
        input_assets=[vp.VideoInputRef(asset_id=first, role="first_frame")],
        prompt=prompt,
    )


async def review(
    ctx, plan, index, source, first_reference, reference_copy, anchor, direction=None, *, edit=None
):
    shot = plan.shots[index - 1]
    meta = await ensure_meta(source, user_id=ctx.user_id)
    if not meta or not meta.get("fps"):
        raise RemakeStopped("生成镜头的实际时长尚未核实，已保留镜头。")
    fps, duration = float(meta["fps"]), float(meta["duration_sec"])
    transcript, asr_id = {}, None
    if meta.get("has_audio"):
        language = for_text(shot.line, plan.language)
        suffix = f":{language}" if language != "zh" else ""
        result = await vp.execute_transcribe(
            vp.VideoTranscribeArgs(
                action="submit",
                asset_id=source,
                language=language, idempotency_key=f"reference-film-asr:{source}{suffix}",
            ),
            ctx,
        )
        if result.metadata.get("status") != "completed":
            raise RemakeStopped("暂未取得实际对白时间，已保留镜头，请重试读取。")
        asr_id = result.metadata["job_id"]
        async with get_db_session() as db:
            asr = await db.get(VideoJob, asr_id)
            transcript = (asr.result_data or {}).get("transcript") or {}
    async with get_db_session() as db:
        row = await db.get(FileAsset, source)
        filming_instruction = await db.scalar(select(VideoJob.prompt).where(
            VideoJob.user_id == ctx.user_id, VideoJob.session_id == ctx.session_id,
            VideoJob.kind == "segment", VideoJob.output_asset_id == source,
        ).limit(1))
        reference_urls = {}
        for asset_id in dict.fromkeys([first_reference, anchor]):
            reference = await db.get(FileAsset, asset_id)
            if (
                not reference
                or reference.user_id != ctx.user_id
                or reference.status != "ready"
                or reference.is_deleted
            ):
                raise RemakeStopped("角色核对所需的首帧或身份参考不可读取。")
            reference_urls[get_oss().presign_get(reference.oss_key)] = reference.id
    mute_audio = not meta.get("has_audio") or (not shot.line and shot.kind != "scene")
    window = (
        ([], 0.0, duration)
        if not shot.line and mute_audio
        else speech_window(shot.line, transcript, duration)
    )
    if not window:
        return None, {"reason": "实际对白与已确认台词不符，无法保留完整对白。", "heard": transcript.get("text", ""), "can_recut": False}
    cues, allowed_start, allowed_end = window
    min_frames = 1
    if cues:
        first, last = cues[0]["from_sec"], cues[-1]["to_sec"]
        starts = sorted(
            {
                math.ceil(allowed_start * fps),
                *(
                    max(
                        math.ceil(allowed_start * fps),
                        math.floor(max(0, first - offset) * fps),
                    )
                    for offset in (1.3, 0.8, 0.4, 0.15)
                ),
            }
        )
        ends = sorted(
            {
                math.floor(allowed_end * fps) - 1,
                *(
                    min(
                        math.floor(allowed_end * fps) - 1,
                        math.ceil((last + offset) * fps) - 1,
                    )
                    for offset in (0.15, 0.4, 0.7, 1.1)
                ),
            }
        )
        ends = [e for e in ends if (e + 1) / fps >= last]
    else:
        starts = [0, round(0.2 * fps)]
        ends = sorted(
            {
                max(1, math.floor((duration - offset) * fps) - 1)
                for offset in (0.08, 0.35, 0.7, 1.0)
            }
        )
        # The half-second samples are already in the visible review sheet.
        # Let the editor remove a defective passage on either side of a usable action.
        min_frames = math.ceil((1.5 if shot.kind == "product" else 1.0) * fps)
        last_frame = math.floor(duration * fps) - 1
        sampled = [round(t * fps / 2) for t in range(math.ceil(duration * 2))]
        starts = sorted({f for f in [*starts, *sampled] if 0 <= f <= last_frame - min_frames + 1})
        ends = sorted({f for f in [*ends, *sampled] if min_frames - 1 <= f <= last_frame})
    if edit and edit.first is not None:
        lo, hi = edit.first, edit.last
        if (lo < math.ceil(allowed_start * fps) or hi >= math.floor(allowed_end * fps)
                or hi - lo + 1 < min_frames or cues and (lo / fps > first or (hi + 1) / fps < last)):
            return None, {"reason": "指定剪点超出素材、截断对白或保留区间过短，请重新决定剪点。"}
        starts, ends = [lo], [hi]
    if not starts or not ends:
        return None, {"reason": "没有能保留完整指定对白且排除多余人声的剪点。", "can_recut": False}
    samples = sorted(
        {*starts, *ends, *(round(t * fps / 2) for t in range(math.ceil(duration * 2)))}
    )
    samples = [f for f in samples if 0 <= f < math.floor(duration * fps)]
    oss = get_oss()
    async with httpx.AsyncClient(timeout=30) as client:

        async def read_frame(frame):
            buf = await wan_edit.exact_frame(client, oss, row.oss_key, frame, fps, 270)
            return frame, Image.open(io.BytesIO(buf)).convert("RGB")

        frames = dict(await _parallel(samples, read_frame, concurrency=4))
    proof = await _reference_asset(
        ctx, f"reference-review-{source}-{_digest(','.join(map(str, samples)))}", wan_edit.sheet(frames)
    )
    async with get_db_session() as db:
        evidence_asset = await db.get(FileAsset, proof)
    decision, model = await wan_edit.decide(
        ctx,
        {
            "shot": index,
            "review_contract": REVIEW_VERSION,
            "expected_speaker": direction.speaker if direction else shot.speaker,
            "fps": fps,
            "title": plan.title,
            "product": plan.product,
            "story_action": shot.action,
            "action": direction.action if direction else shot.action,
            "filming_instruction": filming_instruction,
            "expected_first_frame": shot.frame,
            "cast_and_axis": plan.setting_and_cast,
            "scene": shot.scene,
            "kind": shot.kind,
            "next_shot": plan.shots[index].model_dump()
            if index < len(plan.shots)
            else None,
            "reference_copy": reference_copy,
            "actual_dialogue": transcript.get("text", ""),
            "discard_source_audio": mute_audio,
            "captions": cues,
            "desired_length_sec": round(27 / len(plan.shots), 2),
            "minimum_retained_frames": min_frames,
            "editing": "依实际故事决定接镜。只判断所选区间，剪掉有缺陷的头尾。核对人物视线、道具与动作连续性。",
            "director_edit": edit.instruction if edit else "",
        },
        oss.presign_get(evidence_asset.oss_key),
        proof,
        starts,
        ends,
        references=reference_urls,
    )
    if decision.retake:
        return None, {"reason": decision.reason + "。" + decision.continuity, "review_asset": proof,
                      "heard": transcript.get("text", ""), "fps": fps, "duration_sec": duration}
    return {
        "shot": index,
        "source": source,
        "first": decision.first,
        "last": decision.last,
        "fps": fps,
        "in_sec": round(decision.first / fps, 4),
        "duration_sec": round((decision.last + 1 - decision.first) / fps, 4),
        "review_asset": proof,
        "first_reference": first_reference,
        "editor_model": model,
        "reason": decision.reason,
        "continuity": decision.continuity,
        "cues": cues,
        "heard": transcript.get("text", ""),
        "asr_job": asr_id,
        "review_version": REVIEW_VERSION,
        "speaker_matches": decision.speaker_matches,
        "cast_matches": decision.cast_matches,
        "props_match": decision.props_match,
        "mute_audio": mute_audio,
        "direction": direction.model_dump() if direction else None,
        "filming_instruction": filming_instruction if direction is None else None,
    }, ""


def lettering(headline, shop, *, ink="#FFF8E9", outline=None):
    from light.remake_lettering import FOLDER, _cjk, end_card

    if not _cjk(headline) or len(headline.replace(" ", "")) != 4:
        return end_card(headline, shop, headline_ink=ink)
    canvas = Image.new("RGBA", (1080, 1920))
    draw = ImageDraw.Draw(canvas)

    def text(value, x, y, size, color=None):
        font = ImageFont.truetype(str(FOLDER / "ZhiMangXing-Regular.ttf"), size)
        box = draw.textbbox((0, 0), value, font=font)
        draw.text((x - box[0], y - box[1]), value, font=font, fill=color or ink,
                  stroke_width=3 if color is None and outline else 0, stroke_fill=outline)

    for ch, (x, y, size) in zip(
        headline.replace(" ", ""),
        [(750, 115, 215), (720, 300, 246), (495, 220, 202), (435, 420, 278)],
    ):
        text(ch, x, y, size)
    if shop:
        em = min(49, 330 // len(shop))
        draw.rounded_rectangle(
            (355, 290, 416, 320 + len(shop) * (em + 12)), radius=5, fill="#A42B25"
        )
        for i, ch in enumerate(shop):
            text(ch, 364, 306 + i * (em + 12), em, "#FFF8E9")
    buf = io.BytesIO()
    canvas.save(buf, "PNG")
    return buf.getvalue()


def timeline_for(cuts, art, *, music_start=None, hero_shot=None):
    indices = [c["shot"] for c in cuts]
    if len(cuts) < 5 or len(set(indices)) != len(indices) or min(indices) < 1:
        raise RemakeStopped("所有剧情镜头审片完成后才可以合成。")
    if hero_shot is not None and hero_shot not in indices:
        raise RemakeStopped("商品特写缺失，无法放置题字。")
    hero_shot = hero_shot or indices[-1]
    shots, captions, holds, at = [], [], [], 0.0
    for cut in cuts:
        start, length = cut["in_sec"], cut["duration_sec"]
        if length < 0.5:
            raise RemakeStopped("剪辑区间太短，无法交付完整剧情。")
        shots.append(
            {
                "asset": cut["source"],
                "in_sec": start,
                "duration_sec": cut.get("source_duration_sec", length),
                **({"speed": cut["speed"]} if cut.get("speed") else {}),
                "volume": 0
                if cut.get("mute_audio")
                else (0.85 if cut["cues"] else 0.3),
                "fit": "cover",
            }
        )
        if cut.get("hold_asset"):
            holds.append({"asset": cut["hold_asset"], "kind": "image", "x": 0, "y": 0, "width": 1, "height": 1,
                          "fit": "cover", "from_sec": round(at, 3), "to_sec": round(at + length, 3)})
        for cue in cut["cues"]:
            if cue["from_sec"] < start - 0.01 or cue["to_sec"] > start + length + 0.01:
                raise RemakeStopped("剪辑区间截断了对白。")
            captions.append(
                {
                    "text": cue["text"],
                    "from_sec": round(at + cue["from_sec"] - start, 3),
                    "to_sec": round(at + cue["to_sec"] - start, 3),
                }
            )
        if cut["shot"] == hero_shot:
            hero_at, hero_end = at, at + length
        at += length
    if at < 18:
        raise RemakeStopped("完整剧情不足18秒，请调整分镜，不能交付过短片段。")
    timeline = {
        "version": 1,
        "canvas": {"width": 1080, "height": 1920, "fps": 25},
        "shots": shots,
        "captions": captions,
        "caption_style": {
            "bottom_ratio": 0.12,
            "max_width": 0.88,
            "motion": {},
            "style": {
                "font": "AlibabaPuHuiTi",
                "size": 49,
                "color": "#FFFDF8",
                "outline": 2,
                "outline_color": "#282019",
            },
        },
        "overlays": [*holds,
            {
                "asset": art,
                "kind": "image",
                "x": 0,
                "y": 0,
                "width": 1,
                "height": 1,
                "fit": "contain",
                "from_sec": round(hero_at + 0.12, 3),
                "to_sec": round(hero_end, 3),
            }
        ],
    }
    if music_start is not None:
        timeline["audio"] = [{
            "library_id": "icepublic-1d3edda1fa6df3c45e7b785b54c90133",
            "from_sec": round(max(0, min(music_start, at - 1)), 3),
            "to_sec": round(at, 3), "in_sec": 0, "volume": 0.12,
            "fade_in_sec": 0.6, "fade_out_sec": 0.8,
            "duck_under_captions": True, "duck_volume": 0.025,
        }]
    return timeline


async def paid_takes(ctx, run, index):
    """Read this session's paid sources, their actual media lengths and numbered review boards."""
    prefix = run["job_prefix"]
    async with get_db_session() as db:
        jobs = (await db.scalars(select(VideoJob).where(
            VideoJob.user_id == ctx.user_id, VideoJob.session_id == ctx.session_id,
            VideoJob.kind == "segment", VideoJob.status == "completed",
            VideoJob.idempotency_key.startswith(prefix + ":", autoescape=True),
        ).order_by(VideoJob.created_at))).all()
        result = {}
        for job in jobs:
            asset = job.output_asset_id
            if not asset or (job.request_data or {}).get("shot") != index:
                continue
            row = await db.get(FileAsset, asset)
            if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready":
                continue
            first = submitted_first_frame(job, None)
            if not first:
                continue
            board = await db.scalar(select(FileAsset.id).where(
                FileAsset.user_id == ctx.user_id, FileAsset.session_id == ctx.session_id, FileAsset.status == "ready",
                FileAsset.is_deleted.is_(False), FileAsset.name.startswith(f"reference-review-{asset}-", autoescape=True)
            ).order_by(FileAsset.created_at.desc()).limit(1))
            item = {"source": asset, "first_reference": first,
                    "source_media_seconds": (row.meta or {}).get("duration_sec") or job.request_data.get("duration")}
            if board:
                item["review_asset"] = board
            result[asset] = item
    return result


async def time_cut(ctx, shot, selected, choice):
    """Retime a verified silent interval or hold a real product frame under the end title."""
    if shot.line or selected.get("cues") or selected.get("first") is None or selected.get("last") is None:
        raise ValueError("只能调整已审过的无对白实拍区间。")
    source_length = selected.get("source_duration_sec", selected["duration_sec"])
    output = choice.output_seconds
    rate = round(source_length / output, 3)
    if not 0.25 <= rate <= 4 or choice.action == "retime" and not 0.56 <= rate <= 1.6:
        raise ValueError("请根据已选实拍区间调整停留时长；轻微变速须保留自然动作。")
    revised = {**selected, "duration_sec": round(source_length/rate, 4), "source_duration_sec": source_length,
               "speed": rate, "mute_audio": True, "director_action": choice.action, "reason": choice.reason}
    if choice.action == "hold":
        frame = choice.first if choice.first is not None else (selected["first"] + selected["last"]) // 2
        if shot.kind != "product" or not selected["first"] <= frame <= selected["last"]:
            raise ValueError("片尾定帧必须来自该商品镜头已审过区间内的一帧。")
        async with get_db_session() as db:
            row = await db.get(FileAsset, selected["source"])
            if not row or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready":
                raise RemakeStopped("定帧所需的原素材不可读取。")
            key = row.oss_key
        async with httpx.AsyncClient(timeout=30) as client:
            data = await wan_edit.exact_frame(client, get_oss(), key, frame, selected["fps"], 1080)
        revised["hold_frame"] = frame
        revised["hold_asset"] = await _reference_asset(ctx, f"ad-hold-{selected['source']}-{frame}", data)
    return revised


async def _repair_take(ctx, form, run, plan, materials, anchor, index, current, problem, cuts, phase,
                       *, can_omit, paid_limit=2):
    from light import reference_quality as quality
    from light.production import save_director_decision
    from light.reference_direction import Edit, UnusableFrame, apply_coverage, choose_edit, resolve
    from light.remake_case import reference_copy

    prefix = run["job_prefix"]
    copy = reference_copy(form)
    setup = quality.matching_setup(plan, cuts, index)
    neighbors = sorted((c for c in cuts if c["shot"] != index and c.get("review_asset")),
                       key=lambda c: abs(c["shot"] - index))[:2]
    def take_info(cut):
        return {k: cut[k] for k in ("source", "first_reference", "review_asset", "coverage", "heard", "fps",
                                    "in_sec", "duration_sec", "source_duration_sec", "first", "last", "filming_instruction",
                                    "shot", "direction", "review_version", "asr_job", "cues", "mute_audio", "reason",
                                    "speaker_matches","cast_matches","props_match","continuity","editor_model",
                                    "speed","hold_asset","hold_frame","director_action","take") if k in cut}
    takes = await paid_takes(ctx, run, index)
    for cut in [*(run.get("wan_cuts") or []), *((run.get("wan_quality") or {}).get("cuts") or []), current]:
        if cut.get("shot", index) == index and cut.get("source") and not cut.get("omit_reason"):
            source = cut["source"]
            takes[source] = {**takes.get(source, {}), **take_info(cut)}
    notes, spent, failed_setups, discarded_audio, tried_edits = [], 0, 0, set(), set()
    saved = run.setdefault("director_decisions", {})
    for turn in range(1, 7):
        key = f"{phase}-s{index}-d{turn}"
        problem = {"reason": problem} if isinstance(problem, str) else problem
        current.update({k: v for k, v in problem.items() if k in {"review_asset", "heard", "fps", "duration_sec"}})
        takes[current["source"]] = {**takes.get(current["source"], {}), **take_info(current)}
        if problem.get("can_recut") is False:
            discarded_audio.add(current["source"])
        if key not in saved:
            entry = await choose_edit(ctx, plan, index, copy, anchor, materials, take_info(current), takes, notes, setup,
                                      [{**take_info(c), "shot": c["shot"]} for c in neighbors], problem,
                                      can_omit=can_omit, can_film=spent < paid_limit and failed_setups < 3,
                                      can_recut=bool(set(takes) - discarded_audio))
            record = {**entry, "shot": index, "source": current["source"], "problem": problem,
                      "setup": ({"shot": setup["shot"], "first_reference": setup["first_reference"]}
                                if setup and entry["report"].get("use_matching_setup") else None)}
            saved[key] = await save_director_decision(ctx, prefix, index, key, record)
        record = saved[key]
        decision = Edit.model_validate(record["report"])
        notes.append({"source": current["source"], "problem": problem["reason"], **decision.model_dump(exclude_none=True)})
        if decision.action == "omit" and can_omit:
            return {**current, "shot": index, "omit_reason": decision.reason, "omit_evidence": record["evidence"],
                    "director_action": "omit", "review_version": REVIEW_VERSION}
        if decision.action in {"retime", "hold"}:
            selected = takes[decision.take or current["source"]]
            try:
                revised = await time_cut(ctx, plan.shots[index-1], selected, decision)
            except ValueError as exc:
                problem = {"reason": str(exc)}
                continue
            return {**revised, "shot": index, "review_version": REVIEW_VERSION}
        if decision.action == "recut":
            source = decision.take or current["source"]
            signature = (source, decision.first, decision.last, decision.instruction)
            if source in discarded_audio or signature in tried_edits:
                problem = {"reason": "这个区间已试过或整条素材都缺失对白，请选别的剪法或拍法。"}
                continue
            tried_edits.add(signature)
            selected = takes[source]
            first, coverage = selected["first_reference"], selected.get("coverage")
            active = apply_coverage(plan, index, coverage) if coverage else plan
            cut, problem = await review(ctx, active, index, source, first, copy, anchor, edit=decision)
            current = dict(selected)
        elif decision.action in {"reshoot", "reframe"} and spent < paid_limit and failed_setups < 3:
            selected = takes.get(decision.take) or current
            coverage = (decision.coverage.model_dump() if decision.action == "reframe" else selected.get("coverage"))
            active = apply_coverage(plan, index, coverage) if coverage else plan
            chosen = active.shots[index - 1]
            matched = record.get("setup")
            first = matched["first_reference"] if matched else selected["first_reference"]
            subjects = ([anchor] if chosen.kind == "scene" else []) + (materials if chosen.product_in_first_frame is not False else [])
            prompt = coverage_prompt(active, chosen)
            job_key = f"{prefix}:director:{key}"
            prior = await _existing_job(ctx, "segment", job_key)
            direction = None
            if prior:
                first = submitted_first_frame(prior, first)
            else:
                if decision.action == "reframe":
                    refs = ([first] if matched else []) + subjects
                    first = await _still(ctx, prefix, key + "-setup", prompt, list(dict.fromkeys(refs))[:4])

                async def regenerate(number, image, instruction, mode="edit"):
                    # Only a local edit uses the failed composition. A new camera starts from identity / product.
                    refs = subjects if mode == "reframe" else [image, *subjects]
                    return await _still(ctx, prefix, f"{key}-image-{number}", STILL_STYLE + "。" + instruction,
                                        list(dict.fromkeys(refs))[:4])
                try:
                    first, direction = await resolve(ctx, active, index, first, anchor, regenerate, decision.instruction,
                                                     max_repairs=1, reference_copy=copy)
                except UnusableFrame as exc:
                    failed_setups += 1
                    problem = {"reason": str(exc) + "。本次机位没有拍成，请重新选择素材、拍法或剪法。",
                               "rejected_frame": exc.frame}
                    continue
                if getattr(direction, "coverage", None):
                    coverage = direction.coverage.model_dump()
                    active = apply_coverage(active, index, coverage)
                    chosen = active.shots[index - 1]
            spent += 1
            request = shot_request(prefix, index, chosen, first, direction=direction, language=plan.language).model_copy(
                update={"idempotency_key": job_key})
            source = await _job(ctx, request, kind="segment", label=f"导演补拍镜头 {index}", run=run)
            current = {"shot": index, "source": source, "first_reference": first}
            if coverage:
                current["coverage"] = coverage
            cut, problem = await review(ctx, active, index, source, first, copy, anchor, direction)
        else:
            raise RemakeStopped("保存的导演决定已超出本轮可用的执行范围。")
        if cut:
            result = {**cut, "take": 1 + spent, "director_action": decision.action}
            if coverage:
                result["coverage"] = coverage
            if record.get("setup"):
                result["setup_reference"] = {"shot": record["setup"]["shot"], "asset": record["setup"]["first_reference"]}
            return result
    reason = problem if isinstance(problem, str) else problem["reason"]
    raise RemakeStopped(f"第{index}镜在本轮剪辑预算内仍未通过：{reason[:180]}。决定和素材已保存。")


async def _reviewed_takes(ctx, form, run, plan, materials, anchor):
    from light.production import remake_stage, save_reference_cut
    from light.reference_direction import UnusableFrame, apply_coverage, resolve
    from light.remake_case import reference_copy

    prefix = run["job_prefix"]
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    references, directions = {}, {}
    images = asyncio.Semaphore(2)
    filming = asyncio.Semaphore(4)
    editing = asyncio.Semaphore(2)
    progress_lock = asyncio.Lock()
    failure = None

    def check_running():
        if failure is not None:
            raise RemakeStopped("制作已暂停，已完成的镜头可以接着使用。") from failure

    @asynccontextmanager
    async def slot(permits):
        nonlocal failure
        async with permits:
            check_running()
            try:
                yield
            except (Exception, asyncio.CancelledError) as exc:
                if failure is None:
                    failure = exc
                raise

    async def make_frame(index, previous):
        # Reuse the establishing frame for the location. Other shots can start together.
        prior_frame = await previous if previous is not None else None
        shot = plan.shots[index - 1]
        cast = (
            plan.setting_and_cast
            if shot.kind == "scene"
            else "只拍本镜指定的商品或剧情道具与明确的手部动作，不出现人物脸或身体；本镜没要求商品时不能添加商品。"
        )
        refs = (
            ([anchor] if shot.kind == "scene" else [])
            + ([prior_frame] if prior_frame else [])
            + (materials if shot.product_in_first_frame is not False else [])
        )
        prompt = (
            STILL_STYLE
            + ("。按身份参考辨认画内演员。" if shot.kind == "scene" else cast)
            + f"。拍摄地点：{shot.scene}。只拍动作开始前的这个瞬间：{shot.frame}。"
            + ("商品对照所附原图，" + plan.appearance if shot.product_in_first_frame is not False else "")
        )
        async with slot(images):
            first = await _still(ctx, prefix, f"frame-v2-{index}", prompt, list(dict.fromkeys(refs))[:4])
        from light.shot_editor import publish_request
        await publish_request(ctx, prefix, shot_request(prefix, index, shot, first, language=plan.language))
        references[index] = (first, prompt, refs)
        return first

    async def direct(index):
        check_running()
        first, prompt, refs = references[index]
        shot = plan.shots[index - 1]
        subjects = ([anchor] if shot.kind == "scene" else []) + (materials if shot.product_in_first_frame is not False else [])
        async def regenerate(attempt, current, correction, mode="edit"):
            name = f"frame-{REVIEW_VERSION}-{index}-repair-first-{attempt}-{mode}"
            sources = subjects if mode == "reframe" else [current, *subjects]
            instruction = "从身份与场景参考重新构图。" if mode == "reframe" else "局部修正所附第一张首帧。"
            async with slot(images):
                return await _still(ctx, prefix, name, instruction + STILL_STYLE + "。本轮首帧：" + correction,
                                    list(dict.fromkeys(sources))[:4])
        first, direction = await resolve(ctx, plan, index, first, anchor, regenerate, "",
                                         reference_copy=reference_copy(form))
        references[index] = (first, prompt, refs)
        directions[index] = direction
        return first, direction

    async def film(index):
        # Older paid takes are reviewed again; only failed takes are reshot.
        # New submissions always get visually grounded names and a checked first frame.
        prior = await _existing_job(ctx, "segment", _take_key(prefix, index, 1))
        first, direction = references[index][0], None
        if prior is None:
            first, direction = await direct(index)
        else:
            first = submitted_first_frame(prior, first)
            _, prompt, refs = references[index]
            references[index] = (first, prompt, refs)
            directions.pop(index, None)
        request = shot_request(prefix, index, plan.shots[index - 1], first, direction=direction, language=plan.language)
        from light.shot_editor import publish_request
        await publish_request(ctx, prefix, request)
        check_running()
        return await _job(
            ctx, request, kind="segment", label=f"剧情镜头 {index}", run=run
        )

    remaining = [i for i in range(1, len(plan.shots) + 1) if i not in cuts]
    pending_films = set(remaining)
    filming_started = False

    async def produce(index):
        nonlocal filming_started
        await frames[index]
        rejected_frame = None
        async with slot(filming):
            async with progress_lock:
                if not filming_started:
                    await remake_stage(ctx, prefix, "wan_shots")
                    filming_started = True
            try:
                asset = await film(index)
            except UnusableFrame as exc:
                rejected_frame = exc
                asset = exc.frame
            pending_films.remove(index)
            if not pending_films:
                async with progress_lock:
                    await remake_stage(ctx, prefix, "wan_edit")

        active = plan
        framing = getattr(directions.get(index), "coverage", None)
        if framing:
            active = apply_coverage(plan, index, framing.model_dump())
        first = rejected_frame.frame if rejected_frame else references[index][0]
        if rejected_frame:
            cut, problem = None, {"reason": str(rejected_frame), "can_recut": False, "rejected_frame": first}
        else:
            async with slot(editing):
                cut, problem = await review(ctx, active, index, asset, first, reference_copy(form), anchor, directions.get(index))
        if cut:
            cut = {**cut, "take": 1, **({"coverage": framing.model_dump()} if framing else {})}
        else:
            shot = active.shots[index - 1]
            current = {"shot": index, "source": asset, "first_reference": first,
                       **({"coverage": framing.model_dump()} if framing else {})}
            async with slot(filming):
                cut = await _repair_take(ctx, form, run, active, materials, anchor, index, current,
                                         problem, list(cuts.values()), "shoot",
                                         can_omit=shot.can_omit and len(plan.shots) > 5)
        async with progress_lock:
            await save_reference_cut(ctx, prefix, cut)
        cuts[index] = cut

    if not remaining:
        await remake_stage(ctx, prefix, "wan_edit")
    frames, scene_anchors = {}, {}
    for index, shot in enumerate(plan.shots, 1):
        scene_key = (shot.scene, shot.product_in_first_frame)
        frames[index] = asyncio.create_task(make_frame(index, scene_anchors.get(scene_key)))
        if shot.kind == "scene":
            scene_anchors.setdefault(scene_key, frames[index])
    # Drain any submitted work on a shot failure; only the unfinished shots are needed on retry.
    results = await asyncio.gather(*frames.values(), *(produce(i) for i in remaining), return_exceptions=True)
    if failure is not None:
        raise failure
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return references, [cuts[i] for i in range(1, len(plan.shots) + 1)]


async def render(ctx, form, run, plan):
    from light.production import begin_reference_edit, remake_stage, save_wan_edit, save_reference_quality
    from light.reference_direction import apply_coverage
    from light import reference_quality as quality

    prefix = run["job_prefix"]
    segment_ctx = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, REVIEW_VERSION)
    await remake_stage(ctx, prefix, "remake_frames")
    materials = await _material_refs(ctx, form["asset_ids"])
    if not materials:
        raise RemakeStopped("需要一份可读取的商品参考素材。")
    anchor = await _still(ctx, prefix, "identity-v2", STILL_STYLE + "。仅作为本片身份参考：" + plan.identity_frame, [])
    _, cuts = await _reviewed_takes(segment_ctx, form, run, plan, materials, anchor)
    for cut in cuts:
        if cut.get("coverage") and not cut.get("omit_reason"):
            plan = apply_coverage(plan, cut["shot"], cut["coverage"])
    run = {**run, "wan_cuts": cuts}

    async def save_quality(state):
        nonlocal run
        run = await save_reference_quality(ctx, prefix, state)

    async def quality_repair(index, feedback, attempt, *, existing_only=False):
        current_cuts = run["wan_quality"]["cuts"]
        selected = next(c for c in current_cuts if c["shot"] == index)
        alternatives = [c for c in current_cuts if c["shot"] != index and not c.get("omit_reason")]
        can_omit = (plan.shots[index - 1].can_omit and len(alternatives) >= 5
                    and sum(c["duration_sec"] for c in alternatives) >= 18)
        return await _repair_take(segment_ctx, form, run, plan, materials, anchor, index, dict(selected),
                                  {"reason": feedback}, current_cuts, f"final-{attempt}", can_omit=can_omit, paid_limit=0 if existing_only else 2)

    verdict = await quality.finish(segment_ctx, plan, cuts, materials, anchor,
                                   run.get("wan_quality"), save_quality, quality_repair, reference_copy=form.get("reference_copy", ""))
    delivery = quality.delivery_cuts(run)
    if not delivery:
        raise RemakeStopped("自动审片尚未确认完整可交付的镜头选择。")
    await remake_stage(ctx, prefix, "compose")
    style = await quality.title_style(segment_ctx, delivery[-1], verdict)
    verdict["lettering"] = style
    await save_quality(verdict)
    art = lettering(plan.headline, form["shop"], ink=style["ink"], outline=style["outline"])
    art_id = await _reference_asset(
        ctx,
        "reference-lettering-" + hashlib.sha256(art).hexdigest()[:20],
        art,
        mime="image/png",
    )
    reveal = next((i for i, c in enumerate(delivery)
                   if plan.shots[c["shot"] - 1].kind == "detail"
                   and verdict["products"][c["source"]]["report"]["subject"] == "product"), len(delivery) - 1)
    music_start = min(sum(c["duration_sec"] for c in delivery[:reveal]), sum(c["duration_sec"] for c in delivery) * 0.55)
    timeline = timeline_for(delivery, art_id, music_start=music_start)
    key = f"{prefix}:final:{_digest(json.dumps(timeline, sort_keys=True))}"
    run = await save_wan_edit(ctx, prefix, cuts=cuts, final_key=key)
    output = await _job(
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
                VideoJob.output_asset_id == output,
            )
        )
    meta = await ensure_meta(output, user_id=ctx.user_id)
    expected = sum(c["duration_sec"] for c in delivery)
    if (
        not job
        or not wan_story.is_complete_cut(run, job, require_delivery_review=False)
        or not meta
        or abs(float(meta.get("duration_sec") or 0) - expected) > 0.15
    ):
        raise RemakeStopped(
            "合成结果尚未通过完整镜头与实际时长核对，不能交付片尾片段。"
        )
    if (verdict.get("final") or {}).get("output") != output:
        final = await quality.final(segment_ctx, plan, output, meta, delivery, form["shop"], materials)
        suffix = f":{plan.language}" if plan.language != "zh" else ""
        transcript = await vp.execute_transcribe(vp.VideoTranscribeArgs(
            action="submit", asset_id=output, language=plan.language,
            idempotency_key=f"ad-delivery-asr:{output}{suffix}"), segment_ctx)
        if transcript.metadata.get("status") != "completed":
            raise RemakeStopped("成片对白核对尚未完成，已保留同一份合成视频。")
        async with get_db_session() as db:
            asr = await db.get(VideoJob, transcript.metadata["job_id"])
            actual = ((asr.result_data or {}).get("transcript") or {}).get("text", "")
        from difflib import SequenceMatcher

        wanted = "".join(cue["text"] for c in delivery for cue in c.get("cues") or [])
        similarity = SequenceMatcher(None, wan_edit._words(wanted), wan_edit._words(actual), autojunk=False).ratio()
        verdict["final"] = {**final, "output": output, "asr_job": asr.id, "heard": actual,
                            "dialogue_similarity": similarity,
                            "dialogue_matches": bool(meta.get("has_audio")) and similarity >= 0.95}
        await save_quality(verdict)
    if not wan_story.is_complete_cut(run, job):
        raise RemakeStopped("成片终审未通过：" + (verdict.get("final") or {}).get("report", {}).get("reason", "实际对白不完整"))
    if not await vp._attach_completed(job, ctx):
        raise RemakeStopped("完整成片已合成，待取回同一作业并附到对话。")
    return output, expected
