"""Trace database on a real PostgreSQL server (SPEC 6.1-6.3, partitions).

Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL names a local disposable database whose name starts with
``openbox_trace_test_``. Every test drops and recreates that database and migrates it with the trace chain.
"""
import asyncio
import os
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from trajectory.store import models, partitions
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_dialect, trace_session

URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")

BACKEND = Path(__file__).resolve().parents[2]
AT = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
DEFAULT = partitions.DEFAULT_PARTITION


def _p(day: str) -> str:
    return f"trajectory_events_p{day}"


async def _recreate(url: str) -> None:
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


def _alembic(action: str, revision: str) -> None:
    config = Config(str(BACKEND / "alembic_trajectory.ini"))
    config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    getattr(command, action)(config, revision)


@pytest.fixture
async def migrated(monkeypatch):
    await close_trace_engine()
    await _recreate(URL)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", URL)
    # env.py runs asyncio.run(), which needs a thread without a running loop.
    await asyncio.to_thread(_alembic, "upgrade", "head")
    engine = init_trace_engine(URL)
    yield engine
    await close_trace_engine()


async def _trajectory(connection, trajectory_id="trj_a", session_id="session_a", user_id="user_a") -> None:
    await connection.execute(models.SessionTrajectory.__table__.insert().values(
        id=trajectory_id, user_id=user_id, session_id=session_id, workspace_id="ws_a",
        started_at=AT, updated_at=AT, last_activity_at=AT))


def _event_row(trajectory_id: str, seq: int, day: date) -> dict:
    return dict(trajectory_id=trajectory_id, seq=seq, recorded_on=day, event_id=f"evt_{trajectory_id}_{seq}_{day:%Y%m%d}",
                type="input.accepted", version=1, user_id="user_a", session_id="session_a",
                source_session_id="session_a", context={"user_id": "user_a"}, data={"text": "hello"},
                content_hash="0" * 64, occurred_at=AT, recorded_at=AT)


async def _events(connection, *rows: tuple[str, int, date]) -> None:
    await connection.execute(models.TrajectoryEvent.__table__.insert(), [_event_row(*row) for row in rows])


async def _placement(connection) -> list[tuple[int, str]]:
    return (await connection.execute(text(
        "SELECT seq, tableoid::regclass::text FROM trajectory_events ORDER BY seq"))).all()


