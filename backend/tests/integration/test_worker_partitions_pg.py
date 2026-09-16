"""Archive and retention on a real PostgreSQL trace database: partitions, segments, rebuild (SPEC §8.10, §8.11).

Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL names a local disposable database whose name starts with
``openbox_trace_test_``. Every test drops, recreates and migrates that database with the trace chain.
"""
import asyncio
import io
import json
import os
import time
from datetime import datetime, time as time_of_day, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.unit.test_worker_archive_support import (FakeMetrics, add_events, add_trajectory, event_values, gc_entries,
    hot_seqs, interleave_ingest_and_archive, segment_ranges, trajectory_row, worker_settings)
from trajectory.lifecycle import tombstone_trajectory
from trajectory.ops import rebuild
from trajectory.storage import LocalBlobStore, MemoryBlobStore, trajectory_prefix
from trajectory.store import partitions
from trajectory.store.database import close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryEventKey, TrajectoryMetaSession
from trajectory.types import now
from trajectory.worker import archive as archive_module
from trajectory.worker import retention as retention_module
from trajectory.worker.archive import ArchiveService
from trajectory.worker.retention import RetentionService

URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")

BACKEND = Path(__file__).resolve().parents[2]
DISPOSABLE_PREFIXES = ("openbox_trace_test_", "openbox_trace_rebuild_")


async def _recreate(url: str, *, create: bool = True) -> None:
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").startswith(DISPOSABLE_PREFIXES):
        raise ValueError("PostgreSQL trace tests require a local disposable openbox_trace_test_* database")
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            if create:
                await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


def _upgrade() -> None:
    config = Config(str(BACKEND / "alembic_trajectory.ini"))
    config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    command.upgrade(config, "head")


async def _migrate(url: str, monkeypatch) -> None:
    await _recreate(url)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", url)
    # env.py runs asyncio.run(), which needs a thread without a running loop.
    await asyncio.to_thread(_upgrade)


@pytest.fixture
async def migrated(monkeypatch):
    await close_trace_engine()
    await _migrate(URL, monkeypatch)
    engine = init_trace_engine(URL)
    yield engine
    await close_trace_engine()


def at_noon(day) -> datetime:
    return datetime.combine(day, time_of_day(12), tzinfo=timezone.utc)


async def attached(engine) -> list[str]:
    async with engine.connect() as connection:
        return await partitions.list_partitions(connection)


async def placement(engine, trajectory_id: str) -> list[tuple[int, str]]:
    async with engine.connect() as connection:
        return [tuple(row) for row in (await connection.execute(text(
            "SELECT seq, tableoid::regclass::text FROM trajectory_events WHERE trajectory_id = :t ORDER BY seq"),
            {"t": trajectory_id})).all()]


async def test_maintenance_keeps_a_week_ahead_and_drops_only_empty_old_partitions(migrated):
    today = now().date()
    with_rows, empty, recent = today - timedelta(days=10), today - timedelta(days=9), today - timedelta(days=3)
    async with migrated.begin() as connection:
        for day in (with_rows, empty, recent):
            await partitions.ensure_partitions(connection, day, 0)
    await add_trajectory("trj_a", committed=1, last_activity_at=now() - timedelta(hours=1))
    await add_events([event_values("trj_a", 1, recorded_at=at_noon(with_rows))])
    metrics = FakeMetrics()
    service = ArchiveService(worker_settings(), blob_store=MemoryBlobStore(), metrics=metrics)

    await service.maintain_partitions()
    week = [partitions.partition_name_for(today + timedelta(days=offset)) for offset in range(8)]
    names = await attached(migrated)
    assert names == sorted([partitions.partition_name_for(with_rows), partitions.partition_name_for(recent), *week])
    # with_rows, recent and today are hot; the dropped empty day and the days ahead are not.
    assert metrics.gauges == {"stale_hot_partitions": 1, "hot_partitions": 3}

    # Once archival empties the old partition, the next maintenance drops it.
    assert await service.archive_trajectory("trj_a") == 1
    await service.maintain_partitions()
    assert partitions.partition_name_for(with_rows) not in await attached(migrated)
    assert partitions.partition_name_for(recent) in await attached(migrated)
    assert metrics.gauges == {"stale_hot_partitions": 0, "hot_partitions": 2}


