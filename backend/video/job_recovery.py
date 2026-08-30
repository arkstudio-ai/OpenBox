"""Recovery for stranded direct video segment jobs.

Today the inline `video_generate` tool call owns provider polling and the
download → OSS → business-table finalization. When that process exits mid-way
(deploy, crash, aborted turn) the job stays in `in_progress`/`finalizing`
forever even though the provider output is already paid for — nobody re-drives
it until the agent happens to call the tool again. This module re-drives
finalization from process startup and from the cron timer's maintenance
piggyback, using the video domain row as the sole source of truth.

Recovery only: it re-checks provider state and re-runs the tool's idempotent
finalization, and never submits new provider work. Ambiguous `submitting` rows
(no provider task id) are left for operator review, matching the tool.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("video.recovery")

SWEEP_INTERVAL_MS = 60 * 1000    # piggyback cadence on the cron timer tick
STALE_AFTER_SECONDS = 120        # untouched this long → no live tool call is polling it
FINALIZING_STALE_SECONDS = 300   # matches the tool's stale-finalization threshold
LOOKBACK_DAYS = 7                # bound each sweep to recent recoverable work
MAX_JOBS_PER_SWEEP = 10

_last_sweep_at_ms: int = 0
_startup_task: asyncio.Task | None = None
_failed_once: set[str] = set()
_route_mismatch_once: set[str] = set()
_route_mismatch_overflow_warned = False
_scan_after: tuple[datetime, str] | None = None


def schedule_startup_recovery() -> None:
    """Kick one background sweep right after boot (multi-user mode only)."""
    global _startup_task
    loop = asyncio.get_event_loop()
    if loop.is_closed():
        return
    _startup_task = loop.create_task(_startup_sweep())


async def _startup_sweep() -> None:
    try:
        advanced = await sweep()
        if advanced:
            log.info(f"Startup video job recovery advanced {advanced} job(s)")
    except Exception as exc:
        log.warning(
            f"Startup video job recovery failed: {type(exc).__name__}"
        )


async def sweep_if_due() -> None:
    """Run a sweep if enough time has passed. Called from the cron timer tick."""
    global _last_sweep_at_ms
    now_ms = int(time.time() * 1000)
    if now_ms - _last_sweep_at_ms < SWEEP_INTERVAL_MS:
        return
    _last_sweep_at_ms = now_ms
    await sweep()


async def sweep() -> int:
    """Run one recovery pass; returns how many jobs reached a new settled state."""
    global _scan_after

    from db import base as db_base

    if db_base._engine is None:  # startup/shutdown edge: no database is available
        return 0

    from sqlalchemy import and_, or_, select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=STALE_AFTER_SECONDS)
    lookback_cutoff = now - timedelta(days=LOOKBACK_DAYS)

    conditions = [
        VideoJob.kind == "segment",
        VideoJob.status.in_(
            ("queued", "in_progress", "finalizing", "transfer_failed")
        ),
        VideoJob.provider_task_id.isnot(None),
        VideoJob.updated_at < stale_cutoff,
        VideoJob.updated_at >= lookback_cutoff,
    ]
    if _scan_after is not None:
        cursor_updated_at, cursor_id = _scan_after
        conditions.append(
            or_(
                VideoJob.updated_at > cursor_updated_at,
                and_(
                    VideoJob.updated_at == cursor_updated_at,
                    VideoJob.id > cursor_id,
                ),
            )
        )

    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(VideoJob)
                .where(*conditions)
                .order_by(VideoJob.updated_at.asc(), VideoJob.id.asc())
                .limit(MAX_JOBS_PER_SWEEP)
            )
        ).scalars().all()

    # Route-mismatched rows intentionally retain their old updated_at. Advance
    # a process-local keyset cursor so ten such rows cannot permanently starve
    # newer recoverable work. A short final page wraps the next sweep back to
    # the oldest rows, allowing a restored provider route to be discovered.
    if len(rows) == MAX_JOBS_PER_SWEEP:
        _scan_after = (rows[-1].updated_at, rows[-1].id)
    else:
        _scan_after = None

    advanced = 0
    for job in rows:
        try:
            recovered = await _recover_job(job)
            # Any successful provider check is a state change for failure
            # reporting, even while the remote task is still running. A later
            # outage should therefore be visible once again.
            _failed_once.discard(job.id)
            if recovered:
                advanced += 1
        except Exception as exc:
            # A job whose provider lookup keeps failing (expired relay task,
            # revoked key) would otherwise warn every sweep. The OpenBox
            # logger itself runs at DEBUG, so logging repeats at debug level is
            # still noisy; stay silent until a successful check resets it.
            if job.id not in _failed_once:
                log.warning(
                    f"Video job recovery for {job.id} failed: "
                    f"{type(exc).__name__}"
                )
                _failed_once.add(job.id)
                if len(_failed_once) > 500:
                    _failed_once.clear()
    return advanced


async def _recover_job(job) -> bool:
    global _route_mismatch_overflow_warned

    from tool import video_production as vp
    from tool.video_providers import provider_route_mismatch

    if job.status == "finalizing":
        # A live finalization younger than the tool's own threshold may still
        # be streaming to OSS in another process; only reclaim past it.
        if _age_seconds(job.updated_at) < FINALIZING_STALE_SECONDS:
            return False

    # Route from the model the job was actually submitted with. Resolving the
    # default would poll the wrong endpoint and read the wrong response shape
    # for anything off the default model — and this sweep exists precisely to
    # rescue jobs nobody else is watching.
    target, settings = vp._configured_target(job.model or None)
    mismatch = provider_route_mismatch(job.request_data, target)
    if mismatch:
        # A not-found response from another provider/account says nothing about
        # the task that was actually paid for. Quarantine it for operator review
        # instead of polling the wrong endpoint forever or falsely settling it
        # as failed. This check deliberately precedes reclaiming finalization so
        # a mismatch performs no database mutation at all.
        if job.id not in _route_mismatch_once:
            if len(_route_mismatch_once) < 500:
                log.warning(f"Video job recovery skipped for {job.id}: {mismatch}")
                _route_mismatch_once.add(job.id)
            elif not _route_mismatch_overflow_warned:
                # Keep memory and logs bounded without clearing the set: a
                # clear would make the same 500 jobs warn again every minute.
                log.warning(
                    "Additional video recovery route mismatches are being suppressed"
                )
                _route_mismatch_overflow_warned = True
        return False

    if job.status == "finalizing":
        if not await _reclaim_stale_finalizing(job.id):
            return False
        job = await _load(job.id)
        if job is None or job.status != "transfer_failed":
            return False

    data = await vp._provider_status(target, job.provider_task_id)
    state = vp._provider_state(data, target)
    ctx = _recovery_context(job)

    if state == "completed":
        refreshed = await vp._finalize_segment(job, data, ctx, settings, target)
        done = bool(refreshed is not None and refreshed.status == "completed")
        if done:
            log.info(f"Recovered stranded video job {job.id} to completed")
        return done

    if state in ("failed", "cancelled"):
        detail = data.get("error")
        message = detail.get("message") if isinstance(detail, dict) else str(detail or state)
        await vp._update_job(
            job.id,
            status=state,
            error=str(message)[:1000],
            completed_at=datetime.now(timezone.utc),
        )
        await vp._mark_asset(job.output_asset_id, status="failed")
        if job.segment_id:
            from tool.video_workflow import mark_segment_job

            await mark_segment_job(
                job.segment_id,
                job.id,
                user_id=job.user_id,
                status=state,
            )
        log.info(f"Settled stranded video job {job.id} as {state}")
        return True

    # Provider still working: refresh status/updated_at so staleness stays honest.
    await vp._update_job(job.id, status=state, error=None)
    return False


async def _reclaim_stale_finalizing(job_id: str) -> bool:
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        result = await db.execute(
            update(VideoJob)
            .where(VideoJob.id == job_id, VideoJob.status == "finalizing")
            .values(
                status="transfer_failed",
                error="recovering a stale OSS finalization",
                updated_at=datetime.now(timezone.utc),
            )
        )
    return result.rowcount == 1


async def _load(job_id: str):
    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return await db.get(VideoJob, job_id)


def _recovery_context(job):
    # No message_id: finalization completes OSS and business tables, while chat
    # attachment still happens on the next tool call that sees the job.
    from tool.tool import ToolContext

    return ToolContext(session_id=job.session_id or "", user_id=job.user_id)


def _age_seconds(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()
