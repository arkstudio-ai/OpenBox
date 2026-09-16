"""Budget bookkeeping on PostgreSQL (SPEC §5.7, §13): per-user daily bytes shared by ingest and the budget pass.

Ingest adds the bytes of every batch to one ``trajectory_worker_state`` row and the budget pass records the users
over the limit in the same row. On PostgreSQL both run as separate loops; neither may overwrite the other's
update. Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL is set (see test_worker_ingest_pg.py).
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryWorkerState
from trajectory.worker.budgets import USER_BYTES_STATE_KEY, BudgetService, add_user_bytes
from tests.integration.test_worker_ingest_pg import URL, migrated, settings  # noqa: F401
from tests.unit.test_worker_ingest import FakeMetrics

pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")


async def _state() -> dict:
    async with trace_session() as db:
        return (await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY)).value


async def test_ingest_bytes_and_exceeded_users_are_not_lost_between_concurrent_transactions(migrated, settings):
    now = datetime.now(timezone.utc)
    async with trace_session() as db:
        await add_user_bytes(db, {"u1": 150}, now=now)
    budget = BudgetService(replace(settings, budget_user_daily_bytes=100), metrics=FakeMetrics())
    budget._since = {}

    async def ingest_batch(size: int) -> None:
        async with trace_session() as db:
            await add_user_bytes(db, {"u1": size}, now=now)

    # The budget pass marks u1 over the limit while an ingest batch adds bytes before the pass commits.
    async with trace_session() as db:
        users = await budget._user_levels(db, now)
        assert users["u1"]["level"] == "degraded"
        batch = asyncio.create_task(ingest_batch(30))
        await asyncio.wait({batch}, timeout=0.5)
    await asyncio.wait_for(batch, 5)
    value = await _state()
    assert value["users"]["u1"] == 180 and "u1" in value["exceeded"]

    # And the other way round: an ingest batch in flight while the budget pass marks another user.
    async with trace_session() as db:
        await add_user_bytes(db, {"u2": 500}, now=now)
    async with trace_session() as db:
        await add_user_bytes(db, {"u1": 5}, now=now)
        marking = asyncio.create_task(_levels(budget, now))
        await asyncio.wait({marking}, timeout=0.5)
    await asyncio.wait_for(marking, 5)
    value = await _state()
    assert value["users"] == {"u1": 185, "u2": 500} and set(value["exceeded"]) == {"u1", "u2"}


async def _levels(budget: BudgetService, now: datetime) -> dict:
    async with trace_session() as db:
        return await budget._user_levels(db, now)