async def test_every_partition_helper_runs_in_its_own_read_committed_transaction_with_a_60s_timeout(migrated,
                                                                                                   monkeypatch):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today - timedelta(days=12), 0)
    observed = []
    depth = 0

    def spy(name, helper):
        async def observe(connection, *args):
            nonlocal depth
            if depth == 0:  # the helpers call each other; only the service's calls count
                observed.append((name, *(await connection.execute(text(
                    "SELECT current_setting('transaction_isolation'), current_setting('statement_timeout'), "
                    "txid_current()"))).one()))
            depth += 1
            try:
                return await helper(connection, *args)
            finally:
                depth -= 1
        return observe

    for name in ("ensure_partitions", "list_partitions", "drop_partition_if_empty"):
        monkeypatch.setattr(partitions, name, spy(name, getattr(partitions, name)))
    # Even an engine that defaults to REPEATABLE READ gets READ COMMITTED maintenance transactions.
    repeatable = create_async_engine(URL, isolation_level="REPEATABLE READ")
    monkeypatch.setattr(archive_module, "get_trace_engine", lambda: repeatable)
    try:
        await ArchiveService(worker_settings(), blob_store=MemoryBlobStore(), metrics=FakeMetrics()).maintain_partitions()
    finally:
        await repeatable.dispose()
    assert [name for name, *_ in observed] == ["ensure_partitions", "list_partitions", "drop_partition_if_empty"]
    assert {(isolation, timeout) for _, isolation, timeout, _ in observed} == {("read committed", "1min")}
    assert len({transaction for *_, transaction in observed}) == 3
    assert partitions.partition_name_for(today - timedelta(days=12)) not in await attached(migrated)


async def test_busy_locks_and_refused_isolation_are_handled_and_retried_soon(migrated, monkeypatch):
    monkeypatch.setattr(partitions, "LOCK_ATTEMPTS", 2)
    monkeypatch.setattr(partitions, "LOCK_RETRY_SECONDS", 0.01)
    service = ArchiveService(worker_settings(), blob_store=MemoryBlobStore(), metrics=FakeMetrics())
    reader = await migrated.connect()
    try:
        # An open read transaction holds ACCESS SHARE on the parent table.
        await reader.execute(text("SELECT count(*) FROM trajectory_events"))
        await service.maintain_partitions()
        assert await attached(migrated) == []
        assert service._partitions_due - time.monotonic() <= archive_module.PARTITION_RETRY_SECONDS
    finally:
        await reader.rollback()
        await reader.close()
    await service.maintain_partitions()
    assert len(await attached(migrated)) == 8
    assert service._partitions_due - time.monotonic() > archive_module.PARTITION_RETRY_SECONDS

    async def refuse(*args, **kwargs):
        raise partitions.PartitionIsolationError("REPEATABLE READ")

    monkeypatch.setattr(partitions, "ensure_partitions", refuse)
    await service.maintain_partitions()
    assert service._partitions_due - time.monotonic() <= archive_module.PARTITION_RETRY_SECONDS


