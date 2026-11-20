"""The Silk Road reference shoot: adapt the product, capture once, compose in IMS."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from light.silk_road_world import (
    DIRECTING, PRODUCT_FRAMES, World, animate_endpoints, endpoint_prompt, take_prompt,
)
from light.video_tiers import SEEDANCE, TURBO

TEMPLATE = "silk_road"
FORMAT = "silk-road-v1"
RESOLUTION = "720p"
ASSETS = Path(__file__).with_name("remake_assets") / "silk-road"
CANVAS = {"width": 1680, "height": 660, "fps": 24}
DURATION = 62.125
FRAME_SIZE = (1536, 608)
SEEDANCE_FRAME_SIZE = (1536, 624)
# IMS AudioBeatDetection measured this public library recording. The reveal
# anchor is on the template's edited timeline.
_BEAT_MUSIC = "icepublic-b9dc19f465b7f3114ab19145e67a5278"
_REVEAL_ANCHOR = 50.56
_MUSIC_DOWNBEAT = 50.62


def beat_score():
    offset = round(_MUSIC_DOWNBEAT - _REVEAL_ANCHOR, 3)
    return [
        {"library_id": _BEAT_MUSIC, "in_sec": offset, "from_sec": 0, "to_sec": 39.76,
         "volume": .76, "fade_in_sec": .20, "fade_out_sec": .18},
        {"library_id": _BEAT_MUSIC, "in_sec": round(39.76 + offset, 3), "from_sec": 39.76,
         "to_sec": 40.24, "volume": .045},
        {"library_id": _BEAT_MUSIC, "in_sec": round(40.24 + offset, 3), "from_sec": 40.24,
         "to_sec": DURATION, "volume": .76, "fade_in_sec": .24, "fade_out_sec": 1.325},
    ]


class TakeNote(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    shot: int = Field(ge=1, le=9)
    detail: str = Field(min_length=1, max_length=110)


class Plan(BaseModel):
    """The director interprets the user's copy and selected dish for the reference film."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    format: Literal["silk-road-v1"] = FORMAT
    title: str = Field(min_length=1, max_length=18)
    product: str = Field(min_length=1, max_length=36)
    appearance: str = Field(min_length=1, max_length=280)
    bite: str = Field(min_length=1, max_length=80, description="The one bite of the signature dish, without later plot beats.")
    decoy: str = Field(default="浅米色干馕", min_length=1, max_length=48)
    note: str = Field(default="一路回味", min_length=1, max_length=18)
    direction: str = Field(default="", max_length=260, description="Optional storyboard summary for the preview.")
    take_notes: list[TakeNote] = Field(default_factory=list, max_length=9)
    world: World | None = None

    @field_validator("bite", "decoy")
    @classmethod
    def single_prop(cls, value: str) -> str:
        return re.split(r"[；;。！？!?\r\n]", value, maxsplit=1)[0].strip() or value

    def for_shop(self, shop: str) -> Plan:
        if not shop:
            return self
        if self.product.startswith(shop):
            product = self.product.removeprefix(shop).lstrip(" ·・:：-—")
            if product:
                return self.model_copy(update={"product": product})
        return self


@lru_cache(maxsize=1)
def case():
    return json.loads((ASSETS / "case.json").read_text(encoding="utf-8"))


def display_script(plan: Plan) -> str:
    if plan.world:
        return "\n".join([
            f"《{plan.title}》 · 62 秒",
            plan.world.premise,
            *(f"{take.setting} · {take.action}" for take in plan.world.takes),
            f"片尾 · {plan.product} · {plan.note}",
        ])
    return "\n".join([
        f"《{plan.title}》 · 62 秒 · 丝路叙事",
        f"帐篷初尝 · 旅人在暖光下品尝{plan.product}。",
        f"集市错买 · 把{plan.decoy}当作旅途干粮。沙地试吃和驼队反应相接。",
        f"码头相赠 · 同伴揭开包布，给出一盘{plan.product}。登船复尝，挥手离港。",
        f"菜品外观 · {plan.appearance}",
        f"片尾 · {plan.product} · {plan.note}",
        *([f"表演与调度 · {plan.direction}"] if plan.direction else []),
        *(f"{case()['shots'][note.shot - 1]['label']} · {note.detail}" for note in plan.take_notes),
    ])


