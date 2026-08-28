"""Tenant- and lease-conditioned persistence for skill jobs.

Every query that touches a job carries the owning user or a claim's fencing
token; state changes ride single conditional UPDATEs (the pattern cron proved
safe across replicas) and commit together with their event rows. Nothing here
executes handlers — this is the control plane's ledger API.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from core.log import create_logger
from db.base import get_db_session
from db.models.session_inbox import SessionInbox
from db.models.skill_job import SkillJob
from db.models.skill_job_attempt import SkillJobAttempt
from db.models.skill_job_event import SkillJobEvent
from db.models.skill_job_input import SkillJobInput
from skill_runtime.types import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    Cancelled,
    DesiredState,
    Failed,
    JobEventType,
    JobStatus,
    NeedsAgent,
    Outcome,
    Retry,
    Succeeded,
    WaitExternal,
    WaitUser,
    attempt_outcome_label,
    outcome_payload_summary,
)

log = create_logger("skill_runtime.repository")

_CLAIMABLE = tuple(s.value for s in CLAIMABLE_STATUSES)
_WAITING = tuple(s.value for s in WAITING_STATUSES)
_TERMINAL = tuple(s.value for s in TERMINAL_STATUSES)


class IdempotencyConflict(Exception):
    """Same (user, skill, operation, idempotency_key) with a different request."""


class StaleLeaseError(Exception):
    """A write carried a lease token that no longer owns the job."""


class JobNotFound(Exception):
    pass


def new_id(prefix: str) -> str:
    """Time-ordered id: millisecond hex prefix keeps inserts roughly ascending."""
    return f"{prefix}_{int(time.time() * 1000):013x}{secrets.token_hex(5)}"


def request_hash_of(input_data: dict) -> str:
    canonical = json.dumps(input_data or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

async def admit_job(
    *,
    user_id: str,
    skill_key: str,
    operation: str,
    idempotency_key: str,
    input_data: dict,
    runtime_kind: str,
    queue_name: str = "default",
    session_id: str | None = None,
    project_id: str | None = None,
    skill_version: str = "",
    package_sha256: str = "",
    max_attempts: int = 8,
    deadline_at: datetime | None = None,
    handler_version: int = 1,
    image_digest: str = "",
) -> tuple[SkillJob, bool]:
    """Durably admit a job before anything wakes a worker (§0: DB before wake).

    Returns (job, created). A replay of the same request returns the existing
    job; the same key with a different request raises IdempotencyConflict.
    """
    now = _utcnow()
    req_hash = request_hash_of(input_data)
    job_id = new_id("sjob")
    job = SkillJob(
        id=job_id,
        user_id=user_id,
        session_id=session_id,
        project_id=project_id,
        skill_key=skill_key,
        skill_version=skill_version,
        package_sha256=package_sha256,
        operation=operation,
        runtime_kind=runtime_kind,
        queue_name=queue_name,
        status=JobStatus.QUEUED.value,
        phase="",
        input_data=input_data or {},
        checkpoint_data={},
        progress_data={},
        result_data={},
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        desired_state=DesiredState.RUN.value,
        attempt_count=0,
        max_attempts=max_attempts,
        next_run_at=now,
        deadline_at=deadline_at,
        lease_token=0,
        handler_version=handler_version,
        image_digest=image_digest,
        last_event_seq=1,
        created_at=now,
        updated_at=now,
    )
    try:
        async with get_db_session() as db:
            db.add(job)
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job_id,
                    user_id=user_id,
                    seq=1,
                    event_type=JobEventType.CREATED.value,
                    payload={"skill_key": skill_key, "operation": operation, "queue": queue_name},
                    created_at=now,
                )
            )
        return job, True
    except IntegrityError as exc:
        integrity_error = exc

    async with get_db_session() as db:
        existing = (
            await db.execute(
                select(SkillJob).where(
                    SkillJob.user_id == user_id,
                    SkillJob.skill_key == skill_key,
                    SkillJob.operation == operation,
                    SkillJob.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
    if existing is None:
        # The IntegrityError came from something else (e.g. FK); surface it.
        raise integrity_error
    if existing.request_hash and existing.request_hash != req_hash:
        raise IdempotencyConflict(
            f"job {existing.id} holds idempotency key {idempotency_key!r} with a different request"
        )
    return existing, False


# ---------------------------------------------------------------------------
# Reads (always user-scoped)
# ---------------------------------------------------------------------------

async def get_job(job_id: str, user_id: str) -> SkillJob | None:
    async with get_db_session() as db:
        return (
            await db.execute(
                select(SkillJob).where(SkillJob.id == job_id, SkillJob.user_id == user_id)
            )
        ).scalar_one_or_none()


async def list_jobs(
    user_id: str,
    *,
    session_id: str | None = None,
    statuses: tuple[str, ...] | None = None,
    limit: int = 50,
    before_created_at: datetime | None = None,
) -> list[SkillJob]:
    async with get_db_session() as db:
        stmt = select(SkillJob).where(SkillJob.user_id == user_id)
        if session_id:
            stmt = stmt.where(SkillJob.session_id == session_id)
        if statuses:
            stmt = stmt.where(SkillJob.status.in_(statuses))
        if before_created_at:
            stmt = stmt.where(SkillJob.created_at < before_created_at)
        stmt = stmt.order_by(SkillJob.created_at.desc()).limit(min(limit, 200))
        return list((await db.execute(stmt)).scalars().all())


async def get_events(job_id: str, user_id: str, after_seq: int = 0, limit: int = 200) -> list[SkillJobEvent]:
    async with get_db_session() as db:
        return list(
            (
                await db.execute(
                    select(SkillJobEvent)
                    .where(
                        SkillJobEvent.job_id == job_id,
                        SkillJobEvent.user_id == user_id,
                        SkillJobEvent.seq > after_seq,
                    )
                    .order_by(SkillJobEvent.seq.asc())
                    .limit(limit)
                )
            ).scalars().all()
        )


# ---------------------------------------------------------------------------
# Claim / lease / fencing (§7.2)
# ---------------------------------------------------------------------------

@dataclass
class ClaimedJob:
    job: SkillJob
    attempt_id: str

    @property
    def lease_token(self) -> int:
        return self.job.lease_token


async def claim_next(
    *,
    queues: tuple[str, ...],
    worker_id: str,
    lease_seconds: int = 60,
    per_user_limit: int = 0,
    limit: int = 1,
) -> list[ClaimedJob]:
    """Claim up to `limit` due jobs with the conditional-UPDATE pattern.

    Losing a race costs one rowcount-0 UPDATE; winners get a fresh fencing
    token, an attempt row and a job.claimed event in the same transaction.
    """
    now = _utcnow()
    async with get_db_session() as db:
        candidates = (
            await db.execute(
                select(SkillJob.id, SkillJob.user_id)
                .where(
                    SkillJob.status.in_(_CLAIMABLE),
                    SkillJob.desired_state == DesiredState.RUN.value,
                    SkillJob.queue_name.in_(queues),
                    SkillJob.next_run_at.isnot(None),
                    SkillJob.next_run_at <= now,
                )
                .order_by(SkillJob.next_run_at.asc())
                .limit(max(limit * 4, 16))
            )
        ).all()
        running_counts: dict[str, int] = {}
        if per_user_limit > 0:
            rows = (
                await db.execute(
                    select(SkillJob.user_id, func.count())
                    .where(SkillJob.status == JobStatus.RUNNING.value)
                    .group_by(SkillJob.user_id)
                )
            ).all()
            running_counts = {user: count for user, count in rows}

    claimed: list[ClaimedJob] = []
    for candidate_id, candidate_user in candidates:
        if len(claimed) >= limit:
            break
        if per_user_limit > 0 and running_counts.get(candidate_user, 0) >= per_user_limit:
            continue
        got = await _claim_one(candidate_id, worker_id=worker_id, lease_seconds=lease_seconds)
        if got is not None:
            claimed.append(got)
            running_counts[candidate_user] = running_counts.get(candidate_user, 0) + 1
    return claimed


async def _claim_one(job_id: str, *, worker_id: str, lease_seconds: int) -> ClaimedJob | None:
    now = _utcnow()
    async with get_db_session() as db:
        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status.in_(_CLAIMABLE),
                SkillJob.desired_state == DesiredState.RUN.value,
                SkillJob.next_run_at <= now,
            )
            .values(
                status=JobStatus.RUNNING.value,
                lease_owner=worker_id,
                lease_token=SkillJob.lease_token + 1,
                lease_expires_at=now + timedelta(seconds=lease_seconds),
                attempt_count=SkillJob.attempt_count + 1,
                last_event_seq=SkillJob.last_event_seq + 1,
                started_at=func.coalesce(SkillJob.started_at, now),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return None
        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
        attempt = SkillJobAttempt(
            id=new_id("sjat"),
            job_id=job.id,
            user_id=job.user_id,
            attempt_number=job.attempt_count,
            worker_id=worker_id,
            queue_name=job.queue_name,
            runtime_kind=job.runtime_kind,
            lease_token=job.lease_token,
            started_at=now,
            heartbeat_at=now,
            handler_version=job.handler_version,
            image_digest=job.image_digest,
        )
        db.add(attempt)
        db.add(
            SkillJobEvent(
                id=new_id("sjev"),
                job_id=job.id,
                user_id=job.user_id,
                seq=job.last_event_seq,
                event_type=JobEventType.CLAIMED.value,
                payload={"worker_id": worker_id, "attempt": job.attempt_count},
                created_at=now,
            )
        )
        return ClaimedJob(job=job, attempt_id=attempt.id)


async def heartbeat(job_id: str, lease_token: int, *, extend_seconds: int = 60, attempt_id: str | None = None) -> bool:
    """Extend a live lease. False means the lease is gone — stop working."""
    now = _utcnow()
    async with get_db_session() as db:
        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status == JobStatus.RUNNING.value,
                SkillJob.lease_token == lease_token,
            )
            .values(lease_expires_at=now + timedelta(seconds=extend_seconds), updated_at=now)
        )
        alive = result.rowcount == 1
        if alive and attempt_id:
            await db.execute(
                update(SkillJobAttempt)
                .where(SkillJobAttempt.id == attempt_id)
                .values(heartbeat_at=now)
            )
    return alive


async def is_cancel_requested(job_id: str) -> bool:
    async with get_db_session() as db:
        desired = (
            await db.execute(select(SkillJob.desired_state).where(SkillJob.id == job_id))
        ).scalar_one_or_none()
    return desired == DesiredState.CANCEL.value


# ---------------------------------------------------------------------------
# Progress and settlement (all writes carry the claim's fencing token)
# ---------------------------------------------------------------------------

async def update_progress(
    job_id: str,
    lease_token: int,
    *,
    progress_data: dict | None = None,
    phase: str | None = None,
) -> None:
    """High-frequency progress updates touch the row; only a phase change emits
    an event (§6.3 keeps polling out of the event table)."""
    now = _utcnow()
    async with get_db_session() as db:
        current_phase = (
            await db.execute(
                select(SkillJob.phase).where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_token == lease_token,
                )
            )
        ).scalar_one_or_none()
        if current_phase is None:
            raise StaleLeaseError(f"job {job_id}: progress write with stale lease")

        phase_changed = phase is not None and phase != current_phase
        values: dict = {"updated_at": now}
        if progress_data is not None:
            values["progress_data"] = progress_data
        if phase is not None:
            values["phase"] = phase
        if phase_changed:
            values["last_event_seq"] = SkillJob.last_event_seq + 1
        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status == JobStatus.RUNNING.value,
                SkillJob.lease_token == lease_token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise StaleLeaseError(f"job {job_id}: progress write with stale lease")
        if phase_changed:
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.PROGRESSED.value,
                    payload={"phase": phase, "progress": progress_data or {}},
                    created_at=now,
                )
            )


async def settle_invocation(
    job_id: str,
    lease_token: int,
    outcome: Outcome,
    *,
    attempt_id: str | None = None,
    phase: str | None = None,
) -> SkillJob:
    """Apply one invocation's outcome: exactly one guarded transition, its
    event, and the attempt closure, in a single transaction."""
    now = _utcnow()
    values: dict = {
        "updated_at": now,
        "lease_owner": None,
        "lease_expires_at": None,
        "last_event_seq": SkillJob.last_event_seq + 1,
    }
    if phase is not None:
        values["phase"] = phase

    if isinstance(outcome, Succeeded):
        target = JobStatus.SUCCEEDED
        values.update(
            status=target.value,
            result_data=outcome.result or {},
            error_code=None,
            error_message=None,
            completed_at=now,
            next_run_at=None,
        )
        event_type = JobEventType.SUCCEEDED
    elif isinstance(outcome, WaitExternal):
        target = JobStatus.WAITING_EXTERNAL
        values.update(
            status=target.value,
            checkpoint_data=outcome.checkpoint,
            next_run_at=outcome.wake_at,
        )
        if outcome.progress is not None:
            values["progress_data"] = outcome.progress
        event_type = JobEventType.WAITING_EXTERNAL
    elif isinstance(outcome, WaitUser):
        target = JobStatus.WAITING_USER
        values.update(
            status=target.value,
            checkpoint_data=outcome.checkpoint,
            next_run_at=None,
        )
        event_type = JobEventType.WAITING_USER
    elif isinstance(outcome, NeedsAgent):
        target = JobStatus.WAITING_AGENT
        values.update(
            status=target.value,
            checkpoint_data=outcome.checkpoint,
            next_run_at=None,
        )
        event_type = JobEventType.NEEDS_AGENT
    elif isinstance(outcome, Retry):
        # Budget check happens against the row inside the transaction below.
        target = JobStatus.RETRY_SCHEDULED
        event_type = JobEventType.RETRY_SCHEDULED
    elif isinstance(outcome, Failed):
        target = JobStatus.FAILED
        values.update(
            status=target.value,
            error_code=outcome.error_code,
            error_message=outcome.message,
            completed_at=now,
            next_run_at=None,
        )
        event_type = JobEventType.FAILED
    elif isinstance(outcome, Cancelled):
        target = JobStatus.CANCELLED
        values.update(
            status=target.value,
            result_data=outcome.result or {},
            completed_at=now,
            next_run_at=None,
        )
        event_type = JobEventType.CANCELLED
    else:
        raise TypeError(f"unknown outcome: {outcome!r}")

    async with get_db_session() as db:
        if isinstance(outcome, Retry):
            row = (
                await db.execute(
                    select(SkillJob.attempt_count, SkillJob.max_attempts).where(
                        SkillJob.id == job_id,
                        SkillJob.status == JobStatus.RUNNING.value,
                        SkillJob.lease_token == lease_token,
                    )
                )
            ).one_or_none()
            if row is None:
                raise StaleLeaseError(f"job {job_id}: settle with stale lease")
            attempt_count, max_attempts = row
            if attempt_count >= max_attempts:
                target = JobStatus.FAILED
                event_type = JobEventType.FAILED
                values.update(
                    status=target.value,
                    error_code=outcome.error_code,
                    error_message=(
                        f"retry budget exhausted after {attempt_count} attempts: "
                        f"{outcome.error_message or outcome.error_code}"
                    ),
                    checkpoint_data=outcome.checkpoint,
                    completed_at=now,
                    next_run_at=None,
                )
            else:
                values.update(
                    status=target.value,
                    checkpoint_data=outcome.checkpoint,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message or None,
                    next_run_at=outcome.retry_at,
                )

        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status == JobStatus.RUNNING.value,
                SkillJob.lease_token == lease_token,
            )
            .values(**values)
        )
        if result.rowcount != 1:
            raise StaleLeaseError(f"job {job_id}: settle with stale lease")

        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
        payload = outcome_payload_summary(outcome)
        if target is JobStatus.FAILED and isinstance(outcome, Retry):
            payload = {"error_code": outcome.error_code, "retry_budget_exhausted": True}
        db.add(
            SkillJobEvent(
                id=new_id("sjev"),
                job_id=job.id,
                user_id=job.user_id,
                seq=job.last_event_seq,
                event_type=event_type.value,
                payload=payload,
                created_at=now,
            )
        )
        if isinstance(outcome, NeedsAgent) and job.session_id:
            db.add(
                SessionInbox(
                    id=new_id("sinb"),
                    session_id=job.session_id,
                    user_id=job.user_id,
                    kind="job_needs_agent",
                    source_job_id=job.id,
                    source_event_seq=job.last_event_seq,
                    payload={"reason": outcome.reason, **(outcome.payload or {})},
                    status="pending",
                    created_at=now,
                )
            )
        if attempt_id:
            started = (
                await db.execute(
                    select(SkillJobAttempt.started_at).where(SkillJobAttempt.id == attempt_id)
                )
            ).scalar_one_or_none()
            duration_ms = None
            if started is not None:
                started_aware = started if started.tzinfo else started.replace(tzinfo=timezone.utc)
                duration_ms = int((now - started_aware).total_seconds() * 1000)
            await db.execute(
                update(SkillJobAttempt)
                .where(SkillJobAttempt.id == attempt_id)
                .values(
                    ended_at=now,
                    outcome=attempt_outcome_label(outcome),
                    error_code=getattr(outcome, "error_code", None),
                    error_message=getattr(outcome, "message", None)
                    or getattr(outcome, "error_message", None),
                    duration_ms=duration_ms,
                )
            )
    return job


# ---------------------------------------------------------------------------
# Cancel / wake / inputs
# ---------------------------------------------------------------------------

async def request_cancel(job_id: str, user_id: str) -> SkillJob:
    """§7.4: cancel is a desired state. Unclaimed states settle immediately;
    a running invocation keeps its lease and observes the flag."""
    now = _utcnow()
    async with get_db_session() as db:
        owned = (
            await db.execute(
                select(SkillJob.id).where(SkillJob.id == job_id, SkillJob.user_id == user_id)
            )
        ).scalar_one_or_none()
        if owned is None:
            raise JobNotFound(job_id)

        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status.in_(_CLAIMABLE + _WAITING),
            )
            .values(
                status=JobStatus.CANCELLED.value,
                desired_state=DesiredState.CANCEL.value,
                completed_at=now,
                next_run_at=None,
                last_event_seq=SkillJob.last_event_seq + 1,
                updated_at=now,
            )
        )
        if result.rowcount == 1:
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.CANCELLED.value,
                    payload={"before_claim": True},
                    created_at=now,
                )
            )
            return job

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
        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
        if result.rowcount == 1:
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.CANCEL_REQUESTED.value,
                    payload={},
                    created_at=now,
                )
            )
        return job


async def wake_job(job_id: str, *, reason: str) -> bool:
    """Move a waiting job back to queued so a worker can claim it."""
    now = _utcnow()
    async with get_db_session() as db:
        result = await db.execute(
            update(SkillJob)
            .where(SkillJob.id == job_id, SkillJob.status.in_(_WAITING))
            .values(
                status=JobStatus.QUEUED.value,
                next_run_at=now,
                last_event_seq=SkillJob.last_event_seq + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            return False
        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
        db.add(
            SkillJobEvent(
                id=new_id("sjev"),
                job_id=job.id,
                user_id=job.user_id,
                seq=job.last_event_seq,
                event_type=JobEventType.PROGRESSED.value,
                payload={"woke": reason, "status": JobStatus.QUEUED.value},
                created_at=now,
            )
        )
    return True


async def add_input(
    job_id: str,
    user_id: str,
    *,
    kind: str,
    payload: dict,
    idempotency_key: str,
    source_event_id: str = "",
) -> tuple[SkillJobInput, bool]:
    """Idempotently admit an input, then wake the job. A duplicate key returns
    the original row and wakes nothing."""
    now = _utcnow()
    row = SkillJobInput(
        id=new_id("sjin"),
        job_id=job_id,
        user_id=user_id,
        kind=kind,
        source_event_id=source_event_id,
        idempotency_key=idempotency_key,
        payload=payload or {},
        created_at=now,
    )
    try:
        async with get_db_session() as db:
            owned = (
                await db.execute(
                    select(SkillJob.id).where(SkillJob.id == job_id, SkillJob.user_id == user_id)
                )
            ).scalar_one_or_none()
            if owned is None:
                raise JobNotFound(job_id)
            db.add(row)
    except IntegrityError:
        async with get_db_session() as db:
            existing = (
                await db.execute(
                    select(SkillJobInput).where(
                        SkillJobInput.job_id == job_id,
                        SkillJobInput.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
        return existing, False

    await wake_job(job_id, reason=kind)
    return row, True


async def list_artifacts(job_id: str, user_id: str) -> list[dict]:
    """Artifact rows joined with their asset's display fields, user-scoped."""
    from db.models.file_asset import FileAsset
    from db.models.skill_job_artifact import SkillJobArtifact

    async with get_db_session() as db:
        rows = (
            await db.execute(
                select(SkillJobArtifact, FileAsset)
                .join(FileAsset, FileAsset.id == SkillJobArtifact.asset_id)
                .where(
                    SkillJobArtifact.job_id == job_id,
                    SkillJobArtifact.user_id == user_id,
                )
                .order_by(SkillJobArtifact.ordinal.asc())
            )
        ).all()
    return [
        {
            "artifactId": artifact.id,
            "assetId": asset.id,
            "role": artifact.role,
            "ordinal": artifact.ordinal,
            "name": asset.name,
            "mime": asset.mime,
            "size": asset.size,
            "status": asset.status,
            "metadata": artifact.meta or {},
        }
        for artifact, asset in rows
    ]


async def unconsumed_inputs(job_id: str) -> list[SkillJobInput]:
    async with get_db_session() as db:
        return list(
            (
                await db.execute(
                    select(SkillJobInput)
                    .where(SkillJobInput.job_id == job_id, SkillJobInput.consumed_at.is_(None))
                    .order_by(SkillJobInput.created_at.asc())
                )
            ).scalars().all()
        )


async def mark_inputs_consumed(input_ids: list[str]) -> None:
    if not input_ids:
        return
    now = _utcnow()
    async with get_db_session() as db:
        await db.execute(
            update(SkillJobInput)
            .where(SkillJobInput.id.in_(input_ids), SkillJobInput.consumed_at.is_(None))
            .values(consumed_at=now)
        )
