"""你拍我剪: read a batch of the owner's phone clips without a desktop (F1–F3).

For each clip: the media facts (``video/media_probe``), frames sampled at even
times through OSS's own ``video/snapshot`` processing on a signed GET (no
ffmpeg; HEVC/mov and rotated clips work, measured 2026-11-02), the spoken words
with timings from the ASR engine reading the video URL directly, and one vision
call that turns the timed frames into usable segments. Clips run in parallel.

Billing is one line for the whole batch — 素材分析, 1 credit per started minute
of footage (billing/footage.py) — charged when the analysis completes, before
the plan card, and idempotent on the batch: the same clips analysed twice in a
conversation are charged once. Vision and ASR tokens are inside that price.

Speed: a clip uploaded on a 你拍我剪 page is read as soon as its upload
completes (:func:`prepare_in_background`, from ``/api/assets/{id}/complete``),
so by the time the agent asks, the work is done or under way; the result is
kept on ``file_assets.meta["reading"]``. Within a clip, speech and picture are
read at the same time.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from typing import Any

from pydantic import BaseModel, Field

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.footage_analyze")

MAX_CLIPS = 10
#: Bump when what a clip reading contains changes, so old cached readings are redone.
READING_VERSION = 1
_readings: dict[str, asyncio.Task] = {}
MAX_TOTAL_SECONDS = 5 * 60
MAX_CLIP_SECONDS = 3 * 60
FRAME_WIDTH = 480
SNAPSHOT_LINK_SECONDS = 3600

PROMPT = """你在帮一家小店老板剪片。下面是他用手机拍的一段素材的截帧，每张标了它在素材里的秒数。
素材时长 {duration} 秒，{orientation}；{speech}（人声内容另行转写，这里只看画面。）

请按画面把这段素材切成可用的片段，严格只输出 JSON，不要任何解释：
{{"subject": "这段主要拍的是什么（≤15字）",
  "segments": [{{"from_sec": 0.0, "to_sec": 3.0, "shot": "画面内容（≤20字）", "kind": "门头|环境|菜品特写|制作过程|人物|口播|价格|其他", "quality": "好|晃|暗|糊|遮挡", "usable": true}}],
  "best": {{"from_sec": 0.0, "to_sec": 3.0, "why": "最适合放进成片的一段（≤20字）"}}}}

