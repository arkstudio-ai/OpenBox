"""Trace engine and sessions (SPEC 6.1): separate engine, pool settings, commit/rollback/close."""
from datetime import datetime, timezone

import orjson
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError

import db.base as business_database
from trajectory.store import database
from trajectory.store.database import (TraceBase, close_trace_engine, get_trace_engine, init_trace_engine,
    trace_dialect, trace_session)
from trajectory.store.models import SessionTrajectory, TrajectoryEvent


class Abort(BaseException):
    pass


def _trajectory(trajectory_id="trj_a", session_id="session_a"):
    at = datetime.now(timezone.utc)
    return SessionTrajectory(id=trajectory_id, user_id="user_a", session_id=session_id, workspace_id="ws_a",
                             started_at=at, updated_at=at, last_activity_at=at)


def _event(trajectory_id="trj_a", seq=1):
    at = datetime.now(timezone.utc)
    return TrajectoryEvent(trajectory_id=trajectory_id, seq=seq, recorded_on=at.date(), event_id=f"evt_{trajectory_id}_{seq}",
                           type="input.accepted", version=1, user_id="user_a", session_id="session_a",
                           source_session_id="session_a", context={"user_id": "user_a"},
                           data={"text": "你好", "nested": [1, {"key": None}]}, content_hash="0" * 64,
                           occurred_at=at, recorded_at=at)


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


async def _count(model) -> int:
    async with trace_session() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_accessors_require_an_initialized_engine():
    await close_trace_engine()
    await close_trace_engine()
    with pytest.raises(RuntimeError):
        get_trace_engine()
    with pytest.raises(RuntimeError):
        trace_dialect()
    with pytest.raises(RuntimeError):
        async with trace_session():
            pass


