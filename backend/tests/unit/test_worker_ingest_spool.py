"""Ingest file handling (SPEC §8.2, §8.3, §8.6): readiness, producer loss, quarantine, restarts, blob outages."""
import asyncio
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager

import pytest

from trajectory import spool
from trajectory.store.database import close_trace_engine
from trajectory.store.models import TrajectoryEvent, TrajectoryIngestFile, TrajectoryIngestProducer
from trajectory.worker import ingest as ingest_module
from trajectory.worker import spool_reader
from trajectory.worker.content import BLOB_UNAVAILABLE
from trajectory.worker.ingest import IngestService
from trajectory.worker.metrics import Metrics
from tests.unit.test_worker_ingest import (SpoolWriter, event, events_of, harness, rows, settings,  # noqa: F401
    trace_db)

TORN = b'{"v":1,"k":"event","n":3,"t":"2026-09-14T08:0'
DELETED = {"type": "session.deleted", "session_id": "ses_1", "user_id": "u1", "deleted_at": "2026-09-14T09:00:00.000Z"}
#: A heartbeat this old confirms an idle producer without goodbye gone, whether or not a restart is seen.
CONFIRMED = ingest_module.CRASH_CONFIRM_SECONDS + 1


def _age(path, seconds):
    moment = time.time() - seconds
    os.utime(path, (moment, moment))


def _release(service):
    """Let every file that backs off after a failure retry on the next pass."""
    for failure in service._failures.values():
        failure.next_at = 0.0


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


async def test_an_abandoned_part_whose_writer_resumes_during_the_commit_is_read_on(harness, monkeypatch):
    writer = harness.writer
    part = writer.file([writer.line("event", event(event_id="e1"))], closed=False, age=120)
    _age(writer.directory / spool.PRODUCER_FILE, 120)
    removal = spool_reader.remove_if_unchanged

    def resumed(path, signature):
        # The stalled writer appends a line after the batch was read, before the consumed file is removed.
        with open(path, "ab") as handle:
            handle.write(writer.line("event", event(event_id="e2")))
        return removal(path, signature)

    monkeypatch.setattr(spool_reader, "remove_if_unchanged", resumed)
    await harness.run()
    [file_row] = await rows(TrajectoryIngestFile)
    assert part.exists() and not file_row.done

    monkeypatch.setattr(spool_reader, "remove_if_unchanged", removal)
    _age(part, 120)
    await harness.run()
    assert not part.exists() and await rows(TrajectoryIngestFile) == []
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored if row.type == "input.accepted"] == ["e1", "e2"]


async def test_spool_gauges_count_blobs_and_quarantine_like_the_emitter_budget(harness):
    blobs = spool.blobs_dir(harness.settings.spool_dir)
    spool.ensure_private_dir(blobs)
    (blobs / ("a" * 64)).write_bytes(b"x" * 5000)
    quarantine = harness.settings.spool_dir / spool.QUARANTINE_DIR
    spool.ensure_private_dir(quarantine)
    (quarantine / "producer__000001.jsonl").write_bytes(b"y" * 3000)
    (quarantine / "producer__000001.jsonl.reason").write_bytes(b"{}")
    await harness.run()
    usage = spool.spool_usage(harness.settings.spool_dir)
    gauges = harness.metrics.gauges
    assert gauges["spool_bytes"] == usage.total >= 8000
    assert gauges["spool_blob_bytes"] == usage.blob_bytes >= 5000
    assert (gauges["spool_quarantine_bytes"], gauges["spool_quarantine_files"]) == (usage.quarantine_bytes, 1)


async def test_backlog_metrics_refresh_between_batches_of_one_long_pass(harness, monkeypatch):
    harness.configure(ingest_batch_lines=2)
    harness.writer.events(*(event(event_id=f"e{index}") for index in range(4)), age=60)
    original = harness.service._ingest_batch
    observed = []

    async def ingest_batch(*args, **kwargs):
        gauges = harness.metrics.gauges
        observed.append((gauges.get("ingest_lag_seconds", 0), gauges["spool_bytes"]))
        if len(observed) == 1:
            # New spool content arrives while this pass is still running; let
            # the existing 30-second sampling interval elapse before batch 2.
            blobs = spool.blobs_dir(harness.settings.spool_dir)
            spool.ensure_private_dir(blobs)
            (blobs / ("a" * 64)).write_bytes(b"x" * 5000)
            harness.service._usage_sampled = time.monotonic() - ingest_module.SPOOL_USAGE_SAMPLE_SECONDS - 1
        return await original(*args, **kwargs)

    monkeypatch.setattr(harness.service, "_ingest_batch", ingest_batch)
    result = await harness.run()
    assert result["events"] == 4 and len(observed) >= 2
    assert all(lag >= 60 for lag, _ in observed)
    assert observed[1][1] >= observed[0][1] + 5000
    assert harness.service.last_lag_seconds == harness.metrics.gauges["ingest_lag_seconds"] == 0


