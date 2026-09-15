"""Emitter writer thread: rotation, spool budget, write errors, timeouts, restarts."""
import errno
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import orjson
import pytest

from trajectory import spool
from trajectory.emitter import Emitter

KIB = 1024


@pytest.fixture
def make_emitter(tmp_path):
    created = []

    def factory(root=None, **overrides):
        settings = {"queue_bytes": 8 * 1024 * KIB, "max_event_bytes": 1024 * KIB, "file_bytes": 8 * 1024 * KIB,
                    "file_ms": 60_000, "spool_max_bytes": 1 << 40}
        settings.update(overrides)
        emitter = Emitter(root or tmp_path / "spool", **settings)
        emitter.RETRY_SECONDS = 0.05
        created.append(emitter)
        return emitter

    yield factory
    for emitter in created:
        emitter.close(2.0)


def event(index: int, pad: int = 200) -> bytes:
    return orjson.dumps({"type": "request.delta", "index": index, "pad": "x" * pad})


def emit(emitter, index, pad=200):
    return emitter.emit_bytes(event(index, pad), user_id="user", session_id="root", run_id="run",
                              request_id=f"req{index}")


def data_files(emitter, *, closed_only=True):
    names = [name for name in os.listdir(emitter.producer_dir) if spool.parse_file_name(name)]
    return sorted(name for name in names if not closed_only or name.endswith(spool.CLOSED_SUFFIX))


def records(emitter):
    return [spool.decode_line(line) for name in data_files(emitter)
            for line in (emitter.producer_dir / name).read_bytes().splitlines()]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def start_idle(emitter):
    """Start the writer and wait until it sleeps: the next batch is then exactly what a flush finds queued."""
    emitter.start()

    def idle():
        with emitter._lock:
            return emitter._waiting

    assert wait_for(idle)


def age(path) -> float:
    return time.time() - os.stat(path).st_mtime


def backdate(path) -> None:
    past = time.time() - 600
    os.utime(path, (past, past))


def test_the_heartbeat_keeps_producer_json_fresh_while_the_writer_is_stalled_and_stops_on_close(make_emitter):
    """The worker declares a producer whose producer.json stays unrefreshed for TRAJECTORY_SPOOL_ABANDON_SECONDS
    dead, whatever host it ran on, and deletes its open .part: a writer stuck in a write must not look dead."""
    emitter = make_emitter()
    emitter.HEARTBEAT_SECONDS = 0.02
    release, entered = threading.Event(), threading.Event()

    def stalled_write(descriptor, data):
        entered.set()
        release.wait(10)
        return os.write(descriptor, data)

    emitter._write = stalled_write
    emitter.start()
    beat = emitter._heartbeat_thread
    assert beat.daemon and beat.name == f"trajectory-emitter-heartbeat-{os.getpid()}"
    document = emitter.producer_dir / spool.PRODUCER_FILE
    try:
        assert emit(emitter, 0)
        assert emitter.flush(0.05) is False
        assert entered.wait(5)
        for _ in range(3):
            backdate(document)
            assert wait_for(lambda: age(document) < 60)
        assert not release.is_set() and emitter._thread.is_alive()
    finally:
        release.set()
    emitter.close(5)
    assert emitter.stats()["state"] == "closed" and not beat.is_alive()
    assert records(emitter)[-1]["control"]["type"] == "producer.goodbye"
    backdate(document)
    time.sleep(0.1)
    assert age(document) > 500


def test_the_heartbeat_leaves_a_missing_producer_json_to_the_writer_and_notes_other_errors(make_emitter, tmp_path):
    emitter = make_emitter()
    emitter.HEARTBEAT_SECONDS = 0.02
    start_idle(emitter)
    document = emitter.producer_dir / spool.PRODUCER_FILE
    document.unlink()
    time.sleep(0.1)
    assert not document.exists() and emitter._heartbeat_thread.is_alive()
    assert emitter.stats()["last_error"] is None
    assert emit(emitter, 1) and emitter.flush(5)
    assert [record["n"] for record in records(emitter)] == [1]

    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the producer directory should be")
    other = make_emitter()
    other.producer_dir = blocker / "producer"
    other._heartbeat()
    assert other.stats()["last_error"] == "NotADirectoryError:ENOTDIR"


