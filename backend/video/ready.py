"""Wait for useful media results without sending polling ticks to the model."""
from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Literal

from sqlalchemy import select

from core.log import create_logger
from db.base import get_db_session
from db.models.video_job import VideoJob
from tool.tool import ToolContext, ToolResult

log = create_logger("video.ready")
POLL_SECONDS = 4
WAIT_SECONDS = 15 * 60
TERMINAL = {"completed", "failed", "cancelled"}
_segments: dict[str, asyncio.Task] = {}


def managed(job: VideoJob) -> bool:
    """Built-in pipelines own their own polling, settlement and delivery."""
    if not (getattr(job, "request_data", None) or {}).get("submit_run_id"):
        return False
    from light.first_video import is_first_video_job
    from light.remake_case import owns_job
    from light.shot_editor import is_edit_job
    from video.compose_recovery import SELF_DELIVERED_PREFIXES

    return not (is_first_video_job(job) or owns_job(job) or is_edit_job(job)
                or (job.idempotency_key or "").startswith(SELF_DELIVERED_PREFIXES))


def watch(job: VideoJob) -> None:
    if not job.provider_task_id or job.status in TERMINAL:
        return
    if job.kind == "compose":
        from video.compose_recovery import watch as watch_compose
        watch_compose(job.id)
    elif job.id not in _segments or _segments[job.id].done():
        task = asyncio.create_task(_watch_segment(job.id), name=f"video-ready:{job.id}")
        _segments[job.id] = task
        task.add_done_callback(lambda done: _segments.pop(job.id, None) if _segments.get(job.id) is done else None)


async def _watch_segment(job_id: str) -> None:
    from video.job_recovery import _load, _recover_job

    loop = asyncio.get_running_loop()
    deadline = loop.time() + WAIT_SECONDS
    while loop.time() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        try:
            job = await _load(job_id)
            if job is None or job.status in TERMINAL or await _recover_job(job):
                return
        except Exception as exc:
            log.warning("Background status check for %s failed (%s)", job_id, type(exc).__name__)
            await asyncio.sleep(2 * POLL_SECONDS)


async def wait_for_ready(kind: Literal["segment", "compose"], ids: Sequence[str],
                         ctx: ToolContext) -> list[VideoJob] | ToolResult:
    """Return all currently actionable receipts as soon as any requested job settles."""
    ids = list(dict.fromkeys(ids))
    query = select(VideoJob).where(VideoJob.user_id == ctx.user_id, VideoJob.session_id == ctx.session_id,
                                   VideoJob.kind == kind, VideoJob.id.in_(ids))
    deadline = asyncio.get_running_loop().time() + WAIT_SECONDS
    while True:
        await ctx.assert_run_current()
        if ctx.abort.is_set():
            raise asyncio.CancelledError()
        async with get_db_session() as db:
            rows = {job.id: job for job in (await db.scalars(query)).all()}
        if len(rows) != len(ids):
            return ToolResult(title="Video job not found", output="Use job IDs from this conversation.",
                              metadata={"error": True})
        ready = []
        for key in ids:
            job = rows[key]
            if job.status in TERMINAL or (job.status == "submitting" and not job.provider_task_id):
                ready.append(job)
            else:
                watch(job)
        if ready:
            from video import shot_review
            reviewed_ids = [job.id for job in ready if job.status == "completed"
                            and (job.request_data or {}).get("background_review")]
            if not reviewed_ids:
                return ready
            states = await shot_review.states(ctx, reviewed_ids)
            if len(states) != len(reviewed_ids):
                return ToolResult(title="Reviewed shot unavailable", output="A requested shot is no longer available in this conversation.",
                                  metadata={"error": True})
            actionable = [job for job in ready if states.get(job.id) not in shot_review.PENDING]
            # A failed render or review is actionable right away. On the happy path
            # one tool return carries all requested reviews while the worker reads them as they land.
            if len(actionable) == len(ids) or any(states.get(job.id) != "ready" for job in actionable):
                return actionable
        if asyncio.get_running_loop().time() >= deadline:
            return ToolResult(title="Background rendering continues",
                              output="The jobs are still running. End this turn; completion recovery will notify this conversation.",
                              metadata={"polling_paused": True, "still_running": True})
        await asyncio.sleep(POLL_SECONDS)


def body_timeout(args: dict, ctx: ToolContext) -> float:
    return WAIT_SECONDS + 60.0 if args.get("action") == "await_ready" else 600.0


async def acknowledge_failure(job: VideoJob) -> None:
    data = getattr(job, "result_data", None) or {}
    if job.status == "failed" and data.get("completion_delivery_pending"):
        from tool.video_production import _update_job
        await _update_job(job.id, expected_statuses={"failed"}, result_data={**data, "completion_delivery_pending": False})
