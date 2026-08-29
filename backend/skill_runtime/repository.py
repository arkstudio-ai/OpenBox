"""Tenant- and lease-conditioned persistence for skill jobs.

Every query that touches a job carries the owning user or a claim's fencing
token; state changes ride single conditional UPDATEs (the pattern cron proved
safe across replicas) and commit together with their event rows. Nothing here
executes handlers — this is the control plane's ledger API.
"""
from __future__ import annotations

import hashlib
import json
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
    InputKind,
    JobEventType,
    JobStatus,
    NeedsAgent,
    Outcome,
    Retry,
    Succeeded,
    WaitExternal,
    WaitUser,
    attempt_outcome_label,
    operator_reconciliation_wait,
    outcome_payload_summary,
)

log = create_logger("skill_runtime.repository")

MAX_PROGRESS_BYTES = 64 * 1024
MAX_JOB_INPUT_BYTES = 256 * 1024

_CLAIMABLE = tuple(s.value for s in CLAIMABLE_STATUSES)
_WAITING = tuple(s.value for s in WAITING_STATUSES)
_WAKEABLE = (*_WAITING, JobStatus.RETRY_SCHEDULED.value)
_TERMINAL = tuple(s.value for s in TERMINAL_STATUSES)


class IdempotencyConflict(Exception):
    """Same (user, skill, operation, idempotency_key) with a different request."""


class StaleLeaseError(Exception):
    """A write carried a lease token that no longer owns the job."""


class JobNotFound(Exception):
    pass


class InputNotAllowed(Exception):
    """The input kind/content is invalid for the job's current state."""


def new_id(prefix: str) -> str:
    """Time-ordered id, same ULID scheme as the rest of the codebase."""
    from core.identifier import ascending

    return ascending(prefix)


