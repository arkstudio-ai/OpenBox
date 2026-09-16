"""Emitter: line format, event shape, fail-open emit, gap controls, lifecycle, thread safety."""
import asyncio
import atexit
from datetime import datetime, timezone
import os
import re
import socket
import stat
import threading
import time

import orjson
import pytest
from pydantic import BaseModel

from trajectory import spool
from trajectory.context import TraceContext, bind
from trajectory.emitter import (Emitter, _DropWindow, _json_default, emit, emit_control, emit_stream, get_emitter,
                                reset_emitter_for_tests)
from trajectory.types import TrajectoryError, prepare, prepare_fast

KIB = 1024
LINE = re.compile(rb'^\{"v":1,"k":"(event|control)","n":(\d+),"t":"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z)",'
                  rb'"(event|control)":(\{.*\})\}\n$')


@pytest.fixture
def make_emitter(tmp_path):
    created = []

    def factory(**overrides):
        settings = {"queue_bytes": 8 * 1024 * KIB, "max_event_bytes": 1024 * KIB, "file_bytes": 8 * 1024 * KIB,
                    "file_ms": 60_000, "spool_max_bytes": 1 << 40}
        settings.update(overrides)
        emitter = Emitter(tmp_path / "spool", **settings)
        created.append(emitter)
        return emitter

    yield factory
    for emitter in created:
        emitter.close(2.0)


@pytest.fixture
def spool_env(tmp_path, monkeypatch):
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    yield tmp_path / "spool"
    reset_emitter_for_tests()


def records(emitter):
    names = sorted(name for name in os.listdir(emitter.producer_dir) if name.endswith(spool.CLOSED_SUFFIX))
    return [spool.decode_line(line) for name in names for line in (emitter.producer_dir / name).read_bytes().splitlines()]


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def seconds(text) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def nested(depth):
    value = {}
    for _ in range(depth):
        value = {"a": value}
    return value


def test_lines_are_exact_v1_json_stamped_with_enqueue_time(make_emitter):
    emitter = make_emitter()

    def slow_write(descriptor, data):
        time.sleep(0.3)
        return os.write(descriptor, data)

    emitter._write = slow_write
    emitter.start()
    payloads = [orjson.dumps({"type": "request.delta", "index": index, "text": "日本語\n\u2028"}) for index in range(3)]
    control = {"type": "session.deleted", "session_id": "s", "user_id": "u", "deleted_at": "2026-09-14T08:00:00Z"}
    before = time.time()
    for payload in payloads:
        assert emitter.emit_bytes(payload, user_id="u", session_id="s", run_id=None, request_id=None)
    assert emitter.emit_control(control)
    after = time.time()
    assert emitter.flush(5)
    lines = (emitter.producer_dir / spool.file_name(1)).read_bytes().splitlines(keepends=True)
    assert len(lines) == 4
    for number, line in enumerate(lines, start=1):
        match = LINE.match(line)
        assert match and int(match.group(2)) == number and match.group(1) == match.group(4)
        assert int(before * 1000) / 1000 <= seconds(match.group(3).decode()) <= after
    assert [LINE.match(line).group(5) for line in lines[:3]] == payloads
    stamp = LINE.match(lines[3]).group(3)
    assert lines[3] == b'{"v":1,"k":"control","n":4,"t":"' + stamp + b'","control":' + orjson.dumps(control) + b"}\n"


def test_emit_writes_event_objects_shaped_like_prepare(spool_env):
    context = TraceContext("user", "root", source_session_id="child", workspace_id="ws", turn_id="turn",
                           run_id="run", generation=3, agent_id="agent", step_id="step", request_id="req_x")
    occurred = datetime(2026, 9, 14, 8, 0, 0, 120000, tzinfo=timezone.utc)
    data = {"purpose": "chat", "input": {"messages": [{"role": "user", "content": "hi"}]}}
    with bind(context):
        identifier = emit("request.prepared", data, event_id="request:req_x:prepared", occurred_at=occurred,
                          message_id="msg", unknown_keyword="ignored")
    assert identifier == "request:req_x:prepared"
    expected = prepare(context, {"type": "request.prepared", "data": data, "event_id": identifier,
                                 "occurred_at": occurred, "message_id": "msg"})
    generated = emit("turn.started", {}, context=context.derive(request_id=None))
    assert generated.startswith("evt_") and len(generated) == 36
    emitter = get_emitter()
    assert emitter.flush(5)
    first, second = records(emitter)
    assert first["event"] == expected and list(first["event"]) == list(expected)
    assert first["event"]["occurred_at"] == "2026-09-14T08:00:00.120Z" and first["event"]["generation"] == 3
    assert not {"parent_agent_id", "call_id", "part_id", "unknown_keyword"} & set(first["event"])
    assert second["event"]["event_id"] == generated and "request_id" not in second["event"]