async def test_migration_creates_extension_partitioned_events_and_trigram_index(migrated):
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SELECT version_num FROM trajectory_alembic_version"))).scalar_one() == "t0001_initial"
        assert (await connection.execute(text("SELECT to_regclass('alembic_version')"))).scalar() is None
        assert (await connection.execute(text("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'"))).scalar() == "pg_trgm"
        strategy, key = (await connection.execute(text(
            "SELECT p.partstrat::text, a.attname FROM pg_partitioned_table p JOIN pg_attribute a "
            "ON a.attrelid = p.partrelid AND a.attnum = p.partattrs[0] "
            "WHERE p.partrelid = 'trajectory_events'::regclass"))).one()
        assert (strategy, key) == ("r", "recorded_on")
        assert (await connection.execute(text(
            f"SELECT pg_get_expr(relpartbound, oid) FROM pg_class WHERE relname = '{DEFAULT}'"))).scalar() == "DEFAULT"
        primary_key = await connection.run_sync(lambda sync: sa.inspect(sync).get_pk_constraint("trajectory_events"))
        assert primary_key["constrained_columns"] == ["trajectory_id", "seq", "recorded_on"]
        search = (await connection.execute(text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_trajectory_records_search_trgm'"))).scalar()
        assert "USING gin (search_doc gin_trgm_ops)" in search
        local = (await connection.execute(text(
            f"SELECT indexdef FROM pg_indexes WHERE tablename = '{DEFAULT}'"))).scalars().all()
        for columns in ("(trajectory_id, seq, recorded_on)", "(trajectory_id, seq)", "(trajectory_id, request_id, seq)",
                        "(trajectory_id, call_id, seq)"):
            assert sum(definition.endswith(columns) for definition in local) == 1, columns


def _shape(sync) -> dict:
    inspector = sa.inspect(sync)
    shape = {}
    for table in inspector.get_table_names():
        if table == "trajectory_alembic_version":
            continue
        shape[table] = (
            {column["name"]: (str(column["type"]), column["nullable"], column["default"])
             for column in inspector.get_columns(table)},
            inspector.get_pk_constraint(table)["constrained_columns"],
            sorted((tuple(fk["constrained_columns"]), fk["referred_table"], fk["options"].get("ondelete"))
                   for fk in inspector.get_foreign_keys(table)),
            sorted((unique["name"], tuple(unique["column_names"])) for unique in inspector.get_unique_constraints(table)),
        )
    return shape


async def _catalog(engine) -> tuple:
    async with engine.connect() as connection:
        shape = await connection.run_sync(_shape)
        indexes = (await connection.execute(text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' "
            "AND tablename <> 'trajectory_alembic_version' ORDER BY 1, 2"))).all()
        bounds = (await connection.execute(text(
            "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) FROM pg_class c WHERE c.relispartition ORDER BY 1"))).all()
        extensions = (await connection.execute(text("SELECT extname FROM pg_extension ORDER BY 1"))).scalars().all()
    return shape, indexes, bounds, extensions


async def test_models_create_all_builds_the_migrated_schema(migrated):
    models_url = make_url(URL).set(database=f"{make_url(URL).database}_models").render_as_string(hide_password=False)
    await _recreate(models_url)
    modeled = create_async_engine(models_url)
    try:
        async with modeled.begin() as connection:
            await connection.run_sync(TraceBase.metadata.create_all)
        assert await _catalog(modeled) == await _catalog(migrated)
    finally:
        await modeled.dispose()


async def test_rows_route_by_recorded_on_and_queries_prune(migrated):
    async with migrated.begin() as connection:
        assert await partitions.ensure_partitions(connection, date(2026, 9, 14), 2) == [
            _p("20260914"), _p("20260915"), _p("20260916")]
    async with migrated.begin() as connection:
        assert await partitions.ensure_partitions(connection, datetime(2026, 9, 14, 23, tzinfo=timezone.utc), 2) == []
        assert await partitions.list_partitions(connection) == [_p("20260914"), _p("20260915"), _p("20260916")]
        await _trajectory(connection)
        await _events(connection, ("trj_a", 1, date(2026, 9, 14)), ("trj_a", 2, date(2026, 9, 16)),
                      ("trj_a", 3, date(2026, 10, 1)))
        assert await _placement(connection) == [(1, _p("20260914")), (2, _p("20260916")), (3, DEFAULT)]
        plan = "\n".join((await connection.execute(text(
            "EXPLAIN SELECT * FROM trajectory_events WHERE recorded_on = DATE '2026-09-16'"))).scalars())
        assert _p("20260916") in plan and _p("20260914") not in plan and DEFAULT not in plan


async def test_ensure_moves_default_partition_rows_into_the_new_partition(migrated):
    async with migrated.begin() as connection:
        await _trajectory(connection)
        await _events(connection, ("trj_a", 1, date(2026, 9, 20)), ("trj_a", 2, date(2026, 9, 20)),
                      ("trj_a", 3, date(2026, 9, 21)))
        assert {placement for _, placement in await _placement(connection)} == {DEFAULT}
    async with migrated.begin() as connection:
        assert await partitions.ensure_partitions(connection, date(2026, 9, 20), 0) == [_p("20260920")]
    async with migrated.connect() as connection:
        assert await _placement(connection) == [(1, _p("20260920")), (2, _p("20260920")), (3, DEFAULT)]
        assert (await connection.execute(text(
            f"SELECT count(*) FROM pg_indexes WHERE tablename = '{_p('20260920')}'"))).scalar() == 4
        assert (await connection.execute(text(
            f"SELECT count(*) FROM pg_constraint WHERE conrelid = '{_p('20260920')}'::regclass AND contype = 'f'"))).scalar() == 1


async def test_drop_partition_if_empty(migrated):
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, date(2026, 9, 1), 2)
        await _trajectory(connection)
        await _events(connection, ("trj_a", 1, date(2026, 9, 2)))
    async with migrated.begin() as connection:
        assert await partitions.drop_partition_if_empty(connection, _p("20260902")) is False
        assert await partitions.drop_partition_if_empty(connection, _p("20260901")) is True
        assert await partitions.drop_partition_if_empty(connection, _p("20260901")) is False
        assert await partitions.drop_partition_if_empty(connection, _p("20991231")) is False
        with pytest.raises(ValueError):
            await partitions.drop_partition_if_empty(connection, DEFAULT)
        assert await partitions.list_partitions(connection) == [_p("20260902"), _p("20260903")]
    async with migrated.begin() as connection:
        await connection.execute(text("DELETE FROM trajectory_events WHERE trajectory_id = 'trj_a'"))
        assert await partitions.drop_partition_if_empty(connection, _p("20260902")) is True
        assert await partitions.list_partitions(connection) == [_p("20260903")]
        assert (await connection.execute(text(f"SELECT to_regclass('{DEFAULT}')::text"))).scalar() == DEFAULT