async def test_archival_moves_hot_rows_out_of_every_partition(migrated):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today - timedelta(days=1), 1)
    await add_trajectory("trj_a", committed=6, last_activity_at=now() - timedelta(hours=1))
    days = [today - timedelta(days=1)] * 2 + [today] * 2 + [today + timedelta(days=30)] * 2
    await add_events([event_values("trj_a", seq, recorded_at=at_noon(day)) for seq, day in enumerate(days, start=1)])
    assert {name for _, name in await placement(migrated, "trj_a")} == {
        partitions.partition_name_for(today - timedelta(days=1)), partitions.partition_name_for(today),
        partitions.DEFAULT_PARTITION}
    store, metrics = MemoryBlobStore(), FakeMetrics()
    service = ArchiveService(worker_settings(), blob_store=store, metrics=metrics)
    assert await service.run_once() == 6
    assert (await segment_ranges("trj_a"), await placement(migrated, "trj_a")) == ([(1, 6)], [])
    assert metrics.counters == {"segment_uploads": 1}


async def test_pruning_and_tombstones_cover_partitioned_rows(migrated):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today, 0)
    await add_trajectory("trj_a", committed=4, archived=2, last_activity_at=now())
    await add_events([event_values("trj_a", seq, recorded_at=at_noon(today)) for seq in (3, 4)]
                     + [event_values("trj_a", 5, recorded_at=at_noon(today + timedelta(days=40)))])
    old = now() - timedelta(days=45)
    async with trace_session() as db:
        for event_id, seq in (("key_archived", 1), ("key_hot", 3)):
            db.add(TrajectoryEventKey(event_id=event_id, trajectory_id="trj_a", seq=seq, content_hash="0" * 64,
                                      recorded_at=old))
    service = ArchiveService(worker_settings(), blob_store=MemoryBlobStore(), metrics=FakeMetrics())
    assert await service.prune_event_keys() == 1
    async with trace_session() as db:
        assert (await db.scalars(select(TrajectoryEventKey.event_id))).all() == ["key_hot"]
        await tombstone_trajectory(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")
    assert await hot_seqs("trj_a") == []
    assert [entry for entry in await gc_entries() if entry[0] == "prefix"] == [
        ("prefix", trajectory_prefix("trj_a"), "session_deleted")] * 2
    store = MemoryBlobStore()
    await store.put(f"{trajectory_prefix('trj_a')}blobs/{'a' * 64}", b"x", content_type="application/json")
    assert await RetentionService(worker_settings(), blob_store=store, metrics=FakeMetrics()).process_gc_queue() == 1
    assert store.objects == {}


async def test_retention_purges_run_with_the_longer_statement_timeout(migrated, monkeypatch):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today, 0)
    await add_trajectory("trj_old", committed=2, last_activity_at=now() - timedelta(days=200))
    await add_trajectory("trj_gone", committed=2)
    await add_events([event_values(trajectory_id, seq, recorded_at=at_noon(today))
                      for trajectory_id in ("trj_old", "trj_gone") for seq in (1, 2)])
    async with trace_session() as db:
        db.add(TrajectoryMetaSession(id="session_trj_gone", user_id="user_a", is_deleted=True, updated_at=now(),
                                     synced_at=now()))
    timeouts = []

    def observe(purge):
        async def observed(db, trajectory, **kwargs):
            timeouts.append((await db.execute(text("SHOW statement_timeout"))).scalar_one())
            return await purge(db, trajectory, **kwargs)
        return observed

    for name in ("expire_trajectory_content", "tombstone_trajectory"):
        monkeypatch.setattr(retention_module, name, observe(getattr(retention_module, name)))
    result = await RetentionService(worker_settings(), blob_store=MemoryBlobStore(), metrics=FakeMetrics()).run_once()
    assert (result["tombstoned"], result["expired"]) == (1, 1)
    assert timeouts == ["1min", "1min"]
    assert (await hot_seqs("trj_old"), await hot_seqs("trj_gone")) == ([], [])
    assert ((await trajectory_row("trj_old")).recording_status, (await trajectory_row("trj_gone")).recording_status) == (
        "expired", "deleted")
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "5s"


