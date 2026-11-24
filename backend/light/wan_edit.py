"""Read actual Wan frames and dialogue before the Agent commits a cut or a handoff."""

from __future__ import annotations

import asyncio
import io
import json
import math
from difflib import SequenceMatcher

import httpx
from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageStat
from pydantic import BaseModel, Field

from core.oss import get_oss
from light.remake import RemakeStopped, _check, _reference_asset
from tool import video_production as vp
from tool.footage_analyze import parse_picture, snapshot_url
from video.media_probe import ensure_meta
from video import vision_contract
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob


class Decision(BaseModel):
    first: int
    last: int
    reason: str = Field(min_length=1, max_length=350)
    continuity: str = Field(min_length=1, max_length=350)
    retake: bool = False
    speaker_matches: bool | None = None
    cast_matches: bool | None = None
    props_match: bool | None = None


def _words(text):
    from unicodedata import normalize

    return "".join(ch for ch in normalize("NFC", text) if ch.isalnum()).lower()


def captions(lines, transcript):
    """Use ASR timings, including word boundaries when two speakers share one sentence."""
    sentences = transcript.get("sentences") or []
    words = transcript.get("words") or sentences
    chars = []
    for word in words:
        text = _words(word["text"])
        a, b = word["from_sec"], word["to_sec"]
        if b > a and text:
            for i, ch in enumerate(text):
                chars.append((ch, a + i * (b - a) / len(text), a + (i + 1) * (b - a) / len(text)))
    if not chars or not lines:
        return []
    heard = "".join(ch for ch, _, _ in chars)
    wanted = [_words(line.text) for line in lines]
    match = SequenceMatcher(None, "".join(wanted), heard, autojunk=False)
    if match.ratio() < 0.62:
        return []
    aligned = {i + k: j + k for i, j, n in match.get_matching_blocks() for k in range(n)}
    locations = list(aligned.values())
    if not locations or min(locations) >= 4 or len(chars) - 1 - max(locations) >= 4:
        return []
    offset, result = 0, []
    for line, phrase in zip(lines, wanted):
        positions = [aligned[i] for i in range(offset, offset + len(phrase)) if i in aligned]
        if not positions or len(positions) < len(phrase) * 0.5:
            return []
        start, end = chars[positions[0]][1], chars[positions[-1]][2]
        result.append({"text": line.text, "from_sec": round(start, 3), "to_sec": round(end, 3)})
        offset += len(phrase)
    # Keep lead/trailing interjections covered, provided no other speaker intervenes.
    result[0]["from_sec"] = round(chars[0][1], 3)
    result[-1]["to_sec"] = round(chars[-1][2], 3)
    return result


def candidates(index, fps, duration, cues):
    last = math.floor(duration * fps + 0.01) - 1
    leads = (0, 1 / fps, 0.1, 0.17) if index > 1 else (0, 0.25, 0.5, 0.75)
    starts = sorted({round(t * fps) for t in leads})
    if cues:
        starts = [f for f in starts if f / fps <= cues[0]["from_sec"] + 0.001] or [0]
    distances = (1.0, 0.65, 0.33, 0.1, 0.0) if index == 3 else (2.0, 1.35, 0.85, 0.5, 0.25, 0.1, 0.0)
    ends = sorted({last - round(s * fps) for s in distances})
    if cues:
        ends = [f for f in ends if (f + 1) / fps >= cues[-1]["to_sec"] - 0.002]
    if not ends:
        raise RemakeStopped("对白延伸到可用画面之外，无法保留完整台词。")
    return starts, ends


def dialogue_gaps(cues, transcript):
    """Find separately voiced words between two approved lines, leaving ordinary room tone untouched."""
    omissions = []
    words = transcript.get("words") or []
    for left, right in zip(cues, cues[1:]):
        inside = [
            w
            for w in words
            if w["from_sec"] >= left["to_sec"] - 0.002 and w["to_sec"] <= right["from_sec"] + 0.002
        ]
        if len(_words("".join(w["text"] for w in inside))) >= 3:
            omissions.append(
                (
                    max(left["to_sec"], inside[0]["from_sec"] - 0.1),
                    min(right["from_sec"], inside[-1]["to_sec"] + 0.12),
                )
            )
    return omissions