def test_a_fork_child_never_starts_the_heartbeat(make_emitter):
    """A fork child inherits the running emitter without its threads, marked forked: heartbeating the parent's
    producer from there would keep it alive for the worker after the parent is gone."""
    emitter = make_emitter()
    emitter.ALIVE_CHECK_SECONDS = 0.0
    ended = threading.Thread(target=int)
    ended.start()
    ended.join()
    emitter._state, emitter._thread, emitter._heartbeat_thread, emitter._forked = "running", ended, ended, True
    emitter.start()
    assert emit(emitter, 0)
    assert emitter.flush(0.05) is False
    with emitter._lock:
        emitter._start_heartbeat_locked()
    emitter.close(0.05)
    assert (emitter._thread, emitter._heartbeat_thread) == (ended, ended)
    assert emitter.stats()["writer_restarts"] == 0 and not emitter.producer_dir.exists()


def test_size_rotation_fsyncs_then_renames_complete_files(make_emitter):
    emitter = make_emitter(file_bytes=2 * KIB)
    calls = []

    def fsync(descriptor):
        calls.append("fsync")
        os.fsync(descriptor)

    def rename(source, target):
        calls.append(("rename", Path(source).name, Path(target).name))
        os.replace(source, target)

    emitter._fsync, emitter._rename = fsync, rename
    emitter.start()
    for index in range(20):
        assert emit(emitter, index)
    assert emitter.flush(5)
    names = data_files(emitter)
    assert len(names) >= 3 and names == [spool.file_name(number) for number in range(1, len(names) + 1)]
    assert data_files(emitter, closed_only=False) == names
    renames = [call for call in calls if call != "fsync"]
    assert renames == [("rename", spool.file_name(number, closed=False), spool.file_name(number))
                       for number in range(1, len(names) + 1)]
    assert all(calls[calls.index(call) - 1] == "fsync" for call in renames)
    for name in names[:-1]:
        content = (emitter.producer_dir / name).read_bytes()
        last_line = content.splitlines(keepends=True)[-1]
        assert len(content) >= 2 * KIB > len(content) - len(last_line)
    assert [record["n"] for record in records(emitter)] == list(range(1, 21))
    assert [record["event"]["index"] for record in records(emitter)] == list(range(20))
    assert emitter.stats()["files_closed"] == len(names)


def test_age_rotation_closes_idle_files_without_a_flush(make_emitter):
    emitter = make_emitter(file_ms=50)
    emitter.start()
    assert emit(emitter, 0)
    assert wait_for(lambda: data_files(emitter, closed_only=False) == [spool.file_name(1)], timeout=3)
    assert emit(emitter, 1)
    assert wait_for(lambda: data_files(emitter, closed_only=False) == [spool.file_name(1), spool.file_name(2)],
                    timeout=3)
    assert [record["n"] for record in records(emitter)] == [1, 2]


def test_flush_writes_everything_enqueued_and_rotates_the_open_file(make_emitter):
    emitter = make_emitter()
    emitter.WAIT_SECONDS = 10
    emitter.start()
    for index in range(5):
        assert emit(emitter, index)
    assert emitter.flush(5)
    assert data_files(emitter, closed_only=False) == [spool.file_name(1)]
    assert [record["event"]["index"] for record in records(emitter)] == list(range(5))
    assert emitter.flush(5)
    assert data_files(emitter, closed_only=False) == [spool.file_name(1)]
    stats = emitter.stats()
    assert (stats["queued_lines"], stats["queued_bytes"], stats["written_lines"]) == (0, 0, 5)


