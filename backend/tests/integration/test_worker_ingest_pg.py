"""Ingest and the writer lock on a real PostgreSQL trace database (SPEC §8.1, §8.3).

Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL names a local disposable database whose name starts with
``openbox_trace_test_``. Every test drops and recreates that database and migrates it with the trace chain.
"""
import asyncio
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sql_events, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.exc import TimeoutError as PoolTimeout

from trajectory.store.database import close_trace_engine, init_trace_engine, trace_read_session, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryIngestFile,
    TrajectoryPayload, TrajectoryRecordEvent)
from trajectory.worker.ingest import IngestService
from trajectory.worker.lock import PostgresWriterLock
from trajectory.worker.projection import ProjectionService
from trajectory.worker.settings import WorkerSettings
from tests.unit.test_worker_ingest import Harness, SpoolWriter, event, events_of
from tests.unit.test_worker_services import _services

URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")
BACKEND = Path(__file__).resolve().parents[2]


async def recreate(url: str) -> None:
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").startswith("openbox_trace_test_"):
        raise ValueError("PostgreSQL trace tests require a local disposable openbox_trace_test_* database")
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


def _upgrade() -> None:
    config = Config(str(BACKEND / "alembic_trajectory.ini"))
    config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    command.upgrade(config, "head")


@pytest.fixture
async def migrated(monkeypatch):
    await close_trace_engine()
    await recreate(URL)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", URL)
    # env.py runs asyncio.run(), which needs a thread without a running loop.
    await asyncio.to_thread(_upgrade)
    engine = init_trace_engine(URL)
    yield engine
    await close_trace_engine()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    return WorkerSettings.from_env()


async def test_ingest_writes_partitioned_rows_with_contiguous_seq(migrated, settings):
    harness = Harness(settings)
    body = {"model": "m", "input": {"system": "系统提示 " * 300, "messages": [{"role": "user", "content": "q" * 600}]}}
    harness.writer.events(event("request.prepared", request_id="r1", data=body),
                          event(data={"text": "nul\x00byte 你好 😀"}),
                          event(session="ses_2", user="u2"))
    result = await harness.run()
    assert result["events"] == 3 and len(result["trajectories"]) == 2
    trajectory, stored = await events_of("ses_1")
    assert [row.seq for row in stored] == [1, 2, 3]
    assert stored[1].data["input"]["system"]["$ref"]["kind"] == "system"
    assert stored[2].data == {"text": "nul�byte 你好 😀"}
    async with trace_session() as db:
        partition = await db.scalar(text("SELECT count(*) FROM trajectory_events_default"))
        payloads = await db.scalar(select(func.count()).select_from(TrajectoryPayload))
        on_day = await db.scalar(select(func.count()).select_from(TrajectoryEvent).where(
            TrajectoryEvent.recorded_on == datetime.now(timezone.utc).date()))
    assert partition == 5 and on_day == 5 and payloads == 2
    assert harness.store.objects and all(key.startswith("trajectories/trj_") for key in harness.store.objects)


