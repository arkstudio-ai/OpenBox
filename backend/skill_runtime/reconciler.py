"""Reconciliation: the runtime's safety net.

Wake signals may be lost and workers may die mid-step; periodic scans make the
ledger converge anyway. Three duties (§7.3): reclaim expired running leases,
requeue due external waits, and enforce total deadlines. Waiting on a user is
never treated as a failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.skill_job import SkillJob
from db.models.skill_job_attempt import SkillJobAttempt
from db.models.skill_job_event import SkillJobEvent
from skill_runtime import repository as repo
from skill_runtime.types import DesiredState, JobEventType, JobStatus
from skill_runtime.worker import _retry_at

log = create_logger("skill_runtime.reconciler")

RECONCILE_INTERVAL_SECONDS = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def reconcile_once() -> dict[str, int]:
    lost = await expire_stale_running()
    requeued = await requeue_due_external()
    deadlined = await enforce_deadlines()
    return {"lost_leases": lost, "requeued_external": requeued, "deadline_settled": deadlined}


async def expire_stale_running() -> int:
    """A running job whose lease expired has no live owner: close the attempt
    as lost and reschedule (or fail once the attempt budget is spent)."""
    now = _utcnow()
    async with get_db_session() as db:
        expired = (
            await db.execute(
                select(
                    SkillJob.id, SkillJob.attempt_count, SkillJob.max_attempts
                ).where(
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_expires_at.isnot(None),
                    SkillJob.lease_expires_at < now,
                )
            )
        ).all()

    handled = 0
    for job_id, attempt_count, max_attempts in expired:
        exhausted = attempt_count >= max_attempts
        target = JobStatus.FAILED if exhausted else JobStatus.RETRY_SCHEDULED
        values: dict = {
            "status": target.value,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_event_seq": SkillJob.last_event_seq + 1,
            "updated_at": now,
        }
        if exhausted:
            values.update(
                error_code="worker_lost",
                error_message=f"lease expired with attempt budget spent ({attempt_count})",
                completed_at=now,
                next_run_at=None,
            )
        else:
            values.update(error_code="worker_lost", next_run_at=_retry_at(attempt_count))

        async with get_db_session() as db:
            result = await db.execute(
                update(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_expires_at < now,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                continue  # the worker heartbeat won the race — lease is live again
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=repo.new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=(
                        JobEventType.FAILED.value if exhausted else JobEventType.RETRY_SCHEDULED.value
                    ),
                    payload={"error_code": "worker_lost", "attempt": attempt_count},
                    created_at=now,
                )
            )
            await db.execute(
                update(SkillJobAttempt)
                .where(
                    SkillJobAttempt.job_id == job_id,
                    SkillJobAttempt.attempt_number == attempt_count,
                    SkillJobAttempt.ended_at.is_(None),
                )
                .values(ended_at=now, outcome="lost", error_code="worker_lost")
            )
        handled += 1
        log.warning(f"Reclaimed expired lease on job {job_id} -> {target.value}")
    return handled


async def requeue_due_external() -> int:
    """waiting_external past its wake_at gets one status-advance run — a
    planned check, not a failure retry."""
    now = _utcnow()
    async with get_db_session() as db:
        due = (
            await db.execute(
                select(SkillJob.id).where(
                    SkillJob.status == JobStatus.WAITING_EXTERNAL.value,
                    SkillJob.next_run_at.isnot(None),
                    SkillJob.next_run_at <= now,
                )
            )
        ).scalars().all()
    woken = 0
    for job_id in due:
        if await repo.wake_job(job_id, reason="external_due"):
            woken += 1
    return woken


async def enforce_deadlines() -> int:
    """Past deadline_at: settle unclaimed states as failed; a running job gets
    a cancel request so its lease holder can stop cleanly."""
    now = _utcnow()
    async with get_db_session() as db:
        overdue = (
            await db.execute(
                select(SkillJob.id, SkillJob.status).where(
                    SkillJob.deadline_at.isnot(None),
                    SkillJob.deadline_at < now,
                    SkillJob.status.notin_([s.value for s in
                                            (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)]),
                )
            )
        ).all()

    settled = 0
    for job_id, status in overdue:
        if status == JobStatus.RUNNING.value:
            async with get_db_session() as db:
                result = await db.execute(
                    update(SkillJob)
                    .where(
                        SkillJob.id == job_id,
                        SkillJob.status == JobStatus.RUNNING.value,
                        SkillJob.desired_state == DesiredState.RUN.value,
                    )
                    .values(
                        desired_state=DesiredState.CANCEL.value,
                        last_event_seq=SkillJob.last_event_seq + 1,
                        updated_at=now,
                    )
                )
                if result.rowcount == 1:
                    job = (
                        await db.execute(select(SkillJob).where(SkillJob.id == job_id))
                    ).scalar_one()
                    db.add(
                        SkillJobEvent(
                            id=repo.new_id("sjev"),
                            job_id=job.id,
                            user_id=job.user_id,
                            seq=job.last_event_seq,
                            event_type=JobEventType.CANCEL_REQUESTED.value,
                            payload={"reason": "deadline_exceeded"},
                            created_at=now,
                        )
                    )
                    settled += 1
            continue

        async with get_db_session() as db:
            result = await db.execute(
                update(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status.in_(
                        [
                            JobStatus.QUEUED.value,
                            JobStatus.RETRY_SCHEDULED.value,
                            JobStatus.WAITING_EXTERNAL.value,
                            JobStatus.WAITING_USER.value,
                            JobStatus.WAITING_AGENT.value,
                        ]
                    ),
                )
                .values(
                    status=JobStatus.FAILED.value,
                    error_code="deadline_exceeded",
                    error_message="job passed its total deadline",
                    completed_at=now,
                    next_run_at=None,
                    last_event_seq=SkillJob.last_event_seq + 1,
                    updated_at=now,
                )
            )
            if result.rowcount != 1:
                continue
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=repo.new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.FAILED.value,
                    payload={"error_code": "deadline_exceeded"},
                    created_at=now,
                )
            )
        settled += 1
    return settled


class Reconciler:
    def __init__(self, interval_seconds: float = RECONCILE_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await reconcile_once()
            except Exception as e:
                log.error(f"Reconcile pass failed: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