def test_spool_budget_drops_new_lines_until_usage_falls_then_reports_the_gap(make_emitter, tmp_path):
    other = tmp_path / "spool" / spool.PRODUCERS_DIR / "20260101000000-other-1-00000000"
    other.mkdir(parents=True)
    (other / spool.file_name(1)).write_bytes(b"x" * 40 * KIB)
    emitter = make_emitter(spool_max_bytes=48 * KIB)
    emitter.WAIT_SECONDS = 10
    # Usage is sampled on every writer cycle, so no cycle works from a stale sample.
    emitter.SPOOL_SAMPLE_SECONDS = 0
    start_idle(emitter)
    size = len(event(0, pad=900))
    for index in range(20):
        assert emit(emitter, index, pad=900)
    assert emitter.flush(5) is False
    written = [record for record in records(emitter) if record["k"] == "event"]
    dropped = emitter.stats()["dropped_by_reason"]["spool_full"]
    assert 0 < len(written) < 20 and len(written) + dropped == 20
    # Event lines stop at the cap; the gap line may use the reserve of writer-owned lines.
    assert spool.spool_usage_bytes(tmp_path / "spool") <= 48 * KIB + Emitter.CONTROL_RESERVE_BYTES
    (other / spool.file_name(1)).unlink()
    # Every cycle samples the usage, so the flush's cycle sees the smaller spool.
    assert emitter.flush(5)
    assert emitter.stats()["spool_bytes"] < 20 * KIB
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emit(emitter, 100, pad=900)
    assert emitter.flush(5)
    everything = records(emitter)
    gaps = [record["control"] for record in everything if record["k"] == "control"]
    dropped_bytes = sum(len(event(index, pad=900)) for index in range(len(written), 20))
    assert [(gap["reason"], gap["dropped_events"], gap["dropped_bytes"]) for gap in gaps] == [
        ("spool_full", dropped, dropped_bytes)]
    assert gaps[0]["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"],
                                    "request_ids": [f"req{index}" for index in range(len(written), 20)]}]
    assert [record["n"] for record in everything] == list(range(1, len(everything) + 1))


def test_producers_are_rescanned_every_sample_and_blobs_and_quarantine_every_shared_sample(make_emitter, tmp_path):
    root = tmp_path / "spool"
    emitter = make_emitter()
    emitter.WAIT_SECONDS = 10
    emitter.SPOOL_SAMPLE_SECONDS, emitter.SHARED_SAMPLE_SECONDS = 0, 3600
    start_idle(emitter)
    assert emit(emitter, 0) and emitter.flush(5)
    other = root / spool.PRODUCERS_DIR / "20260101000000-other-1-00000000"
    other.mkdir()
    (other / spool.file_name(1)).write_bytes(b"x" * 256 * KIB)
    spool.ensure_private_dir(spool.blobs_dir(root))
    (spool.blobs_dir(root) / ("b" * 64)).write_bytes(b"x" * 256 * KIB)
    (root / spool.QUARANTINE_DIR).mkdir()
    (root / spool.QUARANTINE_DIR / f"other__{spool.file_name(1)}").write_bytes(b"x" * 256 * KIB)
    assert emit(emitter, 1) and emitter.flush(5)
    # The next cycle sees the other producer's file; blobs/ and quarantine/ keep their first sample.
    assert 256 * KIB <= emitter.stats()["spool_bytes"] < 512 * KIB
    emitter._shared_sampled_at = float("-inf")
    assert emit(emitter, 2) and emitter.flush(5)
    assert emitter.stats()["spool_bytes"] >= 768 * KIB