async def test_busy_read_pool_does_not_starve_ingest(migrated, settings, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_READ_DB_POOL_SIZE", "2")
    async with trace_read_session() as reader:
        assert await reader.scalar(select(func.current_setting("default_transaction_read_only"))) == "on"
        assert await reader.scalar(select(func.current_setting("statement_timeout"))) == "5s"
        assert await reader.scalar(select(func.current_setting("lock_timeout"))) == "1s"
        assert await reader.scalar(select(func.current_setting("idle_in_transaction_session_timeout"))) == "5s"
        assert await reader.scalar(select(func.current_setting("work_mem"))) == "4MB"
        assert await reader.scalar(select(func.current_setting("max_parallel_workers_per_gather"))) == "0"
        blocked = [asyncio.create_task(reader.scalar(select(func.pg_sleep(60)))) for _ in range(2)]
        try:
            async with asyncio.timeout(2):
                while True:
                    async with trace_session() as writer:
                        sleeping = await writer.scalar(text("SELECT count(*) FROM pg_stat_activity "
                            "WHERE datname = current_database() AND application_name = 'openbox-trace-read' "
                            "AND state = 'active' AND wait_event = 'PgSleep'"))
                    if sleeping == 2:
                        break
                    await asyncio.sleep(0.01)
            harness = Harness(settings)
            harness.writer.events(*[event(event_id=f"read_pressure_{index}") for index in range(100)])
            result = await asyncio.wait_for(harness.run(), 2)
            assert result["events"] == 100
            # Only two reader connections; exhausting them fails quickly, without overflow.
            with pytest.raises(PoolTimeout):
                await reader.scalar(select(1))
        finally:
            for task in blocked:
                task.cancel()
            await asyncio.gather(*blocked, return_exceptions=True)
        assert await reader.scalar(select(1)) == 1
    _, stored = await events_of("ses_1")
    assert len(stored) == 101 and [row.seq for row in stored] == list(range(1, 102))


async def test_concurrent_ingesters_never_reuse_a_seq(migrated, settings):
    first, second = Harness(settings), Harness(settings)
    second.writer = SpoolWriter(settings.spool_dir, "20260914080009-other-9-dddddddd")
    # Both producers write for the same new session; the two services bypass the writer lock
    # on purpose, so only the trajectory row lock keeps seq allocation serialized.
    first.writer.events(*[event(event_id=f"a{index}") for index in range(40)])
    second.writer.events(*[event(event_id=f"b{index}") for index in range(40)])
    first.configure(ingest_batch_lines=5)
    second.configure(ingest_batch_lines=5)
    await asyncio.gather(first.run(), second.run())
    await asyncio.gather(first.run(), second.run())
    trajectory, stored = await events_of("ses_1")
    assert [row.seq for row in stored] == list(range(1, 82))
    assert sorted(row.event_id for row in stored[1:]) == sorted([f"a{i}" for i in range(40)] + [f"b{i}" for i in range(40)])
    assert trajectory.committed_seq == 81 and trajectory.next_seq == 82
    async with trace_session() as db:
        assert await db.scalar(select(func.count()).select_from(SessionTrajectory)) == 1


async def test_advisory_lock_is_exclusive_and_lost_with_its_connection(migrated):
    first, second = PostgresWriterLock(URL), PostgresWriterLock(URL)
    try:
        assert await first.acquire() is True
        assert await second.acquire() is False
        assert await first.verify() is True
        pid = await first._connection.scalar(text("SELECT pg_backend_pid()"))
        async with trace_session() as db:
            await db.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
        assert await first.verify() is False and first.held is False
        assert await second.acquire() is True
        await second.release()
        assert await first.acquire() is True
    finally:
        await first.release()
        await second.release()


async def test_services_run_one_task_per_loop_and_resume_without_duplicates(migrated, settings, monkeypatch):
    services = _services(replace(settings, ingest_poll_ms=10, projection_batch_ms=10))
    SpoolWriter(settings.spool_dir).events(event(), event(), event())
    await services.start()
    try:
        # ingest, projection, archive, partitions, retention, gc, orphans, exports, budgets, heartbeat
        assert services.is_writer and len(services._writer_tasks) == 10
        for _ in range(300):
            async with trace_session() as db:
                if await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == 4:
                    break
            await asyncio.sleep(0.02)
    finally:
        await services.stop()
    assert not services.is_writer
    assert len((await events_of("ses_1"))[1]) == 4

    harness = Harness(settings)
    harness.configure(ingest_batch_lines=2)
    # A producer of its own: stopping the loops may leave the first producer's consumed-file row
    # behind, and a real producer id never comes back with a reused file counter.
    harness.writer = SpoolWriter(settings.spool_dir, "20260914080010-resume-5-eeeeeeee")
    harness.writer.events(*[event(session="ses_r", event_id=f"r{index}") for index in range(5)])
    original = IngestService._commit
    calls = {"count": 0}

    class WorkerKilled(BaseException):
        """Like a kill, nothing inside the worker handles it (ingest isolates ordinary exceptions per file)."""

    async def killed(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise WorkerKilled("killed between upload and commit")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(IngestService, "_commit", killed)
    with pytest.raises(WorkerKilled):
        await harness.run()
    monkeypatch.setattr(IngestService, "_commit", original)
    harness.configure()
    await harness.run()
    _, stored = await events_of("ses_r")
    assert [row.event_id for row in stored[1:]] == [f"r{index}" for index in range(5)]
    async with trace_session() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryIngestFile)) == 0


