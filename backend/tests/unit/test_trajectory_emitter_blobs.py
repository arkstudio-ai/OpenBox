"""Spool v2 blob values: what the emitter writer moves to ``blobs/`` and how the worker reads it back (SPEC §3.5)."""
import errno
import hashlib
import os
import time
from pathlib import Path

import orjson

from tests.unit.test_trajectory_emitter_writer import (  # noqa: F401
    KIB,
    data_files,
    make_emitter,
    records,
    start_idle,
    wait_for,
)
from trajectory import emitter as emitter_module
from trajectory import spool
from trajectory.worker.spool_reader import read_batch

MIN_BYTES = 256
VALUE_BYTES = spool.BLOB_VALUE_BYTES


def text(size: int, char: str = "x") -> str:
    """An ASCII string whose compact JSON is exactly ``size`` bytes."""
    return char * (size - 2)


def sha(value) -> str:
    return hashlib.sha256(orjson.dumps(value)).hexdigest()


def ref(value) -> dict:
    return {spool.BLOB_KEY: sha(value)}


def event_bytes(event_type: str, data: dict, index: int = 0) -> bytes:
    return orjson.dumps({"type": event_type, "event_id": f"evt_{index}", "user_id": "user", "session_id": "root",
                         "run_id": "run", "request_id": f"req{index}", "data": data})


def emit(emitter, payload: bytes, index: int = 0) -> bool:
    return emitter.emit_bytes(payload, user_id="user", session_id="root", run_id="run", request_id=f"req{index}")


def blob_files(emitter) -> dict[str, bytes]:
    """Every file in ``blobs/`` (temporary files included) with its content."""
    try:
        names = os.listdir(emitter.blob_dir)
    except FileNotFoundError:
        return {}
    return {name: (emitter.blob_dir / name).read_bytes() for name in names}


def wait_idle(emitter) -> None:
    """The writer sleeps with nothing queued, so nothing touches its blob state until the next emit."""
    def idle():
        with emitter._lock:
            return emitter._waiting and not emitter._queue
    assert wait_for(idle)


def test_request_prepared_inputs_over_the_minimum_move_to_blobs_and_other_inputs_stay_inline(make_emitter):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)
    start_idle(emitter)
    system, instructions = text(MIN_BYTES + 1, "s"), text(MIN_BYTES + 1, "i")
    tools = [{"name": "bash", "description": text(MIN_BYTES, "d")}]
    at_minimum = text(MIN_BYTES, "m")
    message = {"role": "user", "content": text(MIN_BYTES, "c")}
    listed_input = {"role": "user", "content": text(2 * MIN_BYTES, "q")}
    inputs = {"model": "provider/model", "system": system, "instructions": instructions, "tools": tools,
              "messages": [at_minimum, message, message], "input": [listed_input, "short"],
              "temperature": text(MIN_BYTES + 1, "t")}
    prepared = event_bytes("request.prepared", {"input": inputs})
    # Below 16 KiB the same inputs stay inline in any other event, and a string-valued input is not a list.
    started = event_bytes("request.started", {"input": inputs}, 1)
    text_input = event_bytes("request.prepared", {"input": {"input": text(4 * MIN_BYTES, "r")}}, 2)
    for index, payload in enumerate((prepared, started, text_input)):
        assert emit(emitter, payload, index)
    assert emitter.flush(5)

    lines = records(emitter)
    assert [line["v"] for line in lines] == [2, 1, 1]
    assert lines[0]["event"]["data"]["input"] == {
        "model": "provider/model", "system": ref(system), "instructions": ref(instructions), "tools": ref(tools),
        "messages": [at_minimum, ref(message), ref(message)], "input": [ref(listed_input), "short"],
        "temperature": inputs["temperature"]}
    assert [line["event"] for line in lines[1:]] == [orjson.loads(started), orjson.loads(text_input)]
    moved = (system, instructions, tools, message, listed_input)
    assert blob_files(emitter) == {sha(value): orjson.dumps(value) for value in moved}
    # New blob bytes count as spool usage at once, not at the next rescan of blobs/ (empty at the first one).
    assert emitter._shared_estimate == sum(len(content) for content in blob_files(emitter).values())


