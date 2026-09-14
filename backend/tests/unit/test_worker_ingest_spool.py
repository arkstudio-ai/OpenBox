"""Ingest file handling (SPEC §8.2, §8.3, §8.6): readiness, producer loss, quarantine, restarts, blob outages."""
import os
import socket
import subprocess
import sys
import time

import pytest

from trajectory import spool
from trajectory.store.models import TrajectoryEvent, TrajectoryIngestFile, TrajectoryIngestProducer
from trajectory.worker import spool_reader
from trajectory.worker.content import BLOB_UNAVAILABLE
from trajectory.worker.ingest import IngestService
from tests.unit.test_worker_ingest import (SpoolWriter, event, events_of, harness, rows, settings,  # noqa: F401
    trace_db)

TORN = b'{"v":1,"k":"event","n":3,"t":"2026-09-14T08:0'


def _age(path, seconds):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


async def test_open_part_blocks_its_producer_until_abandoned(harness):
    writer = harness.writer
    part = writer.file([writer.line("event", event(event_id="p1", run_id="run_9")),
                        writer.line("event", event(event_id="p2", run_id="run_9")), TORN], closed=False)
    later = writer.file([writer.line("event", event(event_id="later"), n=4)])
    assert (await harness.run())["lines"] == 0 and later.exists()
    _age(part, 120)
    result = await harness.run()
    assert not part.exists() and not later.exists()
    _, stored = await events_of("ses_1")
    producer = writer.producer_id
    assert [(row.type, row.event_id) for row in stored[1:]] == [
        ("input.accepted", "p1"), ("input.accepted", "p2"),
        ("recording.gap", f"gap:{producer}:3-3:ses_1:run_9"), ("input.accepted", "later")]
    assert stored[3].data == {"phase": "lost", "reason": "producer_lines_lost", "producer_id": producer,
                              "from_n": 3, "to_n": 3}
    assert stored[3].context == {"run_id": "run_9"}
    assert result["producer_losses"] == 1 and harness.metrics.counters["producer_loss_events"] == 1
    [producer_row] = await rows(TrajectoryIngestProducer)
    assert (producer_row.last_n, producer_row.abandoned) == (4, False)


async def test_abandoned_newest_part_reports_a_crashed_producer_once(harness):
    writer = harness.writer
    writer.file([writer.line("event", event(event_id="e1", run_id="run_1")), TORN], closed=False, age=120)
    result = await harness.run()
    _, stored = await events_of("ses_1")
    producer = writer.producer_id
    assert [row.event_id for row in stored[1:]] == ["e1", f"gap:{producer}:2-:ses_1:run_1"]
    assert stored[2].data == {"phase": "lost", "reason": "producer_crashed", "producer_id": producer, "from_n": 2,
                              "to_n": None}
    [producer_row] = await rows(TrajectoryIngestProducer)
    assert producer_row.abandoned and result["producer_losses"] == 1
    await harness.run()
    assert not writer.directory.exists()
    assert len((await events_of("ses_1"))[1]) == 3


async def test_goodbye_producer_directory_is_removed_after_its_files(harness):
    writer = harness.writer
    writer.file([writer.line("event", event()), writer.line("control", {"type": "producer.goodbye", "last_n": 2})])
    await harness.run()
    [producer_row] = await rows(TrajectoryIngestProducer)
    assert producer_row.goodbye and producer_row.last_n == 2
    await harness.run()
    assert not writer.directory.exists()


async def test_dead_local_producer_without_goodbye_is_declared_crashed(harness):
    harness.configure(spool_abandon_seconds=1)
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(finished.stdout.strip())
    local = SpoolWriter(harness.settings.spool_dir, "20260914080001-local-2-bbbbbbbb", hostname=socket.gethostname(),
                        pid=dead_pid, boot_id=spool.boot_id())
    local.events(event(session="ses_local", run_id="run_l"))
    await harness.run()
    assert local.directory.exists()
    _age(local.directory, 30)
    result = await harness.run()
    assert not local.directory.exists() and result["producer_losses"] == 1
    _, stored = await events_of("ses_local")
    assert stored[-1].event_id == f"gap:{local.producer_id}:2-:ses_local:run_l"
    # The default producer lives on another host and stays untouched.
    assert harness.writer.directory.exists()


async def test_missing_counters_are_reported_as_lost_lines(harness):
    writer = harness.writer
    writer.file([writer.line("event", event(run_id="run_a")), writer.line("event", event(run_id="run_a"))])
    writer.file([writer.line("event", event(event_id="after_loss"), n=5)])
    await harness.run()
    _, stored = await events_of("ses_1")
    assert stored[3].event_id == f"gap:{writer.producer_id}:3-4:ses_1:run_a"
    assert stored[4].event_id == "after_loss"


