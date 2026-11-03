"""Re-drive composition jobs a crashed or aborted turn left in flight.

Same contract as ``job_recovery`` for generation: only re-check IMS and apply
its answer through the tool's own ``poll_compose_job``; never submit anything.
A composition that settles here finished after its turn gave up waiting, so
nobody would ever show it: the conversation is woken with a continuation that
fetches (and thereby attaches) the result, or explains the failure. Jobs that
a pipeline drives itself (the first video's name overlay) are left alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("video.compose_recovery")

STALE_AFTER_SECONDS = 120
LOOKBACK_DAYS = 7
MAX_JOBS_PER_SWEEP = 10
#: Idempotency-key prefixes of compositions whose own driver delivers the result.
SELF_DELIVERED_PREFIXES = ("first_video_text:", "light_autopilot:")


async def sweep() -> int:
    from db import base as db_base

    if db_base._engine is None:
        return 0
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from tool.video_compose import KIND, poll_compose_job

    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        rows = (await db.execute(
            select(VideoJob).where(
                VideoJob.kind == KIND,
                VideoJob.status == "in_progress",
                VideoJob.provider_task_id.isnot(None),
                VideoJob.updated_at < now - timedelta(seconds=STALE_AFTER_SECONDS),
                VideoJob.updated_at >= now - timedelta(days=LOOKBACK_DAYS),
            ).order_by(VideoJob.updated_at.asc()).limit(MAX_JOBS_PER_SWEEP)
        )).scalars().all()

    advanced = 0
    for job in rows:
        try:
            refreshed = await poll_compose_job(job)
            if refreshed is not None and refreshed.status != "in_progress":
                advanced += 1
                log.info(f"Recovered stranded composition {job.id} to {refreshed.status}")
                await wake_for(refreshed)
        except Exception as exc:
            log.debug(f"compose recovery for {job.id} failed: {type(exc).__name__}")
    return advanced


async def wake_for(job) -> bool:
    """Hand a composition that settled after its turn back to its conversation."""
    if job.attached_message_id or str(job.idempotency_key or "").startswith(SELF_DELIVERED_PREFIXES):
        return False
    from video.job_recovery import COMPOSE_FAILED_PROMPT, COMPOSE_RESUME_PROMPT, resume_conversation
    if job.status == "completed":
        return await resume_conversation(job, COMPOSE_RESUME_PROMPT)
    if job.status == "failed":
        return await resume_conversation(job, COMPOSE_FAILED_PROMPT)
    return False