async def test_an_old_newest_part_waits_while_its_producer_heartbeats(harness):
    """A process whose writer thread is stalled still refreshes producer.json from a thread of its own: its open
    file stays unread however old. Once the heartbeat is stale too the file is consumed, without a gap."""
    writer = harness.writer
    part = writer.file([writer.line("event", event(event_id="e1", run_id="run_1")), TORN], closed=False, age=600)
    result = await harness.run()
    assert (result["lines"], result["producer_losses"]) == (0, 0) and part.exists()
    _age(writer.directory / spool.PRODUCER_FILE, 120)
    result = await harness.run()
    assert (result["events"], result["producer_losses"], result["gaps"]) == (1, 0, 0) and not part.exists()
    assert [row.event_id for row in (await events_of("ses_1"))[1][1:]] == ["e1"]


async def test_the_producer_of_this_process_never_abandons_its_newest_part(harness):
    own = SpoolWriter(harness.settings.spool_dir, "20260914080008-own-9-12345678", hostname=socket.gethostname(),
                      pid=os.getpid(), boot_id=spool.boot_id())
    part = own.file([own.line("event", event(session="ses_own"))], closed=False, age=600)
    _age(own.directory / spool.PRODUCER_FILE, CONFIRMED)
    result = await harness.run()
    assert (result["lines"], result["producer_losses"]) == (0, 0) and part.exists()
    os.rename(part, part.with_name(spool.file_name(own.counter)))  # closed
    assert (await harness.run())["events"] == 1
    assert (await harness.run())["producer_losses"] == 0 and own.directory.exists()


async def test_a_crashed_producer_is_reported_once_its_heartbeat_is_old_enough(harness):
    writer = harness.writer
    heartbeat = writer.directory / spool.PRODUCER_FILE
    writer.file([writer.line("event", event(event_id="e1", run_id="run_1")), TORN], closed=False, age=120)
    _age(heartbeat, 120)
    assert (await harness.run())["producer_losses"] == 0
    # Idle without goodbye, but no restart is seen and the heartbeat is not CRASH_CONFIRM_SECONDS old.
    assert (await harness.run())["producer_losses"] == 0 and writer.directory.exists()
    _age(heartbeat, CONFIRMED)
    result = await harness.run()
    _, stored = await events_of("ses_1")
    producer = writer.producer_id
    assert [row.event_id for row in stored[1:]] == ["e1", f"gap:{producer}:2-:ses_1:run_1"]
    assert stored[2].data == {"phase": "lost", "reason": "producer_crashed", "producer_id": producer, "from_n": 2,
                              "to_n": None}
    [producer_row] = await rows(TrajectoryIngestProducer)
    assert producer_row.abandoned and result["producer_losses"] == 1 and not writer.directory.exists()
    assert (await harness.run())["producer_losses"] == 0
    assert len((await events_of("ses_1"))[1]) == 3


async def test_a_restart_on_the_same_host_and_boot_confirms_a_crash_at_once(harness):
    writer = harness.writer
    spool_dir = harness.settings.spool_dir
    writer.file([writer.line("event", event(event_id="e1", run_id="run_1"))], closed=False, age=120)
    _age(writer.directory / spool.PRODUCER_FILE, 120)
    await harness.run()
    # Another boot, or a start before the old heartbeat stopped, is no restart of that process.
    SpoolWriter(spool_dir, "20260914080100-elsewhere-2-bbbbbbbb", boot_id="another", started_at=spool.timestamp())
    SpoolWriter(spool_dir, "20260914080101-elsewhere-3-cccccccc", started_at=spool.timestamp(time.time() - 3600))
    assert (await harness.run())["producer_losses"] == 0 and writer.directory.exists()
    SpoolWriter(spool_dir, "20260914080102-elsewhere-4-dddddddd", started_at=spool.timestamp())
    result = await harness.run()
    assert result["producer_losses"] == 1 and not writer.directory.exists()
    assert (await events_of("ses_1"))[1][-1].event_id == f"gap:{writer.producer_id}:2-:ses_1:run_1"