def request_hash_of(input_data: dict) -> str:
    canonical = json.dumps(input_data or {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _lease_is_live(expires_at: datetime | None, now: datetime) -> bool:
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at >= now


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
    output_schema: dict | None = None,
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
    invocation_timeout_seconds: int = 120,
    max_external_wait_seconds: int = 86400,
    user_input_timeout_seconds: int | None = None,
    cancel_requires_handler: bool = False,
    continue_agent_on_success: bool = False,
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
        output_schema=output_schema or {},
        checkpoint_data={},
        progress_data={},
        result_data={},
        idempotency_key=idempotency_key,
        request_hash=req_hash,
        desired_state=DesiredState.RUN.value,
        attempt_count=0,
        retry_count=0,
        max_attempts=max_attempts,
        next_run_at=now,
        deadline_at=deadline_at,
        lease_token=0,
        handler_version=handler_version,
        image_digest=image_digest,
        invocation_timeout_seconds=invocation_timeout_seconds,
        max_external_wait_seconds=max_external_wait_seconds,
        user_input_timeout_seconds=user_input_timeout_seconds,
        cancel_requires_handler=cancel_requires_handler,
        continue_agent_on_success=continue_agent_on_success,
        external_wait_seconds=0,
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
    if existing.session_id != session_id or existing.project_id != project_id:
        raise IdempotencyConflict(
            f"job {existing.id} holds idempotency key {idempotency_key!r} "
            "for a different session/project scope"
        )
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
        stmt = stmt.order_by(SkillJob.created_at.desc()).limit(max(1, min(limit, 200)))
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
                    .limit(max(1, min(limit, 500)))
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
    queue_limit: int = 0,
    per_user_limit: int = 0,
    limit: int = 1,
) -> list[ClaimedJob]:
    """Claim up to `limit` due jobs with the conditional-UPDATE pattern.

    Losing a race costs one rowcount-0 UPDATE; winners get a fresh fencing
    token, an attempt row and a job.claimed event in the same transaction.
    """
    now = _utcnow()
    async with get_db_session() as db:
        # Filter already-saturated tenants in SQL, then interleave the due
        # rows by per-tenant rank. A global LIMIT over next_run_at alone lets
        # one saturated tenant fill the whole candidate window forever.
        running = (
            select(
                SkillJob.user_id.label("running_user_id"),
                func.count().label("running_count"),
            )
            .where(SkillJob.status == JobStatus.RUNNING.value)
            .group_by(SkillJob.user_id)
            .subquery()
        )
        due = (
            select(
                SkillJob.id.label("job_id"),
                SkillJob.user_id.label("user_id"),
                SkillJob.next_run_at.label("next_run_at"),
                SkillJob.queue_name.label("queue_name"),
                func.row_number()
                .over(
                    partition_by=(SkillJob.queue_name, SkillJob.user_id),
                    order_by=(SkillJob.next_run_at.asc(), SkillJob.created_at.asc()),
                )
                .label("tenant_rank"),
            )
            .outerjoin(running, running.c.running_user_id == SkillJob.user_id)
            .where(
                SkillJob.status.in_(_CLAIMABLE),
                SkillJob.queue_name.in_(queues),
                SkillJob.next_run_at.isnot(None),
                SkillJob.next_run_at <= now,
                (
                    SkillJob.deadline_at.is_(None)
                    | (SkillJob.deadline_at > now)
                    | (SkillJob.desired_state == DesiredState.CANCEL.value)
                ),
            )
        )
        if per_user_limit > 0:
            due = due.where(func.coalesce(running.c.running_count, 0) < per_user_limit)
        ranked = due.subquery()
        queue_fair = (
            select(
                ranked.c.job_id,
                ranked.c.user_id,
                ranked.c.next_run_at,
                ranked.c.queue_name,
                ranked.c.tenant_rank,
                func.row_number()
                .over(
                    partition_by=ranked.c.queue_name,
                    order_by=(
                        ranked.c.tenant_rank.asc(),
                        ranked.c.next_run_at.asc(),
                        ranked.c.job_id.asc(),
                    ),
                )
                .label("queue_rank"),
            )
            .subquery()
        )
        candidates = (
            await db.execute(
                select(
                    queue_fair.c.job_id,
                    queue_fair.c.user_id,
                    queue_fair.c.queue_name,
                )
                .order_by(
                    queue_fair.c.queue_rank.asc(),
                    queue_fair.c.tenant_rank.asc(),
                    queue_fair.c.next_run_at.asc(),
                )
                .limit(max(limit * 16, 64))
            )
        ).all()

    claimed: list[ClaimedJob] = []
    for candidate_id, candidate_user, candidate_queue in candidates:
        if len(claimed) >= limit:
            break
        got = await _claim_one(
            candidate_id,
            candidate_user=candidate_user,
            candidate_queue=candidate_queue,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            queue_limit=queue_limit,
            per_user_limit=per_user_limit,
        )
        if got is not None:
            claimed.append(got)
    return claimed


async def _claim_one(
    job_id: str,
    *,
    candidate_user: str,
    candidate_queue: str,
    worker_id: str,
    lease_seconds: int,
    queue_limit: int = 0,
    per_user_limit: int = 0,
) -> ClaimedJob | None:
    now = _utcnow()
    async with get_db_session() as db:
        if queue_limit > 0 and db.get_bind().dialect.name == "postgresql":
            # Worker concurrency is a resource-pool policy, not a per-process
            # suggestion. Serialize count+claim for this queue so adding API
            # replicas does not multiply provider/remote-node concurrency.
            await db.execute(
                select(
                    func.pg_advisory_xact_lock(
                        func.hashtextextended(
                            f"skill-job-queue:{candidate_queue}", 0
                        )
                    )
                )
            )
            queue_running = (
                await db.execute(
                    select(func.count()).select_from(SkillJob).where(
                        SkillJob.queue_name == candidate_queue,
                        SkillJob.status == JobStatus.RUNNING.value,
                    )
                )
            ).scalar_one()
            if queue_running >= queue_limit:
                return None

        if per_user_limit > 0:
            # PostgreSQL production replicas serialize the count+claim pair per
            # tenant. Fencing protects a job row, but without this tenant lock
            # two workers can both observe one free user slot and overbook it.
            # SQLite is the documented single-worker development path.
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(
                    select(func.pg_advisory_xact_lock(func.hashtextextended(candidate_user, 0)))
                )
            running_count = (
                await db.execute(
                    select(func.count()).select_from(SkillJob).where(
                        SkillJob.user_id == candidate_user,
                        SkillJob.status == JobStatus.RUNNING.value,
                    )
                )
            ).scalar_one()
            if running_count >= per_user_limit:
                return None

        result = await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.user_id == candidate_user,
                SkillJob.queue_name == candidate_queue,
                SkillJob.status.in_(_CLAIMABLE),
                SkillJob.next_run_at <= now,
                (
                    SkillJob.deadline_at.is_(None)
                    | (SkillJob.deadline_at > now)
                    | (SkillJob.desired_state == DesiredState.CANCEL.value)
                ),
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
    async with get_db_session() as db:
        expires_at = (
            await db.execute(
                select(SkillJob.lease_expires_at)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_token == lease_token,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = _utcnow()
        if not _lease_is_live(expires_at, now):
            return False
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


async def allow_external_start(
    job_id: str,
    lease_token: int,
    *,
    extend_seconds: int = 60,
    attempt_id: str | None = None,
) -> bool:
    """Fence the boundary immediately before a new external side effect.

    Returns False when cancellation won the row lock first. A True result means
    this invocation won the ordering point and may start one idempotent/audited
    external call; cancellation that commits afterwards is an ordinary race the
    handler must reconcile. A dead lease raises instead of being confused with
    a user cancellation.
    """
    async with get_db_session() as db:
        current = (
            await db.execute(
                select(SkillJob.lease_expires_at, SkillJob.desired_state)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_token == lease_token,
                )
                .with_for_update()
            )
        ).one_or_none()
        now = _utcnow()
        if current is None or not _lease_is_live(current.lease_expires_at, now):
            raise StaleLeaseError(f"job {job_id}: external start with stale lease")
        await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.status == JobStatus.RUNNING.value,
                SkillJob.lease_token == lease_token,
            )
            .values(lease_expires_at=now + timedelta(seconds=extend_seconds), updated_at=now)
        )
        if attempt_id:
            await db.execute(
                update(SkillJobAttempt)
                .where(SkillJobAttempt.id == attempt_id)
                .values(heartbeat_at=now)
            )
        return current.desired_state == DesiredState.RUN.value


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
    if progress_data is not None:
        if not isinstance(progress_data, dict):
            raise ValueError("progress_data must be an object")
        try:
            encoded = json.dumps(
                progress_data,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("progress_data must be JSON-serializable") from exc
        if len(encoded) > MAX_PROGRESS_BYTES:
            raise ValueError(
                f"progress_data exceeds the {MAX_PROGRESS_BYTES}-byte limit"
            )
    if phase is not None and (
        not isinstance(phase, str) or not phase or len(phase) > 64
    ):
        raise ValueError("phase must contain 1 to 64 characters")
    async with get_db_session() as db:
        current = (
            await db.execute(
                select(SkillJob.phase, SkillJob.lease_expires_at)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_token == lease_token,
                )
                .with_for_update()
            )
        ).one_or_none()
        now = _utcnow()
        if current is None or not _lease_is_live(current.lease_expires_at, now):
            raise StaleLeaseError(f"job {job_id}: progress write with stale lease")
        phase_changed = phase is not None and phase != current.phase
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
    consumed_input_ids: list[str] | None = None,
    observed_input_ids: list[str] | None = None,
    cancel_known_at_claim: bool = False,
) -> SkillJob:
    """Apply an invocation outcome and every related side effect atomically.

    The job row is locked before cancellation arbitration. That closes the
    classic gap where cancel/input lands after a worker checked but before it
    parks: whichever transaction wins, the loser observes the new state and
    performs the wake in the same commit. Input consumption is part of this
    transaction as well, so a process crash cannot strand an admitted answer.
    """
    consumed_input_ids = list(dict.fromkeys(consumed_input_ids or []))
    observed_input_ids = list(dict.fromkeys(observed_input_ids or []))

    async with get_db_session() as db:
        current = (
            await db.execute(
                select(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.status == JobStatus.RUNNING.value,
                    SkillJob.lease_token == lease_token,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        now = _utcnow()
        if current is None or not _lease_is_live(current.lease_expires_at, now):
            raise StaleLeaseError(f"job {job_id}: settle with stale lease")

        if isinstance(outcome, NeedsAgent) and not current.session_id:
            # NeedsAgent is a typed continuation, not a generic human question.
            # Without a source session there is no legal producer for the
            # required agent_result input; an empty waiting card would park
            # forever while pretending the job was recoverable.
            outcome = Failed(
                error_code="agent_session_required",
                message="handler requested Agent continuation for a job with no session",
            )

        if (
            current.desired_state == DesiredState.CANCEL.value
            and not current.cancel_requires_handler
            and isinstance(outcome, (WaitExternal, WaitUser, NeedsAgent, Retry, Failed))
        ):
            # This operation declared that it owns no external state. Once a
            # cancellation wins the row lock there is nothing for another
            # invocation to unwind, so do not park or honour a retry backoff.
            # A result that already reached Succeeded still wins the race; a
            # failure from work the user stopped does not override cancellation.
            outcome = Cancelled()

        validated_artifact_ids: list[str] = []
        if isinstance(outcome, Succeeded) and outcome.artifacts:
            from db.models.file_asset import FileAsset

            requested_artifact_ids = list(dict.fromkeys(outcome.artifacts))
            owned_ready_artifact_ids = set(
                (
                    await db.execute(
                        select(FileAsset.id)
                        .where(
                            FileAsset.id.in_(requested_artifact_ids),
                            FileAsset.user_id == current.user_id,
                            FileAsset.status == "ready",
                            FileAsset.is_deleted.is_(False),
                        )
                        # Keep the output postcondition true until this job,
                        # its terminal event and artifact relations commit.
                        .with_for_update()
                    )
                ).scalars().all()
            )
            if owned_ready_artifact_ids != set(requested_artifact_ids):
                invalid = sorted(set(requested_artifact_ids) - owned_ready_artifact_ids)
                log.error(
                    f"job {job_id}: handler returned invalid/unowned artifacts {invalid}"
                )
                outcome = Failed(
                    error_code="artifact_contract_violation",
                    message="handler returned an output artifact that is not ready and owned",
                )
                validated_artifact_ids = []
            else:
                # Validation is a set-membership question, but artifact order is
                # part of the handler's result contract. SQL does not guarantee
                # an IN-query's row order, so retain the declaration order.
                validated_artifact_ids = requested_artifact_ids

        waiting_outcome = isinstance(outcome, (WaitExternal, WaitUser, NeedsAgent))
        wake_after_settle = False
        wake_reason: str | None = None
        if current.desired_state == DesiredState.CANCEL.value and waiting_outcome:
            acknowledges = bool(getattr(outcome, "acknowledges_cancel", False))
            if not acknowledges:
                if cancel_known_at_claim:
                    # The handler was explicitly invoked to unwind external
                    # state but returned an ordinary park. Never manufacture a
                    # cancelled fact: treat this as a contract fault, preserve
                    # its checkpoint, and retry with a bounded failure budget.
                    outcome = Retry(
                        checkpoint=outcome.checkpoint,
                        error_code="cancel_unacknowledged",
                        error_message=(
                            "handler returned a waiting outcome without acknowledging "
                            "an already-visible cancel request"
                        ),
                        retry_at=now + timedelta(seconds=30),
                    )
                    waiting_outcome = False
                else:
                    # Cancel landed during the invocation. Preserve the new
                    # checkpoint, but queue it immediately so the next handler
                    # invocation sees cancel before doing any more work.
                    wake_after_settle = True
                    wake_reason = "cancel_pending"

        external_review_retry_count: int | None = None
        external_review_error_code: str | None = None
        if isinstance(outcome, Retry) and outcome.consume_budget:
            proposed_retry_count = current.retry_count + 1
            if proposed_retry_count >= current.max_attempts and current.cancel_requires_handler:
                # A handler that may own remote state cannot be made terminal by
                # a local fault budget: that would stop all reconciliation while
                # the side effect could still be live. Freeze forward progress
                # and require a privileged, audited retry instead.
                external_review_retry_count = proposed_retry_count
                external_review_error_code = outcome.error_code
                outcome = operator_reconciliation_wait(
                    outcome.checkpoint,
                    # Error codes are safe card metadata; arbitrary exception
                    # text may contain provider details or secrets.
                    detail=outcome.error_code,
                )
                waiting_outcome = True

        values: dict = {
            "updated_at": now,
            "lease_owner": None,
            "lease_expires_at": None,
            "external_wait_started_at": None,
            "last_event_seq": SkillJob.last_event_seq + 1,
            # A later healthy step supersedes a prior transient retry error.
            # Error outcomes below write their own fields back explicitly;
            # normal waits/continuations/cancellation must not render as
            # "processing" while carrying a stale failure banner.
            "error_code": None,
            "error_message": None,
        }
        if external_review_retry_count is not None:
            values.update(
                retry_count=external_review_retry_count,
                desired_state=DesiredState.CANCEL.value,
                error_code=external_review_error_code,
                error_message=(
                    "automatic retries exhausted while external state may still exist"
                ),
            )
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
                external_wait_started_at=now,
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
                # The card renders from the snapshot alone (§12.2), so the ask
                # must live on the row, not only in the event log.
                progress_data={
                    "prompt": outcome.prompt,
                    "input_schema": outcome.input_schema or {},
                    "expires_at": outcome.expires_at.isoformat() if outcome.expires_at else None,
                },
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
            next_retry_count = current.retry_count + (1 if outcome.consume_budget else 0)
            values["retry_count"] = next_retry_count
            if outcome.consume_budget and next_retry_count >= current.max_attempts:
                target = JobStatus.FAILED
                event_type = JobEventType.FAILED
                values.update(
                    status=target.value,
                    error_code=outcome.error_code,
                    error_message=(
                        f"retry budget exhausted after {next_retry_count} failures: "
                        f"{outcome.error_message or outcome.error_code}"
                    ),
                    checkpoint_data=outcome.checkpoint,
                    completed_at=now,
                    next_run_at=None,
                )
            else:
                target = JobStatus.RETRY_SCHEDULED
                event_type = JobEventType.RETRY_SCHEDULED
                values.update(
                    status=target.value,
                    checkpoint_data=outcome.checkpoint,
                    error_code=outcome.error_code,
                    error_message=outcome.error_message or None,
                    # A cancel request already wakes the first cleanup attempt.
                    # If that attempt faults (or its pinned handler is absent),
                    # honour the requested backoff instead of hot-looping every
                    # worker poll until the deployment recovers.
                    next_run_at=outcome.retry_at,
                )
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

        if target in TERMINAL_STATUSES:
            # No later invocation can consume anything. Because the job row is
            # locked, this also closes the race with add_input: an earlier input
            # is consumed here; a later transaction observes the terminal job
            # and is rejected instead of creating an orphan row.
            await db.execute(
                update(SkillJobInput)
                .where(
                    SkillJobInput.job_id == job_id,
                    SkillJobInput.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
        elif consumed_input_ids:
            # Consumption is an explicit handler acknowledgement, independent
            # of whether its next durable state is another wait or a retry. If
            # the handler applied an input before a transient downstream fault,
            # replaying that input on the retry would duplicate the application.
            await db.execute(
                update(SkillJobInput)
                .where(
                    SkillJobInput.job_id == job_id,
                    SkillJobInput.id.in_(consumed_input_ids),
                    SkillJobInput.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )

        operator_only_hold = (
            isinstance(outcome, WaitUser)
            and (outcome.input_schema or {}).get("x-operator-only") is True
        )
        if waiting_outcome and not wake_after_settle and not operator_only_hold:
            # Only an input admitted *after* the worker took its immutable
            # snapshot closes the park race. An observed-but-unhandled input is
            # deliberately left for a corrected/operator answer; treating it as
            # new here would immediately requeue the same invocation forever.
            pending_query = select(SkillJobInput.id).where(
                SkillJobInput.job_id == job_id,
                SkillJobInput.consumed_at.is_(None),
            )
            if observed_input_ids:
                pending_query = pending_query.where(
                    SkillJobInput.id.not_in(observed_input_ids)
                )
            pending_input = (
                await db.execute(pending_query.limit(1))
            ).scalar_one_or_none()
            if pending_input is not None:
                wake_after_settle = True
                wake_reason = "input_pending"

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

        if isinstance(outcome, Succeeded) and validated_artifact_ids:
            from db.models.skill_job_artifact import SkillJobArtifact

            for ordinal, asset_id in enumerate(validated_artifact_ids):
                db.add(
                    SkillJobArtifact(
                        id=new_id("sjar"),
                        job_id=job.id,
                        user_id=job.user_id,
                        asset_id=asset_id,
                        role="output",
                        ordinal=ordinal,
                        meta={},
                        created_at=now,
                    )
                )

        payload = outcome_payload_summary(outcome)
        if external_review_error_code is not None:
            payload = {
                **payload,
                "error_code": external_review_error_code,
                "retry_budget_exhausted": True,
            }
        elif target is JobStatus.FAILED and isinstance(outcome, Retry):
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
        if isinstance(outcome, NeedsAgent) and job.session_id and not wake_after_settle:
            continuation_payload = {
                key: value
                for key, value in (outcome.payload or {}).items()
                if key != "reason" and not str(key).startswith("_dispatch_")
            }
            db.add(
                SessionInbox(
                    id=new_id("sinb"),
                    session_id=job.session_id,
                    user_id=job.user_id,
                    kind="job_needs_agent",
                    source_job_id=job.id,
                    source_event_seq=job.last_event_seq,
                    payload={**continuation_payload, "reason": outcome.reason},
                    status="pending",
                    created_at=now,
                )
            )
        elif (
            isinstance(outcome, Succeeded)
            and job.session_id
            and getattr(job, "continue_agent_on_success", False)
        ):
            # A pipeline stage finished and its operation declared that the
            # workflow continues. Same queue as NeedsAgent, different contract:
            # the job is terminal, so the dispatcher must not write an
            # agent_result back into it — see inbox._is_write_back_kind.
            db.add(
                SessionInbox(
                    id=new_id("sinb"),
                    session_id=job.session_id,
                    user_id=job.user_id,
                    kind="job_completed",
                    source_job_id=job.id,
                    source_event_seq=job.last_event_seq,
                    payload={
                        "operation": job.operation,
                        "skill": job.skill_key,
                        "result": outcome.result or {},
                    },
                    status="pending",
                    created_at=now,
                )
            )
        elif isinstance(outcome, Failed) and job.session_id:
            # A job that dies out of band dies silently: the turn that started
            # it ended long ago, so nothing reports the failure and nobody
            # learns why the work stopped — the card just says "failed" next to
            # an error code. Waking the session hands the model the error so it
            # can correct the arguments and start again, which is what a person
            # would otherwise have to notice and ask for by hand.
            #
            # Unconditional, unlike the success path: a failure always needs
            # someone told. One notice per job (source_job_id is unique per
            # settle), so a corrected retry is a new job that earns its own.
            db.add(
                SessionInbox(
                    id=new_id("sinb"),
                    session_id=job.session_id,
                    user_id=job.user_id,
                    kind="job_failed",
                    source_job_id=job.id,
                    source_event_seq=job.last_event_seq,
                    payload={
                        "operation": job.operation,
                        "skill": job.skill_key,
                        "error_code": outcome.error_code,
                        "message": outcome.message,
                        "attempts": job.retry_count,
                        "input": job.input_data or {},
                    },
                    status="pending",
                    created_at=now,
                )
            )

        if wake_after_settle and target in WAITING_STATUSES:
            wake_values: dict = {
                "status": JobStatus.QUEUED.value,
                "next_run_at": now,
                "last_event_seq": SkillJob.last_event_seq + 1,
                "updated_at": now,
            }
            if target is JobStatus.WAITING_EXTERNAL:
                # This park and wake share one transaction, so its elapsed
                # contribution is zero; clear the marker explicitly.
                wake_values["external_wait_started_at"] = None
            await db.execute(
                update(SkillJob)
                .where(SkillJob.id == job_id, SkillJob.status == target.value)
                .values(**wake_values)
            )
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.PROGRESSED.value,
                    payload={"woke": wake_reason or "pending", "status": JobStatus.QUEUED.value},
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
                    error_code=(
                        external_review_error_code
                        or getattr(outcome, "error_code", None)
                    ),
                    error_message=(
                        "automatic retries exhausted; operator review required"
                        if external_review_error_code is not None
                        else getattr(outcome, "message", None)
                        or getattr(outcome, "error_message", None)
                    ),
                    duration_ms=duration_ms,
                )
            )
        # Refresh after an optional same-transaction wake so callers never
        # render the intermediate waiting state as authoritative.
        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
    return job


# ---------------------------------------------------------------------------
# Cancel / wake / inputs
# ---------------------------------------------------------------------------

def _external_wait_total(job: SkillJob, now: datetime) -> int:
    """Fold the current waiting_external interval into its cumulative total."""
    total = int(job.external_wait_seconds or 0)
    started = job.external_wait_started_at
    if started is None:
        return total
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return total + max(0, int((now - started).total_seconds()))


async def request_cancel(
    job_id: str,
    user_id: str,
    *,
    reason: str = "user_requested",
) -> SkillJob:
    """§7.4: cancel is a desired state, not a task kill.

    A job that never executed (queued, attempt 0) settles directly. Otherwise
    cancellation is durably requested and waiting jobs are woken atomically.
    On the next claim the admission-time ``cancel_requires_handler`` snapshot
    decides whether the generic runtime may settle it or its handler must first
    reconcile external state.
    """
    now = _utcnow()
    async with get_db_session() as db:
        job = (
            await db.execute(
                select(SkillJob)
                .where(SkillJob.id == job_id, SkillJob.user_id == user_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if job is None:
            raise JobNotFound(job_id)
        if job.status in _TERMINAL:
            return job

        if job.status == JobStatus.QUEUED.value and job.attempt_count == 0:
            await db.execute(
                update(SkillJob)
                .where(
                    SkillJob.id == job_id,
                    SkillJob.user_id == user_id,
                    SkillJob.status == JobStatus.QUEUED.value,
                    SkillJob.attempt_count == 0,
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
            await db.execute(
                update(SessionInbox)
                .where(
                    SessionInbox.source_job_id == job_id,
                    SessionInbox.status == "pending",
                )
                .values(status="expired", consumed_at=now)
            )
            await db.execute(
                update(SkillJobInput)
                .where(
                    SkillJobInput.job_id == job_id,
                    SkillJobInput.consumed_at.is_(None),
                )
                .values(consumed_at=now)
            )
            job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
            db.add(
                SkillJobEvent(
                    id=new_id("sjev"),
                    job_id=job.id,
                    user_id=job.user_id,
                    seq=job.last_event_seq,
                    event_type=JobEventType.CANCELLED.value,
                    payload={"before_first_run": True, "reason": reason},
                    created_at=now,
                )
            )
            return job

        newly_flagged = job.desired_state != DesiredState.CANCEL.value
        if not newly_flagged:
            # Cancellation is idempotent. In particular, do not let repeated
            # clicks bypass a handler's retry backoff or wake an operator-only
            # reconciliation hold that intentionally requires privileged
            # input before another external-state check.
            return job
        values: dict = {
            "desired_state": DesiredState.CANCEL.value,
            "updated_at": now,
        }
        values["last_event_seq"] = SkillJob.last_event_seq + 1
        if job.status in _WAITING:
            # Waiting states hold no lease. Wake in the same transaction that
            # records the desired state so cancellation can never be stranded.
            values.update(status=JobStatus.QUEUED.value, next_run_at=now)
            if job.status == JobStatus.WAITING_EXTERNAL.value:
                values.update(
                    external_wait_seconds=_external_wait_total(job, now),
                    external_wait_started_at=None,
                )
        elif job.status in (JobStatus.QUEUED.value, JobStatus.RETRY_SCHEDULED.value):
            values["next_run_at"] = now

        # A continuation that has not started must not launch after the source
        # job has entered cancellation. A processing continuation may already
        # own an Agent turn; its eventual input is rejected/consumed by the
        # terminal-state checks instead of being force-killed here.
        await db.execute(
            update(SessionInbox)
            .where(
                SessionInbox.source_job_id == job_id,
                SessionInbox.status == "pending",
            )
            .values(status="expired", consumed_at=now)
        )

        await db.execute(
            update(SkillJob)
            .where(
                SkillJob.id == job_id,
                SkillJob.user_id == user_id,
                SkillJob.status == job.status,
            )
            .values(**values)
        )
        job = (await db.execute(select(SkillJob).where(SkillJob.id == job_id))).scalar_one()
        db.add(
            SkillJobEvent(
                id=new_id("sjev"),
                job_id=job.id,
                user_id=job.user_id,
                seq=job.last_event_seq,
                event_type=JobEventType.CANCEL_REQUESTED.value,
                payload={"reason": reason},
                created_at=now,
            )
        )
        return job


async def wake_job(job_id: str, *, reason: str) -> bool:
    """Move a parked/backoff job to queued so a worker can claim it."""
    now = _utcnow()
    async with get_db_session() as db:
        current = (
            await db.execute(
                select(SkillJob)
                .where(SkillJob.id == job_id, SkillJob.status.in_(_WAKEABLE))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if current is None:
            return False
        input_schema = (current.progress_data or {}).get("input_schema") or {}
        if (
            current.status == JobStatus.WAITING_USER.value
            and input_schema.get("x-operator-only") is True
        ):
            # Operator review is a privileged hold, not an ordinary parked
            # state. Lost-wake repair, callback replay, or a generic wake must
            # not authorize another external reconciliation attempt.
            return False
        values: dict = {
            "status": JobStatus.QUEUED.value,
            "next_run_at": now,
            "last_event_seq": SkillJob.last_event_seq + 1,
            "updated_at": now,
        }
        if current.status == JobStatus.WAITING_EXTERNAL.value:
            values.update(
                external_wait_seconds=_external_wait_total(current, now),
                external_wait_started_at=None,
            )
        result = await db.execute(
            update(SkillJob)
            .where(SkillJob.id == job_id, SkillJob.status == current.status)
            .values(**values)
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
    """Idempotently admit an input and wake the job in one transaction.

    Duplicate delivery also repairs a missed wake. This matters after a crash:
    an idempotent callback replay is often the only signal the caller can send.
    """
    now = _utcnow()
    if kind not in {item.value for item in InputKind}:
        raise InputNotAllowed(f"unsupported input kind {kind!r}")
    if not isinstance(payload, dict):
        raise InputNotAllowed("input payload must be an object")
    try:
        encoded_payload = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InputNotAllowed("input payload must be JSON-serializable") from exc
    if len(encoded_payload) > MAX_JOB_INPUT_BYTES:
        raise InputNotAllowed(
            f"input payload exceeds the {MAX_JOB_INPUT_BYTES}-byte limit"
        )
    if not idempotency_key or len(idempotency_key) > 180:
        raise InputNotAllowed("input idempotency_key must contain 1 to 180 characters")

    # A replay is successful even after its first delivery already woke or
    # completed the job. Check it before state admission so idempotency never
    # degenerates into a misleading "wrong state" error.
    async with get_db_session() as db:
        existing = (
            await db.execute(
                select(SkillJobInput).where(
                    SkillJobInput.job_id == job_id,
                    SkillJobInput.user_id == user_id,
                    SkillJobInput.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
    if existing is not None:
        if existing.kind != kind or (existing.payload or {}) != payload:
            raise InputNotAllowed(
                f"input idempotency key {idempotency_key!r} was already used "
                "with different content"
            )
        await _repair_replayed_input_wake(existing)
        return existing, False

    row = SkillJobInput(
        id=new_id("sjin"),
        job_id=job_id,
        user_id=user_id,
        kind=kind,
        source_event_id=source_event_id,
        idempotency_key=idempotency_key,
        payload=payload,
        created_at=now,
    )
    try:
        async with get_db_session() as db:
            job = (
                await db.execute(
                    select(SkillJob).where(
                        SkillJob.id == job_id, SkillJob.user_id == user_id
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                raise JobNotFound(job_id)
            # Close the race between the optimistic replay lookup above and
            # this row lock. A concurrent first delivery may have committed
            # while we waited for the job; the same key must still replay as
            # success even though that delivery already changed job.status.
            concurrent_existing = (
                await db.execute(
                    select(SkillJobInput).where(
                        SkillJobInput.job_id == job_id,
                        SkillJobInput.user_id == user_id,
                        SkillJobInput.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
            if concurrent_existing is not None:
                if (
                    concurrent_existing.kind != kind
                    or (concurrent_existing.payload or {}) != payload
                ):
                    raise InputNotAllowed(
                        f"input idempotency key {idempotency_key!r} was already used "
                        "with different content"
                    )
                return concurrent_existing, False
            if job.status in _TERMINAL:
                # Accepting it would strand an orphan row no invocation can
                # ever consume, while the caller believes it was delivered.
                raise InputNotAllowed(f"job {job_id} already ended {job.status}")
            input_schema = (job.progress_data or {}).get("input_schema") or {}
            if kind == InputKind.USER_ANSWER.value:
                if job.status != JobStatus.WAITING_USER.value:
                    raise InputNotAllowed(
                        f"job {job_id} is {job.status}, not waiting for a user answer"
                    )
                if input_schema.get("x-operator-only") is True:
                    raise InputNotAllowed(
                        f"job {job_id} is waiting for a privileged platform operator decision"
                    )
                if not input_schema:
                    raise InputNotAllowed(f"job {job_id} is a notification and accepts no answer")
                if input_schema:
                    from skill_runtime.manifest import ManifestError, validate_schema_value

                    try:
                        validate_schema_value(input_schema, payload or {}, label="answer")
                    except ManifestError as exc:
                        raise InputNotAllowed(str(exc)) from exc
            elif kind == InputKind.OPERATOR_RESUME.value:
                if job.status != JobStatus.WAITING_USER.value:
                    raise InputNotAllowed(
                        f"job {job_id} is {job.status}, not waiting for operator review"
                    )
                if input_schema.get("x-operator-only") is not True:
                    raise InputNotAllowed(f"job {job_id} did not request operator review")
                from skill_runtime.manifest import ManifestError, validate_schema_value

                try:
                    validate_schema_value(input_schema, payload, label="operator input")
                except ManifestError as exc:
                    raise InputNotAllowed(str(exc)) from exc
            elif kind == InputKind.AGENT_RESULT.value:
                if job.status != JobStatus.WAITING_AGENT.value:
                    raise InputNotAllowed(
                        f"job {job_id} is {job.status}, not waiting for an Agent result"
                    )
            db.add(row)
            await db.flush()
            operator_only_hold = (
                job.status == JobStatus.WAITING_USER.value
                and input_schema.get("x-operator-only") is True
            )
            if kind == InputKind.PROVIDER_CALLBACK.value:
                # A callback may only wake the wait it can actually advance.
                # Waking a job that is asking a person (or awaiting an Agent
                # continuation) would discard that pending question: the human
                # answer then arrives to a job that is no longer asking and is
                # refused above.
                wakeable = job.status in (
                    JobStatus.WAITING_EXTERNAL.value,
                    JobStatus.RETRY_SCHEDULED.value,
                )
            else:
                # Every other kind already validated the exact state it answers.
                wakeable = job.status in _WAITING
            should_wake = wakeable and not (
                operator_only_hold and kind != InputKind.OPERATOR_RESUME.value
            )
            if should_wake:
                values: dict = {
                    "status": JobStatus.QUEUED.value,
                    "next_run_at": now,
                    "last_event_seq": SkillJob.last_event_seq + 1,
                    "updated_at": now,
                }
                if job.status == JobStatus.WAITING_EXTERNAL.value:
                    values.update(
                        external_wait_seconds=_external_wait_total(job, now),
                        external_wait_started_at=None,
                    )
                if job.status == JobStatus.WAITING_AGENT.value:
                    # The admitted Agent result wins over any duplicate pending
                    # continuation for the same wait. User/operator inputs are
                    # rejected above unless the job is WAITING_USER.
                    await db.execute(
                        update(SessionInbox)
                        .where(
                            SessionInbox.source_job_id == job_id,
                            SessionInbox.status == "pending",
                        )
                        .values(status="expired", consumed_at=now)
                    )
                await db.execute(
                    update(SkillJob)
                    .where(SkillJob.id == job_id, SkillJob.status == job.status)
                    .values(**values)
                )
                refreshed = (
                    await db.execute(select(SkillJob).where(SkillJob.id == job_id))
                ).scalar_one()
                db.add(
                    SkillJobEvent(
                        id=new_id("sjev"),
                        job_id=refreshed.id,
                        user_id=refreshed.user_id,
                        seq=refreshed.last_event_seq,
                        event_type=JobEventType.PROGRESSED.value,
                        payload={"woke": kind, "status": JobStatus.QUEUED.value},
                        created_at=now,
                    )
                )
    except IntegrityError as integrity_error:
        async with get_db_session() as db:
            existing = (
                await db.execute(
                    select(SkillJobInput).where(
                        SkillJobInput.job_id == job_id,
                        SkillJobInput.user_id == user_id,
                        SkillJobInput.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one_or_none()
        if existing is None:
            raise integrity_error
        if existing.kind != kind or (existing.payload or {}) != payload:
            raise InputNotAllowed(
                f"input idempotency key {idempotency_key!r} was already used "
                "with different content"
            )
        await _repair_replayed_input_wake(existing)
        return existing, False

    return row, True


async def _repair_replayed_input_wake(existing: SkillJobInput) -> None:
    """Wake only when this delivery is newer than the current park.

    An older unconsumed input may have been observed and deliberately left
    unacknowledged by the handler. Replaying it must not hot-loop that wait or
    release a later operator hold. The timestamp relation is the same durable
    evidence used by the Reconciler for rows written by pre-atomic binaries.
    """
    if existing.consumed_at is not None:
        return
    repairable_statuses = (
        _WAKEABLE
        if existing.kind == InputKind.PROVIDER_CALLBACK.value
        else _WAITING
    )
    async with get_db_session() as db:
        stranded = (
            await db.execute(
                select(SkillJob.id).where(
                    SkillJob.id == existing.job_id,
                    SkillJob.user_id == existing.user_id,
                    SkillJob.status.in_(repairable_statuses),
                    SkillJob.updated_at < existing.created_at,
                )
            )
        ).scalar_one_or_none()
    if stranded is not None:
        await wake_job(existing.job_id, reason=f"{existing.kind}_replay")


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
                    FileAsset.user_id == user_id,
                    FileAsset.status == "ready",
                    FileAsset.is_deleted.is_(False),
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