VALID = [
    {"type": "request.delta", "request_id": "r", "data": {"chunk_index": 0, "mode": "replace", "blocks": []}},
    {"type": "tool.output", "call_id": "c", "data": {"output": "x", "duration_ms": 1.5}},
    {"type": "request.usage", "request_id": "r", "data": {"mode": "delta"}},
    {"type": "run.started", "run_id": "run", "generation": 2, "data": {}},
    {"type": "message.committed", "source_session_id": "child", "caused_by_event_id": "evt_x", "data": {"x": 1}},
    {"type": "baseline.captured", "version": 1},
]
INVALID = [
    {"type": "nope.event", "data": {}},
    {"type": "turn.started", "turn_id": "t", "version": 2},
    {"type": "turn.started", "turn_id": "t", "data": []},
    {"type": "request.started", "data": {}},
    {"type": "tool.requested", "call_id": "c" * 129},
    {"type": "tool.requested", "call_id": 5},
    {"type": "tool.requested", "call_id": ""},
    {"type": "request.delta", "request_id": "r", "data": {"chunk_index": -1}},
    {"type": "tool.output", "call_id": "c", "data": {"chunk_index": True}},
    {"type": "tool.output", "call_id": "c", "data": {"mode": "sideways"}},
    {"type": "request.delta", "request_id": "r", "data": {"blocks": {}}},
    {"type": "request.usage", "request_id": "r", "data": {"mode": "other"}},
    {"type": "step.finished", "step_id": "s", "data": {"duration_ms": -1}},
    {"type": "step.finished", "step_id": "s", "data": {"elapsed_ms": True}},
]


def test_prepare_fast_matches_prepare_except_for_the_canonical_json_pass():
    context = TraceContext("user", "root", workspace_id="ws", agent_id="agent")
    stamp = datetime(2026, 9, 14, tzinfo=timezone.utc)
    for event in VALID:
        full = {"event_id": "evt_fixed", "occurred_at": stamp, **event}
        fast, slow = prepare_fast(context, full), prepare(context, full)
        assert fast == slow and list(fast) == list(slow)
    for event in INVALID:
        with pytest.raises(TrajectoryError) as slow_error:
            prepare(context, event)
        with pytest.raises(TrajectoryError) as fast_error:
            prepare_fast(context, event)
        assert str(fast_error.value) == str(slow_error.value)
    with pytest.raises(TrajectoryError, match="Invalid timing"):
        prepare_fast(context, {"type": "step.finished", "step_id": "s", "data": {"ttft_ms": float("nan")}})
    for data in ({"value": float("nan")}, {"value": object()}, {"when": stamp}):
        with pytest.raises((TypeError, ValueError)):
            prepare(context, {"type": "baseline.captured", "data": data})
        assert prepare_fast(context, {"type": "baseline.captured", "data": data})["data"] is data


def test_null_identity_overrides_are_omitted_from_spooled_events(spool_env):
    context = TraceContext("user", "root", source_session_id="child", generation=0, request_id="req", call_id="call")
    stamp = datetime(2026, 9, 14, tzinfo=timezone.utc)
    event = {"type": "message.committed", "event_id": "evt_fixed", "occurred_at": stamp, "data": {},
             "source_session_id": None, "generation": None, "request_id": None, "call_id": None}
    slow, fast = prepare(context, event), prepare_fast(context, event)
    assert {key for key, value in slow.items() if value is None} == {"source_session_id", "generation", "request_id",
                                                                       "call_id"}
    # No null identity keys (SPEC §3.3); a missing source means the root session, as in TraceContext.
    assert fast == {**{key: value for key, value in slow.items() if value is not None}, "source_session_id": "root"}
    assert list(fast) == [key for key in slow if key not in {"generation", "request_id", "call_id"}]
    with pytest.raises(TrajectoryError, match="request.started requires request_id"):
        prepare_fast(context, {"type": "request.started", "request_id": None})
    assert emit("message.committed", {}, context=context, event_id="evt_nulls", request_id=None, call_id=None)
    emitter = get_emitter()
    assert emitter.flush(5)
    [record] = records(emitter)
    assert None not in record["event"].values()
    assert not {"request_id", "call_id"} & set(record["event"]) and record["event"]["generation"] == 0