def shot_request(prefix, index, shot, plan, ref, product_refs, *, cast_ref=None, location_ref=None):
    from light.wan_story import MODEL
    from tool.video_production import VideoGenerateArgs, VideoInputRef

    inputs = [VideoInputRef(asset_id=ref, role="reference_video")]
    if plan.world and index != 8 and cast_ref:
        inputs.append(VideoInputRef(asset_id=cast_ref, role="reference_image"))
    has_location = bool(plan.world and location_ref)
    if has_location:
        inputs.append(VideoInputRef(asset_id=location_ref, role="reference_image"))
    product = ""
    if shot["reference_product"]:
        inputs.extend(VideoInputRef(asset_id=asset, role="reference_image") for asset in product_refs)
        image = "最后一张图片" if plan.world else "图片1"
        product = (
            f"{image}只锁定真实菜品「{plan.product}」及摆盘。{plan.appearance}。"
            f"镜中实际入口的是同一盘的{plan.bite}。全程只出现一份菜。"
        )
    if plan.world:
        prompt = take_prompt(plan.world, index, has_cast=index != 8 and bool(cast_ref),
                             has_location=has_location, product=product)
    else:
        prompt = case()["camera"] + product + shot["prompt"].format(
            product=plan.product, bite=plan.bite, decoy=plan.decoy,
        )
        note = next((note.detail for note in plan.take_notes if note.shot == index), "")
        if note:
            prompt += f"只在本段场景内参考补充调度：{note}。"
        prompt += "本段时间和地点保持连续，不插入其他场景或前后剧情。"
    return VideoGenerateArgs(
        action="submit", model=MODEL, resolution=RESOLUTION, ratio="21:9", duration=shot["duration"],
        generate_audio=False, watermark=False, no_speech=True, seed=31952026,
        idempotency_key=f"{prefix}:shot{index}", shot=index, input_assets=inputs, prompt=prompt,
    )


def framed_shot_request(prefix, index, plan, frames, *, model=None, resolution=RESOLUTION):
    from light.wan_story import MODEL
    from tool.video_production import VideoGenerateArgs, VideoInputRef

    shot = case()["shots"][index - 1]
    span = max(section["end"] for section in case()["cuts"] if section["take"] == shot["name"])
    model = model or MODEL
    minimum = {TURBO: 5, SEEDANCE: 4}.get(model, 2)
    return VideoGenerateArgs(
        action="submit", model=model, resolution=resolution, ratio="adaptive",
        duration=max(minimum, math.ceil(span)), generate_audio=model == TURBO, watermark=False, no_speech=True,
        seed=31952026 if model == MODEL else None, idempotency_key=f"{prefix}:shot{index}", shot=index,
        input_assets=[VideoInputRef(asset_id=asset, role=role) for role, asset in frames.items()],
        prompt=animate_endpoints(plan.world, index),
    )


def reaction_cut_index():
    return next(i for i, section in enumerate(case()["cuts"]) if section["take"] == "09-gift-reverse")


def reaction_after_reveal(shots):
    index = reaction_cut_index()
    return [*shots[:index], shots[index + 1], shots[index], *shots[index + 2:]]


