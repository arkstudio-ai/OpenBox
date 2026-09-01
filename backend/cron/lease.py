"""Durable ownership and fencing for Cron executions.

Every scheduled and manual run enters through :func:`claim_job`.  A random
token identifies one claim while a monotonically increasing generation fences
an older backend replica after takeover.  Heartbeats keep healthy long-running
Agent jobs from being reclaimed by another scheduler.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import secrets
import socket
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, TypeVar

from core.log import create_logger
from cron.types import STUCK_RUN_MS


log = create_logger("cron.lease")

CRON_LEASE_TTL_SECONDS = 90
CRON_HEARTBEAT_SECONDS = 20
PROCESS_OWNER_ID = (
    f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(6)}"
)

T = TypeVar("T")


# SQLite has no advisory-lock namespace. ``BEGIN IMMEDIATE`` is the durable
# cross-process writer mutex for file-backed desktop databases, while this
# small per-event-loop guard prevents two sessions in this process from trying
# to start write transactions on the same aiosqlite connection concurrently.
# The lock covers the session context exit so commit is part of the critical
# section. Weak keys avoid binding a module-global asyncio primitive forever to
# a test/server event loop that has since been closed.
_SQLITE_CLAIM_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = weakref.WeakKeyDictionary()


def _sqlite_claim_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _SQLITE_CLAIM_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _SQLITE_CLAIM_LOCKS[loop] = lock
    return lock


def _quota_advisory_key(scope: str) -> int:
    """Return one stable signed-int64 PostgreSQL advisory-lock key."""
    digest = hashlib.sha256(
        f"openbox:cron:claim-quota:v1:{scope}".encode("utf-8")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


_GLOBAL_QUOTA_LOCK_KEY = _quota_advisory_key("global")


async def _lock_postgres_global_quota(db) -> None:
    """Acquire the first lock in the PostgreSQL Cron quota order."""
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _GLOBAL_QUOTA_LOCK_KEY},
    )


async def _lock_postgres_user_quota(db, user_id: str) -> None:
    """Acquire the second lock in the PostgreSQL Cron quota order."""
    from sqlalchemy import text

    if db.get_bind().dialect.name != "postgresql":
        return
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:key)"),
        {"key": _quota_advisory_key(f"user:{user_id}")},
    )


class CronLeaseLost(RuntimeError):
    """The job was taken over by a newer scheduler generation."""


@dataclass(frozen=True)
class CronLease:
    job_id: str
    token: str
    generation: int
    owner_id: str
    lease_expires_at: datetime

    def to_payload(self) -> dict:
        return {
            "job_id": self.job_id,
            "token": self.token,
            "generation": self.generation,
            "owner_id": self.owner_id,
            "lease_expires_at": self.lease_expires_at,
        }

    @classmethod
    def from_payload(cls, payload: dict | None) -> "CronLease | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                job_id=str(payload["job_id"]),
                token=str(payload["token"]),
                generation=int(payload["generation"]),
                owner_id=str(payload["owner_id"]),
                lease_expires_at=payload["lease_expires_at"],
            )
        except (KeyError, TypeError, ValueError):
            return None


def claimable_clause(job, now, *, legacy_cutoff=None):
    """SQL predicate for an unowned or expired CronJob row."""
    from sqlalchemy import and_, or_

    return or_(
        # Never claimed under the lease protocol.
        and_(job.run_token.is_(None), job.running_at.is_(None)),
        expired_claim_clause(job, now, legacy_cutoff=legacy_cutoff),
    )


def expired_claim_clause(job, now, *, legacy_cutoff=None):
    """SQL predicate for an existing claim that is safe to recover/take over."""
    from sqlalchemy import and_, or_

    if legacy_cutoff is None:
        # Python callers may keep using an application timestamp. Lease
        # ownership paths pass a database expression and an explicit database
        # cutoff so one replica's wall clock can never expire another's lease.
        legacy_cutoff = now - timedelta(milliseconds=STUCK_RUN_MS)
    return or_(
        # Pre-migration running marker: reclaim only after the legacy TTL.
        and_(
            job.run_token.is_(None),
            job.running_at.isnot(None),
            job.running_at < legacy_cutoff,
        ),
        # Modern lease with an explicit expiry.
        and_(
            job.run_token.isnot(None),
            job.lease_expires_at.isnot(None),
            job.lease_expires_at < now,
        ),
        # Defensive recovery for a malformed modern row. Do not guess until
        # its heartbeat/running marker is older than the generous legacy TTL.
        and_(
            job.run_token.isnot(None),
            job.lease_expires_at.is_(None),
            job.running_at.isnot(None),
            job.running_at < legacy_cutoff,
        ),
    )


def live_claim_clause(job, now, *, legacy_cutoff=None):
    """SQL predicate for a claim that still consumes concurrency quota."""
    from sqlalchemy import and_, or_

    if legacy_cutoff is None:
        legacy_cutoff = now - timedelta(milliseconds=STUCK_RUN_MS)
    return or_(
        and_(
            job.run_token.is_(None),
            job.running_at.isnot(None),
            job.running_at >= legacy_cutoff,
        ),
        and_(
            job.run_token.isnot(None),
            job.lease_expires_at.isnot(None),
            job.lease_expires_at >= now,
        ),
        and_(
            job.run_token.isnot(None),
            job.lease_expires_at.is_(None),
            job.running_at.isnot(None),
            job.running_at >= legacy_cutoff,
        ),
    )


def is_live_claim(job, now: datetime) -> bool:
    """Python equivalent of :func:`live_claim_clause` for API precedence."""
    running_at = job.running_at
    if running_at is not None and running_at.tzinfo is None:
        running_at = running_at.replace(tzinfo=timezone.utc)
    expires_at = job.lease_expires_at
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    if job.run_token is not None and expires_at is not None:
        return expires_at >= now
    if running_at is None:
        return False
    legacy_cutoff = now - timedelta(milliseconds=STUCK_RUN_MS)
    return running_at >= legacy_cutoff


def _database_now(db):
    """Statement-time database clock used by strict lease fences."""
    from sqlalchemy import func

    if db.get_bind().dialect.name == "postgresql":
        # PostgreSQL now() is transaction-start time and can be stale after a
        # row-lock wait; clock_timestamp() is evaluated at the actual fence.
        return func.clock_timestamp()
    return func.current_timestamp()


def _database_lease_expiry(db):
    """Database expression for one full lease from statement execution."""
    from sqlalchemy import func, text

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() + text(
            f"INTERVAL '{int(CRON_LEASE_TTL_SECONDS)} seconds'"
        )
    if dialect == "sqlite":
        return func.datetime(
            "now", f"+{int(CRON_LEASE_TTL_SECONDS)} seconds"
        )
    return _database_now(db) + timedelta(seconds=CRON_LEASE_TTL_SECONDS)


def _database_legacy_cutoff(db):
    """Database expression for the pre-lease stuck-marker cutoff."""
    from sqlalchemy import func, text

    seconds = STUCK_RUN_MS / 1000
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() - text(
            f"INTERVAL '{seconds:g} seconds'"
        )
    if dialect == "sqlite":
        return func.datetime("now", f"-{seconds:g} seconds")
    return _database_now(db) - timedelta(milliseconds=STUCK_RUN_MS)


def _aware_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def claim_job(
    job_id: str,
    *,
    user_id: str | None = None,
    require_enabled: bool = True,
    due_before: datetime | None = None,
    owner_id: str = PROCESS_OWNER_ID,
) -> CronLease | None:
    """Atomically acquire one CronJob within cluster-wide concurrency caps.

    PostgreSQL replicas serialize quota decisions with transaction-scoped
    advisory locks. SQLite uses ``BEGIN IMMEDIATE`` plus a process-local guard.
    Both scheduled and manual callers enter through this function, so neither
    path can observe a count and then race another job's independent UPDATE.
    """
    from db.base import get_db_session, get_engine
    from sqlalchemy import text

    token = secrets.token_hex(24)
    dialect = get_engine().dialect.name

    if dialect == "sqlite":
        async with _sqlite_claim_lock():
            async with get_db_session() as db:
                # This must be the first statement in the session. It obtains
                # SQLite's database writer reservation before quota reads and
                # holds it until get_db_session commits on context exit.
                await db.execute(text("BEGIN IMMEDIATE"))
                return await _claim_job_in_transaction(
                    db,
                    job_id,
                    token=token,
                    user_id=user_id,
                    require_enabled=require_enabled,
                    due_before=due_before,
                    owner_id=owner_id,
                )

    async with get_db_session() as db:
        return await _claim_job_in_transaction(
            db,
            job_id,
            token=token,
            user_id=user_id,
            require_enabled=require_enabled,
            due_before=due_before,
            owner_id=owner_id,
        )


async def _claim_job_in_transaction(
    db,
    job_id: str,
    *,
    token: str,
    user_id: str | None,
    require_enabled: bool,
    due_before: datetime | None,
    owner_id: str,
) -> CronLease | None:
    """Claim implementation whose caller owns the surrounding transaction."""
    from core.config import get_config
    from db.models.cron import CronJob, CronRun
    from sqlalchemy import func, or_, select, update

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # Every claimant takes locks in exactly this order. The global lock
        # makes the count/decision/update sequence linearizable across jobs
        # and replicas; the user lock names the narrower invariant explicitly
        # and keeps the protocol extensible without introducing lock inversion.
        await _lock_postgres_global_quota(db)

    target_predicates = [
        CronJob.id == job_id,
        CronJob.is_deleted == False,  # noqa: E712
    ]
    if user_id is not None:
        target_predicates.append(CronJob.user_id == user_id)
    target_user_id = await db.scalar(
        select(CronJob.user_id).where(*target_predicates)
    )
    if target_user_id is None:
        return None

    if dialect == "postgresql":
        await _lock_postgres_user_quota(db, target_user_id)

    database_now = _database_now(db)
    legacy_cutoff = _database_legacy_cutoff(db)
    live_predicate = live_claim_clause(
        CronJob,
        database_now,
        legacy_cutoff=legacy_cutoff,
    )
    config = get_config()
    global_limit = max(1, int(config.cron_max_concurrent_jobs))
    user_limit = max(1, int(config.cron_max_concurrent_per_user))

    global_live = int(
        await db.scalar(
            select(func.count())
            .select_from(CronJob)
            .where(
                CronJob.is_deleted == False,  # noqa: E712
                live_predicate,
            )
        )
        or 0
    )
    if global_live >= global_limit:
        return None

    user_live = int(
        await db.scalar(
            select(func.count())
            .select_from(CronJob)
            .where(
                CronJob.user_id == target_user_id,
                CronJob.is_deleted == False,  # noqa: E712
                live_claim_clause(
                    CronJob,
                    database_now,
                    legacy_cutoff=legacy_cutoff,
                ),
            )
        )
        or 0
    )
    if user_live >= user_limit:
        return None

    lease_until = _database_lease_expiry(db)
    predicates = [
        CronJob.id == job_id,
        CronJob.user_id == target_user_id,
        CronJob.is_deleted == False,  # noqa: E712
        claimable_clause(
            CronJob,
            database_now,
            legacy_cutoff=legacy_cutoff,
        ),
    ]
    if require_enabled:
        predicates.append(CronJob.enabled == True)  # noqa: E712
    if due_before is not None:
        # Due time is business scheduling time and may come from the timer's
        # application UTC clock. Lease expiry and quota liveness remain based
        # exclusively on the database statement clock.
        predicates.extend([
            CronJob.next_run_at.isnot(None),
            CronJob.next_run_at <= due_before,
        ])
    result = await db.execute(
        update(CronJob)
        .where(*predicates)
        .values(
            running_at=database_now,
            run_generation=CronJob.run_generation + 1,
            run_token=token,
            run_owner=owner_id,
            lease_expires_at=lease_until,
            heartbeat_at=database_now,
            updated_at=database_now,
        )
        .returning(CronJob.run_generation, CronJob.lease_expires_at)
    )
    claimed = result.one_or_none()
    generation = claimed.run_generation if claimed is not None else None

    if generation is not None:
        # A takeover must also close orphaned audit rows. Startup recovery
        # is not guaranteed to run between an expired generation and its
        # replacement on an already-live replica.
        await db.execute(
            update(CronRun)
            .where(
                CronRun.job_id == job_id,
                CronRun.status == "running",
                or_(
                    CronRun.claim_generation.is_(None),
                    CronRun.claim_generation < generation,
                ),
            )
            .values(
                status="error",
                error_message=(
                    f"Execution fenced by Cron generation {int(generation)}"
                ),
                ended_at=database_now,
            )
        )

    if generation is None:
        return None
    return CronLease(
        job_id=job_id,
        token=token,
        generation=int(generation),
        owner_id=owner_id,
        lease_expires_at=_aware_utc(claimed.lease_expires_at),
    )


async def claimed_job_payload(lease: CronLease) -> dict | None:
    """Read the latest execution config guarded by this exact live claim."""
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import select

    async with get_db_session() as db:
        database_now = _database_now(db)
        result = await db.execute(
            select(CronJob).where(
                CronJob.id == lease.job_id,
                CronJob.run_token == lease.token,
                CronJob.run_generation == lease.generation,
                CronJob.run_owner == lease.owner_id,
                CronJob.lease_expires_at.isnot(None),
                CronJob.lease_expires_at >= database_now,
                CronJob.is_deleted == False,  # noqa: E712
            )
        )
        row = result.scalar_one_or_none()
    if row is None:
        return None
    return {
        "id": row.id,
        "user_id": row.user_id,
        "project_id": row.project_id,
        "session_id": row.session_id,
        "name": row.name,
        "schedule": row.schedule,
        "task_prompt": row.task_prompt,
        "agent": row.agent,
        "model": row.model,
        "timeout_seconds": row.timeout_seconds,
        "delivery": row.delivery,
        "delete_after_run": row.delete_after_run,
        "max_retries": row.max_retries,
        "consecutive_errors": row.consecutive_errors,
        "summary_cache": row.summary_cache,
        "summary_cache_msg_id": row.summary_cache_msg_id,
        "_cron_claim": lease.to_payload(),
    }


async def renew_lease(lease: CronLease) -> datetime | None:
    """Extend an exact lease without racing the cluster quota snapshot."""
    from db.base import get_db_session, get_engine
    from sqlalchemy import text

    if get_engine().dialect.name == "sqlite":
        async with _sqlite_claim_lock():
            async with get_db_session() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                return await _renew_lease_in_transaction(db, lease)

    async with get_db_session() as db:
        return await _renew_lease_in_transaction(db, lease)


async def _renew_lease_in_transaction(db, lease: CronLease) -> datetime | None:
    """Renew while participating in the same global→user lock protocol."""
    from db.models.cron import CronJob
    from sqlalchemy import select, update

    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # A renewal can turn an apparently expired old row version back into a
        # committed live claim. Serialize it with claim snapshots so a claimer
        # cannot undercount while the renewal UPDATE is still uncommitted.
        await _lock_postgres_global_quota(db)

    lease_user_id = await db.scalar(
        select(CronJob.user_id).where(
            CronJob.id == lease.job_id,
            CronJob.is_deleted == False,  # noqa: E712
            CronJob.run_token == lease.token,
            CronJob.run_generation == lease.generation,
            CronJob.run_owner == lease.owner_id,
        )
    )
    if lease_user_id is None:
        return None
    if dialect == "postgresql":
        await _lock_postgres_user_quota(db, lease_user_id)

    database_now = _database_now(db)
    lease_until = _database_lease_expiry(db)
    result = await db.execute(
        update(CronJob)
        .where(
            CronJob.id == lease.job_id,
            CronJob.is_deleted == False,  # noqa: E712
            CronJob.run_token == lease.token,
            CronJob.run_generation == lease.generation,
            CronJob.run_owner == lease.owner_id,
            # A paused worker may not resurrect itself after its lease
            # deadline; once expired it must yield to the next generation.
            CronJob.lease_expires_at.isnot(None),
            CronJob.lease_expires_at >= database_now,
        )
        .values(heartbeat_at=database_now, lease_expires_at=lease_until)
        .returning(CronJob.lease_expires_at)
    )
    renewed_until = result.scalar_one_or_none()
    return _aware_utc(renewed_until) if renewed_until is not None else None


async def _renew_before_deadline(
    lease: CronLease,
    local_deadline: float,
) -> datetime | None:
    """Bound a possibly lock-blocked renewal by a monotonic local budget."""
    remaining = local_deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return await asyncio.wait_for(renew_lease(lease), timeout=remaining)
    except asyncio.TimeoutError:
        return None


async def _heartbeat_loop(
    lease: CronLease,
    stop: asyncio.Event,
    lost: asyncio.Event,
) -> None:
    # ``lease_expires_at`` is generated by the database and is only diagnostic
    # on this host. The database conditional UPDATE is the sole validity check;
    # local timing merely bounds how long this coroutine may wait for it.
    local_deadline = time.monotonic() + CRON_LEASE_TTL_SECONDS
    next_delay = CRON_HEARTBEAT_SECONDS
    while not stop.is_set():
        remaining = local_deadline - time.monotonic()
        if remaining <= 0:
            lost.set()
            return
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=min(next_delay, remaining),
            )
            return
        except asyncio.TimeoutError:
            pass

        # Never remain active past the last locally measured renewal budget.
        if time.monotonic() >= local_deadline:
            lost.set()
            return

        try:
            renewed_until = await _renew_before_deadline(lease, local_deadline)
        except Exception as exc:
            # A brief DB wobble should not cancel a healthy Agent immediately;
            # keep retrying until the last confirmed lease really expires.
            log.warning(
                "Cron lease heartbeat failed job=%s error_type=%s",
                lease.job_id,
                type(exc).__name__,
            )
            if time.monotonic() >= local_deadline:
                lost.set()
                return
            # Retry a transient DB failure promptly, while still bounding the
            # final sleep by the monotonic budget at the top of the loop.
            next_delay = min(5, CRON_HEARTBEAT_SECONDS)
            continue

        if renewed_until is None:
            lost.set()
            return
        local_deadline = time.monotonic() + CRON_LEASE_TTL_SECONDS
        next_delay = CRON_HEARTBEAT_SECONDS


async def run_with_heartbeat(
    lease: CronLease,
    work: Callable[[], Awaitable[T]],
    *,
    timeout: float,
) -> T:
    """Validate first, then run work while renewing the acquired claim."""
    # Do not translate the database's absolute expiry through this replica's
    # wall clock. A full local TTL bounds the validation call, while the DB
    # predicate inside renew_lease decides whether ownership is still valid.
    local_deadline = time.monotonic() + CRON_LEASE_TTL_SECONDS
    renewed_until = await _renew_before_deadline(lease, local_deadline)
    if renewed_until is None:
        raise CronLeaseLost(
            f"Cron lease expired before execution for {lease.job_id} "
            f"generation {lease.generation}"
        )
    active_lease = CronLease(
        job_id=lease.job_id,
        token=lease.token,
        generation=lease.generation,
        owner_id=lease.owner_id,
        lease_expires_at=renewed_until,
    )

    stop = asyncio.Event()
    lost = asyncio.Event()
    execution = asyncio.create_task(work())
    heartbeat = asyncio.create_task(_heartbeat_loop(active_lease, stop, lost))
    lost_waiter = asyncio.create_task(lost.wait())

    try:
        done, _ = await asyncio.wait(
            {execution, lost_waiter},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            execution.cancel()
            with contextlib.suppress(BaseException):
                await execution
            raise asyncio.TimeoutError
        if lost.is_set():
            execution.cancel()
            with contextlib.suppress(BaseException):
                await execution
            raise CronLeaseLost(
                f"Cron lease lost for {lease.job_id} generation {lease.generation}"
            )
        return await execution
    finally:
        stop.set()
        lost_waiter.cancel()
        with contextlib.suppress(BaseException):
            await lost_waiter
        with contextlib.suppress(BaseException):
            await heartbeat