class Model(BaseModel):
    name: str
    when: datetime


def test_unsupported_values_become_markers_and_non_finite_numbers_null(spool_env):
    stamp = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    data = {"nan": float("nan"), "inf": float("-inf"), "blob": b"\x00\x01", "view": memoryview(b"ab"),
            "set": {3}, "tuple": (1, "two"), "model": Model(name="m", when=stamp), "object": object(),
            "keys": {1: "one", None: "none"}, "when": stamp}
    assert emit("baseline.captured", data, context=TraceContext("user", "root"))
    emitter = get_emitter()
    assert emitter.flush(5)
    [record] = records(emitter)
    stored = record["event"]["data"]
    assert stored["nan"] is None and stored["inf"] is None
    assert stored["blob"] == stored["view"] == {"availability": "not_recorded", "reason": "binary_value"}
    assert stored["set"] == [3] and stored["tuple"] == [1, "two"]
    assert stored["model"] == {"name": "m", "when": "2026-09-14T08:00:00Z"}
    assert stored["object"] == {"availability": "not_recorded", "reason": "unsupported_value"}
    assert stored["keys"] == {"1": "one", "null": "none"} and stored["when"] == "2026-09-14T08:00:00+00:00"
    assert _json_default(stamp) == stamp.isoformat() and _json_default(frozenset()) == []


def test_emit_never_raises_and_reports_invalid_or_unserializable_events(spool_env, monkeypatch):
    context = TraceContext("user", "root", run_id="run", request_id="req")
    invalid = [
        lambda: emit("unknown.type", {}, context=context),
        lambda: emit("request.started", ["not", "a", "dict"], context=context),
        lambda: emit("tool.started", {}, context=context),
        lambda: emit("request.delta", {"chunk_index": -1}, context=context),
        lambda: emit("request.started", {}, context=context, occurred_at="not a time"),
        lambda: emit("request.started", {}, context=context, request_id=["unhashable"]),
    ]
    unserializable = [
        lambda: emit("request.started", {"big": 2 ** 70}, context=context),
        lambda: emit("request.started", {(1, 2): "tuple key"}, context=context),
        lambda: emit("request.started", {"text": "\ud800"}, context=context),
        lambda: emit_stream(context, {"type": "request.delta", "data": {"nested": nested(300)}}),
    ]
    for call in invalid + unserializable:
        assert call() is None
    assert emit_stream(context, "not an event") is None
    assert emit_control({"type": "producer.goodbye", "last_n": 1}) is False
    assert emit_control("not a control") is False
    assert emit_control({"type": "session.deleted", "session_id": "root", "user_id": "user", (1, 2): 1}) is False
    assert emit("request.started", {"x": 1}) is None
    emitter = get_emitter()
    assert emitter.stats()["dropped_by_reason"] == {"invalid_event": 9, "serialization_failed": 5}
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    assert emit("unknown.type", {}, context=context) is None
    assert emit_control({"type": "recording.state", "user_id": "user", "session_id": "root", "state": "paused",
                         "reason": "recording_disabled", "at": "2026-09-14T08:00:00Z"}) is True
    assert emitter.stats()["dropped_events"] == 14
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    controls = [record["control"] for record in records(emitter) if record["k"] == "control"]
    gaps = {control["reason"]: control for control in controls if control["type"] == "gap"}
    assert gaps["invalid_event"]["dropped_events"] == 9 and gaps["serialization_failed"]["dropped_events"] == 5
    assert gaps["invalid_event"]["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"],
                                                  "request_ids": ["req"]}]
    assert [control["type"] for control in controls].count("recording.state") == 1