async def test_a_producer_that_writes_again_before_its_crash_is_confirmed_gets_no_gap(harness):
    writer = harness.writer
    heartbeat = writer.directory / spool.PRODUCER_FILE
    writer.file([writer.line("event", event(event_id="e1", run_id="run_1"))], closed=False, age=120)
    _age(heartbeat, 120)
    assert (await harness.run())["events"] == 1
    assert (await harness.run())["producer_losses"] == 0  # idle, not confirmed
    # The process was only frozen: its heartbeat is fresh again and its next lines go to a new file.
    os.utime(heartbeat)
    writer.events(event(event_id="e2", run_id="run_1"))
    for _ in range(2):
        assert (await harness.run())["producer_losses"] == 0
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored[1:]] == ["e1", "e2"] and writer.directory.exists()


async def test_the_crash_gap_names_sessions_seen_in_the_300_seconds_before_the_heartbeat_stopped(harness):
    """Sessions come from RecentSessions, which keeps RECENT_SESSION_SECONDS: a crash confirmed only
    CRASH_CONFIRM_SECONDS after the last heartbeat still names the sessions seen in the 300 s before it."""
    window, confirm = ingest_module.RECENT_SESSION_SECONDS, ingest_module.CRASH_CONFIRM_SECONDS
    assert window - confirm >= 300
    writer = harness.writer
    writer.events(event(event_id="e1", run_id="run_1"), event(session="ses_old", event_id="old", run_id="run_o"))
    await harness.run()
    sessions = harness.service._recent.producers[writer.producer_id]
    now = time.time()
    sessions[("u1", "ses_1")] = (now - confirm - 290, "run_1")
    sessions[("u1", "ses_old")] = (now - window - 10, "run_o")
    _age(writer.directory / spool.PRODUCER_FILE, confirm + 1)
    assert (await harness.run())["producer_losses"] == 1
    assert (await events_of("ses_1"))[1][-1].event_id == f"gap:{writer.producer_id}:3-:ses_1:run_1"
    assert [row.type for row in (await events_of("ses_old"))[1]] == ["trajectory.started", "input.accepted"]


async def test_an_idle_producer_without_a_readable_producer_json_ages_by_its_directory(harness):
    gone = SpoolWriter(harness.settings.spool_dir, "20260914080007-gone-8-99999999", hostname="gone", pid=8)
    gone.events(event(session="ses_gone", run_id="run_g"))
    await harness.run()
    (gone.directory / spool.PRODUCER_FILE).unlink()
    assert (await harness.run())["producer_losses"] == 0 and gone.directory.exists()  # the directory changed just now
    _age(gone.directory, CONFIRMED)
    result = await harness.run()
    assert result["producer_losses"] == 1 and not gone.directory.exists()
    assert (await events_of("ses_gone"))[1][-1].event_id == f"gap:{gone.producer_id}:2-:ses_gone:run_g"


async def test_goodbye_producer_directory_is_removed_after_its_files(harness):
    writer = harness.writer
    writer.file([writer.line("event", event()), writer.line("control", {"type": "producer.goodbye", "last_n": 2})])
    await harness.run()
    [producer_row] = await rows(TrajectoryIngestProducer)
    assert producer_row.goodbye and producer_row.last_n == 2
    await harness.run()
    assert not writer.directory.exists()