def test_usage_and_free_space_are_sampled_ten_times_as_often_near_the_spool_cap(make_emitter, monkeypatch):
    """Processes sharing a spool each check the cap against their own sample, so near it they could pass together."""
    emitter = make_emitter(spool_max_bytes=10_000)
    usage, scans, paths = {"producers": 0}, [], []
    monkeypatch.setattr(spool, "shared_usage_bytes", lambda root: scans.append("shared") or 0)
    monkeypatch.setattr(spool, "producer_usage_bytes", lambda root: scans.append("producers") or usage["producers"])

    def statvfs(path):
        scans.append("statvfs")
        paths.append(path)
        return SimpleNamespace(f_bavail=1 << 20, f_frsize=4096)

    emitter._statvfs = statvfs

    def cycle(now):
        scans.clear()
        emitter._maintain(now)
        return scans[:]

    both, producers = ["shared", "producers", "statvfs"], ["producers", "statvfs"]
    assert (cycle(1000.0), cycle(1004.9), cycle(1005.0)) == (both, [], producers)
    usage["producers"] = 8_999
    assert (cycle(1010.0), cycle(1014.9)) == (producers, [])
    # 90 % of the cap: producers/ every 0.5 s, blobs/ and quarantine/ every 5 s.
    usage["producers"] = 9_000
    assert (cycle(1015.0), cycle(1015.4), cycle(1015.5), cycle(1016.0)) == (producers, [], both, producers)
    assert (cycle(1020.0), cycle(1020.5)) == (producers, both)
    # Back below 90 %: every 5 s again, until this writer's own writes since the last rescan reach it.
    usage["producers"] = 100
    assert (cycle(1021.0), cycle(1021.5)) == (producers, [])
    emitter._spool_estimate += 9_000
    assert (cycle(1022.0), cycle(1022.5)) == (producers, [])
    assert paths and set(paths) == {emitter.spool_dir}


def test_the_disk_floor_drops_events_and_leaves_writer_lines_half_of_it(make_emitter):
    """The floor protects the host disk even when spool_max_bytes is misconfigured or other data fills it."""
    free = {"bytes": 48 * KIB}
    emitter = make_emitter(spool_min_free_bytes=64 * KIB)
    emitter.WAIT_SECONDS = 10
    # Every cycle samples; free space is f_bavail blocks of f_frsize bytes (not f_bfree, not f_bsize).
    emitter.SPOOL_SAMPLE_SECONDS = 0
    emitter._statvfs = lambda path: SimpleNamespace(f_bavail=free["bytes"] // 512, f_frsize=512,
                                                    f_bfree=1 << 30, f_bsize=1 << 20)
    start_idle(emitter)
    for index in range(3):
        assert emit(emitter, index)
    assert emitter.flush(5) is False
    free["bytes"] = 1 << 30
    assert emit(emitter, 3) and emitter.flush(5)
    free["bytes"] = 48 * KIB
    assert emit(emitter, 4)
    # Between half the floor and the floor, gap lines and the goodbye are still written.
    emitter.close(5)
    everything = records(emitter)
    assert [record["n"] for record in everything] == [1, 2, 3, 4]
    assert [record["event"]["index"] for record in everything if record["k"] == "event"] == [3]
    controls = [record["control"] for record in everything if record["k"] == "control"]
    assert [(control["type"], control.get("reason"), control.get("dropped_events")) for control in controls] == [
        ("gap", "disk_full", 3), ("gap", "disk_full", 1), ("producer.goodbye", None, None)]
    assert controls[0]["dropped_bytes"] == 3 * len(event(0))
    assert controls[0]["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"],
                                        "request_ids": ["req0", "req1", "req2"]}]
    stats = emitter.stats()
    assert stats["dropped_by_reason"] == {"disk_full": 4} and stats["writer_lines_skipped"] == 0

    # Below half the floor a writer-owned line is skipped, and counted.
    free["bytes"] = 16 * KIB
    second = make_emitter(spool_min_free_bytes=64 * KIB)
    second._statvfs = emitter._statvfs
    start_idle(second)
    second.close(5)
    stats = second.stats()
    assert (stats["state"], stats["writer_lines_skipped"]) == ("closed", 1)
    assert data_files(second, closed_only=False) == []


def test_a_failed_free_space_sample_sets_no_floor(make_emitter):
    emitter = make_emitter(spool_min_free_bytes=64 * KIB)
    emitter.WAIT_SECONDS = 10
    emitter.SPOOL_SAMPLE_SECONDS = 0
    failing = threading.Event()

    def statvfs(path):
        if failing.is_set():
            raise OSError(errno.EIO, "Input/output error")
        return SimpleNamespace(f_bavail=1, f_frsize=4 * KIB)

    emitter._statvfs = statvfs
    start_idle(emitter)
    assert emit(emitter, 0)
    assert emitter.flush(5) is False
    failing.set()
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emit(emitter, 1) and emitter.flush(5)
    stats = emitter.stats()
    assert stats["last_error"] == "OSError:EIO" and stats["dropped_by_reason"] == {"disk_full": 1}
    everything = records(emitter)
    assert [record["n"] for record in everything] == [1, 2]
    assert [record["event"]["index"] for record in everything if record["k"] == "event"] == [1]
    [gap] = [record["control"] for record in everything if record["k"] == "control"]
    assert (gap["reason"], gap["dropped_events"]) == ("disk_full", 1)