要求：片段首尾相接覆盖整段素材；from_sec/to_sec 用截帧的秒数估计；晃、暗、糊、遮挡的片段 usable=false；不要编造截帧里看不到的东西。"""


class FootageAnalyzeArgs(BaseModel):
    #: The owner's uploaded clips, in the order they were shot or sent.
    asset_ids: list[str] = Field(min_length=1, max_length=MAX_CLIPS)
    #: Transcribe clips that have sound (default true).
    transcribe: bool = True


class FootageRefusal(Exception):
    public_message = True


def _frame_times(duration: float) -> list[float]:
    """About one frame every 2.5 s, 4–6 frames, never on the very first or last frame.

    Six frames read a 15 s clip as well as ten did and the call is a third faster.
    """
    count = max(4, min(6, math.ceil(duration / 2.5)))
    step = duration / count
    return [round(step * (i + 0.5), 2) for i in range(count)]


def snapshot_url(oss, key: str, at_sec: float, *, width: int = FRAME_WIDTH) -> str:
    """A signed GET that returns one JPEG frame of the video at ``at_sec`` (OSS video/snapshot)."""
    process = f"video/snapshot,t_{int(at_sec * 1000)},f_jpg,w_{width},m_fast,ar_auto"
    return oss._presign("GET", key, SNAPSHOT_LINK_SECONDS, subresource={"x-oss-process": process})


async def _vision(frames: list[tuple[float, str]], *, duration: float, orientation: str, speech: str) -> dict:
    """One vision call over the timed frames; its tokens are inside the per-minute price."""
    import litellm

    from agent.llm import _get_provider_kwargs
    from core.config import get_config
    from question.runtime import assert_current

    cfg = get_config().video_analysis
    await assert_current("request")
    content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT.format(
        duration=round(duration, 1), orientation=orientation, speech=speech)}]
    for at_sec, url in frames:
        content.append({"type": "text", "text": f"第 {at_sec:g} 秒："})
        content.append({"type": "image_url", "image_url": {"url": url}})
    response = await litellm.acompletion(model=cfg.model, messages=[{"role": "user", "content": content}],
                                         temperature=0.2, max_tokens=1500, timeout=cfg.timeout_seconds,
                                         **_get_provider_kwargs(cfg.model))
    text = response.choices[0].message.content or ""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise FootageRefusal("the picture reading returned no result")
    return json.loads(match.group(0))


async def read_clip(row, meta: dict, oss, *, transcribe: bool) -> dict:
    """Speech and picture of one clip, read at the same time; cached on its meta."""
    from tool.video_production import _configured_transcription_target, _provider_transcribe

    duration = float(meta["duration_sec"])
    orientation = {"portrait": "竖拍", "landscape": "横拍（成片会居中裁成竖屏）", "square": "方形"}.get(
        meta.get("orientation") or "", "方向未知")

    async def speech() -> dict[str, Any]:
        if not (transcribe and meta.get("has_audio")):
            return {}
        try:
            return await _provider_transcribe(_configured_transcription_target(),
                                              oss.presign_get(row.oss_key, expires_sec=SNAPSHOT_LINK_SECONDS))
        except Exception as exc:  # silence or noise is not a failure: the picture still counts
            log.info("footage %s: no transcript (%s)", row.id, type(exc).__name__)
            return {"error": str(exc)[:120]}

    async def picture() -> dict[str, Any]:
        frames = [(at, snapshot_url(oss, row.oss_key, at)) for at in _frame_times(duration)]
        try:
            return await _vision(frames, duration=duration, orientation=orientation,
                                 speech="有声音" if meta.get("has_audio") else "没有声音")
        except Exception as exc:
            log.warning("footage %s: picture reading failed: %s", row.id, type(exc).__name__)
            return {"subject": "", "segments": [], "error": "画面没读出来"}

    heard, seen = await asyncio.gather(speech(), picture())
    reading = {
        "v": READING_VERSION, "transcript": str(heard.get("text") or "").strip() or None,
        "sentences": heard.get("sentences") or [], "subject": seen.get("subject"),
        "segments": seen.get("segments") or [], "best": seen.get("best"),
        **({"note": seen["error"]} if seen.get("error") else {}),
    }
    if not seen.get("error"):
        from db.base import get_db_session
        from db.models.file_asset import FileAsset
        async with get_db_session() as db:
            fresh = await db.get(FileAsset, row.id)
            if fresh is not None:
                fresh.meta = {**(fresh.meta or meta), "reading": reading}
    return reading


async def _prepare(asset_id: str, user_id: str) -> dict | None:
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from video.media_probe import ensure_meta

    meta = await ensure_meta(asset_id, user_id=user_id)
    if not meta or not meta.get("duration_sec") or float(meta["duration_sec"]) > MAX_CLIP_SECONDS:
        return None
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
    if row is None:
        return None
    return await read_clip(row, meta, get_oss(), transcribe=True)


def prepare_in_background(asset_id: str, user_id: str) -> None:
    """Upload hook on a 你拍我剪 page: read the clip now, while the owner uploads the rest."""
    if asset_id in _readings and not _readings[asset_id].done():
        return
    task = asyncio.create_task(_prepare(asset_id, user_id))
    _readings[asset_id] = task
    task.add_done_callback(lambda _t: _readings.pop(asset_id, None) if _readings.get(asset_id) is _t else None)


async def _one(index: int, asset_id: str, ctx: ToolContext, oss, transcribe: bool) -> dict:
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from video.media_probe import ensure_meta

    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
    if row is None or row.user_id != ctx.user_id or row.is_deleted or row.status != "ready":
        raise FootageRefusal(f"clip {index + 1} ({asset_id}) is not a ready upload of yours")
    if not str(row.mime or "").startswith("video/"):
        raise FootageRefusal(f"clip {index + 1} is {row.mime}; only videos can be cut")
    pending = _readings.get(asset_id)
    if pending is not None:
        # Read on upload and still running here: wait for it rather than paying twice.
        try:
            await asyncio.shield(pending)
        except Exception:
            pass
        async with get_db_session() as db:
            row = await db.get(FileAsset, asset_id)
    meta = await ensure_meta(asset_id, user_id=ctx.user_id)
    if not meta or not meta.get("duration_sec"):
        raise FootageRefusal(f"clip {index + 1}: the video could not be read")
    duration = float(meta["duration_sec"])
    if duration > MAX_CLIP_SECONDS:
        raise FootageRefusal(f"clip {index + 1} is {duration:.0f}s; one clip may be at most {MAX_CLIP_SECONDS}s")
    reading = (row.meta or {}).get("reading") if isinstance(row.meta, dict) else None
    if not isinstance(reading, dict) or reading.get("v") != READING_VERSION:
        reading = await read_clip(row, meta, oss, transcribe=transcribe)
    return {
        "clip": index + 1, "asset_id": asset_id, "name": row.name,
        "duration_sec": round(duration, 2), "orientation": meta.get("orientation"),
        "width": meta.get("width"), "height": meta.get("height"), "codec": meta.get("codec"),
        "has_audio": bool(meta.get("has_audio")),
        "transcript": reading.get("transcript"), "sentences": reading.get("sentences") or [],
        "subject": reading.get("subject"), "segments": reading.get("segments") or [],
        "best": reading.get("best"), **({"note": reading["note"]} if reading.get("note") else {}),
    }


async def execute(args: FootageAnalyzeArgs, ctx: ToolContext) -> ToolResult:
    from billing.footage import quote_analysis, settle_analysis
    from core.oss import OssNotConfigured, get_oss

    ids = list(dict.fromkeys(args.asset_ids))
    try:
        oss = get_oss()
    except OssNotConfigured:
        return ToolResult(title="Footage analysis unavailable", output="The asset bucket is not configured.")
    await ctx.update_output(f"Reading {len(ids)} clip(s)…")
    results = await asyncio.gather(*(_one(i, asset_id, ctx, oss, args.transcribe) for i, asset_id in enumerate(ids)),
                                   return_exceptions=True)
    clips: list[dict] = []
    refused: list[str] = []
    for result in results:
        if isinstance(result, FootageRefusal):
            refused.append(str(result))
        elif isinstance(result, BaseException):
            from question.runtime import RunRevoked
            if isinstance(result, RunRevoked):
                raise result
            refused.append(f"{type(result).__name__}")
        else:
            clips.append(result)
    if not clips:
        return ToolResult(title="Footage analysis refused", output="\n".join(refused) or "no readable clips",
                          metadata={"error": True})
    total = sum(clip["duration_sec"] for clip in clips)
    if total > MAX_TOTAL_SECONDS:
        return ToolResult(title="Footage analysis refused",
                          output=f"the clips add up to {total:.0f}s; the limit is {MAX_TOTAL_SECONDS}s of footage per cut",
                          metadata={"error": True})

    batch = hashlib.sha256("|".join(sorted(c["asset_id"] for c in clips)).encode()).hexdigest()[:16]
    quote = quote_analysis(total)
    credits = await settle_analysis(key=f"footage_analyze:{ctx.session_id}:{batch}", workspace_id=ctx.workspace_id,
                                    user_id=ctx.user_id, session_id=ctx.session_id, total_seconds=total,
                                    clips=len(clips))
    charged = credits if credits is not None else quote.credits
    charged = format(charged.normalize(), "f") if charged is not None else None
    lines = [
        f"clips={len(clips)} total_footage_sec={total:.1f} analysis_credits={charged} (already charged; "
        "tell the owner on the plan card: 已含素材分析费 N 积分)",
        *(f"refused: {item}" for item in refused),
        "clips_json=" + json.dumps(clips, ensure_ascii=False),
    ]
    return ToolResult(title=f"素材分析 · {len(clips)} 段 · {total:.0f} 秒", output="\n".join(lines),
                      metadata={"clips": clips, "total_footage_sec": round(total, 2),
                                "analysis_credits": charged,
                                "refused": refused})


FOOTAGE_ANALYZE_DESCRIPTION = """\
Read the owner's uploaded phone clips (你拍我剪) without a cloud desktop: for \
each clip its length, orientation (横拍 clips are centre-cropped to the 9:16 \
canvas), codec, whether it has sound, the spoken words with sentence timings, \
and the usable segments with what each shows and its quality. Pass every clip \
in one call (asset_ids, up to 10, at most 5 minutes in total). It charges the \
素材分析 fee once for the batch — 1 credit per started minute of footage — and \
the result says how much (analysis_credits=), which the plan card must show. \
Calling again with the same clips in this conversation is not charged again."""

footage_analyze_tool = define_tool(
    "footage_analyze",
    description=FOOTAGE_ANALYZE_DESCRIPTION,
    parameters=FootageAnalyzeArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