async def test_partition_ddl_backs_off_instead_of_queueing_behind_busy_locks(migrated, monkeypatch):
    monkeypatch.setattr(partitions, "LOCK_ATTEMPTS", 3)
    monkeypatch.setattr(partitions, "LOCK_RETRY_SECONDS", 0.01)
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, date(2026, 9, 1), 0)
    reader = await migrated.connect()
    try:
        # An open read transaction holds ACCESS SHARE on the parent table.
        await reader.execute(text("SELECT count(*) FROM trajectory_events"))
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(partitions.PartitionLockUnavailable):
            async with migrated.begin() as connection:
                await partitions.drop_partition_if_empty(connection, _p("20260901"))
        with pytest.raises(partitions.PartitionLockUnavailable):
            async with migrated.begin() as connection:
                await partitions.ensure_partitions(connection, date(2026, 9, 2), 0)
        assert loop.time() - started < 5
        async with migrated.begin() as connection:
            await connection.execute(text("SET LOCAL lock_timeout = '7s'"))
            with pytest.raises(partitions.PartitionLockUnavailable):
                await partitions.drop_partition_if_empty(connection, _p("20260901"))
            assert (await connection.execute(text("SELECT current_setting('lock_timeout')"))).scalar() == "7s"
    finally:
        await reader.rollback()
        await reader.close()
    async with migrated.begin() as connection:
        await connection.execute(text("SET LOCAL lock_timeout = '7s'"))
        assert await partitions.drop_partition_if_empty(connection, _p("20260901")) is True
        assert (await connection.execute(text("SELECT current_setting('lock_timeout')"))).scalar() == "7s"