def test_goodbye_and_gap_lines_fit_above_the_spool_cap_within_the_control_reserve(make_emitter, tmp_path):
    """Without its goodbye, a producer restarted on a full spool is reported as crashed for every recent session."""
    other = tmp_path / "spool" / spool.PRODUCERS_DIR / "20260101000000-other-1-00000000"
    other.mkdir(parents=True)
    (other / spool.file_name(1)).write_bytes(b"x" * 64 * KIB)
    emitter = make_emitter(spool_max_bytes=64 * KIB)
    emitter.WAIT_SECONDS = 10
    start_idle(emitter)
    assert emit(emitter, 0)
    assert emitter.flush(5) is False
    emitter.close(5)
    everything = records(emitter)
    assert [(record["k"], record["control"]["type"]) for record in everything] == [
        ("control", "gap"), ("control", "producer.goodbye")]
    assert everything[0]["control"]["reason"] == "spool_full"
    stats = emitter.stats()
    assert stats["dropped_by_reason"] == {"spool_full": 1} and stats["writer_lines_skipped"] == 0

    # Beyond the reserve a writer-owned line is skipped, and counted.
    (other / spool.file_name(2)).write_bytes(b"x" * Emitter.CONTROL_RESERVE_BYTES)
    second = make_emitter(spool_max_bytes=64 * KIB)
    start_idle(second)
    second.close(5)
    stats = second.stats()
    assert (stats["state"], stats["writer_lines_skipped"]) == ("closed", 1)
    assert data_files(second, closed_only=False) == []


def test_lines_for_an_open_file_the_worker_deleted_go_to_the_next_file(make_emitter):
    """A writer stalled past TRAJECTORY_SPOOL_ABANDON_SECONDS finds its .part consumed and deleted."""
    emitter = make_emitter()
    start_idle(emitter)

    def written(counter):
        path = emitter.producer_dir / spool.file_name(counter, closed=False)
        assert wait_for(lambda: path.exists() and path.stat().st_size > 0)
        return path

    # Deleted before its rotation: the rename finds nothing and the file does not count as closed.
    assert emit(emitter, 0)
    written(1).unlink()
    assert emitter.flush(5)
    stats = emitter.stats()
    assert (stats["files_lost"], stats["files_closed"]) == (1, 0)
    # Deleted before more lines reach it: they go to the next file, keeping their counters.
    assert emit(emitter, 1)
    written(2).unlink()
    assert emit(emitter, 2)
    assert emitter.flush(5)
    assert data_files(emitter, closed_only=False) == [spool.file_name(3)]
    assert [(record["n"], record["event"]["index"]) for record in records(emitter)] == [(3, 2)]
    stats = emitter.stats()
    assert (stats["files_lost"], stats["files_closed"], stats["written_lines"], stats["dropped_events"]) == (
        2, 1, 3, 0)


