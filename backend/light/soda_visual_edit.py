"""A single, bounded visual edit of the takes already filmed for the quick recipe."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import math
from time import monotonic

import httpx
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel, ConfigDict, Field

from assets.service import owned_ready_query
from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from light.reference_quality import ask
from light.remake import _reference_asset
from question.runtime import RunRevoked
from tool.footage_analyze import snapshot_url
from video.media_probe import ensure_meta

BUDGET_SECONDS = 25
log = logging.getLogger(__name__)


class Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    shot: int = Field(ge=1, le=12)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    reason: str = Field(min_length=1, max_length=100)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    note: str = Field(default="", max_length=150)
    edits: list[Edit] = Field(default_factory=list, max_length=4)


async def sample(ctx, asset_id, meta, target_seconds):
    """Read a few timed frames of an owned new take while ASR is running."""
    duration = float((meta or {}).get("duration_sec") or 0)
    if duration < 0.5:
        return duration, []
    async with get_db_session() as db:
        row = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id))
    if row is None:
        return duration, []
    positions = sorted({0.12, round(duration * .32, 2), round(duration * .62, 2),
                        min(round(duration - .12, 2), max(.12, target_seconds - .13)),
                        round(duration - .12, 2)})
    oss = get_oss()
    async with httpx.AsyncClient(timeout=4.5) as client:
        async def frame(at):
            try:
                response = await client.get(snapshot_url(oss, row.oss_key, at, width=230, exact=True))
                response.raise_for_status()
                if response.headers.get("content-type", "").startswith("image/"):
                    return at, response.content
            except httpx.HTTPError:
                pass
            return None

        frames = await asyncio.gather(*(frame(at) for at in positions))
    return duration, [frame for frame in frames if frame]


def contact_sheet(samples):
    width, height = 134, 238
    gap = 6
    rows = sorted((index, frames) for index, (_, frames) in samples.items() if frames)
    if not rows:
        return None
    board = Image.new("RGB", (5 * (width + gap) + gap, len(rows) * (height + 34) + gap), "#151515")
    draw = ImageDraw.Draw(board)
    drawn = 0
    for row, (index, frames) in enumerate(rows):
        y = row * (height + 34) + gap
        for col, (at, data) in enumerate(frames):
            x = col * (width + gap) + gap
            try:
                with Image.open(io.BytesIO(data)) as frame:
                    image = ImageOps.fit(frame.convert("RGB"), (width, height))
            except OSError:
                continue
            board.paste(image, (x, y + 22))
            draw.text((x + 3, y + 3), f"{index:02d} / {at:.2f}s", fill="white")
            drawn += 1
    if not drawn:
        return None
    buf = io.BytesIO()
    board.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def apply_decision(cuts, durations, decision):
    """Keep every source asset and cue, preserving feasible in/out ranges."""
    chosen = {cut["shot"]: cut for cut in cuts}
    accepted = []
    total = sum(cut["duration_sec"] for cut in cuts)
    minimum_total = max(18.0, total - 2.5)
    maximum_total = total + 2.5
    for edit in decision.edits:
        old = chosen.get(edit.shot)
        limit = durations.get(edit.shot, 0)
        if old is None or edit.shot in accepted or edit.end > limit + .001:
            continue
        fps = old["fps"]
        start = round(math.floor(edit.start * fps) / fps, 4)
        end = round(min(limit, math.ceil(edit.end * fps) / fps), 4)
        length = round(end - start, 4)
        min_length = min(old["duration_sec"], 2.0 if old["shot"] == cuts[-1]["shot"] else 1.0)
        revised_total = total - old["duration_sec"] + length
        if length < min_length or not minimum_total <= revised_total <= maximum_total:
            continue
        if any(cue["from_sec"] < start or cue["to_sec"] > end for cue in old["cues"]):
            continue
        if abs(start - old["in_sec"]) + abs(length - old["duration_sec"]) < 1 / fps:
            continue
        chosen[edit.shot] = {
            **old, "in_sec": start, "duration_sec": length,
            "first": math.floor((start + .00005) * fps),
            "last": math.ceil((end - .00005) * fps) - 1,
        }
        total = revised_total
        accepted.append(edit.shot)
    return [chosen[cut["shot"]] for cut in cuts], accepted


async def revise(ctx, plan, cuts, samples_by_shot):
    start = monotonic()
    report = {"status": "unavailable", "adjusted_shots": []}
    revised = cuts
    try:
        async with asyncio.timeout(BUDGET_SECONDS):
            indices = list(samples_by_shot)
            values = await asyncio.gather(*samples_by_shot.values())
            samples = dict(zip(indices, values, strict=True))
            board = await asyncio.to_thread(contact_sheet, samples)
            if board and len([frames for _, frames in values if frames]) >= 2:
                digest = hashlib.sha256(board).hexdigest()[:20]
                evidence = await _reference_asset(ctx, f"soda-review-{digest}", board)
                durations = {i: duration for i, (duration, frames) in samples.items() if frames}
                guide = (
                    "你负责把已生成的短广告做最后一遍快速接镜审片。图板每行是一镜，标号和秒数是源视频时间。"
                    "只在实际帧证实有明显动作接不上、道具提前出现、菜品状态跳变、切掉关键动作，"
                    "或模型在成像中生成文字和水印时，修改该镜入点或出点。"
                    "图板是原始回片，正确的品牌题字会在合成时加入。如果片尾后半截自带文字，提前切出，保留至少两秒清晰产品画面。"
                    "允许合理的角度变化和冷暖转场，只有小误差就保留。"
                    "剪辑范围必须覆盖全部实际对白。只改现有素材窗口，保留镜头顺序，每镜只提一条改动。"
                    "start 和 end 均为该镜的源视频绝对秒数。保留尾部商品题字展示时间。"
                    "看到证据不足、已有剪点合适或无法靠裁剪解决时，返回空 edits，note 留简短说明。"
                )
                data = {
                    "product": plan.product,
                    "appearance": plan.appearance,
                    "shots": [{
                        "shot": cut["shot"], "action": plan.shots[cut["shot"] - 1].action,
                        "start": cut["in_sec"], "end": round(cut["in_sec"] + cut["duration_sec"], 3),
                        "dialogue": cut["cues"], "duration": durations.get(cut["shot"]),
                    } for cut in cuts],
                }
                answer = await ask(ctx, "adjust_soda_cuts", Decision, guide, data, [evidence],
                                   attempts=1, max_tokens=1050, timeout=BUDGET_SECONDS, reasoning_effort="low")
                decision = Decision.model_validate(answer["report"])
                revised, adjusted = apply_decision(cuts, durations, decision)
                report = {"status": "reviewed", "adjusted_shots": adjusted, "model": answer["model"],
                          "notes": decision.note, "proposed": [edit.model_dump() for edit in decision.edits],
                          "evidence": evidence}
    except TimeoutError:
        report["status"] = "timed_out"
    except RunRevoked:
        raise
    except Exception as exc:
        log.info("Quick visual edit unavailable for %s (%s)", ctx.session_id, type(exc).__name__)
    report["elapsed_seconds"] = round(monotonic() - start, 3)
    return revised, report


async def sample_existing(ctx, cut, target_seconds):
    meta = await ensure_meta(cut["source"], user_id=ctx.user_id)
    return await sample(ctx, cut["source"], meta, target_seconds)