def test_emit_never_raises_when_the_spool_directory_is_unusable(tmp_path, monkeypatch):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(blocker / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    try:
        assert emit("turn.started", {}, context=TraceContext("user", "root"), turn_id="turn")
        emitter = get_emitter()
        assert emitter is not None and emitter.flush(1) is False
        assert emitter.stats()["dropped_by_reason"] == {"writer_error": 1}
    finally:
        reset_emitter_for_tests()


def test_oversized_events_are_dropped_and_reported_with_their_size(spool_env, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_EMIT_MAX_EVENT_BYTES", str(64 * KIB))
    context = TraceContext("user", "root", run_id="run", request_id="req")
    stamp = datetime(2026, 9, 14, tzinfo=timezone.utc)
    event = {"type": "request.started", "event_id": "evt_large", "occurred_at": stamp, "data": {"text": "x" * 70 * KIB}}
    size = len(orjson.dumps(prepare_fast(context, event)))
    assert emit("request.started", event["data"], context=context, event_id="evt_large", occurred_at=stamp) is None
    assert emit("request.started", {"text": "small"}, context=context, event_id="evt_small") == "evt_small"
    emitter = get_emitter()
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    everything = records(emitter)
    assert [record["event"]["event_id"] for record in everything if record["k"] == "event"] == ["evt_small"]
    [gap] = [record["control"] for record in everything if record["k"] == "control"]
    assert (gap["reason"], gap["dropped_events"], gap["dropped_bytes"]) == ("event_too_large", 1, size)
    assert gap["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"], "request_ids": ["req"]}]


def test_gap_counts_survive_identities_that_cannot_be_listed(make_emitter):
    emitter = make_emitter()
    emitter.start()
    emitter.drop("serialization_failed", 5, "user", "root", "run", "req")
    # Malformed identities from callers: counted, never listed, never able to break the gap line.
    for user_id, run_id in ((2 ** 70, None), (["unhashable"], None), ("u" * 129, None), ("user", {"not": "str"})):
        emitter.drop("serialization_failed", 1, user_id, "root", run_id, None)
    assert emitter.emit_control({"type": "session.deleted", "user_id": 2 ** 70, "session_id": "s",
                                 "deleted_at": "2026-09-14T08:00:00Z"}) is False
    # A lone surrogate is a listable str that orjson cannot encode: the counts are still written.
    emitter.drop("queue_overflow", 3, "user\ud800", "root", None, None)
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    gaps = {record["control"]["reason"]: record["control"] for record in records(emitter) if record["k"] == "control"}
    assert (gaps["serialization_failed"]["dropped_events"], gaps["serialization_failed"]["dropped_bytes"]) == (6, 9)
    assert gaps["serialization_failed"]["sessions"] == [{"user_id": "user", "session_id": "root", "run_ids": ["run"],
                                                         "request_ids": ["req"]}]
    assert (gaps["queue_overflow"]["dropped_events"], gaps["queue_overflow"]["sessions"]) == (1, [])
    assert emitter.stats()["gap_lines"] == 2


def hold_start(emitter):
    """Run ``start()`` on a thread that waits inside directory preparation until released."""
    entered, release, starter = threading.Event(), threading.Event(), {}
    original = emitter._prepare_directory

    def prepare():
        if threading.get_ident() == starter.get("ident"):
            entered.set()
            release.wait(5)
        return original()

    def run():
        starter["ident"] = threading.get_ident()
        emitter.start()

    emitter._prepare_directory = prepare
    thread = threading.Thread(target=run)
    thread.start()
    assert entered.wait(5)
    return thread, release


def test_close_during_start_leaves_no_writer_on_the_closed_emitter(make_emitter):
    emitter = make_emitter()
    thread, release = hold_start(emitter)
    emitter.close(1.0)
    assert emitter.stats()["state"] == "closed"
    release.set()
    thread.join(5)
    time.sleep(0.05)
    assert emitter._thread is None and emitter._heartbeat_thread is None and not emitter.stats()["writer_alive"]
    assert emitter.emit_bytes(b'{"late":true}', user_id="u", session_id="s") is False


def test_emits_during_start_never_start_a_second_writer(make_emitter):
    emitter = make_emitter(file_bytes=4 * KIB)
    thread, release = hold_start(emitter)
    for index in range(5):
        assert emitter.emit_bytes(orjson.dumps({"index": index}), user_id="u", session_id="s")
    assert emitter._thread is None
    release.set()
    thread.join(5)
    writer = emitter._thread
    assert writer is not None and writer.is_alive()
    for index in range(5, 400):
        assert emitter.emit_bytes(orjson.dumps({"index": index, "pad": "x" * 50}), user_id="u", session_id="s")
    assert emitter.flush(5)
    assert emitter._thread is writer and emitter.stats()["writer_restarts"] == 0
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, 401))
    assert [record["event"]["index"] for record in everything] == list(range(400))