def test_enospc_drops_affected_lines_keeps_complete_ones_and_recovers(make_emitter):
    emitter = make_emitter()
    emitter.WAIT_SECONDS = 10
    size = len(event(0))
    calls = []

    def failing_write(descriptor, data):
        calls.append(len(data))
        if len(calls) == 1:
            # The first line and part of the second reach the file.
            return os.write(descriptor, bytes(data[:bytes(data).index(b"\n") + 41]))
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return os.write(descriptor, data)

    emitter._write = failing_write
    emitter.start()
    for index in range(4):
        assert emit(emitter, index)
    assert emitter.flush(5) is False
    first = (emitter.producer_dir / spool.file_name(1)).read_bytes()
    assert first.endswith(b"\n") and [spool.decode_line(line)["n"] for line in first.splitlines()] == [1]
    stats = emitter.stats()
    assert stats["write_errors"] == 1 and stats["last_error"] == "OSError:ENOSPC"
    assert stats["dropped_by_reason"] == {"writer_error": 3} and stats["queued_bytes"] == 0
    for index in (4, 5):
        assert emit(emitter, index)
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, len(everything) + 1))
    assert [record["event"]["index"] for record in everything if record["k"] == "event"] == [0, 4, 5]
    gaps = [record["control"] for record in everything if record["k"] == "control"]
    assert [(gap["reason"], gap["dropped_events"], gap["dropped_bytes"]) for gap in gaps] == [
        ("writer_error", 3, 3 * size)]
    assert gaps[0]["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"],
                                    "request_ids": ["req1", "req2", "req3"]}]
    assert not [name for name in os.listdir(emitter.producer_dir) if name.endswith(".part")]


