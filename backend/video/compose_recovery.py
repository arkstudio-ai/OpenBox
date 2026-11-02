"""Re-drive composition jobs a crashed or aborted turn left in flight.

Same contract as ``job_recovery`` for generation: only re-check IMS and apply
its answer through the tool's own ``poll_compose_job``; never submit anything.
A composition that settles here finished after its turn gave up waiting, so
nobody would ever show it: the conversation is woken with a continuation that
fetches (and thereby attaches) the result, or explains the failure. Jobs that
a pipeline drives itself (the first video's name overlay) are left alone.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("video.compose_recovery")

STALE_AFTER_SECONDS = 120
LOOKBACK_DAYS = 7
MAX_JOBS_PER_SWEEP = 10
#: A watched cut is asked about this often (IMS renders a 60 s cut in 20 s–3 min).
WATCH_INTERVAL_SECONDS = 4
WATCH_LIMIT_SECONDS = 15 * 60
#: While the agent's own turn runs it may still fetch the cut itself; check again this often.
BUSY_RECHECK_SECONDS = 5
_watching: dict[str, asyncio.Task] = {}
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


def watch(job_id: str) -> None:
    """Follow one submitted cut in this process and wake its chat the moment it settles.

    The sweep above remains the fallback (a restart drops the watcher); a cut
    the agent already attached is left alone.
    """
    if job_id in _watching and not _watching[job_id].done():
        return
    task = asyncio.create_task(_watch(job_id))
    _watching[job_id] = task
    task.add_done_callback(lambda _t: _watching.pop(job_id, None) if _watching.get(job_id) is _t else None)


async def _watch(job_id: str) -> None:
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    from tool.video_compose import poll_compose_job

    loop = asyncio.get_running_loop()
    deadline = loop.time() + WATCH_LIMIT_SECONDS
    job = None
    try:
        while loop.time() < deadline:
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            async with get_db_session() as db:
                job = await db.get(VideoJob, job_id)
            if job is None:
                return
            if job.status == "in_progress":
                job = await poll_compose_job(job)
            if job is not None and job.status != "in_progress":
                break
        if job is None or job.status == "in_progress":
            return
        # Let a running turn finish first: it may fetch (and attach) the cut itself.
        while loop.time() < deadline and await _session_busy(job.session_id):
            await asyncio.sleep(BUSY_RECHECK_SECONDS)
        async with get_db_session() as db:
            job = await db.get(VideoJob, job_id)
        if job is not None:
            await wake_for(job)
    except Exception as exc:  # the sweep still covers it
        log.warning(f"compose watcher for {job_id} stopped: {type(exc).__name__}: {exc}")


async def _session_busy(session_id: str | None) -> bool:
    if not session_id:
        return False
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
    return bool(session is not None and str(getattr(session, "status", "") or "") in ("busy", "retry", "compacting", "queued"))