def test_close_finishes_while_drops_keep_arriving(make_emitter):
    emitter = make_emitter()
    append = emitter._append

    def append_then_drop_again(kind, payload, at, routing, window=None):
        written = append(kind, payload, at, routing, window)
        if window is not None:
            # A producer drops again while each gap line is being written.
            emitter.drop(window[0], 1, "user", "root", None, None)
        return written

    emitter._append = append_then_drop_again
    emitter.start()
    emitter.drop("budget", 1, "user", "root", "run", "req")
    emitter.drop("invalid_event", 1, "user", "root", "run", "req")
    started = time.monotonic()
    emitter.close(3.0)
    assert emitter.stats()["state"] == "closed" and time.monotonic() - started < 2.0
    everything = records(emitter)
    assert everything[-1]["control"] == {"type": "producer.goodbye", "last_n": len(everything)}
    assert [record["n"] for record in everything] == list(range(1, len(everything) + 1))


def test_queue_overflow_is_reported_by_one_gap_with_capped_identities(make_emitter):
    emitter = make_emitter(queue_bytes=16 * KIB)
    payload = orjson.dumps({"type": "request.delta", "pad": "x" * 1000})
    accepted = 0
    while emitter.emit_bytes(payload, user_id="user", session_id="warm", run_id="r", request_id="q"):
        accepted += 1
    assert accepted == 16 * KIB // len(payload)
    for index in range(60):
        assert not emitter.emit_bytes(payload, user_id="user", session_id="busy", run_id=f"run{index % 30}",
                                      request_id=f"req{index}")
    for index in range(250):
        assert not emitter.emit_bytes(payload, user_id="user", session_id=f"s{index}")
    total = 1 + 60 + 250
    stats = emitter.stats()
    assert stats["dropped_by_reason"] == {"queue_overflow": total} and stats["dropped_bytes"] == total * len(payload)
    emitter.start()
    assert emitter.flush(5)
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, accepted + 2))
    [gap] = [record["control"] for record in everything if record["k"] == "control"]
    assert (gap["reason"], gap["dropped_events"], gap["dropped_bytes"]) == ("queue_overflow", total, total * len(payload))
    assert seconds(gap["first_dropped_at"]) <= seconds(gap["last_dropped_at"])
    sessions = gap["sessions"]
    assert len(sessions) == spool.GAP_MAX_SESSIONS
    assert gap["sessions_truncated"] is True
    assert sessions[0] == {"user_id": "user", "session_id": "warm", "run_ids": ["r"], "request_ids": ["q"]}
    assert sessions[1] == {"user_id": "user", "session_id": "busy", "run_ids": [f"run{i}" for i in range(20)],
                           "request_ids": [f"req{i}" for i in range(50)]}
    assert [session["session_id"] for session in sessions[2:]] == [f"s{i}" for i in range(198)]
    assert sessions[2]["run_ids"] == [] and sessions[2]["request_ids"] == []


@pytest.mark.parametrize("overlapping", [False, True])
def test_restoring_gap_windows_keeps_session_overflow_visible(overlapping):
    first, later = _DropWindow(1.0), _DropWindow(2.0)
    for index in range(spool.GAP_MAX_SESSIONS):
        first.add(10, 1.0, "user", f"s{index}", None, None)
    assert first.control("queue_overflow")["sessions_truncated"] is False
    for index in range(spool.GAP_MAX_SESSIONS + 1):
        later.add(10, 2.0, "user", f"s{index}" if overlapping else f"other{index}", None, None)
    first.merge(later)
    control = first.control("queue_overflow")
    assert len(control["sessions"]) == spool.GAP_MAX_SESSIONS
    assert control["sessions_truncated"] is True
    assert control["dropped_events"] == spool.GAP_MAX_SESSIONS * 2 + 1


def test_gap_lines_are_rate_limited_and_totals_stay_exact(make_emitter):
    emitter = make_emitter()
    emitter.start()
    reasons = ("invalid_event", "serialization_failed")
    counts = dict.fromkeys(reasons, 0)
    deadline, index = time.monotonic() + 0.6, 0
    while time.monotonic() < deadline:
        reason = reasons[index % 2]
        emitter.drop(reason, 10, "user", "root", "run", f"req{index}")
        counts[reason] += 1
        index += 1
        time.sleep(0.002)
    assert wait_for(lambda: emitter.stats()["pending_gaps"] == 0)
    assert emitter.flush(5)
    gaps = [record for record in records(emitter) if record["k"] == "control"]
    assert len(gaps) >= 3
    for reason in reasons:
        mine = [gap["control"] for gap in gaps if gap["control"]["reason"] == reason]
        assert sum(gap["dropped_events"] for gap in mine) == counts[reason]
        assert sum(gap["dropped_bytes"] for gap in mine) == counts[reason] * 10
    stamps = [seconds(gap["t"]) for gap in gaps]
    assert all(later - earlier >= 0.098 for earlier, later in zip(stamps, stamps[1:]))


