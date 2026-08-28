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

from sqlalchemy import select, update

from core.log import create_logger
from db.base import get_db_session
from db.models.skill_job_event import SkillJobEvent

log = create_logger("skill_runtime.outbox")

SKILL_JOB_EVENT = "skill.job.event"

PUBLISH_INTERVAL_SECONDS = 1.0
BATCH_LIMIT = 200

#: A poisoned receipt (schema drift, FK oddity) must not block the wire event
#: forever; after this many attempts the receipt is abandoned with an error.
RECEIPT_RETRY_LIMIT = 3
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
        events = list(
            (
                await db.execute(
                    select(SkillJobEvent)
                    .where(SkillJobEvent.published_at.is_(None))
                    .order_by(
                        SkillJobEvent.created_at.asc(),
                        SkillJobEvent.job_id.asc(),
                        SkillJobEvent.seq.asc(),
                    )
                    .limit(limit)
                )
            ).scalars().all()
        )
    if not events:
        return 0

    published = 0
    stamped_ids: list[str] = []
    for event in events:
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
            except Exception as e:
                if _receipt_failures.get(event.id, 0) < RECEIPT_RETRY_LIMIT:
                    # Leave the event unstamped so the whole step retries.
                    _receipt_failures[event.id] = _receipt_failures.get(event.id, 0) + 1
                    log.warning(f"Chat receipt for {event.job_id} failed, will retry: {e}")
                    continue
                log.error(f"Chat receipt for {event.job_id} abandoned after retries: {e}")

        try:
            bus.publish(
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
        except Exception as e:
            # Leave unstamped; the next pass retries delivery.
            log.warning(f"Outbox publish failed for event {event.id}: {e}")
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
            except Exception as e:
                log.error(f"Outbox pass failed: {e}")
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
