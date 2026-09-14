"""Budget computation, control/budgets.json and the worker heartbeat (SPEC §5.7, §13)."""
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import orjson
import pytest
from sqlalchemy import select, update

from trajectory import spool
from trajectory.budget import BudgetReader
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryWorkerState
from trajectory.worker.budgets import (BLOCKED, DEGRADED, NORMAL, USER_BYTES_STATE_KEY, BudgetService, add_user_bytes,
    trajectory_level, write_heartbeat)
from trajectory.worker.settings import WorkerSettings

AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)


class FakeMetrics:
    def __init__(self):
        self.counters, self.gauges = {}, {}

    def inc(self, name, value=1):
        self.counters[name] = self.counters.get(name, 0) + value

    def set_gauge(self, name, value):
        self.gauges[name] = value


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    return replace(WorkerSettings.from_env(), budget_trajectory_events=100, budget_trajectory_bytes=1000,
                   budget_trajectory_block_bytes=5000, budget_user_daily_bytes=300)


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


def test_levels_follow_the_thresholds(settings):
    assert trajectory_level(settings, event_count=99, stored_bytes=999) == (NORMAL, None)
    assert trajectory_level(settings, event_count=100, stored_bytes=0) == (DEGRADED, "trajectory_events")
    assert trajectory_level(settings, event_count=0, stored_bytes=1000) == (DEGRADED, "trajectory_bytes")
    assert trajectory_level(settings, event_count=500, stored_bytes=5000) == (BLOCKED, "trajectory_block_bytes")


async def test_user_bytes_accumulate_per_utc_day(trace_db):
    async with trace_session() as db:
        await add_user_bytes(db, {"u1": 100, "u2": 0}, now=AT)
    async with trace_session() as db:
        await add_user_bytes(db, {"u1": 50, "u3": 7}, now=AT + timedelta(hours=1))
    async with trace_session() as db:
        row = await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY)
        assert row.value == {"day": "2026-09-14", "users": {"u1": 150, "u3": 7}, "exceeded": {}}
    async with trace_session() as db:
        await add_user_bytes(db, {"u3": 1}, now=AT + timedelta(days=1))
    async with trace_session() as db:
        assert (await db.get(TrajectoryWorkerState, USER_BYTES_STATE_KEY)).value["users"] == {"u3": 1}


async def _trajectory(db, identifier, session_id, *, events=0, stored=0, deleted=False):
    db.add(SessionTrajectory(id=identifier, user_id="u1", session_id=session_id, workspace_id="w", started_at=AT,
                             updated_at=AT, last_activity_at=AT, event_count=events, stored_bytes=stored,
                             deleted_at=AT if deleted else None))


async def test_budget_file_levels_since_and_rows(trace_db, settings):
    async with trace_session() as db:
        await _trajectory(db, "trj_ok", "s_ok", events=5, stored=10)
        await _trajectory(db, "trj_events", "s_events", events=150)
        await _trajectory(db, "trj_bytes", "s_bytes", stored=2000)
        await _trajectory(db, "trj_block", "s_block", stored=6000)
        await _trajectory(db, "trj_deleted", "s_deleted", stored=9000, deleted=True)
        await add_user_bytes(db, {"heavy": 400, "light": 10}, now=AT)
    metrics = FakeMetrics()
    service = BudgetService(settings, metrics=metrics)
    result = await service.run_once(now=AT)
    assert result == {"sessions": 3, "users": 1, "changed": 3, "written": True}
    path = spool.budgets_path(settings.spool_dir)
    document = orjson.loads(path.read_bytes())
    assert document["version"] == 1 and document["generated_at"] == "2026-09-14T08:00:00.000Z"
    assert document["sessions"] == {
        "s_events": {"level": "degraded", "reason": "trajectory_events", "since": "2026-09-14T08:00:00.000Z"},
        "s_bytes": {"level": "degraded", "reason": "trajectory_bytes", "since": "2026-09-14T08:00:00.000Z"},
        "s_block": {"level": "blocked", "reason": "trajectory_block_bytes", "since": "2026-09-14T08:00:00.000Z"}}
    assert document["users"] == {"heavy": {"level": "degraded", "reason": "user_daily_bytes",
                                           "since": "2026-09-14T08:00:00.000Z"}}
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert metrics.gauges == {"trajectories_degraded": 2, "trajectories_blocked": 1}
    reader = BudgetReader(path, 5000)
    reader.maybe_refresh(force=True)
    assert reader.level("heavy", "s_ok") == DEGRADED and reader.level("u1", "s_block") == BLOCKED
    async with trace_session() as db:
        levels = {row.id: (row.budget_level, row.budget_reason)
                  for row in (await db.scalars(select(SessionTrajectory))).all()}
    assert levels["trj_block"] == (BLOCKED, "trajectory_block_bytes") and levels["trj_ok"] == (NORMAL, None)

    # Unchanged: no rewrite. Since is kept across runs and a fresh service reads it back.
    later = AT + timedelta(seconds=10)
    assert (await service.run_once(now=later))["written"] is False
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_bytes").values(stored_bytes=10))
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_events").values(stored_bytes=1500))
    fresh = BudgetService(settings, metrics=metrics)
    result = await fresh.run_once(now=later)
    assert result["written"] is True and result["changed"] == 2
    document = orjson.loads(path.read_bytes())
    assert set(document["sessions"]) == {"s_events", "s_block"}
    assert document["sessions"]["s_block"]["since"] == "2026-09-14T08:00:00.000Z"
    assert document["sessions"]["s_events"] == {"level": "degraded", "reason": "trajectory_bytes",
                                                "since": "2026-09-14T08:00:10.000Z"}
    # The user stays degraded for the rest of the day, then the counters reset.
    assert document["users"]["heavy"]["since"] == "2026-09-14T08:00:00.000Z"
    result = await fresh.run_once(now=AT + timedelta(days=1))
    assert result["users"] == 0 and orjson.loads(path.read_bytes())["users"] == {}
    path.unlink()
    assert (await fresh.run_once(now=AT + timedelta(days=1)))["written"] is True


def test_heartbeat_file(tmp_path):
    write_heartbeat(tmp_path, ingest_lag_seconds=1.23456, now=AT)
    path = tmp_path / "control" / "worker.json"
    document = orjson.loads(path.read_bytes())
    assert document["version"] == 1 and document["pid"] == os.getpid()
    assert document["updated_at"] == "2026-09-14T08:00:00.000Z" and document["ingest_lag_seconds"] == 1.235
    assert set(document) == {"version", "pid", "hostname", "updated_at", "ingest_lag_seconds"}
    assert (os.stat(path).st_mode & 0o777) == 0o600