async def test_a_local_producer_is_declared_crashed_only_once_its_heartbeat_stops(harness):
    """A pid this process cannot see proves nothing: the containers of one pod share hostname and boot id
    but not their pid namespace. Only a stale producer.json does."""
    finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True)
    dead_pid = int(finished.stdout.strip())
    local = SpoolWriter(harness.settings.spool_dir, "20260914080001-local-2-bbbbbbbb", hostname=socket.gethostname(),
                        pid=dead_pid, boot_id=spool.boot_id())
    local.events(event(session="ses_local", run_id="run_l"))
    await harness.run()
    assert (await harness.run())["producer_losses"] == 0 and local.directory.exists()  # heartbeat fresh
    _age(local.directory / spool.PRODUCER_FILE, 120)
    assert (await harness.run())["producer_losses"] == 0 and local.directory.exists()  # stale, not yet confirmed
    _age(local.directory / spool.PRODUCER_FILE, CONFIRMED)
    result = await harness.run()
    assert not local.directory.exists() and result["producer_losses"] == 1
    _, stored = await events_of("ses_local")
    assert stored[-1].event_id == f"gap:{local.producer_id}:2-:ses_local:run_l"
    # The default producer lives on another host and its producer.json is fresh: it stays untouched.
    assert harness.writer.directory.exists()