async def test_deleting_a_trajectory_cascades_to_its_child_rows(migrated):
    tables = models.TraceBase.metadata.tables
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, date(2026, 9, 14), 0)
        for trajectory_id, session_id in (("trj_a", "session_a"), ("trj_b", "session_b")):
            await _trajectory(connection, trajectory_id, session_id)
            await _events(connection, (trajectory_id, 1, date(2026, 9, 14)), (trajectory_id, 2, date(2026, 12, 1)))
            await connection.execute(tables["trajectory_segments"].insert().values(
                trajectory_id=trajectory_id, from_seq=1, to_seq=2, storage_key=f"trajectories/{trajectory_id}/segments/1-2",
                event_count=2, raw_bytes=10, stored_bytes=5, sha256="1" * 64, created_at=AT))
            await connection.execute(tables["trajectory_payloads"].insert().values(
                payload_id=f"pld_{trajectory_id}", trajectory_id=trajectory_id, dedupe_key="2" * 64, sha256="3" * 64,
                size_bytes=10, media_type="application/json", storage_kind="blob", storage_key="k", first_seq=1,
                created_at=AT))
            await connection.execute(tables["trajectory_records"].insert().values(
                trajectory_id=trajectory_id, record_id="turn:1", kind="turn", status="completed", start_seq=1,
                applied_seq=2, projector_version=1, data={}, summary={}, search_doc="turn"))
            await connection.execute(tables["trajectory_session_summaries"].insert().values(
                trajectory_id=trajectory_id, user_id="user_a", session_id=session_id, workspace_id="ws_a",
                last_activity_at=AT, running_status="idle", recording_status="recording", applied_seq=2, statistics={}))
            await connection.execute(tables["trajectory_checkpoints"].insert().values(
                trajectory_id=trajectory_id, through_seq=2, projector_version=1, state={}, digest="4" * 64, created_at=AT))
            await connection.execute(tables["trajectory_exports"].insert().values(
                id=f"exp_{trajectory_id}", trajectory_id=trajectory_id, viewer_id="admin", through_seq=2,
                status="completed", created_at=AT, updated_at=AT))
            await connection.execute(tables["trajectory_event_keys"].insert().values(
                event_id=f"key_{trajectory_id}", trajectory_id=trajectory_id, seq=1, content_hash="0" * 64, recorded_at=AT))
            await connection.execute(tables["trajectory_record_events"].insert().values(
                trajectory_id=trajectory_id, record_id="turn:1", seq=1))
    async with migrated.begin() as connection:
        await connection.execute(text("DELETE FROM session_trajectories WHERE id = 'trj_a'"))
    async with migrated.connect() as connection:
        for name in ("trajectory_events", "trajectory_segments", "trajectory_payloads", "trajectory_records",
                     "trajectory_session_summaries", "trajectory_checkpoints", "trajectory_exports",
                     "trajectory_event_keys", "trajectory_record_events"):
            counts = dict((await connection.execute(text(
                f"SELECT trajectory_id, count(*) FROM {name} GROUP BY trajectory_id"))).all())
            # The idempotency registry and record/event links carry no foreign key (SPEC 6.3);
            # tombstoning deletes them explicitly.
            expected_a = 1 if name in {"trajectory_event_keys", "trajectory_record_events"} else None
            assert counts.get("trj_a") == expected_a, name
            assert counts.get("trj_b"), name


async def test_trigram_index_serves_substring_search(migrated):
    records = models.TrajectoryRecord.__table__
    async with migrated.begin() as connection:
        await _trajectory(connection)
        rows = [dict(trajectory_id="trj_a", record_id=f"tool:call_{index}", kind="tool", status="completed",
                     start_seq=index, applied_seq=index, projector_version=1, data={}, summary={},
                     search_doc=f"tool tool:call_{index} bash output line {index}") for index in range(300)]
        rows.append(dict(trajectory_id="trj_a", record_id="block:assistant_1", kind="block", status="completed",
                         start_seq=301, applied_seq=301, projector_version=1, data={}, summary={},
                         search_doc="block assistant 我来读取文件 needle-in-haystack"))
        await connection.execute(records.insert(), rows)
    async with migrated.begin() as connection:
        await connection.execute(text("ANALYZE trajectory_records"))
        await connection.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join((await connection.execute(text(
            "EXPLAIN SELECT record_id FROM trajectory_records WHERE search_doc ILIKE '%needle-in%'"))).scalars())
        assert "ix_trajectory_records_search_trgm" in plan
        for query in ("%NEEDLE-IN%", "%读取%"):
            hits = (await connection.execute(text(
                "SELECT record_id FROM trajectory_records WHERE trajectory_id = 'trj_a' AND search_doc ILIKE :query "
                "ORDER BY start_seq, record_id"), {"query": query})).scalars().all()
            assert hits == ["block:assistant_1"], query


