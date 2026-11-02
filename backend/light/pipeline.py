"""Drive one clip through the real video tools without an agent turn.

The first video and 省心托管 runs both make a clip the way a chat would: the
same ``ToolContext`` (so the result attaches to a real assistant message),
``video_generate`` submit + wait, then an optional ``video_compose`` pass that
writes the shop name on top (the model would misdraw the characters). Waiting
happens here, in-process, instead of in model turns.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from core.log import create_logger

log = create_logger(__name__)

MAX_GENERATION_SECONDS = 20 * 60
MAX_OVERLAY_SECONDS = 5 * 60
WAIT_SECONDS = 25.0


@dataclass
class Outcome:
    job_id: str | None
    status: str  # completed | failed | cancelled | timeout | refused
    asset_id: str | None
    error: str | None


async def _load_job(job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        return await db.get(VideoJob, job_id)


async def generate(ctx, *, key: str, prompt: str, reference: str | None, duration: int, ratio: str,
                   on_submitted=None) -> Outcome:
    """Submit one clip and wait for it; ``on_submitted(job_id)`` runs once the provider has it."""
    from tool import video_production as vp
    from tool.video_production import VideoGenerateArgs, VideoInputRef
    args = VideoGenerateArgs(
        action="submit", prompt=prompt, idempotency_key=key, duration=duration, ratio=ratio,
        allow_duplicate=True,
        input_assets=[VideoInputRef(asset_id=reference, role="reference_image")] if reference else [],
    )
    result = await vp.execute_generate(args, ctx)
    meta = result.metadata or {}
    job_id = meta.get("job_id")
    if not job_id:
        return Outcome(None, "refused", None, f"{result.title}: {result.output}"[:400])
    if on_submitted is not None:
        await on_submitted(job_id)
    status, asset_id = meta.get("status"), meta.get("asset_id")
    deadline = time.monotonic() + MAX_GENERATION_SECONDS
    while status not in ("completed", "failed", "cancelled") and time.monotonic() < deadline:
        waited = await vp.execute_generate(
            VideoGenerateArgs(action="wait", job_id=job_id, wait_seconds=WAIT_SECONDS, wait_iteration=1), ctx)
        meta = waited.metadata or {}
        status = meta.get("status") or status
        asset_id = meta.get("asset_id") or asset_id
        if status not in ("completed", "failed", "cancelled"):
            await asyncio.sleep(2)
    if status == "completed":
        if not asset_id:
            job = await _load_job(job_id)
            asset_id = job.output_asset_id if job else None
        return Outcome(job_id, "completed", asset_id, None)
    if status in ("failed", "cancelled"):
        job = await _load_job(job_id)
        return Outcome(job_id, status, None, (job.error if job else None) or status)
    return Outcome(job_id, "timeout", None, "generation did not finish within the inline budget")


async def overlay(ctx, asset_id: str, *, shop_name: str, slogan: str, key: str, duration: float,
                  filename: str) -> str | None:
    """The shop name (and a slogan) under the clip; None when IMS refuses or fails."""
    from tool.video_compose import VideoComposeArgs, execute_compose
    end = float(duration) - 0.2
    texts = [{"text": shop_name, "from_sec": 0.6, "to_sec": end, "y": 0.76,
              "style": {"size": 64, "outline": 4}}]
    if slogan:
        texts.append({"text": slogan, "from_sec": 1.2, "to_sec": end, "y": 0.835,
                      "style": {"size": 38, "outline": 3}})
    timeline = {"version": 1, "shots": [{"asset": asset_id, "duration_sec": float(duration)}], "texts": texts}
    try:
        result = await execute_compose(VideoComposeArgs(action="submit", timeline=timeline, idempotency_key=key,
                                                        filename=filename), ctx)
        meta = result.metadata or {}
        job_id = meta.get("job_id")
        if not job_id:
            log.info("light overlay refused user=%s: %s", ctx.user_id, result.title)
            return None
        status, out_asset = meta.get("status"), meta.get("asset_id")
        deadline = time.monotonic() + MAX_OVERLAY_SECONDS
        while status not in ("completed", "failed", "cancelled") and time.monotonic() < deadline:
            waited = await execute_compose(
                VideoComposeArgs(action="wait", job_id=job_id, wait_seconds=WAIT_SECONDS, wait_iteration=1), ctx)
            meta = waited.metadata or {}
            status = meta.get("status") or status
            out_asset = meta.get("asset_id") or out_asset
            if status not in ("completed", "failed", "cancelled"):
                await asyncio.sleep(2)
        return out_asset if status == "completed" else None
    except Exception as exc:  # the plain clip is still a result
        log.warning("light overlay failed user=%s error=%s", ctx.user_id, type(exc).__name__)
        return None
