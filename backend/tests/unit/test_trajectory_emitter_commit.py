"""After-commit emits on SQLAlchemy sessions: commit, rollback, savepoints, coexistence."""
import pytest
from sqlalchemy import create_engine, event as sa_event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session as SyncSession, mapped_column

from trajectory import spool
import trajectory.emitter as emitter_module
from trajectory.context import TraceContext
from trajectory.emitter import PENDING_KEY, emit_after_commit, emit_control, get_emitter, reset_emitter_for_tests

CONTEXT = TraceContext("user", "root", turn_id="turn")


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "commit_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str]


@pytest.fixture
def spool_env(tmp_path, monkeypatch):
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    yield tmp_path / "spool"
    reset_emitter_for_tests()


@pytest.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'business.sqlite'}")

    @sa_event.listens_for(engine.sync_engine, "connect")
    def connect(dbapi_connection, connection_record):
        # Documented aiosqlite recipe: SQLAlchemy emits BEGIN and SAVEPOINT itself.
        dbapi_connection.isolation_level = None

    @sa_event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def records(emitter):
    assert emitter.flush(5)
    return [spool.decode_line(line) for path in sorted(emitter.producer_dir.glob("*.jsonl"))
            for line in path.read_bytes().splitlines()]


def written(emitter) -> list[str]:
    return [record["event"]["event_id"] if record["k"] == "event" else record["control"]["type"]
            for record in records(emitter)]


def emitted(db, label):
    return emit_after_commit(db, "message.committed", {"label": label}, context=CONTEXT, event_id=label)


def forbidden(*args, **kwargs):
    raise AssertionError("listener touched a session without pending emits")


async def test_commit_enqueues_while_rollback_and_failed_commit_discard(spool_env, factory):
    emitter = get_emitter()
    data = {"label": "original"}
    async with factory() as db:
        await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (1, 'kept')"))
        assert emit_after_commit(db, "message.committed", data, context=CONTEXT, event_id="kept") == "kept"
        data["label"] = "mutated after registration"
        assert emitter.stats()["queued_lines"] == 0 and len(db.info[PENDING_KEY]) == 1
        await db.commit()
        assert PENDING_KEY not in db.info
    async with factory() as db:
        await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (2, 'rolled back')"))
        assert emitted(db, "rolled back")
        await db.rollback()
        assert PENDING_KEY not in db.info
    with pytest.raises(RuntimeError):
        async with factory.begin() as db:
            await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (3, 'raised')"))
            emitted(db, "raised")
            raise RuntimeError("business failure")
    async with factory() as db:
        db.add(Row(id=1, label="duplicate primary key"))
        assert emitted(db, "failed commit")
        with pytest.raises(IntegrityError):
            await db.commit()
        assert PENDING_KEY not in db.info
        await db.rollback()
    [record] = records(emitter)
    assert record["event"]["event_id"] == "kept" and record["event"]["data"] == {"label": "original"}
    async with factory() as db:
        assert list(await db.scalars(text("SELECT id FROM commit_rows"))) == [1]


async def test_each_commit_in_one_block_enqueues_what_was_registered_before_it(spool_env, factory):
    emitter = get_emitter()
    async with factory() as db:
        await db.execute(text("SELECT 1"))
        emitted(db, "first")
        await db.commit()
        assert written(emitter) == ["first"]
        emitted(db, "second")
        emitted(db, "third")
        assert written(emitter) == ["first"]
        await db.commit()
    assert written(emitter) == ["first", "second", "third"]


async def test_savepoints_neither_enqueue_nor_discard_the_outer_transactions_emits(spool_env, factory):
    emitter = get_emitter()
    async with factory() as db:
        await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (10, 'outer')"))
        emitted(db, "outer")
        async with db.begin_nested():
            await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (11, 'released')"))
            emitted(db, "released savepoint")
        assert emitter.stats()["queued_lines"] == 0 and len(db.info[PENDING_KEY]) == 2
        with pytest.raises(RuntimeError):
            async with db.begin_nested():
                await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (12, 'rolled back')"))
                emitted(db, "rolled back savepoint")
                raise RuntimeError("savepoint failure")
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                db.add(Row(id=10, label="duplicate inside a savepoint"))
                await db.flush()
        assert len(db.info[PENDING_KEY]) == 3
        await db.commit()
    assert written(emitter) == ["outer", "released savepoint", "rolled back savepoint"]
    async with factory() as db:
        assert sorted(await db.scalars(text("SELECT id FROM commit_rows"))) == [10, 11]
        emitted(db, "discarded with the outer transaction")
        async with db.begin_nested():
            emitted(db, "released then outer rollback")
        await db.rollback()
    assert written(emitter) == ["outer", "released savepoint", "rolled back savepoint"]


async def test_sessions_without_pending_emits_are_untouched(spool_env, factory, monkeypatch):
    emitter = get_emitter()
    monkeypatch.setattr(emitter, "enqueue_encoded", forbidden)
    monkeypatch.setattr(emitter_module, "get_emitter", forbidden)
    async with factory() as db:
        await db.execute(text("INSERT INTO commit_rows (id, label) VALUES (20, 'plain')"))
        await db.commit()
        await db.execute(text("SELECT 1"))
        await db.rollback()
        async with db.begin_nested():
            await db.execute(text("SELECT 1"))
        await db.commit()
    with SyncSession(create_engine("sqlite://")) as session:
        session.execute(text("SELECT 1"))
        session.commit()
    monkeypatch.undo()
    assert written(emitter) == []


async def test_closing_an_uncommitted_session_discards_and_reuse_starts_clean(spool_env, factory):
    emitter = get_emitter()
    db = factory()
    await db.execute(text("SELECT 1"))
    emitted(db, "closed without commit")
    await db.close()
    assert PENDING_KEY not in db.info
    await db.execute(text("SELECT 1"))
    await db.commit()
    await db.close()
    assert written(emitter) == []


async def test_controls_wait_for_commit_and_unusable_sessions_are_counted(spool_env, factory, monkeypatch):
    emitter = get_emitter()
    control = {"type": "session.deleted", "session_id": "root", "user_id": "user", "deleted_at": "2026-09-14T08:00:00Z"}
    async with factory.begin() as db:
        await db.execute(text("SELECT 1"))
        assert emit_control(control, db=db) is True
        assert emitted(db, "event after control") == "event after control"
        assert emitter.stats()["queued_lines"] == 0
    assert written(emitter) == ["session.deleted", "event after control"]
    assert emit_control(control, db=object()) is False
    assert emit_after_commit(object(), "message.committed", {}, context=CONTEXT) is None
    assert emitter.stats()["dropped_by_reason"] == {"invalid_event": 2}
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    async with factory.begin() as db:
        await db.execute(text("SELECT 1"))
        assert emitted(db, "recording disabled") is None and PENDING_KEY not in db.info
        assert emit_control(control, db=db) is True
    # The two unusable sessions above were reported through a gap control.
    assert [item for item in written(emitter) if item != "gap"] == ["session.deleted", "event after control",
                                                                    "session.deleted"]


def test_plain_sync_sessions_are_supported(spool_env):
    emitter = get_emitter()
    with SyncSession(create_engine("sqlite://")) as session:
        session.execute(text("SELECT 1"))
        assert emit_after_commit(session, "message.committed", {}, context=CONTEXT, event_id="sync") == "sync"
        assert emitter.stats()["queued_lines"] == 0
        session.commit()
    assert written(emitter) == ["sync"]