async def test_unique_constraints_reject_duplicates(migrated):
    tables = models.TraceBase.metadata.tables
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, date(2026, 9, 14), 1)
        await _trajectory(connection)
        await _events(connection, ("trj_a", 1, date(2026, 9, 14)))
        await connection.execute(tables["trajectory_payloads"].insert().values(
            payload_id="pld_1", trajectory_id="trj_a", dedupe_key="d" * 64, size_bytes=1, media_type="text/plain",
            storage_kind="blob", storage_key="k", first_seq=1, created_at=AT))
        await connection.execute(tables["trajectory_ingest_files"].insert().values(
            producer_id="p", file_name="00000000000000000001.jsonl", updated_at=AT))

    duplicates = [
        (tables["session_trajectories"], dict(id="trj_b", user_id="user_b", session_id="session_a", workspace_id="ws",
                                              started_at=AT, updated_at=AT, last_activity_at=AT)),
        (tables["trajectory_payloads"], dict(payload_id="pld_2", trajectory_id="trj_a", dedupe_key="d" * 64,
                                             size_bytes=1, media_type="text/plain", storage_kind="blob",
                                             storage_key="k", first_seq=2, created_at=AT)),
        (tables["trajectory_events"], _event_row("trj_a", 1, date(2026, 9, 14))),
        (tables["trajectory_ingest_files"], dict(producer_id="p", file_name="00000000000000000001.jsonl", updated_at=AT)),
    ]
    for table, values in duplicates:
        with pytest.raises(IntegrityError):
            async with migrated.begin() as connection:
                await connection.execute(table.insert().values(**values))
    # PostgreSQL can only enforce (trajectory_id, seq) within one recorded_on day; the writer's seq
    # allocation under the trajectory row lock is what keeps seq unique across days.
    async with migrated.begin() as connection:
        await _events(connection, ("trj_a", 1, date(2026, 9, 15)))
        assert (await connection.execute(text("SELECT count(*) FROM trajectory_events WHERE seq = 1"))).scalar() == 2


async def test_engine_settings_and_session_semantics_on_postgresql(migrated):
    assert trace_dialect() == "postgresql"
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar() == "5s"
        assert (await connection.execute(text("SELECT current_setting('application_name')"))).scalar() == "openbox-trace"
    async with migrated.begin() as connection:
        await connection.execute(text("SET LOCAL statement_timeout = '60s'"))
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar() == "1min"
    async with migrated.connect() as connection:
        assert (await connection.execute(text("SHOW statement_timeout"))).scalar() == "5s"

    def trajectory(trajectory_id: str, session_id: str) -> models.SessionTrajectory:
        return models.SessionTrajectory(id=trajectory_id, user_id="user_a", session_id=session_id, workspace_id="ws_a",
                                        started_at=AT, updated_at=AT, last_activity_at=AT)

    async with trace_session() as session:
        session.add(trajectory("trj_a", "session_a"))
    with pytest.raises(ValueError):
        async with trace_session() as session:
            session.add(trajectory("trj_b", "session_b"))
            await session.flush()
            raise ValueError("failure after flush")
    async with trace_session() as session:
        assert (await session.execute(select(models.SessionTrajectory.id))).scalars().all() == ["trj_a"]
    assert migrated.sync_engine.pool.checkedout() == 0


async def test_downgrade_base_drops_every_table_and_partition(migrated):
    async with migrated.begin() as connection:
        await partitions.ensure_partitions(connection, date(2026, 9, 14), 1)
    await close_trace_engine()
    await asyncio.to_thread(_alembic, "downgrade", "base")
    engine = create_async_engine(URL)
    try:
        async with engine.connect() as connection:
            tables = (await connection.execute(text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))).scalars().all()
            assert tables == ["trajectory_alembic_version"]
            assert (await connection.execute(text("SELECT count(*) FROM trajectory_alembic_version"))).scalar() == 0
    finally:
        await engine.dispose()
    await asyncio.to_thread(_alembic, "upgrade", "head")