def test_other_values_over_16_kib_move_leaves_first_at_most_six_levels_deep(make_emitter):
    emitter = make_emitter()
    start_idle(emitter)
    big = text(VALUE_BYTES + 1, "b")
    fits = text(VALUE_BYTES, "f")
    rows = [text(64, "r")] * 300    # small items adding up to more than 16 KiB
    deep = {"l7": big}              # a container at depth 6 moves whole
    data = {"output": big, "fits": fits, "outer": {"big": big, "small": "kept"}, "rows": rows,
            "l1": {"l2": {"l3": {"l4": {"l5": {"l6": big}}}}},
            "d1": {"d2": {"d3": {"d4": {"d5": {"d6": deep}}}}}}
    many_small = {f"key{index}": text(1000) for index in range(20)}
    caller_reference = {"output": big, "note": {spool.BLOB_KEY: "set by the caller"}}
    payloads = (event_bytes("tool.finished", data), event_bytes("tool.finished", many_small, 1),
                event_bytes("tool.finished", caller_reference, 2))
    for index, payload in enumerate(payloads):
        assert emit(emitter, payload, index)
    assert emitter.flush(5)

    lines = records(emitter)
    assert [line["v"] for line in lines] == [2, 1, 1]
    # A container keeps the reference of a moved leaf inline instead of moving itself.
    assert lines[0]["event"]["data"] == {
        "output": ref(big), "fits": fits, "outer": {"big": ref(big), "small": "kept"}, "rows": ref(rows),
        "l1": {"l2": {"l3": {"l4": {"l5": {"l6": ref(big)}}}}},
        "d1": {"d2": {"d3": {"d4": {"d5": {"d6": ref(deep)}}}}}}
    # Event data itself never moves, and an event that already contains "$blob" is written as it came.
    assert [line["event"] for line in lines[1:]] == [orjson.loads(payload) for payload in payloads[1:]]
    blobs = blob_files(emitter)
    assert blobs == {sha(value): orjson.dumps(value) for value in (big, rows, deep)}
    assert all(spool.BLOB_REFERENCE not in content for content in blobs.values())


def test_blob_file_holds_the_inline_bytes_under_their_sha256_and_is_synced_before_its_line_closes(
        make_emitter, monkeypatch):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)
    calls = []
    fsync_directory = emitter_module._fsync_directory

    def rename(source, target):
        calls.append(("rename", Path(target).parent.name, Path(target).name))
        os.replace(source, target)

    def synced(path):
        if Path(path) in (emitter.blob_dir, emitter.producer_dir):
            calls.append(("fsync_directory", Path(path).name))
        return fsync_directory(path)
    emitter._rename = rename
    monkeypatch.setattr(emitter_module, "_fsync_directory", synced)
    start_idle(emitter)
    system = "系统提示 " * 200
    payload = event_bytes("request.prepared", {"input": {"system": system}})
    assert emit(emitter, payload)
    assert emitter.flush(5)

    content = payload[payload.index(b'"system":') + len(b'"system":'):-len(b"}}}")]
    assert content == orjson.dumps(system)
    name = hashlib.sha256(content).hexdigest()
    assert blob_files(emitter) == {name: content}
    [line] = records(emitter)
    assert line["event"]["data"]["input"]["system"] == {spool.BLOB_KEY: name}
    assert calls == [("rename", spool.BLOBS_DIR, name), ("fsync_directory", spool.BLOBS_DIR),
                     ("rename", emitter.producer_id, spool.file_name(1)),
                     ("fsync_directory", emitter.producer_id)]


