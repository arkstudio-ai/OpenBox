"""Cross-process claiming and exponential failure backoff."""
import asyncio
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select, update

from cron import internal_tasks
from db.base import get_db_session
from db.models.internal_task import InternalTaskState


async def test_concurrent_ticks_claim_one_execution(monkeypatch):
    monkeypatch.setattr(internal_tasks, "_tasks", {})
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def work():
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()

    internal_tasks.register(f"claim-{uuid.uuid4().hex}", 60, work)
    first = asyncio.create_task(internal_tasks.tick())
    await entered.wait()
    second = asyncio.create_task(internal_tasks.tick())
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)

    assert calls == 1


async def test_failure_backoff_increases_and_suppresses_early_retry(monkeypatch):
    monkeypatch.setattr(internal_tasks, "_tasks", {})
    name = f"backoff-{uuid.uuid4().hex}"
    calls = 0

    async def fail():
        nonlocal calls
        calls += 1
        raise RuntimeError(f"failure {calls}")

    internal_tasks.register(name, 2, fail)
    await internal_tasks.tick()
    async with get_db_session() as db:
        first = await db.get(InternalTaskState, name)
        assert first is not None
        first_delay = (first.backoff_until - first.last_run_at).total_seconds()
        assert first.last_status == "error"
        assert first.consecutive_failures == 1
        assert first_delay == 4

    await internal_tasks.tick()
    assert calls == 1

    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    async with get_db_session() as db:
        await db.execute(
            update(InternalTaskState)
            .where(InternalTaskState.name == name)
            .values(last_run_at=past, backoff_until=past)
        )
    await internal_tasks.tick()

    async with get_db_session() as db:
        second = (
            await db.execute(
                select(InternalTaskState).where(InternalTaskState.name == name)
            )
        ).scalar_one()
        second_delay = (second.backoff_until - second.last_run_at).total_seconds()
        assert calls == 2
        assert second.consecutive_failures == 2
        assert second_delay == 8
        assert second_delay > first_delay
