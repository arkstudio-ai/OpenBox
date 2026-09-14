"""TRAJECTORY_SINK dispatch of record/record_stream/flush and emitter settings."""
import asyncio
import concurrent.futures
import logging
from pathlib import Path

import pytest
from sqlalchemy import event as sa_event, text

import db.base as database
from db.base import get_db_session
from trajectory import config, spool
import trajectory.emitter as emitter_module
from trajectory.context import TraceContext, bind
from trajectory.emitter import get_emitter, reset_emitter_for_tests
import trajectory.recorder as recorder


@pytest.fixture
def spool_env(tmp_path, monkeypatch):
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SINK", "spool")
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    yield tmp_path / "spool"
    reset_emitter_for_tests()


@pytest.fixture
def statements():
    captured = []
    engine = database.get_engine().sync_engine

    def capture(connection, cursor, statement, parameters, context, executemany):
        captured.append(statement)

    sa_event.listen(engine, "before_cursor_execute", capture)
    yield captured
    sa_event.remove(engine, "before_cursor_execute", capture)


def records(emitter):
    names = sorted(name for name in emitter.producer_dir.iterdir() if name.name.endswith(spool.CLOSED_SUFFIX))
    return [spool.decode_line(line) for name in names for line in name.read_bytes().splitlines()]


@pytest.fixture
def caplog(caplog, monkeypatch):
    # Tests that run alembic in-process call fileConfig(), which disables every
    # logger that already exists, including trajectory.config.
    monkeypatch.setattr(logging.getLogger("trajectory.config"), "disabled", False)
    previous = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    try:
        yield caplog
    finally:
        logging.disable(previous)


def forbidden(*args, **kwargs):
    raise AssertionError("unexpected recorder path")


def test_default_sink_is_db_and_invalid_values_fall_back(monkeypatch, tmp_path, caplog):
    reset_emitter_for_tests()
    monkeypatch.delenv("TRAJECTORY_SINK", raising=False)
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    assert config.sink() == "db" and get_emitter() is None
    monkeypatch.setenv("TRAJECTORY_SINK", " SPOOL ")
    assert config.sink() == "spool"
    monkeypatch.setenv("TRAJECTORY_SINK", "kafka-sink-test")
    with caplog.at_level(logging.WARNING, logger="trajectory.config"):
        assert config.sink() == "db" and get_emitter() is None
    assert "Invalid TRAJECTORY_SINK" in caplog.text
    assert not (tmp_path / "spool").exists()


def test_emitter_settings_defaults_minimums_and_invalid_values(monkeypatch, tmp_path, caplog):
    for name in ("TRAJECTORY_SPOOL_DIR", "TRAJECTORY_EMIT_QUEUE_BYTES", "TRAJECTORY_EMIT_MAX_EVENT_BYTES",
                 "TRAJECTORY_SPOOL_FILE_BYTES", "TRAJECTORY_SPOOL_FILE_MS", "TRAJECTORY_SPOOL_MAX_BYTES",
                 "TRAJECTORY_BUDGET_REFRESH_MS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(config, "SERVER_SPOOL_DIR", tmp_path / "missing")
    assert config.BACKEND_DIR == Path(recorder.__file__).resolve().parent.parent
    assert config.emitter_settings() == config.EmitterSettings(
        spool_dir=config.BACKEND_DIR / ".openbox" / "trajectory-spool", queue_bytes=67108864,
        max_event_bytes=33554432, file_bytes=8388608, file_ms=1000, spool_max_bytes=2147483648,
        budget_refresh_ms=5000)
    server = tmp_path / "server-spool"
    server.mkdir()
    monkeypatch.setattr(config, "SERVER_SPOOL_DIR", server)
    assert config.spool_dir() == server
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "explicit"))
    assert config.spool_dir() == tmp_path / "explicit"
    monkeypatch.setenv("TRAJECTORY_EMIT_QUEUE_BYTES", "not-a-number-settings-test")
    monkeypatch.setenv("TRAJECTORY_SPOOL_FILE_MS", "0")
    monkeypatch.setenv("TRAJECTORY_SPOOL_MAX_BYTES", " 4096 ")
    with caplog.at_level(logging.WARNING, logger="trajectory.config"):
        settings = config.emitter_settings()
    assert (settings.queue_bytes, settings.file_ms, settings.spool_max_bytes) == (67108864, 1, 4096)
    assert "Invalid TRAJECTORY_EMIT_QUEUE_BYTES" in caplog.text


def test_get_emitter_starts_one_process_emitter_and_reset_closes_it(spool_env):
    first = get_emitter()
    assert first is get_emitter() and first.stats()["state"] == "running"
    assert first.producer_dir.parent == spool_env / spool.PRODUCERS_DIR
    assert (first.producer_dir / spool.PRODUCER_FILE).exists()
    reset_emitter_for_tests()
    assert first.stats()["state"] == "closed"
    assert records(first)[-1]["control"]["type"] == "producer.goodbye"
    second = get_emitter()
    assert second is not first and second.producer_id != first.producer_id