def test_concurrent_emits_under_write_faults_keep_counters_contiguous_and_account_every_line(make_emitter):
    emitter = make_emitter(file_bytes=16 * KIB, queue_bytes=256 * KIB)
    calls = {"count": 0}

    def faulty_write(descriptor, data):
        # Only the writer thread writes. Every 5th call is short, every 7th fails.
        calls["count"] += 1
        if calls["count"] % 7 == 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        if calls["count"] % 5 == 0 and len(data) > 1:
            return os.write(descriptor, bytes(data[:len(data) // 2]))
        return os.write(descriptor, data)

    emitter._write = faulty_write
    emitter.start()
    accepted = {}

    def producer(name):
        count = 0
        for sequence in range(300):
            payload = orjson.dumps({"producer": name, "sequence": sequence, "pad": "x" * (sequence % 300)})
            count += emitter.emit_bytes(payload, user_id="user", session_id=name, request_id=f"{name}:{sequence}")
            if sequence % 25 == 0:
                time.sleep(0.001)
        accepted[name] = count

    threads = [threading.Thread(target=producer, args=(f"p{index}",)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    emitter._write = os.write
    time.sleep(emitter.RETRY_SECONDS * 2)
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    emitter.close(5)
    stats = emitter.stats()
    assert stats["state"] == "closed" and stats["write_errors"] > 0 and stats["queued_bytes"] == 0
    assert not [name for name in os.listdir(emitter.producer_dir) if name.endswith(".part")]
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, len(everything) + 1))
    assert everything[-1]["control"] == {"type": "producer.goodbye", "last_n": len(everything)}
    events = [record["event"] for record in everything if record["k"] == "event"]
    gaps = [record["control"] for record in everything if record["k"] == "control" and record["control"]["type"] == "gap"]
    # Every accepted line is either in the spool or counted as a writer_error drop; gaps carry every drop.
    assert len(events) + stats["dropped_by_reason"].get("writer_error", 0) == sum(accepted.values())
    assert {reason: sum(gap["dropped_events"] for gap in gaps if gap["reason"] == reason)
            for reason in stats["dropped_by_reason"]} == stats["dropped_by_reason"]
    for name in accepted:
        sequences = [event["sequence"] for event in events if event["producer"] == name]
        assert sequences == sorted(set(sequences))


def test_torn_tail_that_cannot_be_truncated_stays_in_an_abandoned_part_file(make_emitter):
    emitter = make_emitter()
    emitter.WAIT_SECONDS = 10
    calls = []

    def failing_write(descriptor, data):
        calls.append(len(data))
        if len(calls) == 1:
            return os.write(descriptor, bytes(data[:len(data) // 2 + 10]))
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return os.write(descriptor, data)

    def failing_truncate(descriptor, length):
        raise OSError(errno.EIO, "Input/output error")

    emitter._write, emitter._truncate = failing_write, failing_truncate
    start_idle(emitter)
    for index in range(4):
        assert emit(emitter, index)
    assert emitter.flush(5) is False
    torn = (emitter.producer_dir / spool.file_name(1, closed=False)).read_bytes()
    assert not torn.endswith(b"\n")
    assert [spool.decode_line(line)["n"] for line in torn.splitlines()[:-1]] == [1, 2]
    assert emit(emitter, 4)
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    # Lost counters are not reused when the torn tail stays on disk.
    assert data_files(emitter) == [spool.file_name(2)]
    assert sorted(record["n"] for record in records(emitter)) == [5, 6]
    assert emitter.stats()["dropped_by_reason"] == {"writer_error": 2}


def test_flush_and_close_time_out_while_the_writer_is_stalled_and_emits_stay_fast(make_emitter):
    emitter = make_emitter()
    release, entered = threading.Event(), threading.Event()

    def stalled_write(descriptor, data):
        entered.set()
        release.wait(10)
        return os.write(descriptor, data)

    emitter._write = stalled_write
    emitter.start()
    try:
        assert emit(emitter, 0)
        started = time.monotonic()
        assert emitter.flush(0.2) is False
        assert time.monotonic() - started < 2.0
        assert entered.wait(5)
        started = time.monotonic()
        for index in range(1, 501):
            assert emit(emitter, index)
        assert time.monotonic() - started < 1.0
        started = time.monotonic()
        emitter.close(0.3)
        assert time.monotonic() - started < 2.0
        assert emitter.stats()["state"] == "closing"
        assert emit(emitter, 501) is False
        assert emitter.stats()["rejected_after_close"] == 1
    finally:
        release.set()
    assert wait_for(lambda: emitter.stats()["state"] == "closed")
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, 503))
    assert everything[-1]["control"] == {"type": "producer.goodbye", "last_n": 502}


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_dead_writer_thread_is_restarted_without_losing_queued_events(make_emitter):
    emitter = make_emitter()
    emitter.ALIVE_CHECK_SECONDS = 0.0
    emitter.start()
    thread = emitter._thread

    def die():
        raise SystemExit

    emitter._iterate = die
    assert wait_for(lambda: not thread.is_alive())
    del emitter._iterate
    for index in range(3):
        assert emit(emitter, index)
    assert emitter.flush(5)
    assert [(record["n"], record["event"]["index"]) for record in records(emitter)] == [(1, 0), (2, 1), (3, 2)]
    stats = emitter.stats()
    assert stats["writer_restarts"] == 1 and stats["writer_alive"]


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_emits_never_raise_when_a_dead_writer_cannot_be_restarted(make_emitter):
    emitter = make_emitter(queue_bytes=4 * KIB)
    emitter.ALIVE_CHECK_SECONDS = 0.0
    emitter.start()
    thread = emitter._thread

    def die():
        raise SystemExit

    def cannot_start():
        raise RuntimeError("can't start new thread")

    emitter._iterate = die
    assert wait_for(lambda: not thread.is_alive())
    emitter._start_thread_locked = cannot_start
    results = [emit(emitter, index) for index in range(40)]
    assert results.count(True) == 4 * KIB // len(event(0))
    assert emitter.flush(0.2) is False
    emitter.close(0.2)
    stats = emitter.stats()
    assert stats["dropped_by_reason"] == {"queue_overflow": results.count(False)}
    assert stats["last_error"] == "RuntimeError" and not stats["writer_alive"]


def test_unwritable_spool_directory_drops_lines_and_recovers_when_writable(make_emitter, tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_text("a file where the spool directory should be")
    emitter = make_emitter(root=blocker / "spool")
    emitter.WAIT_SECONDS = 10
    emitter.start()
    assert emitter.stats()["last_error"] in {"FileExistsError:EEXIST", "NotADirectoryError:ENOTDIR"}
    for index in range(3):
        assert emit(emitter, index)
    assert emitter.flush(2) is False
    assert emitter.stats()["dropped_by_reason"] == {"writer_error": 3}
    blocker.unlink()
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emit(emitter, 3)
    assert emitter.flush(5)
    assert (emitter.producer_dir / spool.PRODUCER_FILE).exists()
    everything = records(emitter)
    assert [record["n"] for record in everything] == [1, 2]
    assert everything[0]["control"]["reason"] == "writer_error"
    assert everything[0]["control"]["dropped_events"] == 3
    assert everything[1]["event"]["index"] == 3
