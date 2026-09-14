"""Single-writer lock (SPEC §8.1): SQLite file lock and in-memory lock behavior; the object guard."""
import asyncio

import pytest

from trajectory.store.database import close_trace_engine, init_trace_engine
from trajectory.worker.lock import FileWriterLock, ObjectGuard, ProcessWriterLock, writer_lock_for


async def test_object_guard_keeps_deletes_apart_from_batches():
    guard = ObjectGuard()
    order = []
    inside, release = asyncio.Event(), asyncio.Event()

    async def batch(name, hold=None):
        async with guard.shared():
            order.append(f"{name}+")
            if hold is not None:
                inside.set()
                await hold.wait()
            order.append(f"{name}-")

    async def delete():
        async with guard.exclusive():
            order.append("delete")

    first = asyncio.create_task(batch("a", release))
    await inside.wait()
    deleting = asyncio.create_task(delete())
    await asyncio.sleep(0.01)
    # A batch that arrives while a delete waits queues behind the delete, so deletes cannot starve.
    later = asyncio.create_task(batch("b"))
    await asyncio.sleep(0.01)
    assert order == ["a+"] and guard.holders == 1
    release.set()
    await asyncio.wait_for(asyncio.gather(first, deleting, later), 1)
    assert order == ["a+", "a-", "delete", "b+", "b-"]
    # A holder cancelled inside the guard does not keep it.
    held = asyncio.Event()

    async def stuck():
        async with guard.shared():
            held.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(stuck())
    await held.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(delete(), 1)
    assert guard.holders == 0 and order[-1] == "delete"


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