def test_only_event_lines_that_reference_blobs_are_version_2(make_emitter):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)
    emitter.WAIT_SECONDS = 10
    start_idle(emitter)
    large = text(VALUE_BYTES + 1)
    assert emit(emitter, event_bytes("request.delta", {"blocks": []}), 0)
    assert emit(emitter, event_bytes("request.prepared", {"input": {"system": text(MIN_BYTES + 1)}}, 1), 1)
    # Controls are never externalized, even with a value over 16 KiB.
    assert emitter.emit_control({"type": "session.meta", "session": {"id": "root", "user_id": "user", "title": large}})
    emitter.drop("budget", 10, "user", "root", "run", "req9")
    assert emitter.flush(5)
    emitter.close(5)

    lines = records(emitter)
    assert [(line["k"], line["v"], line[line["k"]]["type"]) for line in lines] == [
        ("event", 1, "request.delta"), ("event", 2, "request.prepared"), ("control", 1, "session.meta"),
        ("control", 1, "gap"), ("control", 1, "producer.goodbye")]
    assert lines[2]["control"]["session"]["title"] == large
    assert list(blob_files(emitter)) == [sha(text(MIN_BYTES + 1))]


def test_existing_blob_gets_its_mtime_refreshed_at_most_once_a_minute_instead_of_being_rewritten(make_emitter):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)
    emitter.WAIT_SECONDS = 10
    system = text(MIN_BYTES + 1)
    content = orjson.dumps(system)
    name = hashlib.sha256(content).hexdigest()
    spool.ensure_private_dir(emitter.blob_dir)
    path = emitter.blob_dir / name
    path.write_bytes(content)  # stored earlier by another producer
    inode = os.stat(path).st_ino
    hour_ago = time.time_ns() - 3600 * 10**9
    os.utime(path, ns=(hour_ago, hour_ago))
    start_idle(emitter)
    payload = event_bytes("request.prepared", {"input": {"system": system}})

    def emitted_mtime(index):
        assert emit(emitter, payload, index)
        assert emitter.flush(5)
        wait_idle(emitter)
        status = os.stat(path)
        assert status.st_ino == inode
        return status.st_mtime_ns

    assert emitted_mtime(0) > hour_ago
    os.utime(path, ns=(hour_ago, hour_ago))
    # Referenced again within a minute of the refresh: neither checked nor touched.
    assert emitted_mtime(1) == hour_ago
    with emitter._lock:
        emitter._blobs_seen[name] -= spool.BLOB_REFRESH_SECONDS
    assert emitted_mtime(2) > hour_ago

    assert [line["v"] for line in records(emitter)] == [2, 2, 2]
    assert blob_files(emitter) == {name: content}
    stats = emitter.stats()
    assert (stats["blobs_written"], stats["blob_errors"]) == (0, 0)


def test_blob_write_failure_writes_the_event_inline_as_version_1(make_emitter):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)

    def no_space_for_blobs(source, target):
        if Path(target).parent == emitter.blob_dir:
            raise OSError(errno.ENOSPC, "No space left on device")
        os.replace(source, target)
    emitter._rename = no_space_for_blobs
    start_idle(emitter)
    system = text(MIN_BYTES + 1)
    payload = event_bytes("request.prepared", {"input": {"system": system}})
    assert emit(emitter, payload)
    assert emitter.flush(5)

    [line] = records(emitter)
    assert (line["v"], line["event"]) == (1, orjson.loads(payload))
    assert blob_files(emitter) == {}  # the temporary file is removed as well
    stats = emitter.stats()
    assert (stats["blob_errors"], stats["blobs_written"], stats["dropped_events"]) == (1, 0, 0)
    assert stats["last_error"] == "OSError:ENOSPC"

    emitter._rename = os.replace
    assert emit(emitter, payload, 1)
    assert emitter.flush(5)
    assert [line["v"] for line in records(emitter)] == [1, 2]
    assert list(blob_files(emitter)) == [sha(system)]


