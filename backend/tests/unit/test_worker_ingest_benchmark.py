"""Ingest throughput and memory bounds: at least 5,000 small events per second on SQLite, streamed reads."""
import time
import tracemalloc

import orjson

from trajectory import spool
from trajectory.storage import MemoryBlobStore
from trajectory.worker import spool_reader
from tests.unit.test_worker_ingest import event, harness, settings, trace_db  # noqa: F401

EVENTS = 20000
MIN_EVENTS_PER_SECOND = 5000


async def test_ingest_handles_5000_small_events_per_second_on_sqlite(harness):
    writer = harness.writer
    lines = [writer.line("event", event(session=f"ses_{index % 50}", event_id=f"bench_{index}", run_id="run_bench",
                                        data={"role": "user", "text": f"message {index}"}))
             for index in range(EVENTS)]
    for offset in range(0, EVENTS, 2000):
        writer.file(lines[offset:offset + 2000])
    del lines
    started = time.perf_counter()
    ingested = 0
    for _ in range(50):
        ingested += (await harness.run())["events"]
        if ingested >= EVENTS:
            break
    elapsed = time.perf_counter() - started
    rate = ingested / elapsed
    print(f"ingest: {ingested} small events in {elapsed:.2f} s = {rate:.0f} events/s")
    assert ingested == EVENTS
    assert rate >= MIN_EVENTS_PER_SECOND


def test_reading_a_large_file_holds_only_one_batch(tmp_path):
    path = tmp_path / spool.file_name(1)
    body = orjson.dumps(event(data={"text": "x" * 500}))
    with open(path, "wb") as handle:
        for n in range(1, 100_001):
            handle.write(spool.encode_event_line(n, b"2026-09-14T08:00:00.000Z", body))
    size = path.stat().st_size
    tracemalloc.start()
    try:
        offset = lines = 0
        while True:
            batch = spool_reader.read_batch(path, offset, max_lines=2000, max_bytes=1024 * 1024)
            if not batch.lines:
                break
            lines += len(batch.lines)
            offset = batch.end_offset
            del batch
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert lines == 100_000 and offset == size
    assert peak < 8 * 1024 * 1024 < size


class DiscardingBlobStore(MemoryBlobStore):
    """Keeps keys and sizes only, so traced memory is the ingest's own and not the store's contents."""

    async def put(self, key, data, *, content_type, if_absent=True):
        await super().put(key, b"", content_type=content_type, if_absent=if_absent)
        self.bytes += len(data)


async def test_ingest_memory_stays_bounded_for_large_events(harness):
    harness.store = DiscardingBlobStore()
    harness.configure(ingest_batch_bytes=2 * 1024 * 1024)
    writer = harness.writer
    big = "abc" * (1024 * 1024 // 3)
    path = writer.file([writer.line("event", event("tool.finished", call_id=f"c{index}",
                                                   data={"output": f"{big}{index}"})) for index in range(40)])
    size = path.stat().st_size
    tracemalloc.start()
    try:
        result = await harness.run()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    print(f"ingest: {size} byte file, traced peak {peak} bytes")
    assert result["events"] == 40 and not path.exists()
    assert peak < size * 3 // 4