async def test_session_counters_are_batched_and_rollback_with_events_and_offsets(migrated, settings):
    harness = Harness(settings)
    sessions = 501  # Cross the 500-row UPDATE boundary.
    harness.writer.events(*(event(session=f"ses_batch_{i}", event_id=f"seed_{i}") for i in range(sessions)))
    assert (await harness.run())["events"] == sessions
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).values(projected_seq=1, checkpoint_seq=1))

    statements = []
    fail_second = False

    def observe(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.startswith("UPDATE session_trajectories SET"):
            statements.append(statement)
            if fail_second and len(statements) == 2:
                raise RuntimeError("batch update interrupted before the second group")

    sql_events.listen(migrated.sync_engine, "before_cursor_execute", observe)
    try:
        harness.writer.events(*(event(session=f"ses_batch_{i}", event_id=f"next_{i}_{j}")
                                for i in range(sessions) for j in range(i % 3 + 1)))
        assert (await harness.run())["events"] == sum(i % 3 + 1 for i in range(sessions))
        assert len(statements) == 2 and all("FROM (VALUES" in statement for statement in statements)
        async with trace_session() as db:
            rows = {row.session_id: row for row in (await db.scalars(select(SessionTrajectory))).all()}
            before_events = await db.scalar(select(func.count()).select_from(TrajectoryEvent))
        for i in range(sessions):
            row = rows[f"ses_batch_{i}"]
            assert (row.committed_seq, row.next_seq, row.event_count) == (3 + i % 3, 4 + i % 3, 3 + i % 3)
            assert (row.projected_seq, row.checkpoint_seq) == (1, 1)

        statements.clear()
        fail_second = True
        harness.writer.events(*(event(session=f"ses_batch_{i}", event_id=f"retry_{i}") for i in range(sessions)))
        assert (await harness.run())["failed_batches"] == 1
        async with trace_session() as db:
            assert await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == before_events
            counters = {row.session_id: row.committed_seq for row in (await db.scalars(select(SessionTrajectory))).all()}
            assert counters == {key: row.committed_seq for key, row in rows.items()}
            assert not await db.scalar(select(func.count()).select_from(TrajectoryIngestFile))

        fail_second = False
        for failure in harness.service._failures.values():
            failure.next_at = 0
        assert (await harness.run())["events"] == sessions
        async with trace_session() as db:
            assert await db.scalar(select(func.count()).select_from(TrajectoryEvent)) == before_events + sessions
            for row in (await db.scalars(select(SessionTrajectory))).all():
                assert row.committed_seq == counters[row.session_id] + 1
                assert row.next_seq == row.committed_seq + 1
                assert (row.projected_seq, row.checkpoint_seq) == (1, 1)
    finally:
        sql_events.remove(migrated.sync_engine, "before_cursor_execute", observe)


async def test_projection_skips_busy_sessions_and_retries_without_duplicate_links(migrated, settings):
    harness = Harness(settings)
    harness.writer.events(event(session="ses_busy", event_id="busy"), event(session="ses_free", event_id="free"))
    await harness.run()
    service = ProjectionService(settings, blob_store=harness.store, metrics=harness.metrics)
    async with trace_session() as db:
        trajectories = (await db.scalars(select(SessionTrajectory).order_by(SessionTrajectory.id))).all()
    async with trace_session() as blocker:
        await blocker.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectories[0].id).with_for_update())
        assert await asyncio.wait_for(service.run_once(), 2) == 2
        for _ in range(3):
            assert await asyncio.wait_for(service.run_once(), 2) == 0
        assert not service._retry  # Lock contention is not a failed projection.
        async with trace_session() as db:
            assert (await db.get(SessionTrajectory, trajectories[0].id)).projected_seq == 0
            assert (await db.get(SessionTrajectory, trajectories[1].id)).projected_seq == 2

    assert await service.run_once() == 2
    async with trace_session() as db:
        assert all(row.projected_seq == row.committed_seq == 2
                   for row in (await db.scalars(select(SessionTrajectory))).all())
        links = (await db.execute(select(TrajectoryRecordEvent))).all()
        assert links
        before_links = await db.scalar(select(func.count()).select_from(TrajectoryRecordEvent))
    assert await service.run_once() == 0
    async with trace_session() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryRecordEvent)) == before_links


@pytest.mark.parametrize("repair", [False, True])
async def test_checkpoint_creation_and_repair_skip_busy_rows_then_catch_up(migrated, settings, repair):
    harness = Harness(settings)
    harness.writer.events(event(event_id="checkpoint_input"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    service = ProjectionService(replace(settings, checkpoint_interval=1),
                                blob_store=harness.store, metrics=harness.metrics)
    assert await service.project(trajectory.id) == 2
    if repair:
        assert await service.maybe_checkpoint(trajectory.id)
        async with trace_session() as db:
            await db.execute(update(SessionTrajectory).values(checkpoint_seq=0))
    async with trace_session() as blocker:
        await blocker.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory.id).with_for_update())
        assert await asyncio.wait_for(service.maybe_checkpoint(trajectory.id), 2) is False
        assert not service._retry
        async with trace_session() as db:
            assert (await db.get(SessionTrajectory, trajectory.id)).checkpoint_seq == 0
    assert await service.maybe_checkpoint(trajectory.id) is (not repair)
    async with trace_session() as db:
        assert (await db.get(SessionTrajectory, trajectory.id)).checkpoint_seq == 2
        assert await db.scalar(select(func.count()).select_from(TrajectoryCheckpoint)) == 1