async def test_the_rebuild_drill_restores_postgresql_segments_into_a_scratch_database(migrated, monkeypatch, tmp_path):
    blobs = tmp_path / "blobs"
    rows = [event_values("trj_a", seq, type="tool.output", call_id="call_1",
                         data={"output": f"line {seq} 你好", "chunk_index": seq, "ratio": seq / 7},
                         hints={"preview": {"output": f"line {seq}"}} if seq % 2 else None)
            for seq in range(1, 8)]
    await add_trajectory("trj_a", committed=7)
    await add_events(rows)
    async with trace_session() as db:
        for row in rows:
            db.add(TrajectoryEventKey(event_id=row["event_id"], trajectory_id="trj_a", seq=row["seq"],
                                      content_hash=row["content_hash"], recorded_at=row["recorded_at"]))
    archive = ArchiveService(worker_settings(segment_events=3), blob_store=LocalBlobStore(blobs), metrics=FakeMetrics())
    assert await archive.archive_trajectory("trj_a") == 6

    source = make_url(URL)
    scratch = source.set(database=f"openbox_trace_rebuild_{source.database.removeprefix('openbox_trace_test_')}")
    scratch_url = scratch.render_as_string(hide_password=False)
    await _migrate(scratch_url, monkeypatch)
    try:
        environ = {"TRAJECTORY_DATABASE_URL": URL, "REBUILD_SCRATCH_URL": scratch_url,
                   "TRAJECTORY_BLOB_PROVIDER": "local", "TRAJECTORY_BLOB_LOCAL_PATH": str(blobs)}
        stdout = io.StringIO()
        code = await asyncio.to_thread(rebuild.main, [], environ=environ, stdout=stdout)
        report = json.loads(stdout.getvalue())
        assert code == 0, report
        (detail,) = report["reports"]
        assert (report["ok"], report["segments"], report["events"]) == (True, 2, 7)
        assert detail["source_digest"] == detail["scratch_digest"]
        assert (detail["segment_errors"], detail["key_mismatches"], detail["stale_hot_rows"]) == ([], [], 0)
        copy = create_async_engine(scratch_url)
        try:
            async with copy.connect() as connection:
                restored = (await connection.execute(text(
                    "SELECT seq, data, hints, occurred_at FROM trajectory_events ORDER BY seq"))).all()
        finally:
            await copy.dispose()
        assert [(seq, data, hints, occurred_at) for seq, data, hints, occurred_at in restored] == [
            (row["seq"], row["data"], row["hints"], row["occurred_at"]) for row in rows]
    finally:
        await _recreate(scratch_url, create=False)


async def test_archival_interleaved_with_ingest_commits_on_partitioned_postgresql(migrated):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today - timedelta(days=2), 2)
    inserted = await interleave_ingest_and_archive(
        MemoryBlobStore(), "trj_a", batches=30, per_batch=7, lag=3,
        recorded_at=lambda seq: at_noon(today - timedelta(days=seq % 3)) + timedelta(microseconds=seq))
    assert len(inserted) == 210
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SELECT count(*) FROM trajectory_events"))).scalar_one() == 0


async def test_a_tombstone_in_the_callers_transaction_runs_with_the_long_statement_timeout(migrated):
    today = now().date()
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, today, 0)
    await add_trajectory("trj_a", committed=2, last_activity_at=now())
    await add_events([event_values("trj_a", seq, recorded_at=at_noon(today)) for seq in (1, 2)])
    retention = RetentionService(worker_settings(), blob_store=MemoryBlobStore(), metrics=FakeMetrics())
    async with trace_session() as db:
        assert (await db.execute(text("SHOW statement_timeout"))).scalar_one() == "5s"
        # Ingest applies session.deleted inside its batch transaction.
        await retention.tombstone(db, await db.get(SessionTrajectory, "trj_a"), reason="session_deleted")
        assert (await db.execute(text("SHOW statement_timeout"))).scalar_one() == "1min"
    assert ((await trajectory_row("trj_a")).recording_status, await hot_seqs("trj_a")) == ("deleted", [])
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "5s"
