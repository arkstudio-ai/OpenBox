"""Transactional outbox publisher: DB events → bus notifications.

Events commit with their state change; this publisher delivers them to the
Redis/WS bus afterwards and stamps published_at. Delivery is at-least-once
(publish, then stamp) — clients order and dedup by (job_id, seq), and a
snapshot GET is always the source of truth, so a duplicate on the wire only
costs a no-op. A stamped event is never re-published, which is what keeps
replays from flooding users after a backlog.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.skill_job_event import SkillJobEvent

log = create_logger("skill_runtime.outbox")

SKILL_JOB_EVENT = "skill.job.event"

PUBLISH_INTERVAL_SECONDS = 1.0
BATCH_LIMIT = 200

_receipt_failures: dict[str, int] = {}


def _iso_utc(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def publish_pending(limit: int = BATCH_LIMIT) -> int:
    """Publish unpublished events in per-job seq order. Returns the count."""
    from bus import bus

    async with get_db_session() as db:
        # Pull a bounded stripe from every job, then interleave that stripe by
        # tenant. One user's thousands of jobs cannot fill the whole batch;
        # within each tenant, rank 1 for all jobs precedes rank 2 so per-job
        # sequence order is retained. A poisoned terminal receipt blocks only
        # its own job's later events.
        per_job = (
            select(
                SkillJobEvent.id.label("event_id"),
                SkillJobEvent.user_id.label("user_id"),
                SkillJobEvent.created_at.label("created_at"),
                func.row_number()
                .over(
                    partition_by=SkillJobEvent.job_id,
                    order_by=SkillJobEvent.seq.asc(),
                )
                .label("job_rank"),
            )
            .where(SkillJobEvent.published_at.is_(None))
            .subquery()
        )
        fair = (
            select(
                per_job.c.event_id,
                per_job.c.created_at,
                per_job.c.job_rank,
                func.row_number()
                .over(
                    partition_by=per_job.c.user_id,
                    order_by=(
                        per_job.c.job_rank.asc(),
                        per_job.c.created_at.asc(),
                        per_job.c.event_id.asc(),
                    ),
                )
                .label("tenant_rank"),
            )
            .where(per_job.c.job_rank <= 10)
            .subquery()
        )
        events = list(
            (
                await db.execute(
                    select(SkillJobEvent)
                    .join(fair, fair.c.event_id == SkillJobEvent.id)
                    .order_by(
                        fair.c.tenant_rank.asc(),
                        fair.c.job_rank.asc(),
                        fair.c.created_at.asc(),
                        fair.c.event_id.asc(),
                    )
                    .limit(limit)
                )
            ).scalars().all()
        )
    if not events:
        return 0

    published = 0
    stamped_ids: list[str] = []
    blocked_jobs: set[str] = set()
    for event in events:
        if event.job_id in blocked_jobs:
            continue
        # Receipt FIRST: it is durable state, the wire event is not. Once
        # published_at is stamped the event is never revisited, so a receipt
        # written after the stamp would be lost to any crash or DB blip in
        # between. Order receipt → publish → stamp keeps everything
        # at-least-once (the DB unique marker absorbs the replays).
        if event.event_type in ("job.succeeded", "job.failed", "job.cancelled"):
            try:
                from db.models.skill_job import SkillJob
                from skill_runtime.receipt import write_receipt

                async with get_db_session() as db:
                    job = await db.get(SkillJob, event.job_id)
                if job is not None:
                    await write_receipt(job)
            except Exception as exc:
                # Receipt delivery is part of the durable terminal contract. An
                # in-memory retry counter must never silently downgrade it after
                # a process restart; keep the event unstamped until the database
                # problem is repaired. Throttle repetitive logs only.
                attempts = _receipt_failures.get(event.id, 0) + 1
                _receipt_failures[event.id] = attempts
                if attempts == 1 or attempts % 60 == 0:
                    # A receipt that stays poisoned blocks this job's whole
                    # event stream, so the operator needs the actual cause —
                    # the class name alone cannot be acted on.
                    log.warning(
                        f"Chat receipt for {event.job_id} failed, will retry "
                        f"(attempt {attempts})",
                        exc_info=True,
                    )
                else:
                    log.debug(
                        f"Chat receipt for {event.job_id} still blocked: "
                        f"{type(exc).__name__}"
                    )
                blocked_jobs.add(event.job_id)
                continue

        try:
            await bus.publish_confirmed(
                SKILL_JOB_EVENT,
                {
                    "userId": event.user_id,
                    "jobId": event.job_id,
                    "seq": event.seq,
                    "eventType": event.event_type,
                    "payload": event.payload or {},
                    "createdAt": _iso_utc(event.created_at),
                },
            )
        except Exception as exc:
            # Leave unstamped; the next pass retries delivery.
            log.warning(
                f"Outbox publish failed for event {event.id}: "
                f"{type(exc).__name__}"
            )
            blocked_jobs.add(event.job_id)
            continue
        stamped_ids.append(event.id)
        _receipt_failures.pop(event.id, None)
        published += 1

    if stamped_ids:
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            await db.execute(
                update(SkillJobEvent)
                .where(SkillJobEvent.id.in_(stamped_ids), SkillJobEvent.published_at.is_(None))
                .values(published_at=now)
            )
    return published


class OutboxPublisher:
    """Background drain loop. Runs inside the worker role (and the embedded
    dev worker); the API process only writes events."""

    def __init__(self, interval_seconds: float = PUBLISH_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._poke = asyncio.Event()

    def notify(self) -> None:
        """Ask the loop to drain now (e.g. right after a local settlement)."""
        self._poke.set()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        self._poke.set()
        try:
            await asyncio.wait_for(self._task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                drained = await publish_pending()
                if drained:
                    log.debug(f"Outbox published {drained} event(s)")
            except Exception as exc:
                log.error(f"Outbox pass failed: {type(exc).__name__}")
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