def test_spool_budget_counts_blob_files_and_drops_a_line_whose_new_blob_does_not_fit(make_emitter, tmp_path):
    root = tmp_path / "spool"
    blob_dir = spool.blobs_dir(root)
    spool.ensure_private_dir(blob_dir)
    filler = blob_dir / hashlib.sha256(b"another producer's blob").hexdigest()
    filler.write_bytes(b"f" * 128 * KIB)
    assert spool.spool_usage(root).blob_bytes >= 128 * KIB
    emitter = make_emitter()
    emitter.WAIT_SECONDS = 10
    # Every writer cycle rescans producers/, blobs/ and quarantine/.
    emitter.SPOOL_SAMPLE_SECONDS = emitter.SHARED_SAMPLE_SECONDS = 0
    start_idle(emitter)
    # Room for 32 KiB beyond what is on disk, whatever the file system's block size.
    emitter.spool_max_bytes = spool.spool_usage_bytes(root) + 32 * KIB
    system = text(64 * KIB)
    content = orjson.dumps(system)
    name = hashlib.sha256(content).hexdigest()
    payload = event_bytes("request.prepared", {"input": {"system": system}})

    # The line alone fits in the budget; with its new 64 KiB blob it does not.
    assert emit(emitter, payload)
    assert emitter.flush(5) is False
    assert emitter.stats()["dropped_by_reason"] == {"spool_full": 1}
    assert name not in blob_files(emitter)
    wait_idle(emitter)

    # Less usage with that blob already stored: only the line counts.
    filler.write_bytes(b"f" * 48 * KIB)
    (blob_dir / name).write_bytes(content)
    assert emit(emitter, payload, 1)
    assert emitter.flush(5)

    lines = records(emitter)
    assert [(line["k"], line["v"]) for line in lines] == [("control", 1), ("event", 2)]
    gap = lines[0]["control"]
    assert (gap["reason"], gap["dropped_events"], gap["dropped_bytes"]) == ("spool_full", 1, len(payload))
    assert lines[1]["event"]["data"]["input"]["system"] == {spool.BLOB_KEY: name}
    assert emitter.stats()["blobs_written"] == 0
    assert spool.spool_usage_bytes(root) <= emitter.spool_max_bytes


def test_worker_reads_a_version_2_line_as_the_inline_event(make_emitter):
    emitter = make_emitter(blob_min_bytes=MIN_BYTES)
    start_idle(emitter)
    message = {"role": "user", "content": "请总结这段对话 " * 40}
    payloads = [
        event_bytes("request.prepared", {"input": {"system": "你是助手。" * 60,
                                                   "tools": [{"name": "bash", "schema": text(MIN_BYTES)}],
                                                   "messages": [message, "short", message]}}),
        event_bytes("tool.finished", {"output": text(VALUE_BYTES + 1), "nested": {"rows": [text(64)] * 300}}, 1),
        event_bytes("request.delta", {"blocks": [{"type": "text", "delta": "inline"}]}, 2),
    ]
    for index, payload in enumerate(payloads):
        assert emit(emitter, payload, index)
    assert emitter.flush(5)

    [name] = data_files(emitter)
    path = emitter.producer_dir / name
    written = [spool.decode_line(line) for line in path.read_bytes().splitlines()]
    assert [line["v"] for line in written] == [2, 2, 1]
    assert len(blob_files(emitter)) == 5
    batch = read_batch(path, 0, max_lines=10, max_bytes=1 << 20)
    assert batch.eof and len(batch.lines) == 3
    for line, record, payload in zip(batch.lines, written, payloads):
        # Every reference is replaced by its blob: the event is the inline one, byte for byte.
        inline = spool.encode_event_line(record["n"], record["t"].encode(), payload, version=record["v"])
        assert line.data == inline.rstrip(b"\n")
        assert spool.decode_line(line.data)["event"] == orjson.loads(payload)


def test_blob_minimum_comes_from_trajectory_spool_blob_min_bytes(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.delenv("TRAJECTORY_SPOOL_BLOB_MIN_BYTES", raising=False)
    emitter_module.reset_emitter_for_tests()
    try:
        emitter = emitter_module.get_emitter()
        assert (emitter.blob_min_bytes, emitter.blob_dir) == (spool.BLOB_MIN_BYTES, spool.blobs_dir(tmp_path / "spool"))
        monkeypatch.setenv("TRAJECTORY_SPOOL_BLOB_MIN_BYTES", "4096")
        emitter_module.reset_emitter_for_tests()
        assert emitter_module.get_emitter().blob_min_bytes == 4096
    finally:
        emitter_module.reset_emitter_for_tests()