async def audio_edits(ctx, cuts):
    """Read the original ASR jobs on resume as well as on the first composition."""
    result = []
    async with get_db_session() as db:
        for cut in cuts:
            if len(cut["cues"]) < 2:
                result.append(cut)
                continue
            job = await db.get(VideoJob, cut["asr_job"])
            if not job or job.user_id != ctx.user_id or job.session_id != ctx.session_id:
                raise RemakeStopped("无法核对此镜头的对白时间。")
            transcript = (job.result_data or {}).get("transcript") or {}
            result.append({**cut, "omit_speech": dialogue_gaps(cut["cues"], transcript)})
    return result


def _distance(a, b):
    size = (36, 64)
    return ImageStat.Stat(
        ImageChops.difference(a.convert("L").resize(size), b.convert("L").resize(size))
    ).mean[0]


def matched_starts(starts, frames, previous):
    distances = {i: _distance(previous, frames[i]) for i in starts}
    closest = min(distances.values())
    return [i for i in starts if distances[i] <= min(30, closest + 4)], distances


def held_product_ends(ends, frames, previous):
    # Small steam and focus changes are expected. A sustained pullback to actors breaks a held product shot.
    changed = next((i for i in sorted(frames) if _distance(previous, frames[i]) > 36), None)
    return [i for i in ends if changed is None or i < changed]


def product_backdrop(reference, frames):
    """Keep a changing advertising background steady while the real food and steam remain in motion."""

    def upper(im):
        return im.crop((0, 0, im.width, round(im.height * 0.32)))

    original = upper(reference)
    if all(_distance(original, upper(im)) < 18 for im in frames.values()):
        return None
    h = reference.height
    alpha = Image.new("L", (1, h))
    alpha.putdata([round(255 * min(1, max(0, (0.40 - y / h) / 0.08))) for y in range(h)])
    overlay = reference.convert("RGBA")
    overlay.putalpha(alpha.resize(reference.size))
    return overlay


def continuous_ends(ends, frames):
    # A forced endpoint may replace the scene for just the final few frames. Never hand that jump to Wan.
    for i in range(2, len(ends)):
        delta = _distance(frames[ends[i - 1]], frames[ends[i]])
        previous = _distance(frames[ends[i - 2]], frames[ends[i - 1]])
        if delta > max(24, previous * 3.8) and ends[-1] - ends[i - 1] <= 12:
            return ends[:i]
    return ends


async def exact_frame(client, oss, key, frame, fps, width):
    # OSS default snapshot mode decodes the requested timestamp. The thumbnail mode returns an earlier keyframe.
    at = (frame + 0.05) / fps
    url = snapshot_url(oss, key, at, width=width, exact=True)
    for attempt in range(2):
        try:
            response = await client.get(url)
            response.raise_for_status()
            if not response.headers.get("content-type", "").startswith("image/"):
                raise RemakeStopped("视频精确截帧没有返回图片。")
            return response.content
        except httpx.HTTPError:
            if attempt:
                raise RemakeStopped("暂时无法读取实际视频画面，请重试使用已生成的镜头。") from None
            await asyncio.sleep(0.5)


def sheet(frames):
    thumb = (150, 266)
    cols = min(5, len(frames))
    rows = math.ceil(len(frames) / cols)
    image = Image.new("RGB", (cols * thumb[0], rows * (thumb[1] + 27)), "#252824")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=18)
    for i, (frame, im) in enumerate(sorted(frames.items())):
        x = i % cols * thumb[0]
        y = i // cols * (thumb[1] + 27)
        image.paste(im.resize(thumb, Image.Resampling.LANCZOS), (x, y))
        draw.text((x + 3, y + thumb[1] + 2), str(frame), fill="white", font=font)
    data = io.BytesIO()
    image.save(data, "JPEG", quality=86)
    return data.getvalue()