def cut_specs(cuts, black, *, align_handoff=False):
    """Keep the reference beat grid with different take lengths."""
    from video.timeline import Shot, Transition

    filmed = {spec["name"]: cut for spec, cut in zip(case()["shots"], cuts, strict=True)}
    result = []
    for clip in case()["cuts"]:
        seconds = clip["frames"] / CANVAS["fps"]
        name = clip["take"]
        if name == "black":
            result.append(Shot(asset=black, duration_sec=round(seconds, 4), volume=0))
            continue
        take = filmed[name]
        span = take["duration_sec"]
        # Apply the archive's offsets proportionally to the actual take. This
        # avoids overrun and the frozen tail of a modeled shorter video.
        planned_end = max(row["end"] for row in case()["cuts"] if row["take"] == name)
        scale = span / planned_end if take.get("frame_anchored") else min(1, span / planned_end)
        source_start = clip["start"] * scale
        source_end = clip["end"] * scale
        if source_end - source_start > 4 * seconds:
            # Some models cannot shoot a one-second insert. Take a short
            # window from the middle of the action instead of overcranking it.
            midpoint = (source_start + source_end) / 2
            source_start, source_end = midpoint - 1.5 * seconds, midpoint + 1.5 * seconds
        overlap = clip.get("overlap")
        result.append(Shot(
            asset=take["source"], in_sec=round(source_start, 4),
            duration_sec=round(source_end - source_start, 4), speed=(source_end - source_start) / seconds,
            volume=0, transition_out=Transition(type="fade", seconds=overlap / 24) if overlap else None,
        ))
    return reaction_after_reveal(result) if align_handoff else result


def is_complete_cut(run, job) -> bool:
    """Only the complete reference timeline can land in the result or advanced chat."""
    from video.timeline import AudioClip, Timeline

    cuts = run.get("wan_cuts") or []
    key = run.get("wan_final_key")
    actual = getattr(job, "idempotency_key", "") or ""
    if (
        not key or (actual != key and not actual.startswith(key + ":retry:"))
        or job.kind != "compose" or job.status != "completed" or not job.output_asset_id
        or [cut["shot"] for cut in cuts] != list(range(1, len(case()["shots"]) + 1))
        or len({cut["source"] for cut in cuts}) != len(cuts)
    ):
        return False

    def matches(asset, reference):
        return asset == reference or reference in unquote(urlsplit(asset).path).split("/")

    request = job.request_data or {}
    try:
        duration = float((job.result_data or {}).get("duration_sec") or 0)
        if not math.isclose(duration, DURATION, abs_tol=0.25):
            return False
        timeline = Timeline.model_validate(request["timeline"])
        controls = run.get("silk_controls") or {}
        expected = cut_specs(cuts, "black", align_handoff=controls.get("align_handoff", False))
        if (timeline.canvas.model_dump() != CANVAS or len(timeline.shots) != len(expected)
                or len(timeline.overlays) != 9
                or not math.isclose(float(request["duration_sec"]), DURATION, abs_tol=0.05)):
            return False
        if controls and not controls["beat_music"]:
            if timeline.audio:
                return False
        elif controls or len(timeline.audio) != 1:
            expected_score = [AudioClip.model_validate(clip) for clip in beat_score()]
            if [audio.model_copy(update={"source_sec": None}) for audio in timeline.audio] != expected_score:
                return False
        for actual_shot, expected_shot in zip(timeline.shots, expected, strict=True):
            if not (matches(actual_shot.asset, expected_shot.asset) if expected_shot.asset != "black"
                    else unquote(urlsplit(actual_shot.asset).path).endswith(f"/{FORMAT}-black.mp4")):
                return False
            if actual_shot.model_dump(exclude={"asset"}) != expected_shot.model_dump(exclude={"asset"}):
                return False
    except (KeyError, TypeError, ValueError):
        return False
    return True


