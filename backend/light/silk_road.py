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

TEMPLATE = "silk_road"
FORMAT = "silk-road-v1"
RESOLUTION = "720p"
ASSETS = Path(__file__).with_name("remake_assets") / "silk-road"
CANVAS = {"width": 1680, "height": 660, "fps": 24}
DURATION = 62.125


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


DIRECTING = (
    "选定《丝路留香》样片。用用户所选素材辨识真实菜品和摆盘，用户文案是创作参考。"
    "用现成的九段无字参考保持机位、运镜、暖色帐篷和集市、沙漠到灰青码头和船上。"
    "情节为帐中初尝招牌菜、市场误买旅途干粮、两次试吃、驼队反应、码头获赠招牌菜、船上复尝。"
    "填写片名、独立菜名（不带店名）、菜品外观、片尾副题。bite 只写一口菜品的样子、decoy 只写误买道具，"
    "这两个字段都只写名词短语。direction 是供用户看的全片梗概，不会作为拍摄提示。"
    "确需适配参考文案时，take_notes 只给需要调整的一段写本段可拍的细节，不添加其它场景或流程指令。"
    "段号顺序为：1帐篷、2集市、3出摊起驼、4沙地试吃、5驼队笑点、6码头揭礼、7船上复尝和挥手、8船离港、9收礼反打。"
    "避免自创价格或促销。"
    "保留宽银幕和原配乐，视觉叙事，无同步对白，题字在剪辑时添加。Wan 3.0 九段并行各拍一次，"
    "自动根据交付时长在参考剪点附近裁切，阿里云 IMS 完整剪辑。小的道具偏差不用补拍。"
)


def shot_request(prefix, index, shot, plan, ref, product_refs):
    from light.wan_story import MODEL
    from tool.video_production import VideoGenerateArgs, VideoInputRef

    inputs = [VideoInputRef(asset_id=ref, role="reference_video")]
    product = ""
    if shot["reference_product"]:
        inputs.extend(VideoInputRef(asset_id=asset, role="reference_image") for asset in product_refs)
        product = (
            f"图片1只锁定真实菜品「{plan.product}」及摆盘。{plan.appearance}。"
            f"镜中实际入口的是同一盘的{plan.bite}。全程只出现一份菜。"
        )
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


def cut_specs(cuts, black):
    """Keep the reference beat grid even if Wan shortens an individual take."""
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
        # Apply the archive's offsets proportionally to the actual Wan take. This
        # avoids overrun and the frozen tail of a modeled shorter video.
        planned_end = max(row["end"] for row in case()["cuts"] if row["take"] == name)
        source_start = clip["start"] * min(1, span / planned_end)
        source_end = clip["end"] * min(1, span / planned_end)
        overlap = clip.get("overlap")
        result.append(Shot(
            asset=take["source"], in_sec=round(source_start, 4),
            duration_sec=round(source_end - source_start, 4), speed=(source_end - source_start) / seconds,
            volume=0, transition_out=Transition(type="fade", seconds=overlap / 24) if overlap else None,
        ))
    return result


def is_complete_cut(run, job) -> bool:
    """Only the complete reference timeline can land in the result or advanced chat."""
    from video.timeline import Timeline

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
        expected = cut_specs(cuts, "black")
        if (timeline.canvas.model_dump() != CANVAS or len(timeline.shots) != len(expected)
                or len(timeline.overlays) != 9 or len(timeline.audio) != 1
                or not math.isclose(float(request["duration_sec"]), DURATION, abs_tol=0.05)):
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
    from light.production import begin_reference_edit, remake_stage, save_reference_cut, save_wan_edit
    from light.remake import RemakeStopped, _digest, _job, _material_refs, _parallel, _reference_asset
    from light.silk_road_lettering import artwork
    from tool import video_compose, video_production
    from video.media_probe import ensure_meta
    from video.timeline import Timeline

    prefix = run["job_prefix"]
    task = replace(ctx, message_id="", sandbox=None, _on_output=None)
    run = await begin_reference_edit(ctx, prefix, FORMAT)
    cuts = {cut["shot"]: cut for cut in run.get("wan_cuts") or []}

    async def package(name, mime):
        data = await asyncio.to_thread((ASSETS / name).read_bytes)
        return await _reference_asset(ctx, f"{FORMAT}-{Path(name).stem}", data, mime=mime)

    async def shoot():
        if len(cuts) == len(case()["shots"]):
            return
        product = await _material_refs(task, form["asset_ids"])
        if not product:
            raise RemakeStopped("没有读到菜品参考。请选择清楚的菜品照片，或在素材库中选择包含菜品的实拍。")
        await remake_stage(ctx, prefix, "wan_shots")

        async def film(item):
            index, spec = item
            ref = await package(f"reference-{spec['name']}.mp4", "video/mp4")
            request = shot_request(prefix, index, spec, plan, ref, product)
            asset = await _job(task, request, kind="segment", label=f"参考段 {index}", run=run)
            meta = await ensure_meta(asset, user_id=ctx.user_id)
            seconds = float((meta or {}).get("duration_sec") or 0)
            if seconds <= 0:
                raise RemakeStopped(f"第 {index} 段已生成，暂未读取到视频时长。")
            cut = {"shot": index, "source": asset, "duration_sec": seconds, "review_version": FORMAT}
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
        return await _parallel([
            *cards, "black.mp4", "source-audio.mp3",
        ], lambda item: card(item) if isinstance(item, tuple) else package(
            item, "video/mp4" if item.endswith(".mp4") else "audio/mpeg",
        ), concurrency=6)

    await remake_stage(ctx, prefix, "remake_frames")
    filmed, graphics = await asyncio.gather(shoot(), backgrounds(), return_exceptions=True)
    for result in (filmed, graphics):
        if isinstance(result, BaseException):
            raise result
    *layers, black, music = graphics
    await remake_stage(ctx, prefix, "wan_edit")
    ordered = [cuts[index] for index in range(1, len(cuts) + 1)]
    timeline = Timeline(
        canvas=CANVAS, shots=cut_specs(ordered, black), overlays=layers,
        audio=[{"asset": music, "to_sec": DURATION, "volume": 1, "fade_in_sec": 0.12, "fade_out_sec": 0.38}],
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