async def test_unparsable_file_is_quarantined_after_its_good_lines(harness):
    writer = harness.writer
    path = writer.file([writer.line("event", event(event_id="good", run_id="run_q")), b"{not json}\n",
                        writer.line("event", event(event_id="unreachable"), n=3)])
    result = await harness.run()
    assert not path.exists()
    target = harness.settings.spool_dir / "quarantine" / f"{writer.producer_id}__{path.name}"
    assert target.exists()
    reason = spool_reader.read_producer_document(target.with_name(target.name + ".reason"))
    assert reason["reason"] == "unparsable_line" and reason["last_n"] == 3
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored[1:]] == ["good", f"gap:{writer.producer_id}:2-3:ses_1:run_q"]
    assert result["quarantined_files"] == 1 and harness.metrics.counters["quarantined_files"] == 1
    assert await rows(TrajectoryIngestFile) == []
    newer = writer.file([b'{"v":2,"k":"event","n":4,"t":"2026-09-14T08:00:00.000Z","event":{}}\n'])
    await harness.run()
    assert not newer.exists()
    assert spool_reader.read_producer_document(harness.settings.spool_dir / "quarantine" /
                                               f"{writer.producer_id}__{newer.name}.reason")["reason"] == \
        "unsupported_version"


async def test_offsets_commit_with_rows_and_a_restart_never_duplicates(harness, monkeypatch):
    harness.configure(ingest_batch_lines=2)
    system = {"model": "m", "input": {"system": "S" * 3000}}
    harness.writer.events(event("request.prepared", request_id="r0", event_id="e0", data=system),
                          *[event(event_id=f"e{index}") for index in range(1, 5)])
    original = IngestService._commit
    calls = {"count": 0}

    async def killed_on_second_batch(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("worker killed between upload and commit")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(IngestService, "_commit", killed_on_second_batch)
    with pytest.raises(RuntimeError):
        await harness.run()
    [file_row] = await rows(TrajectoryIngestFile)
    assert (file_row.lines_consumed, file_row.done) == (2, False)
    objects = dict(harness.store.objects)
    monkeypatch.setattr(IngestService, "_commit", original)
    # Kill between commit and file deletion: the file survives with a done row.
    monkeypatch.setattr(spool_reader, "remove_file", lambda path: False)
    harness.configure()
    await harness.run()
    [file_row] = await rows(TrajectoryIngestFile)
    assert file_row.done and file_row.lines_consumed == 5
    monkeypatch.undo()
    harness.configure()
    result = await harness.run()
    assert result["lines"] == 0 and result["duplicates"] == 0
    assert await rows(TrajectoryIngestFile) == [] and not list(harness.writer.directory.glob("*.jsonl"))
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored[1:]] == [f"e{index}" for index in range(5)]
    assert [row.seq for row in stored] == list(range(1, 7))
    assert harness.store.objects == objects and len(objects) == 1


async def test_blob_store_outage_backs_off_then_records_not_recorded(harness, tmp_path):
    harness.store.fail("put")
    outage = harness.writer
    outage.events(event("request.prepared", session="ses_a", request_id="r1", event_id="big",
                        data={"model": "m", "input": {"system": "S" * 3000}}))
    other = SpoolWriter(harness.settings.spool_dir, "20260914080002-other-3-cccccccc")
    other.events(event(session="ses_b"))
    result = await harness.run()
    assert result["deferred_batches"] == 1 and result["blob_put_failures"] == 1
    assert (await events_of("ses_a"))[0] is None and len((await events_of("ses_b"))[1]) == 2
    [backoff] = harness.service._backoff.values()
    assert backoff.attempts == 1 and backoff.next_at > time.monotonic()
    assert (await harness.run())["deferred_batches"] == 1  # still waiting: no new attempt
    assert harness.metrics.counters["blob_put_failures"] == 1
    for _ in range(9):
        for held in harness.service._backoff.values():
            held.next_at = 0.0
        await harness.run()
    assert harness.metrics.counters["blob_put_failures"] == 10
    trajectory, stored = await events_of("ses_a")
    big = next(row for row in stored if row.event_id == "big")
    assert big.data["input"]["system"] == BLOB_UNAVAILABLE and big.data["model"] == "m"
    gap = stored[-1]
    assert gap.event_id == f"gap:{outage.producer_id}:1-1:ses_a"
    assert gap.data["reason"] == "blob_store_unavailable" and gap.data["event_ids"] == ["big"]
    assert harness.service._backoff == {} and not list(outage.directory.glob("*.jsonl"))


async def test_max_lines_limits_one_pass(harness):
    harness.writer.events(*[event() for _ in range(5)])
    assert (await harness.run(max_lines=3))["lines"] == 3
    assert harness.service.last_lag_seconds >= 0 and list(harness.writer.directory.glob("*.jsonl"))
    assert (await harness.run())["lines"] == 2
    assert len(await rows(TrajectoryEvent)) == 6