async def test_a_producer_whose_heartbeat_stopped_is_declared_crashed_on_any_host(harness):
    """A killed backend container comes back under a new hostname, so no later producer ever shares the old one.
    A running emitter refreshes its producer.json every few seconds; one left unrefreshed for
    CRASH_CONFIRM_SECONDS without goodbye is dead: loss reported once, directory removed."""
    gone = SpoolWriter(harness.settings.spool_dir, "20260914080006-oldcontainer-7-ffffffff", hostname="oldcontainer",
                       pid=7)
    gone.events(event(session="ses_gone", run_id="run_g"))
    await harness.run()
    assert (await harness.run())["producer_losses"] == 0 and gone.directory.exists()  # heartbeat fresh
    _age(gone.directory / spool.PRODUCER_FILE, CONFIRMED)
    result = await harness.run()
    assert not gone.directory.exists() and result["producer_losses"] == 1
    _, stored = await events_of("ses_gone")
    assert stored[-1].event_id == f"gap:{gone.producer_id}:2-:ses_gone:run_g"
    assert stored[-1].data == {"phase": "lost", "reason": "producer_crashed", "producer_id": gone.producer_id,
                               "from_n": 2, "to_n": None}
    [row] = await rows(TrajectoryIngestProducer, TrajectoryIngestProducer.producer_id == gone.producer_id)
    assert row.abandoned
    assert harness.writer.directory.exists()
    assert (await harness.run())["producer_losses"] == 0


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
    newer = writer.file([b'{"v":3,"k":"event","n":4,"t":"2026-09-14T08:00:00.000Z","event":{}}\n'])
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

    class WorkerKilled(BaseException):
        """Like a kill, nothing inside the worker handles it (test_worker_ingest_crash.py kills processes)."""

    async def killed_on_second_batch(self, *args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 2:
            raise WorkerKilled("worker killed between upload and commit")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(IngestService, "_commit", killed_on_second_batch)
    with pytest.raises(WorkerKilled):
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


async def test_finished_producer_directories_take_their_file_rows_along(harness):
    from datetime import datetime, timezone

    from trajectory.store.database import trace_session

    writer = harness.writer
    writer.file([writer.line("event", event()), writer.line("control", {"type": "producer.goodbye", "last_n": 2})])
    await harness.run()
    # A partially consumed file that vanished (removed by hand) leaves a row that nothing will finish.
    async with trace_session() as db:
        db.add(TrajectoryIngestFile(producer_id=writer.producer_id, file_name=spool.file_name(9), bytes_consumed=10,
                                    lines_consumed=1, done=False, updated_at=datetime.now(timezone.utc)))
    await harness.run()
    assert not writer.directory.exists()
    assert await rows(TrajectoryIngestFile) == []


async def test_a_crash_after_file_deletion_leaves_no_orphan_rows(harness, monkeypatch):
    path = harness.writer.events(event(), event())
    forget = IngestService._forget_files

    async def crashed(self, keys):
        return None  # the process died after unlinking the file, before its row was deleted

    monkeypatch.setattr(IngestService, "_forget_files", crashed)
    await harness.run()
    [row] = await rows(TrajectoryIngestFile)
    assert row.done and not path.exists()
    monkeypatch.setattr(IngestService, "_forget_files", forget)
    harness.configure()
    result = await harness.run()
    assert result["lines"] == 0 and await rows(TrajectoryIngestFile) == []
    assert len((await events_of("ses_1"))[1]) == 3


async def test_a_failing_batch_backs_off_while_other_producers_continue(harness, monkeypatch):
    """An unexpected error in one file's batch (a tombstone purge that times out, a row the database rejects)
    must not end the pass: the other producers' files are ingested and the failing file retries from its
    committed offset after a backoff."""
    harness.writer.events(event(event_id="first"))
    await harness.run()
    tombstone = harness.retention.tombstone
    failures = {"left": 1}

    async def flaky_tombstone(db, trajectory, *, reason):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("canceling statement due to statement timeout")
        await tombstone(db, trajectory, reason=reason)

    monkeypatch.setattr(harness.retention, "tombstone", flaky_tombstone)
    failing = harness.writer.controls({"type": "session.deleted", "session_id": "ses_1", "user_id": "u1",
                                       "deleted_at": "2026-09-14T09:00:00.000Z"}, age=30)
    other = SpoolWriter(harness.settings.spool_dir, "20260914080003-other-4-eeeeeeee")
    other.events(event(session="ses_2", event_id="other"), age=10)
    result = await harness.run()
    assert result["failed_batches"] == 1 and result["events"] == 1
    assert [row.event_id for row in (await events_of("ses_2"))[1][1:]] == ["other"]
    assert failing.exists() and (await events_of("ses_1"))[0].deleted_at is None
    # The failed file waits for its backoff instead of failing on every pass.
    result = await harness.run()
    assert result["failed_batches"] == 0 and result["deferred_batches"] == 1 and failing.exists()
    _release(harness.service)
    result = await harness.run()
    assert result["failed_batches"] == 0 and not failing.exists()
    assert (await events_of("ses_1"))[0].deleted_at is not None and harness.service._failures == {}


async def test_a_poison_batch_is_quarantined_after_repeated_failures_and_the_producer_goes_on(harness, monkeypatch):
    """TRAJECTORY_INGEST_MAX_BATCH_FAILURES failures in a row while the trace database answers: the file is
    quarantined like one with an unparsable line (reason batch_failed), and the producer's later files proceed.
    Every failed attempt counts failed_batches in the metrics registry."""
    registry = Metrics()
    harness.metrics = registry
    harness.configure(ingest_max_batch_failures=3)
    writer = harness.writer
    writer.events(event(event_id="first", run_id="run_p"))
    await harness.run()

    async def poisoned(db, trajectory, *, reason):
        raise RuntimeError("value out of range for type integer")

    monkeypatch.setattr(harness.retention, "tombstone", poisoned)
    poison = writer.controls(DELETED, age=30)
    later = writer.events(event(event_id="later"), age=20)
    for attempt in (1, 2):
        result = await harness.run()
        assert (result["failed_batches"], result["quarantined_files"], result["events"]) == (1, 0, 0)
        assert poison.exists() and later.exists()  # the later file waits behind the failing one
        assert registry.snapshot()["counters"]["failed_batches"] == attempt
        _release(harness.service)
    result = await harness.run()
    assert (result["failed_batches"], result["quarantined_files"], result["events"]) == (1, 1, 1)
    assert not poison.exists() and not later.exists()
    target = harness.settings.spool_dir / "quarantine" / f"{writer.producer_id}__{poison.name}"
    reason = spool_reader.read_producer_document(target.with_name(target.name + ".reason"))
    assert reason["reason"] == "batch_failed" and reason["error"] == "RuntimeError: value out of range for type integer"
    assert (reason["offset"], reason["first_n"], reason["last_n"]) == (0, 2, 2)
    _, stored = await events_of("ses_1")
    assert [row.event_id for row in stored[1:]] == ["first", f"gap:{writer.producer_id}:2-2:ses_1:run_p", "later"]
    assert stored[2].data == {"phase": "lost", "reason": "producer_lines_lost", "producer_id": writer.producer_id,
                              "from_n": 2, "to_n": 2}
    counters = registry.snapshot()["counters"]
    assert (counters["failed_batches"], counters["quarantined_files"]) == (3, 1)
    assert harness.service._failures == {} and await rows(TrajectoryIngestFile) == []


async def test_failures_while_the_trace_database_does_not_answer_never_quarantine(harness, monkeypatch):
    harness.configure(ingest_max_batch_failures=2)
    harness.writer.events(event(event_id="first"))
    await harness.run()

    async def unexplained(db, trajectory, *, reason):
        # Not a failure ingest recognizes as transient: only the probe tells that the database is out.
        raise RuntimeError("the statement failed")

    async def unreachable(timeout=ingest_module.DB_PROBE_SECONDS):
        return False

    probe = ingest_module.trace_db_available
    monkeypatch.setattr(harness.retention, "tombstone", unexplained)
    monkeypatch.setattr(ingest_module, "trace_db_available", unreachable)
    failing = harness.writer.controls(DELETED, age=30)
    for _ in range(5):
        result = await harness.run()
        assert (result["failed_batches"], result["quarantined_files"]) == (1, 0)
        _release(harness.service)
    [failure] = harness.service._failures.values()
    assert (failure.attempts, failure.failures) == (5, 0) and failing.exists()
    assert not (harness.settings.spool_dir / "quarantine").exists()
    # Once the database answers again, failures of the batch count.
    monkeypatch.setattr(ingest_module, "trace_db_available", probe)
    assert (await harness.run())["quarantined_files"] == 0
    _release(harness.service)
    assert (await harness.run())["quarantined_files"] == 1 and not failing.exists()


async def test_the_trace_database_probe_reports_errors_and_timeouts_as_unavailable(trace_db, monkeypatch):
    assert await ingest_module.trace_db_available() is True

    @asynccontextmanager
    async def hanging():
        await asyncio.sleep(10)
        yield None

    monkeypatch.setattr(ingest_module, "trace_session", hanging)
    assert await ingest_module.trace_db_available(timeout=0.05) is False
    monkeypatch.undo()
    await close_trace_engine()
    assert await ingest_module.trace_db_available() is False


async def test_out_of_range_times_are_invalid_values_not_fatal_errors(harness):
    harness.writer.events(event(event_id="late", occurred_at="9999-12-31T23:00:00-05:00"), event(event_id="kept"))
    harness.writer.controls({"type": "session.meta", "session": {"id": "ses_1", "user_id": "u1",
                                                                 "updated_at": "0001-01-01T00:30:00+01:00"}})
    result = await harness.run()
    assert (result["invalid_events"], result["events"], result["failed_batches"]) == (1, 1, 0)
    assert [row.event_id for row in (await events_of("ses_1"))[1][1:]] == ["kept"]


async def test_a_backlog_of_thousands_of_small_files_drains_in_one_pass_with_linear_selection(harness, monkeypatch):
    """A worker outage leaves one file per second of activity: the pass must follow each producer from file to file,
    not select afresh over the consumed prefix after each one (quadratic in the files), and delete the bookkeeping
    rows of all of them in one transaction at its end, not one per file."""
    writer = harness.writer
    files = 3000
    for index in range(files):
        writer.file([writer.line("event", event(event_id=f"e{index}"))] if index % 100 == 0 else [])
    rounds, examined, forgets = [], [], []
    ready, name, forget = spool_reader.ReadyCursor.ready, spool_reader.SpoolFile.name, IngestService._forget_files

    def counting_ready(cursor):
        items = ready(cursor)
        rounds.append(len(items))
        return items

    async def counting_forget(service, keys):
        keys = list(keys)
        forgets.append(len(keys))
        await forget(service, keys)

    monkeypatch.setattr(spool_reader.ReadyCursor, "ready", counting_ready)
    monkeypatch.setattr(spool_reader.SpoolFile, "name", property(lambda item: examined.append(1) or name.fget(item)))
    monkeypatch.setattr(IngestService, "_forget_files", counting_forget)
    result = await harness.run()
    assert (result["files_done"], result["events"]) == (files, files // 100)
    assert not list(writer.directory.glob("*.jsonl")) and await rows(TrajectoryIngestFile) == []
    assert len((await events_of("ses_1"))[1]) == files // 100 + 1
    # One producer: one file per round, each selected once, and the empty round that ends the pass.
    assert sum(rounds) == files and len(rounds) == files + 1
    # About nine file name lookups per file; selecting afresh after each finished file made 4.5 million here.
    assert len(examined) < 20 * files
    assert forgets == [files]


async def test_max_lines_limits_one_pass(harness):
    harness.writer.events(*[event() for _ in range(5)])
    assert (await harness.run(max_lines=3))["lines"] == 3
    assert harness.service.last_lag_seconds >= 0 and list(harness.writer.directory.glob("*.jsonl"))
    assert (await harness.run())["lines"] == 2
    assert len(await rows(TrajectoryEvent)) == 6