async def decide(ctx, evidence, url, asset_id, starts, ends, *, references=None):
    import litellm
    from agent.llm import _get_provider_kwargs
    from agent.trajectory import capture_service_dispatch, service_scope
    from core.config import get_config

    model = get_config().video_analysis.model
    guide = (
        "你是这条广告的剪辑师。图中下方数字为原始帧号，结合实际画面和已识别对白选入点 first、最后保留帧 last。"
        "first 和 last 必须分别从候选整数中选。保留完整对话、动作和反应；选择人物、物品、背景和动作自然且接得上的帧。"
        "留足商品特写时间。continuity 用中文描述选中尾帧实际谁在何处、手和商品在哪里，以及下一镜如何延续。"
        "reason 简述剪点原因。若可见的明显穿帮无法通过选点剪除，例如锅凭空多出一份、硬切到另一场景、"
        "后期浮字已烙入剧情画面，填 retake=true。真实环境招牌、商品包装和采样图下方帧号属于正常。"
        "按真实镜头选择，参考文案仅说明创意，不作为台词或必须逐字呈现的内容。"
        "只审选定区间内的动作和连续性，入点与出点外的问题可以剪除。"
        "如给出 minimum_retained_frames，保留帧数 last-first+1 至少达到此数。"
        "调用 choose_cut 提交 first、last、reason、continuity、retake；如果不能调用函数，只返回同一结构的 JSON。\n"
    )
    data = {**evidence, "first_candidates": starts, "last_candidates": ends}
    check_cast = evidence.get("review_contract") == "cast-v3"
    if check_cast:
        guide += (
            "本次必须分别判断 speaker_matches、cast_matches、props_match。对白时间里真正持续张口说话的人，"
            "必须是expected_speaker及角色设定对应的演员；不能只核对ASR文字就放行。其他演员明显抢台词则speaker_matches=false。"
            "有对白却看不清指定说话人的脸，也不能证明通过。cast_matches检查实际动作的执行人和衣服是否符合剧情，"
            "核对本镜实际人物与动作。props_match检查所选区间内所有关键道具的前后状态，"
            "不能只因开头或末尾看起来正常就忽略中间凭空出现的道具。任一项失败必须retake=true。"
            "后附图片分别是该镜首帧与身份参考，不是额外的候选视频帧。\n"
        )
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": guide + json.dumps(data, ensure_ascii=False)},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    for reference_url in references or {}:
        messages[0]["content"].append({"type": "image_url", "image_url": {"url": reference_url}})
    error = ""
    schema = Decision.model_json_schema()
    schema["properties"]["first"]["enum"] = starts
    schema["properties"]["last"]["enum"] = ends
    if check_cast:
        for field in ("speaker_matches", "cast_matches", "props_match"):
            schema["properties"][field] = {"type": "boolean"}
            schema["required"].append(field)
    function = {
        "name": "choose_cut",
        "description": "Commit the edit after looking at the actual take. Frame numbers must be supplied from the two candidate lists.",
        "parameters": schema,
    }
    for _ in range(3):
        await _check(ctx)
        try:
            options = dict(
                model=model,
                messages=messages,
                temperature=0.15,
                max_tokens=6200,
                timeout=75,
                tools=[{"type": "function", "function": function}],
                **vision_contract.request_options(model, "choose_cut", _get_provider_kwargs(model)),
            )
            async with service_scope(ctx, asset_urls={url: asset_id, **(references or {})}):
                async with capture_service_dispatch(
                    purpose="video_analysis",
                    provider="agent_vision",
                    model=model,
                    operation="choose_remake_cut",
                    profile="wan_edit",
                    body={
                        "messages": messages,
                        "tools": options["tools"],
                        "tool_choice": options["tool_choice"],
                        "extra_body": options.get("extra_body"),
                    },
                ) as trace:
                    answer = await asyncio.wait_for(litellm.acompletion(**options), 80)
                    content = vision_contract.output(answer, "choose_cut")
                    await trace.chunk(vision_contract.response_evidence(answer, content))
            selected = Decision.model_validate(parse_picture(content))
            if selected.first not in starts or selected.last not in ends:
                raise ValueError("选了候选以外的帧号")
            if selected.last - selected.first + 1 < evidence.get("minimum_retained_frames", 1):
                raise ValueError("所选区间太短或出点早于入点，请重新选择完整区间")
            if check_cast:
                checks = [selected.speaker_matches, selected.cast_matches, selected.props_match]
                if any(value is None for value in checks):
                    raise ValueError("缺少说话人、演员或道具的判断")
                if not all(checks):
                    selected.retake = True
            return selected, model
        except (asyncio.CancelledError,):
            raise
        except Exception as exc:
            from question.runtime import RunRevoked

            if isinstance(exc, RunRevoked):
                raise
            error = type(exc).__name__
            messages[0]["content"][0]["text"] += (
                "\n请从两个候选列表选择帧号，调用 choose_cut 提交这次剪辑决定。"
            )
    raise RemakeStopped(f"剪辑审片暂未得到有效的剪点（{error}），可基于原镜头重试。")