def test_spool_dir_falls_back_when_the_server_directory_cannot_be_inspected(monkeypatch):
    class Unsearchable:
        def is_dir(self):
            raise PermissionError(13, "Permission denied")

    monkeypatch.delenv("TRAJECTORY_SPOOL_DIR", raising=False)
    monkeypatch.setattr(config, "SERVER_SPOOL_DIR", Unsearchable())
    assert config.spool_dir() == config.BACKEND_DIR / ".openbox" / "trajectory-spool"


def test_fork_child_gets_a_fresh_emitter_and_never_closes_the_parents(spool_env):
    parent = get_emitter()
    limiter_lock = emitter_module._log_limiter._lock
    # Another parent thread was inside a rate-limited log call at fork time.
    limiter_lock.acquire()
    try:
        emitter_module._after_fork_in_child()
    finally:
        limiter_lock.release()
    try:
        assert emitter_module._log_limiter._lock is not limiter_lock
        assert emitter_module._log_limiter.allow(("fork test", "fresh lock")) is True
        child = get_emitter()
        assert child is not parent and child.producer_id != parent.producer_id
        parent.close(1)
        assert parent.stats()["state"] == "running"
    finally:
        parent._forked = False
        parent.close(2)


async def test_spool_sink_record_stream_and_flush_never_touch_the_database(spool_env, monkeypatch, statements):
    for name in ("get_db_session", "append_events_in_tx", "mark_capture_paused_in_tx", "ensure_trajectory_in_tx"):
        monkeypatch.setattr(recorder, name, forbidden)
    context = TraceContext("user", "root", run_id="run", request_id="req", call_id="call")
    assert await recorder.record("request.started", {"model": "m"}, context=context) is None
    receipt = recorder.record_stream(context, {"type": "request.delta", "event_id": "request:req:chunk:1",
                                               "data": {"chunk_index": 1, "blocks": []}})
    assert isinstance(receipt, asyncio.Future) and receipt.done()
    assert await receipt is None
    with bind(context):
        assert await recorder.flush() == "0"
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    assert await recorder.record("tool.started", {"tool": "bash"}, context=context) is None
    assert await recorder.record_stream(context, {"type": "tool.output", "data": {}}) is None
    assert await recorder.flush(context) == "0"
    assert statements == []
    events = [record["event"] for record in records(get_emitter())]
    assert [(event["type"], event["request_id"]) for event in events] == [("request.started", "req"),
                                                                            ("request.delta", "req")]
    assert events[1]["event_id"] == "request:req:chunk:1"


async def test_spool_sink_record_with_db_waits_for_commit_without_trajectory_sql(spool_env, monkeypatch, statements):
    monkeypatch.setattr(recorder, "append_events_in_tx", forbidden)
    emitter = get_emitter()
    context = TraceContext("user", "root", turn_id="turn")
    async with get_db_session() as db:
        await db.execute(text("SELECT 1"))
        assert await recorder.record("turn.started", {"origin": "prompt"}, context=context, db=db,
                                     event_id="turn:1") is None
        assert emitter.stats()["queued_lines"] == 0
        assert len(db.info[emitter_module.PENDING_KEY]) == 1
    assert emitter.flush(5)
    assert statements and all("trajectory" not in statement.lower() for statement in statements)
    assert [record["event"]["event_id"] for record in records(emitter)] == ["turn:1"]


def test_record_stream_outside_an_event_loop_returns_a_resolved_future(spool_env):
    receipt = recorder.record_stream(TraceContext("user", "root", request_id="req"),
                                     {"type": "request.delta", "data": {}})
    assert isinstance(receipt, concurrent.futures.Future) and receipt.result(timeout=0) is None


async def test_db_sink_keeps_the_legacy_recorder_path(monkeypatch, tmp_path):
    reset_emitter_for_tests()
    monkeypatch.delenv("TRAJECTORY_SINK", raising=False)
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    for name in ("emit", "emit_after_commit", "emit_stream", "flush_spool", "completed_receipt"):
        monkeypatch.setattr(recorder, name, forbidden)
    calls = []

    async def legacy_append(db, context, events):
        calls.append([event["type"] for event in events])
        return recorder.PendingRange("trj", "1", "1", ())

    monkeypatch.setattr(recorder, "append_events_in_tx", legacy_append)
    async with get_db_session() as db:
        result = await recorder.record("turn.started", {}, context=TraceContext("user", "root", turn_id="t"), db=db)
    assert calls == [["turn.started"]] and isinstance(result, recorder.PendingRange)
    assert await recorder.flush() == "0"
    assert get_emitter() is None and not (tmp_path / "spool").exists()
