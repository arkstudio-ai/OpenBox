"""Decoded buffers and native decompressors must outlive only their download."""
import asyncio
import gc
import hashlib
import threading
import weakref

import pytest
import zstandard

from trajectory import payload
from trajectory.read_budget import ReadBudget, ReadTooLarge
from trajectory.types import CorruptContent


@pytest.mark.parametrize("memory_bytes", [0, 4 * 1024 * 1024])
@pytest.mark.parametrize("outcome", ["success", "source_error", "mismatch", "budget", "cancelled"])
async def test_spooling_releases_decoder_and_buffer_on_every_exit(monkeypatch, memory_bytes, outcome):
    writers, files = [], []

    class TrackedWriter(payload._SpoolWriter):
        def __init__(self, *args):
            super().__init__(*args)
            writers.append(weakref.ref(self))

        def write(self, data):
            result = super().write(data)
            if self.file is not None and self.file not in files:
                files.append(self.file)
            return result

    monkeypatch.setattr(payload, "_SpoolWriter", TrackedWriter)
    raw = b"archived event content\n" * 100_000
    stored = zstandard.ZstdCompressor().compress(raw)
    sha256 = hashlib.sha256(raw).hexdigest() if outcome != "mismatch" else "0" * 64
    waiting, closed = asyncio.Event(), asyncio.Event()

    async def chunks():
        try:
            yield stored
            if outcome == "source_error":
                raise ConnectionError("source disconnected")
            if outcome == "cancelled":
                waiting.set()
                await asyncio.Event().wait()
        finally:
            closed.set()

    async def run():
        consume = ReadBudget(1024 * 1024).consume if outcome == "budget" else None
        task = asyncio.create_task(payload._spool(chunks(), encoding="zstd", sha256=sha256,
            mismatch="digest mismatch", memory_bytes=memory_bytes, consume=consume))
        if outcome == "cancelled":
            await asyncio.wait_for(waiting.wait(), 2)
            task.cancel()
        if outcome == "success":
            spooled = await task
            try:
                # The decoder has finished; the returned file must still belong to the response.
                assert all(not file.closed for file in files)
                assert b"".join([chunk async for chunk in spooled.chunks()]) == raw
            finally:
                spooled.close()
        else:
            error = {"source_error": ConnectionError, "mismatch": CorruptContent,
                     "budget": ReadTooLarge, "cancelled": asyncio.CancelledError}[outcome]
            with pytest.raises(error):
                await task

    await run()
    await asyncio.sleep(0)
    gc.collect()
    assert closed.is_set()
    assert writers and all(writer() is None for writer in writers)
    assert all(file.closed for file in files)


async def test_cancelled_finish_joins_thread_before_releasing_its_file(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    files = []

    class SlowFinish(payload._SpoolWriter):
        def finish(self):
            result = super().finish()
            files.append(self.file)
            entered.set()
            assert release.wait(5)
            assert not self.file.closed
            return result

    monkeypatch.setattr(payload, "_SpoolWriter", SlowFinish)

    async def chunks():
        yield zstandard.ZstdCompressor().compress(b"event content" * 1000)

    task = asyncio.create_task(payload._spool(chunks(), encoding="zstd", sha256=None, mismatch="unused"))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not files[0].closed
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert files[0].closed
