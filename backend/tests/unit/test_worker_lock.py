"""Single-writer lock (SPEC §8.1): SQLite file lock and in-memory lock behavior."""
import pytest

from trajectory.store.database import close_trace_engine, init_trace_engine
from trajectory.worker.lock import FileWriterLock, ProcessWriterLock, writer_lock_for


@pytest.fixture
async def engine(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    yield engine
    await close_trace_engine()


async def test_file_lock_is_exclusive_even_inside_one_process(engine, tmp_path):
    first, second = writer_lock_for(engine), writer_lock_for(engine)
    assert isinstance(first, FileWriterLock)
    assert first.path == (tmp_path / "trace.db.writer.lock").resolve()
    assert await first.acquire() is True
    assert await first.acquire() is True  # idempotent for the holder
    assert await first.verify() is True
    assert await second.acquire() is False
    assert second.held is False
    await first.release()
    await first.release()  # idempotent
    assert await first.verify() is False
    assert await second.acquire() is True
    await second.release()


async def test_file_lock_reports_an_unusable_path(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    lock = FileWriterLock(blocker / "trace.db.writer.lock")
    assert await lock.acquire() is False
    assert lock.held is False


async def test_memory_database_uses_a_process_local_lock():
    await close_trace_engine()
    engine = init_trace_engine("sqlite+aiosqlite:///:memory:")
    try:
        first, second = writer_lock_for(engine), writer_lock_for(engine)
        assert isinstance(first, ProcessWriterLock)
        assert await first.acquire() is True
        assert await second.acquire() is False
        await first.release()
        assert await second.acquire() is True
        await second.release()
    finally:
        await close_trace_engine()