async def review(ctx, plan, index, asset_id, requested, lines, reference_copy, first_reference):
    oss = get_oss()
    meta = await ensure_meta(asset_id, user_id=ctx.user_id)
    if not meta or not meta.get("fps"):
        raise RemakeStopped("待核实生成视频的帧率与时长。")
    fps = float(meta["fps"])
    duration = min(float(meta["duration_sec"]), requested)
    language = plan.language
    suffix = f":{language}" if language != "zh" else ""
    trans = await vp.execute_transcribe(
        vp.VideoTranscribeArgs(
            action="submit", asset_id=asset_id, language=language, idempotency_key=f"light-remake-caption:{asset_id}{suffix}"
        ),
        ctx,
    )
    if trans.metadata.get("status") != "completed":
        raise RemakeStopped("暂未取得对白识别结果，已保留原镜头。")
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        job = await db.get(VideoJob, trans.metadata["job_id"])
        transcript = (job.result_data or {}).get("transcript") or {}
        source_key = row.oss_key
        previous = await db.get(FileAsset, first_reference) if index > 1 else None
        previous_key = previous.oss_key if previous and previous.user_id == ctx.user_id else None
        if index > 1 and not previous_key:
            raise RemakeStopped("尚未取到上一镜已确认的接拍帧。")
    cues = captions(lines, transcript)
    if lines and not cues:
        return None
    starts, ends = candidates(index, fps, duration, cues)
    samples = sorted({*starts, *ends, *(round(duration * fps * r) for r in (0.2, 0.44, 0.68))})
    async with httpx.AsyncClient(timeout=30) as client:
        pool = asyncio.Semaphore(4)

        async def read(i):
            async with pool:
                buf = await exact_frame(client, oss, source_key, i, fps, 220)
            return i, Image.open(io.BytesIO(buf)).convert("RGB")

        frames = dict(await asyncio.gather(*(read(i) for i in samples)))
        entry_delta = {}
        if previous_key:
            frame = Image.open(io.BytesIO(await oss.get_object(previous_key))).convert("RGB")
            starts, entry_delta = matched_starts(starts, frames, frame)
            if not starts:
                return None
        usable = continuous_ends(ends, frames)
        backdrop = None
        if index == 3:
            usable = held_product_ends(usable, frames, frame)
            if not usable:
                return None
            overlay = product_backdrop(frame, frames)
            if overlay is not None:
                buf = io.BytesIO()
                overlay.save(buf, "PNG")
                backdrop = await _reference_asset(
                    ctx, f"product-background-{asset_id}", buf.getvalue(), mime="image/png"
                )
                for im in frames.values():
                    plate = overlay.resize(im.size, Image.Resampling.LANCZOS)
                    im.paste(plate, (0, 0), plate)
        visual = sheet(frames)
        evidence = await _reference_asset(
            ctx, f"edit-review-{asset_id}" + ("-background" if backdrop else ""), visual
        )
        async with get_db_session() as db:
            proof = await db.get(FileAsset, evidence)
        decision, model = await decide(
            ctx,
            {
                "shot": index,
                "fps": fps,
                "title": plan.title,
                "product": plan.product,
                "action": (plan.opening.action, plan.response.action, "同一商品的片尾特写")[index - 1],
                "reference_copy": reference_copy,
                "background_treatment": "片尾上方已用首帧稳定背景，商品区域为原视频" if backdrop else None,
                "actual_dialogue": transcript.get("text", ""),
                "captions": cues,
            },
            oss.presign_get(proof.oss_key),
            evidence,
            starts,
            usable,
        )
        if decision.retake:
            return None
        handoff = None
        if index < 3:
            full = await exact_frame(client, oss, source_key, decision.last, fps, min(1080, meta["width"]))
            handoff = await _reference_asset(ctx, f"handoff-{asset_id}-{decision.last}", full)
    return {
        "shot": index,
        "source": asset_id,
        "first": decision.first,
        "last": decision.last,
        "fps": fps,
        "in_sec": round(decision.first / fps, 4),
        "duration_sec": round((decision.last + 1 - decision.first) / fps, 4),
        "handoff": handoff,
        "review_asset": evidence,
        "backdrop": backdrop,
        "editor_model": model,
        "entry_luma_delta": round(entry_delta[decision.first], 2) if entry_delta else None,
        "reason": decision.reason,
        "continuity": decision.continuity,
        "cues": cues,
        "heard": transcript.get("text", ""),
        "asr_job": job.id,
    }