def test_producer_json_goodbye_and_private_permissions(make_emitter, tmp_path):
    emitter = make_emitter()
    emitter.start()
    emitter.start()
    root = tmp_path / "spool"
    assert re.fullmatch(r"\d{14}-[A-Za-z0-9._-]+-\d+-[0-9a-f]{8}", emitter.producer_id)
    assert orjson.loads((emitter.producer_dir / spool.PRODUCER_FILE).read_bytes()) == {
        "version": 1, "producer_id": emitter.producer_id, "boot_id": spool.boot_id(),
        "hostname": socket.gethostname(), "pid": os.getpid(), "role": "backend",
        "started_at": spool.timestamp(emitter.started_at)}
    for directory in (root, root / spool.PRODUCERS_DIR, emitter.producer_dir):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for index in range(3):
        assert emitter.emit_bytes(orjson.dumps({"index": index}), user_id="u", session_id="s")
    thread = emitter._thread
    emitter.close(5)
    emitter.close(5)
    assert not thread.is_alive() and emitter.stats()["state"] == "closed"
    everything = records(emitter)
    assert [record["n"] for record in everything] == [1, 2, 3, 4]
    assert everything[-1]["control"] == {"type": "producer.goodbye", "last_n": 4}
    for name in os.listdir(emitter.producer_dir):
        assert stat.S_IMODE((emitter.producer_dir / name).stat().st_mode) == 0o600
    assert emitter.emit_bytes(b'{"late":true}', user_id="u", session_id="s") is False
    assert emitter.emit_control({"type": "session.deleted", "session_id": "s", "user_id": "u", "deleted_at": "x"}) is False
    assert emitter.flush(1) is True
    assert emitter.stats()["rejected_after_close"] == 2


def test_start_registers_an_atexit_close_with_a_two_second_timeout(make_emitter, monkeypatch):
    registered, unregistered, closes = [], [], []
    monkeypatch.setattr(atexit, "register", lambda function: registered.append(function) or function)
    monkeypatch.setattr(atexit, "unregister", unregistered.append)
    emitter = make_emitter()
    emitter.start()
    emitter.start()
    assert registered == [emitter._close_at_exit]
    emitter.close = lambda timeout=5.0: closes.append(timeout)
    registered[0]()
    assert closes == [2.0]
    del emitter.close
    emitter.close(2.0)
    assert unregistered == [emitter._close_at_exit] and emitter.stats()["state"] == "closed"


async def test_concurrent_emits_from_threads_and_tasks_keep_counters_contiguous(make_emitter):
    emitter = make_emitter(file_bytes=32 * KIB)
    emitter.start()
    per_producer = 400

    def thread_producer(name):
        for sequence in range(per_producer):
            emitter.emit_bytes(orjson.dumps({"producer": name, "sequence": sequence, "pad": "p" * (sequence % 50)}),
                               user_id="u", session_id=name)

    async def task_producer(name):
        for sequence in range(per_producer):
            emitter.emit_bytes(orjson.dumps({"producer": name, "sequence": sequence}), user_id="u", session_id=name)
            if sequence % 10 == 0:
                await asyncio.sleep(0)

    threads = [threading.Thread(target=thread_producer, args=(f"thread{index}",)) for index in range(6)]
    for thread in threads:
        thread.start()
    await asyncio.gather(*(task_producer(f"task{index}") for index in range(6)))
    for thread in threads:
        thread.join()
    assert await asyncio.to_thread(emitter.flush, 10)
    everything = records(emitter)
    assert [record["n"] for record in everything] == list(range(1, 12 * per_producer + 1))
    sequences = {}
    for record in everything:
        sequences.setdefault(record["event"]["producer"], []).append(record["event"]["sequence"])
    assert sorted(sequences) == sorted([f"thread{i}" for i in range(6)] + [f"task{i}" for i in range(6)])
    assert all(values == list(range(per_producer)) for values in sequences.values())
    assert len([name for name in os.listdir(emitter.producer_dir) if name.endswith(".jsonl")]) > 2
    assert all((emitter.producer_dir / name).read_bytes().endswith(b"\n")
               for name in os.listdir(emitter.producer_dir) if name.endswith(".jsonl"))
    assert emitter.stats()["dropped_events"] == 0