async def test_sqlite_engine_is_separate_and_gets_no_pool_arguments(tmp_path, monkeypatch):
    calls = []
    real_create = database.create_async_engine

    def capture(url, **kwargs):
        calls.append(kwargs)
        return real_create(url, **kwargs)

    monkeypatch.setattr(database, "create_async_engine", capture)
    business = (business_database._engine, business_database._session_factory)
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}", pool_size=9, max_overflow=9)
    try:
        assert calls == [{"echo": False}]
        assert get_trace_engine() is engine and trace_dialect() == "sqlite"
        assert engine is not business[0]
        async with engine.connect() as connection:
            assert (await connection.execute(text("PRAGMA foreign_keys"))).scalar() == 1
            assert (await connection.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
    finally:
        await close_trace_engine()
    assert (business_database._engine, business_database._session_factory) == business
    with pytest.raises(RuntimeError):
        get_trace_engine()


@pytest.mark.parametrize("kwargs, size, overflow", [({}, 5, 5), ({"pool_size": 3, "max_overflow": 4}, 3, 4)])
async def test_postgresql_engine_pre_pings_and_sets_server_settings(kwargs, size, overflow):
    class Refused(Exception):
        pass

    captured = {}

    def refuse(dialect, connection_record, cargs, cparams):
        captured.update(cparams)
        raise Refused()

    await close_trace_engine()
    engine = init_trace_engine("postgresql+asyncpg://trace:secret@127.0.0.1:9/openbox_trace", **kwargs)
    event.listen(engine.sync_engine, "do_connect", refuse)
    try:
        assert trace_dialect() == "postgresql"
        pool = engine.sync_engine.pool
        assert (pool.size(), pool._max_overflow, pool._pre_ping) == (size, overflow, True)
        with pytest.raises(Refused):
            async with engine.connect():
                pass
        assert captured["server_settings"] == {"statement_timeout": "5000", "application_name": "openbox-trace"}
    finally:
        await close_trace_engine()


async def test_postgresql_engines_bind_and_read_jsonb_through_orjson(monkeypatch):
    """JSONB binds went through the stdlib json; both PostgreSQL engines (writer and TraceReader) now share orjson."""
    calls = []
    real_create = database.create_async_engine

    def capture(url, **kwargs):
        calls.append(kwargs)
        return real_create(url, **kwargs)

    monkeypatch.setattr(database, "create_async_engine", capture)
    await close_trace_engine()
    engine = init_trace_engine("postgresql+asyncpg://trace:secret@127.0.0.1:9/openbox_trace")
    try:
        assert engine.dialect._json_serializer is database.json_serializer
        assert engine.dialect._json_deserializer is orjson.loads
        database.TraceReader()._factory()
        assert database._read_engine.dialect._json_serializer is database.json_serializer
        assert database._read_engine.dialect._json_deserializer is orjson.loads
        assert [(kwargs["json_serializer"], kwargs["json_deserializer"]) for kwargs in calls] == [
            (database.json_serializer, orjson.loads)] * 2
    finally:
        await close_trace_engine()
    document = {"text": "你好", "nested": [1, {"key": None}], "when": datetime(2026, 9, 14, tzinfo=timezone.utc)}
    text_form = database.json_serializer(document)
    assert text_form == '{"text":"你好","nested":[1,{"key":null}],"when":"2026-09-14T00:00:00+00:00"}'
    assert orjson.loads(text_form) == {**document, "when": "2026-09-14T00:00:00+00:00"}
    # What orjson refuses to write goes through the stdlib json.dumps the engines used before: integers beyond
    # 64 bits and non-string keys.
    assert database.json_serializer({"big": 2 ** 70 + 1, 1: "one"}) == '{"big": 1180591620717411303425, "1": "one"}'


async def test_unsupported_dialect_is_rejected():
    await close_trace_engine()
    with pytest.raises(ValueError, match="mysql"):
        init_trace_engine("mysql+aiomysql://trace:secret@localhost/trace")
    with pytest.raises(RuntimeError):
        get_trace_engine()


async def test_trace_session_commits_on_success(trace_db):
    async with trace_session() as session:
        session.add(_trajectory())
    async with trace_session() as session:
        row = await session.get(SessionTrajectory, "trj_a")
    assert (row.next_seq, row.committed_seq, row.schema_version, row.recording_status, row.budget_level) == (
        1, 0, 2, "recording", "normal")
    assert trace_db.sync_engine.pool.checkedout() == 0


async def test_trace_session_rolls_back_on_exception_and_closes(trace_db):
    with pytest.raises(ValueError):
        async with trace_session() as session:
            session.add(_trajectory())
            await session.flush()
            raise ValueError("failure after flush")
    assert await _count(SessionTrajectory) == 0
    assert trace_db.sync_engine.pool.checkedout() == 0


async def test_failed_commit_is_rolled_back_and_closed(trace_db):
    async with trace_session() as session:
        session.add(_trajectory())
    with pytest.raises(IntegrityError):
        async with trace_session() as session:
            session.add(_trajectory("trj_b", session_id="session_b"))
            session.add(_trajectory("trj_c", session_id="session_a"))
    assert await _count(SessionTrajectory) == 1
    assert trace_db.sync_engine.pool.checkedout() == 0


async def test_base_exception_discards_uncommitted_work_and_closes(trace_db):
    with pytest.raises(Abort):
        async with trace_session() as session:
            session.add(_trajectory())
            await session.flush()
            raise Abort()
    assert await _count(SessionTrajectory) == 0
    assert trace_db.sync_engine.pool.checkedout() == 0


async def test_sqlite_foreign_keys_cascade_and_reject_orphans(trace_db):
    async with trace_session() as session:
        session.add(_trajectory())
        await session.flush()
        session.add(_event())
    async with trace_session() as session:
        stored = await session.get(TrajectoryEvent, ("trj_a", 1))
        assert stored.data == {"text": "你好", "nested": [1, {"key": None}]}
    with pytest.raises(IntegrityError):
        async with trace_session() as session:
            session.add(_event("trj_missing"))
    async with trace_session() as session:
        await session.delete(await session.get(SessionTrajectory, "trj_a"))
    assert await _count(TrajectoryEvent) == 0