async def render(ctx, form, run, plan):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from light import remake_case
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_wan_edit
    from light.remake import RemakeStopped, _digest, _job, _material_refs, _parallel, _reference_asset
    from light.shot_editor import FRAME_ROLES, _normalize_frame, _references, publish_inputs, publish_request
    from light.wan_remake import _still
    from light.silk_road_lettering import artwork
    from tool import video_compose, video_production
    from video.media_probe import ensure_meta
    from video.timeline import Timeline

    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, FORMAT)
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}
    controls = run.get("silk_controls")
    framed = controls is not None and plan.world is not None
    filming_model = run.get("video_model")
    frame_size = SEEDANCE_FRAME_SIZE if filming_model == SEEDANCE else FRAME_SIZE
    use_beats = controls["beat_music"] if controls is not None else bool(
        re.search(r"配乐(?:按|跟)节拍|按节拍配乐", remake_case.reference_copy(form))
    )
    keep_source_audio = controls is None and not use_beats
    frame_slots = asyncio.Semaphore(6)

    async def package(name, mime):
        data = await asyncio.to_thread((ASSETS / name).read_bytes)
        key = name.rsplit(".", 1)[0].replace("/", "-")
        return await _reference_asset(ctx, f"{FORMAT}-{key}", data, mime=mime)

    async def shoot():
        if len(cuts) == len(case()["shots"]):
            return
        cast_ref = None
        locations = {}
        if plan.world:
            async def selected(index):
                if not index:
                    return []
                return await _material_refs(task, form["asset_ids"][index-1:index])

            indices = sorted({take.location_material for take in plan.world.takes if take.location_material})
            product, actors, *sets = await asyncio.gather(
                selected(plan.world.product_material), selected(plan.world.cast_material), *(selected(i) for i in indices),
            )
            locations = {index: refs[0] for index, refs in zip(indices, sets, strict=True) if refs}
            if product:
                if framed:
                    cast_prompt = (
                        f"商业电影演员定妆图。{plan.world.cast}。每位演员只出现一次，胸上和衣服可辨认。"
                        "统一纯灰摄影棚背景，中性光，只有主演。服装和脸可辨，无手持物、文字、路人或动物。"
                    )
                    cast_materials = []
                else:
                    cast_prompt = (
                        f"商业电影定妆，按{plan.world.look}选择符合设定的人物和服装，"
                        f"准确呈现：{plan.world.cast}。每位演员只出现一次，胸上和衣服可辨认，"
                        "自然中性光、统一环境、微微侧身，演员不扮演剧情。参考照片只用于场地和色彩。无文字。"
                    )
                    cast_materials = list(locations.values())[:2]
                cast_ref = actors[0] if actors else await _still(
                    task, prefix, "silk-cast", cast_prompt, cast_materials, size="1536x1024", quality="medium",
                )
        else:
            product = await _material_refs(task, form["asset_ids"])
        if not product:
            raise RemakeStopped("没有读到菜品参考。请选择清楚的菜品照片，或在素材库中选择包含菜品的实拍。")
        await remake_stage(ctx, prefix, "wan_shots")

        async def anchored_request(index, location):
            actors = cast_ref if index != 8 else None
            common = [asset for asset in (actors, location) if asset]
            saved = (run.get("shot_inputs") or {}).get(str(index), {})
            known = _references(saved)
            prompts = {
                role: endpoint_prompt(plan, index, role, has_cast=bool(actors), has_location=bool(location),
                                      has_product=role in PRODUCT_FRAMES.get(index, set()))
                for role in FRAME_ROLES
            }
            share_location = index != 7
            if share_location:
                prompts["last_frame"] += "第一张图是同段首帧，保持人物身份、位置关系、照明和场地结构。仅按本帧描述推进动作和道具状态。"
            motion = animate_endpoints(plan.world, index)

            async def make(role, opening=None):
                if known.get(role):
                    return known[role]
                refs = [*([opening] if opening else ()), *common,
                        *(product[:1] if role in PRODUCT_FRAMES.get(index, set()) else ())]
                async with frame_slots:
                    raw = await _still(task, f"{prefix}:shot{index}:{role}", "silk-frame", prompts[role],
                                       list(dict.fromkeys(refs)), size="1536x608", quality="medium")
                    frame = await _normalize_frame(task, raw, frame_size)
                await publish_inputs(ctx, prefix, index, motion, {
                    "roles": [role], "input_asset_ids": [frame], "frame_prompts": prompts,
                }, merge=True)
                return frame

            if share_location:
                first = await make("first_frame")
                frames = [first, await make("last_frame", opening=first)]
            else:
                frames = await _parallel(FRAME_ROLES, make, concurrency=2)
            request = framed_shot_request(
                prefix, index, plan, dict(zip(FRAME_ROLES, frames, strict=True)),
                model=filming_model, resolution=run.get("video_resolution") or RESOLUTION,
            )
            await publish_request(ctx, prefix, request, frame_prompts=prompts)
            return request

        async def film(item):
            index, spec = item
            location = locations.get(plan.world.takes[index - 1].location_material) if plan.world else None
            if framed:
                request = await anchored_request(index, location)
            else:
                name = f"reference-{spec['name']}.mp4"
                ref = await package(f"motion/{name}" if plan.world else name, "video/mp4")
                request = shot_request(prefix, index, spec, plan, ref, product, cast_ref=cast_ref, location_ref=location)
                reference_duration = max(cut["end"] for cut in case()["cuts"] if cut["take"] == spec["name"])
                await publish_request(ctx, prefix, request, reference_duration=reference_duration)
            asset = await _job(task, request, kind="segment", label=f"参考段 {index}", run=run)
            meta = await ensure_meta(asset, user_id=ctx.user_id)
            seconds = float((meta or {}).get("duration_sec") or 0)
            if seconds <= 0:
                raise RemakeStopped(f"第 {index} 段已生成，暂未读取到视频时长。")
            cut = {"shot": index, "source": asset, "duration_sec": seconds, "review_version": FORMAT}
            if framed:
                cut["frame_anchored"] = True
            await save_reference_cut(ctx, prefix, cut)
            cuts[index] = cut

        pending = [(i, spec) for i, spec in enumerate(case()["shots"], 1) if i not in cuts]
        await _parallel(pending, film, concurrency=len(case()["shots"]))

    async def backgrounds():
        cards = await asyncio.to_thread(artwork, plan, form["shop"])
        async def card(item):
            (start, end, data) = item
            asset = await _reference_asset(ctx, f"{FORMAT}-title-{hashlib.sha256(data).hexdigest()[:28]}", data, mime="image/png")
            return {"asset": asset, "from_sec": start, "to_sec": end, "kind": "image",
                    "x": 0, "y": 0, "width": 1, "height": 1, "fit": "contain"}
        assets = [*cards, "black.mp4"]
        if keep_source_audio:
            assets.append("source-audio.mp3")
        return await _parallel(assets, lambda item: card(item) if isinstance(item, tuple) else package(
            item, "video/mp4" if item.endswith(".mp4") else "audio/mpeg",
        ), concurrency=6)

    await remake_stage(ctx, prefix, "remake_frames")
    filmed, graphics = await asyncio.gather(shoot(), backgrounds(), return_exceptions=True)
    for result in (filmed, graphics):
        if isinstance(result, BaseException):
            raise result
    if keep_source_audio:
        *layers, black, music = graphics
        soundtrack = [{"asset": music, "to_sec": DURATION, "volume": 1, "fade_in_sec": 0.12, "fade_out_sec": 0.38}]
    else:
        *layers, black = graphics
        soundtrack = beat_score() if use_beats else []
    await remake_stage(ctx, prefix, "wan_edit")
    ordered = [cuts[index] for index in range(1, len(cuts) + 1)]
    timeline = Timeline(
        canvas=CANVAS, shots=cut_specs(ordered, black, align_handoff=(controls or {}).get("align_handoff", False)), overlays=layers,
        audio=soundtrack,
    )
    data = timeline.model_dump(mode="json")
    key = f"{prefix}:final:{_digest(json.dumps(data, sort_keys=True))}"
    run = await save_wan_edit(ctx, prefix, cuts=ordered, final_key=key)
    await remake_stage(ctx, prefix, "compose")
    asset = await _job(task, video_compose.VideoComposeArgs(
        action="submit", idempotency_key=key, timeline=data, filename=f"{plan.title}-{plan.product}.mp4",
    ), kind="compose", label="丝路风格完整成片", run=run)
    async with get_db_session() as db:
        job = await db.scalar(select(VideoJob).where(
            VideoJob.session_id == ctx.session_id, VideoJob.kind == "compose", VideoJob.output_asset_id == asset,
        ))
    meta = await ensure_meta(asset, user_id=ctx.user_id)
    if not job or not is_complete_cut(run, job) or not math.isclose(
        float((meta or {}).get("duration_sec") or 0), DURATION, abs_tol=0.25,
    ):
        raise RemakeStopped("云端成片尚未通过镜头清单与时长校验。")
    if not await video_production._attach_completed(job, ctx):
        raise RemakeStopped("云端已合成，暂未附到这条对话。")
    return asset, DURATION
