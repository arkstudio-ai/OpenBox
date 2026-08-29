"""Reconciliation: the runtime's safety net.

Wake signals may be lost and workers may die mid-step; periodic scans make the
ledger converge anyway. Three duties (§7.3): reclaim expired running leases,
requeue due external waits, and enforce total deadlines. Waiting on a user is
never treated as a failure.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, or_, select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.skill_job import SkillJob
from db.models.skill_job_attempt import SkillJobAttempt
from db.models.skill_job_event import SkillJobEvent
from db.models.skill_job_input import SkillJobInput
from skill_runtime import repository as repo
from skill_runtime.types import (
    DesiredState,
    InputKind,
    JobEventType,
    JobStatus,
    WAITING_STATUSES,
    operator_reconciliation_wait,
    outcome_payload_summary,
)
from skill_runtime.worker import _retry_at

log = create_logger("skill_runtime.reconciler")

RECONCILE_INTERVAL_SECONDS = 5.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def reconcile_once() -> dict[str, int]:
    lost = await expire_stale_running()
    external_expired = await enforce_external_wait_limits()
    # Enforce the cumulative bound while the row still carries its active
    # waiting interval. A normal due wake would otherwise turn it into queued
    # first and postpone cancellation until a worker happens to claim it.
    requeued = await requeue_due_external()
    repaired_wakes = await repair_stranded_wakes()
    expired_asks = await expire_user_waits()
    deadlined = await enforce_deadlines()
    return {
        "lost_leases": lost,
        "requeued_external": requeued,
        "external_wait_expired": external_expired,
        "repaired_wakes": repaired_wakes,
        "expired_user_waits": expired_asks,
        "deadline_settled": deadlined,
    }


async def expire_user_waits() -> int:
    """A question nobody answers must not park a job forever: past the
    operation's userInputTimeoutSeconds the job gets a cancel request and is
    woken, so the handler settles it (§7.3 — a wait for a person is never a
    failure to retry, only one to time out)."""
    now = _utcnow()
    async with get_db_session() as db:
        waiting = (
            await db.execute(
                select(
                    SkillJob.id,
                    SkillJob.updated_at,
                    SkillJob.user_input_timeout_seconds,
                    SkillJob.progress_data,
                ).where(
                    SkillJob.status == JobStatus.WAITING_USER.value,
                    SkillJob.desired_state == DesiredState.RUN.value,
                )
            )
        ).all()

    expired = 0
    for job_id, updated_at, ttl, progress_data in waiting:
        input_schema = (progress_data or {}).get("input_schema") or {}
        if input_schema.get("x-operator-only") is True:
            # An operator audit is not an unanswered user question. It remains
            # blocked until a privileged decision arrives.
            continue
        parked_since = updated_at
        if parked_since is not None and parked_since.tzinfo is None:
            parked_since = parked_since.replace(tzinfo=timezone.utc)
        expires_at = None
        explicit = (progress_data or {}).get("expires_at")
        if explicit:
            try:
                expires_at = datetime.fromisoformat(str(explicit).replace("Z", "+00:00"))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning(f"Job {job_id} has invalid waiting-user expires_at={explicit!r}")
        if ttl and parked_since is not None:
            ttl_deadline = parked_since + timedelta(seconds=ttl)
            expires_at = min(expires_at, ttl_deadline) if expires_at else ttl_deadline
        if expires_at is None or now < expires_at:
            continue

        async with get_db_session() as db:
            result = await db.execute(
                update(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.WAITING_USER.value,
                    SkillJob.desired_state == DesiredState.RUN.value,
                )
                .values(
                    status=JobStatus.QUEUED.value,
                    desired_state=DesiredState.CANCEL.value,
                    next_run_at=now,
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
                    event_type=JobEventType.CANCEL_REQUESTED.value,
                    payload={"reason": "user_input_timeout"},
                    created_at=now,
                )
            )
        expired += 1
        log.info(f"Job {job_id} passed its user-input TTL; cancellation requested")
    return expired


async def expire_stale_running() -> int:
    """A running job whose lease expired has no live owner: close the attempt
    as lost and reschedule. On budget exhaustion, operations that may own remote
    state enter operator review instead of becoming an orphaning terminal."""
    now = _utcnow()
    async with get_db_session() as db:
        expired = (
            await db.execute(
                select(
                    SkillJob.id,
                    SkillJob.attempt_count,
                    SkillJob.retry_count,
                    SkillJob.max_attempts,
                    SkillJob.cancel_requires_handler,
                    SkillJob.checkpoint_data,
                    SkillJob.desired_state,
                ).where(
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_expires_at.isnot(None),
                    SkillJob.lease_expires_at < now,
                )
            )
        ).all()

    handled = 0
    for (
        job_id,
        attempt_count,
        retry_count,
        max_attempts,
        cancel_requires_handler,
        checkpoint_data,
        desired_state,
    ) in expired:
        next_retry_count = retry_count + 1
        exhausted = next_retry_count >= max_attempts
        cancel_without_external = (
            desired_state == DesiredState.CANCEL.value and not cancel_requires_handler
        )
        operator_review = exhausted and cancel_requires_handler
        target = (
            JobStatus.CANCELLED
            if cancel_without_external
            else JobStatus.WAITING_USER
            if operator_review
            else JobStatus.FAILED if exhausted else JobStatus.RETRY_SCHEDULED
        )
        values: dict = {
            "status": target.value,
            "lease_owner": None,
            "lease_expires_at": None,
            "last_event_seq": SkillJob.last_event_seq + 1,
            "retry_count": next_retry_count,
            "updated_at": now,
        }
        review_outcome = None
        if cancel_without_external:
            values.update(
                error_code=None,
                error_message=None,
                completed_at=now,
                next_run_at=None,
            )
        elif operator_review:
            review_outcome = operator_reconciliation_wait(
                checkpoint_data or {},
                detail=f"worker lease expired; retry budget spent ({next_retry_count})",
            )
            values.update(
                desired_state=DesiredState.CANCEL.value,
                progress_data={
                    "prompt": review_outcome.prompt,
                    "input_schema": review_outcome.input_schema,
                    "expires_at": None,
                },
                error_code="worker_lost",
                error_message=(
                    "automatic attempts exhausted while external state may still exist"
                ),
                next_run_at=None,
            )
        elif exhausted:
            values.update(
                error_code="worker_lost",
                error_message=f"lease expired with retry budget spent ({next_retry_count})",
                completed_at=now,
                next_run_at=None,
            )
        else:
            values.update(
                error_code="worker_lost",
                error_message="worker lease expired before the invocation settled",
                # A cancel that races lease recovery must not be put back onto
                # the ordinary failure backoff. Evaluate desired_state in the
                # guarded UPDATE itself so a concurrent request cannot be lost.
                next_run_at=case(
                    (SkillJob.desired_state == DesiredState.CANCEL.value, now),
                    else_=_retry_at(next_retry_count),
                ),
            )

        async with get_db_session() as db:
            result = await db.execute(
                update(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_expires_at < now,
                    # If cancellation races this scan, let its transaction win
                    # and re-evaluate with the new desired state next pass.
                    SkillJob.desired_state == desired_state,
                )
                .values(**values)
            )
            if result.rowcount != 1:
                continue  # the worker heartbeat won the race — lease is live again
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            if target is JobStatus.FAILED and job.session_id:
                # Dying here is the least visible death of all: no handler ran,
                # so nothing wrote an outcome and the only trace is a status
                # flip in a table. The session has to be told the same way it
                # is told about a handler's own failure.
                db.add(
                    repo.failure_notice(
                        job,
                        error_code=values.get("error_code"),
                        message=values.get("error_message"),
                        now=now,
                    )
                )
            db.add(
                SkillJobEvent(
                    id=repo.new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=(
                        JobEventType.CANCELLED.value
                        if cancel_without_external
                        else JobEventType.WAITING_USER.value
                        if operator_review
                        else JobEventType.FAILED.value
                        if exhausted
                        else JobEventType.RETRY_SCHEDULED.value
                    ),
                    payload=(
                        {
                            **outcome_payload_summary(review_outcome),
                            "error_code": "worker_lost",
                            "attempt": attempt_count,
                            "retry_count": next_retry_count,
                        }
                        if review_outcome is not None
                        else {
                            "reason": "cancel_requested",
                            "recovered_from": "worker_lost",
                            "attempt": attempt_count,
                            "retry_count": next_retry_count,
                        }
                        if cancel_without_external
                        else {
                            "error_code": "worker_lost",
                            "attempt": attempt_count,
                            "retry_count": next_retry_count,
                        }
                    ),
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
            if target in (JobStatus.FAILED, JobStatus.CANCELLED):
                await db.execute(
                    update(SkillJobInput)
                    .where(
                        SkillJobInput.job_id == job_id,
                        SkillJobInput.consumed_at.is_(None),
                    )
                    .values(consumed_at=now)
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


async def enforce_external_wait_limits() -> int:
    """Cancel jobs whose cumulative waiting_external budget has elapsed.

    This scan uses the admission snapshot on the job, not today's manifest.
    The cancel request wakes the handler so a provider task is reconciled and
    cancelled against remote facts instead of being abandoned locally.
    """
    now = _utcnow()
    async with get_db_session() as db:
        waiting = (
            await db.execute(
                select(
                    SkillJob.id,
                    SkillJob.user_id,
                    SkillJob.external_wait_seconds,
                    SkillJob.external_wait_started_at,
                    SkillJob.max_external_wait_seconds,
                ).where(
                    SkillJob.status == JobStatus.WAITING_EXTERNAL.value,
                    SkillJob.desired_state == DesiredState.RUN.value,
                    SkillJob.external_wait_started_at.isnot(None),
                )
            )
        ).all()

    expired = 0
    for job_id, user_id, accumulated, started_at, maximum in waiting:
        if not maximum or started_at is None:
            continue
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        total = int(accumulated or 0) + max(0, int((now - started_at).total_seconds()))
        if total < maximum:
            continue
        try:
            await repo.request_cancel(
                job_id,
                user_id,
                reason="external_wait_timeout",
            )
        except repo.JobNotFound:
            continue
        expired += 1
        log.warning(f"Job {job_id} exhausted cumulative external wait; cancellation requested")
    return expired


async def repair_stranded_wakes() -> int:
    """Backstop historical input/settlement lost-wake windows.

    New writes are transactionally coupled, but rows created by an older
    binary can still be parked with an unconsumed input. Only an input newer
    than the park transition is evidence of a lost wake; an older unconsumed
    input may have been deliberately rejected by the handler and must not cause
    a five-second hot loop.
    """
    async with get_db_session() as db:
        pending_input_jobs = (
            await db.execute(
                select(SkillJob.id)
                .join(SkillJobInput, SkillJobInput.job_id == SkillJob.id)
                .where(
                    or_(
                        SkillJob.status.in_(tuple(s.value for s in WAITING_STATUSES)),
                        and_(
                            SkillJob.status == JobStatus.RETRY_SCHEDULED.value,
                            SkillJobInput.kind == InputKind.PROVIDER_CALLBACK.value,
                        ),
                    ),
                    SkillJobInput.consumed_at.is_(None),
                    SkillJobInput.created_at > SkillJob.updated_at,
                )
                .distinct()
            )
        ).scalars().all()
    repaired = 0
    reasons = {job_id: "input_pending_repair" for job_id in pending_input_jobs}
    for job_id, reason in reasons.items():
        if await repo.wake_job(job_id, reason=reason):
            repaired += 1
    return repaired


async def enforce_deadlines() -> int:
    """Past deadline_at, a job that ever executed may hold external side
    effects (a provider task, a media job), so its termination MUST reach
    the handler: it gets a cancel request — woken immediately when waiting —
    and the handler settles against provider facts (§7.4, §10.2). Only a job
    that never ran (queued, attempt 0) fails directly."""
    now = _utcnow()
    async with get_db_session() as db:
        overdue = (
            await db.execute(
                select(
                    SkillJob.id,
                    SkillJob.user_id,
                    SkillJob.status,
                    SkillJob.attempt_count,
                ).where(
                    SkillJob.deadline_at.isnot(None),
                    SkillJob.deadline_at < now,
                    SkillJob.desired_state == DesiredState.RUN.value,
                    SkillJob.status.notin_([s.value for s in
                                            (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED)]),
                )
            )
        ).all()

    settled = 0
    for job_id, user_id, status, attempt_count in overdue:
        if status == JobStatus.QUEUED.value and attempt_count == 0:
            async with get_db_session() as db:
                result = await db.execute(
                    update(SkillJob)
                    .where(
                        SkillJob.id == job_id,
                        SkillJob.status == JobStatus.QUEUED.value,
                        SkillJob.attempt_count == 0,
                    )
                    .values(
                        status=JobStatus.FAILED.value,
                        error_code="deadline_exceeded",
                        error_message="job passed its total deadline before ever running",
                        completed_at=now,
                        next_run_at=None,
                        last_event_seq=SkillJob.last_event_seq + 1,
                        updated_at=now,
                    )
                )
                if result.rowcount != 1:
                    continue
                job = (
                    await db.execute(select(SkillJob).where(SkillJob.id == job_id))
                ).scalar_one()
                if job.session_id:
                    # It never ran, so there is nothing else anywhere that says
                    # what happened to it.
                    db.add(
                        repo.failure_notice(
                            job,
                            error_code="deadline_exceeded",
                            message="job passed its total deadline before ever running",
                            now=now,
                        )
                    )
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
                await db.execute(
                    update(SkillJobInput)
                    .where(
                        SkillJobInput.job_id == job_id,
                        SkillJobInput.consumed_at.is_(None),
                    )
                    .values(consumed_at=now)
                )
            settled += 1
            continue

        # Executed at least once (or currently running): use the same locked
        # cancellation path as the API. It folds an active external-wait
        # interval and wakes the job in the same transaction.
        try:
            await repo.request_cancel(job_id, user_id, reason="deadline_exceeded")
        except repo.JobNotFound:
            continue
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
            except Exception as exc:
                log.error(f"Reconcile pass failed: {type(exc).__name__}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
