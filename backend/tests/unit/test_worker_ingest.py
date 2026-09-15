"""Spool ingest (SPEC §8.3, §8.4, §8.7): trajectories, seq, keep-first dedupe, redaction, content addressing.

The helpers here (spool writer, fake metrics and retention, harness) are shared by the other
``test_worker_ingest_*`` modules.
"""
import hashlib
import os
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import orjson
import pytest
from sqlalchemy import delete, select, update

from bus import bus
from trajectory import spool
from trajectory.lifecycle import publish_after_commit
from trajectory.projector import _preview
from trajectory.redaction import sanitize
from trajectory.storage import MemoryBlobStore, blob_key, decode_blob, encode_blob
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryEvent, TrajectoryEventKey, TrajectoryGcQueue,
    TrajectoryIngestFile, TrajectoryIngestProducer, TrajectoryPayload)
from trajectory.types import canonical, digest
from trajectory.worker.content import dedupe_key
from trajectory.worker.ingest import IngestService
from trajectory.worker.meta import parse_time
from trajectory.worker.settings import WorkerSettings

AT = "2026-09-14T08:00:00.000Z"


class FakeMetrics:
    def __init__(self):
        self.counters, self.gauges = {}, {}

    def inc(self, name, value=1):
        self.counters[name] = self.counters.get(name, 0) + value

    def set_gauge(self, name, value):
        self.gauges[name] = value


class FakeRetention:
    """The tombstone of RetentionService (SPEC §8.11), reduced to what ingest relies on.

    Like ``lifecycle.tombstone_trajectory`` it publishes the deleted notification once the caller's
    transaction commits.
    """

    def __init__(self):
        self.calls = []

    async def tombstone(self, db, trajectory, *, reason):
        self.calls.append((trajectory.id, reason))
        now = datetime.now(timezone.utc)
        trajectory.deleted_at, trajectory.recording_status = now, "deleted"
        await db.execute(delete(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == trajectory.id))
        await db.execute(update(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory.id)
                         .values(availability="deleted", deleted_at=now))
        db.add(TrajectoryGcQueue(kind="prefix", storage_key=f"trajectories/{trajectory.id}/", reason=reason,
                                 attempts=0, next_attempt_at=now, created_at=now))
        await db.flush()
        publish_after_commit(db, trajectory, deleted=True)


class SpoolWriter:
    """Producer directories and lines in spool format v1, as the emitter writes them."""

    def __init__(self, spool_dir: Path, producer_id: str = "20260914080000-host-1-aaaaaaaa", *,
                 hostname: str = "elsewhere", pid: int = 1, boot_id: str = "boot", started_at: str = AT):
        self.producer_id = producer_id
        self.directory = Path(spool_dir) / "producers" / producer_id
        spool.ensure_private_dir(self.directory)
        spool.write_json_atomic(self.directory / spool.PRODUCER_FILE, {
            "version": 1, "producer_id": producer_id, "boot_id": boot_id, "hostname": hostname, "pid": pid,
            "role": "backend", "started_at": started_at})
        self.n = 0
        self.counter = 0

    def line(self, kind: str, body, *, n: int | None = None) -> bytes:
        self.n = self.n + 1 if n is None else n
        encode = spool.encode_event_line if kind == "event" else spool.encode_control_line
        return encode(self.n, AT.encode(), body if isinstance(body, bytes) else orjson.dumps(body))

    def file(self, lines, *, closed: bool = True, counter: int | None = None, age: float | None = None) -> Path:
        self.counter = self.counter + 1 if counter is None else counter
        path = self.directory / spool.file_name(self.counter, closed=closed)
        path.write_bytes(b"".join(lines))
        if age is not None:
            moment = time.time() - age
            os.utime(path, (moment, moment))
        return path

    def events(self, *events, **options) -> Path:
        return self.file([self.line("event", item) for item in events], **options)

    def controls(self, *controls, **options) -> Path:
        return self.file([self.line("control", item) for item in controls], **options)


def event(event_type: str = "input.accepted", *, session: str = "ses_1", user: str = "u1", event_id: str | None = None,
          data: dict | None = None, occurred_at: str = AT, **ids) -> dict:
    """A prepared event object (SPEC §3.3) with the key order of ``prepare_fast``."""
    body = {"user_id": user, "session_id": session, "source_session_id": ids.pop("source_session_id", session),
            "workspace_id": ids.pop("workspace_id", "ws_1"), **ids, "type": event_type, "version": 1,
            "event_id": event_id or f"evt_{uuid.uuid4().hex}", "occurred_at": occurred_at,
            "data": {"text": "hello"} if data is None else data}
    return body


class Harness:
    def __init__(self, settings: WorkerSettings):
        self.settings = settings
        self.store = MemoryBlobStore()
        self.metrics = FakeMetrics()
        self.retention = FakeRetention()
        self.writer = SpoolWriter(settings.spool_dir)
        self.service = self._service()

    def _service(self) -> IngestService:
        return IngestService(self.settings, blob_store=self.store, metrics=self.metrics, retention=self.retention)

    def configure(self, **overrides) -> None:
        self.settings = replace(self.settings, **overrides)
        self.service = self._service()

    async def run(self, **options) -> dict:
        return await self.service.run_once(**options)


@pytest.fixture
async def trace_db(tmp_path):
    await close_trace_engine()
    engine = init_trace_engine(f"sqlite+aiosqlite:///{tmp_path / 'trace.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    yield engine
    await close_trace_engine()


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    return WorkerSettings.from_env()


@pytest.fixture
def harness(trace_db, settings):
    return Harness(settings)


async def rows(model, *where, order_by=None) -> list:
    async with trace_session() as db:
        statement = select(model).where(*where)
        if order_by is not None:
            statement = statement.order_by(order_by)
        return list((await db.scalars(statement)).all())


async def events_of(session_id: str):
    trajectories = await rows(SessionTrajectory, SessionTrajectory.session_id == session_id)
    if not trajectories:
        return None, []
    trajectory = trajectories[0]
    return trajectory, await rows(TrajectoryEvent, TrajectoryEvent.trajectory_id == trajectory.id,
                                  order_by=TrajectoryEvent.seq)


async def test_empty_spool_pass_reports_gauges(harness):
    result = await harness.run()
    assert result["lines"] == 0 and result["trajectories"] == set()
    # Only the producer.json of the writer's directory takes space.
    usage = spool.spool_usage(harness.settings.spool_dir)
    assert harness.metrics.gauges == {"spool_bytes": usage.total, "spool_blob_bytes": 0, "spool_quarantine_bytes": 0,
                                      "spool_quarantine_files": 0, "spool_files": 0,
                                      "spool_oldest_age_seconds": 0.0, "ingest_lag_seconds": 0.0}


async def test_first_events_create_a_trajectory_started_at_seq_1(harness):
    received = []
    unsubscribe = bus.subscribe("trajectory.available", received.append)
    try:
        path = harness.writer.events(
            event(event_id="e1", occurred_at="2026-09-14T08:00:02.000Z", run_id="run_1"),
            event("message.committed", event_id="e2", occurred_at="2026-09-14T08:00:01.000Z", data={"role": "user"}))
        result = await harness.run()
    finally:
        unsubscribe()
    trajectory, stored = await events_of("ses_1")
    assert trajectory.id.startswith("trj_")
    assert [(row.seq, row.type, row.event_id) for row in stored] == [
        (1, "trajectory.started", f"evt_start_{trajectory.id}"), (2, "input.accepted", "e1"),
        (3, "message.committed", "e2")]
    assert stored[0].data == {"existing_session": False, "coverage_start": "2026-09-14T08:00:02.000Z",
                              "schema_version": 2}
    assert (trajectory.next_seq, trajectory.committed_seq, trajectory.event_count) == (4, 3, 3)
    assert (trajectory.user_id, trajectory.workspace_id, trajectory.schema_version) == ("u1", "ws_1", 2)
    assert parse_time(trajectory.last_activity_at) == datetime(2026, 9, 14, 8, 0, 2, tzinfo=timezone.utc)
    assert stored[1].context == {"source_session_id": "ses_1", "run_id": "run_1"}
    assert stored[1].content_hash == digest({key: value for key, value in event(
        event_id="e1", occurred_at="x", run_id="run_1").items() if key not in {"event_id", "occurred_at"}})
    assert [(key.event_id, key.seq) for key in await rows(TrajectoryEventKey, order_by=TrajectoryEventKey.seq)] == [
        (f"evt_start_{trajectory.id}", 1), ("e1", 2), ("e2", 3)]
    assert not path.exists() and await rows(TrajectoryIngestFile) == []
    [producer] = await rows(TrajectoryIngestProducer)
    assert (producer.last_n, producer.hostname, producer.role, producer.goodbye) == (2, "elsewhere", "backend", False)
    assert result["trajectories"] == {trajectory.id} and (result["lines"], result["events"]) == (2, 2)
    assert harness.metrics.counters["ingest_lines"] == 2 and harness.metrics.counters["ingest_events"] == 2
    assert received[-1]["data"] == {"user_id": "u1", "owner_user_id": "u1", "session_id": "ses_1",
                                    "trajectory_id": trajectory.id, "committed_seq": "3"}


async def test_existing_session_comes_from_a_first_baseline_with_history(harness):
    harness.writer.events(event("baseline.captured", session="ses_b", data={"history": [{"id": "m1"}]}),
                          event(session="ses_b"))
    harness.writer.events(event("baseline.captured", session="ses_c", data={"history": []}))
    await harness.run()
    assert (await events_of("ses_b"))[1][0].data["existing_session"] is True
    assert (await events_of("ses_c"))[1][0].data["existing_session"] is False


async def test_keep_first_dedupe_counts_duplicates_and_conflicts(harness):
    original = event(event_id="same", data={"text": "first"})
    harness.writer.events(original, dict(original))
    first = await harness.run()
    harness.writer.events(dict(original), event(event_id="same", data={"text": "changed"}),
                          event(session="ses_other", event_id="same", data={"text": "first"}))
    second = await harness.run()
    trajectory, stored = await events_of("ses_1")
    assert [row.event_id for row in stored] == [f"evt_start_{trajectory.id}", "same"]
    assert stored[1].data == {"text": "first"}
    assert first["duplicates"] == 1 and (second["duplicates"], second["idempotency_conflicts"]) == (1, 2)
    assert (await events_of("ses_other"))[0] is None
    assert harness.metrics.counters["duplicates"] == 2 and harness.metrics.counters["idempotency_conflicts"] == 2
    assert second["trajectories"] == set()


async def test_redaction_runs_before_hashing_and_storage(harness):
    secret = event("tool.finished", event_id="t1", call_id="c1",
                   data={"arguments": {"api_key": "sk-abcdefghijklmnop"}, "output": "Authorization: Bearer abc.def.ghi"})
    harness.writer.events(secret)
    await harness.run()
    row = (await events_of("ses_1"))[1][1]
    assert row.data["arguments"]["api_key"] == "[REDACTED]" and "abc.def" not in row.data["output"]
    expected = {key: value for key, value in secret.items() if key not in {"event_id", "occurred_at"}}
    expected["data"] = sanitize(secret["data"])
    assert row.content_hash == digest(expected)
    assert (row.call_id, row.request_id, row.agent_id) == ("c1", None, None)


async def test_request_inputs_are_content_addressed_once_per_trajectory(harness):
    system = "You are a careful assistant. " * 60
    tools = [{"name": f"tool_{index}", "description": "d" * 100} for index in range(12)]
    messages = [{"role": "user", "content": "q" * 700}, {"role": "assistant", "content": "short"}]

    def prepared(request_id):
        return event("request.prepared", event_id=f"request:{request_id}:prepared", request_id=request_id,
                     data={"model": "m", "input": {"system": system, "tools": tools, "messages": messages}})

    harness.writer.events(prepared("r1"))
    await harness.run()
    objects = dict(harness.store.objects)
    harness.writer.events(prepared("r2"))
    await harness.run()
    trajectory, stored = await events_of("ses_1")
    first, second = stored[1], stored[2]
    value = first.data["input"]
    assert value["system"]["$ref"]["kind"] == "system" and value["tools"]["$ref"]["kind"] == "tools"
    assert value["messages"][0]["$ref"]["kind"] == "message"
    assert value["messages"][1] == {"role": "assistant", "content": "short"}
    assert second.data["input"] == value and first.data["model"] == "m"
    assert harness.store.objects == objects and len(objects) == 3
    payloads = await rows(TrajectoryPayload, TrajectoryPayload.trajectory_id == trajectory.id)
    assert len(payloads) == 3 and {row.first_seq for row in payloads} == {first.seq}
    by_id = {row.payload_id: row for row in payloads}
    for row in payloads:
        raw = decode_blob(harness.store.objects[row.storage_key], row.encoding)
        assert hashlib.sha256(raw).hexdigest() == row.sha256 and row.size_bytes == len(raw)
        assert (row.storage_kind, row.media_type, row.availability) == ("blob", "application/json", "available")
        assert row.storage_key == f"trajectories/{trajectory.id}/blobs/{row.sha256}"
    system_row = by_id[value["system"]["$ref"]["payload_id"]]
    assert orjson.loads(decode_blob(harness.store.objects[system_row.storage_key], system_row.encoding)) == system
    assert system_row.encoding == "zstd" and system_row.stored_bytes < system_row.size_bytes
    assert first.hints == {"preview": {"input": _preview({"system": system, "tools": tools, "messages": messages})}}
    assert trajectory.stored_bytes == sum(row.stored_bytes for row in payloads)
    assert harness.metrics.counters["blob_puts"] == 3


async def test_large_values_and_whole_data_are_externalized(harness):
    harness.configure(inline_bytes=4096)
    wide = {f"field_{index}": "v" * 200 for index in range(40)}
    harness.writer.events(event("tool.finished", call_id="c1", event_id="big", data={"status": "ok", "output": "o" * 10000}),
                          event("tool.finished", call_id="c2", event_id="wide", data=wide))
    await harness.run()
    stored = (await events_of("ses_1"))[1]
    big, whole = stored[1], stored[2]
    assert big.data["status"] == "ok" and big.data["output"]["$ref"]["kind"] == "value"
    assert big.hints == {"preview": {"output": "o" * 240}}
    envelope = whole.data["$payload"]
    assert set(envelope) == {"payload_id", "sha256", "size_bytes", "media_type", "availability"}
    [row] = await rows(TrajectoryPayload, TrajectoryPayload.payload_id == envelope["payload_id"])
    assert orjson.loads(decode_blob(harness.store.objects[row.storage_key], row.encoding)) == wide
    assert row.first_seq == whole.seq and whole.hints is None


async def test_references_lower_first_seq_to_the_earliest_position_without_resurrecting(harness):
    """Rows another writer registered with a later first_seq (projection record values, converted legacy rows)
    become visible from the first event that references them (SPEC §8.4 step 8, projection's
    ``ensure_payload_rows``). Availability never changes: deleted content stays deleted and is not stored again."""
    harness.writer.events(event())
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    system, instructions = "S" * 3000, "D" * 3000
    now = datetime.now(timezone.utc)
    async with trace_session() as db:
        for value, availability, first_seq in ((system, "available", 50), (instructions, "deleted", 60)):
            body = canonical(value)
            sha = hashlib.sha256(body).hexdigest()
            key = blob_key(trajectory.id, sha)
            stored, encoding = encode_blob(body, "application/json")
            if availability == "available":
                await harness.store.put(key, stored, content_type="application/json")
            db.add(TrajectoryPayload(
                payload_id=f"pld_{availability}", trajectory_id=trajectory.id,
                dedupe_key=dedupe_key(sha, "application/json", None, "blob"), sha256=sha, size_bytes=len(body),
                stored_bytes=len(stored), media_type="application/json", encoding=encoding, storage_kind="blob",
                storage_key=key, availability=availability, first_seq=first_seq, created_at=now,
                deleted_at=None if availability == "available" else now))
    puts = harness.store.puts
    harness.writer.events(event("request.prepared", request_id="r1", event_id="r1",
                                data={"model": "m", "input": {"system": system, "instructions": instructions}}))
    await harness.run()
    _, stored = await events_of("ses_1")
    prepared = stored[-1]
    assert prepared.seq == 3
    assert prepared.data["input"]["system"]["$ref"]["payload_id"] == "pld_available"
    assert prepared.data["input"]["instructions"]["$ref"]["payload_id"] == "pld_deleted"
    payloads = {row.payload_id: row for row in await rows(TrajectoryPayload)}
    assert set(payloads) == {"pld_available", "pld_deleted"}
    assert (payloads["pld_available"].first_seq, payloads["pld_available"].availability) == (3, "available")
    assert (payloads["pld_deleted"].first_seq, payloads["pld_deleted"].availability) == (3, "deleted")
    assert payloads["pld_deleted"].deleted_at is not None and harness.store.puts == puts
    # A later reference leaves the earlier position alone.
    harness.writer.events(event("request.prepared", request_id="r2", event_id="r2",
                                data={"model": "m", "input": {"system": system}}))
    await harness.run()
    [row] = await rows(TrajectoryPayload, TrajectoryPayload.payload_id == "pld_available")
    assert row.first_seq == 3


async def test_blob_puts_count_stored_and_uncompressed_bytes(harness):
    system = "S" * 3000
    harness.writer.events(event("request.prepared", request_id="r1", data={"model": "m", "input": {"system": system}}))
    await harness.run()
    counters = harness.metrics.counters
    assert counters["blob_puts"] == 1
    # blob_put_bytes counts what was stored (compressed), blob_put_raw_bytes the content itself.
    assert counters["blob_put_raw_bytes"] == len(canonical(system)) > counters["blob_put_bytes"] > 0


async def test_blob_upload_is_idempotent_across_trajectories(harness):
    body = {"model": "m", "input": {"system": "S" * 2000}}
    harness.writer.events(event("request.prepared", request_id="r1", data=body),
                          event("request.prepared", session="ses_2", request_id="r2", data=body))
    await harness.run()
    first, second = (await events_of("ses_1"))[0], (await events_of("ses_2"))[0]
    keys = sorted(harness.store.objects)
    # Content addressing is per trajectory: each trajectory owns its copy under its own prefix.
    assert len(keys) == 2 and keys[0].split("/")[1] != keys[1].split("/")[1]
    assert {key.split("/")[1] for key in keys} == {first.id, second.id}


async def test_invalid_events_are_counted_and_logged_once_per_field(harness, monkeypatch):
    import logging
    from trajectory.worker import ingest as ingest_module

    class Records(logging.Handler):
        def __init__(self):
            super().__init__()
            self.records = []

        def emit(self, record):
            self.records.append(record)

    monkeypatch.setattr(ingest_module, "_invalid_logged", {})
    handler = Records()
    logger = logging.getLogger("openbox.trajectory.worker.ingest")
    logger.addHandler(handler)
    no_data = event(event_id="no_data")
    no_data["data"] = "not an object"
    try:
        harness.writer.events(event(session="s" * 65), no_data, event(session="s" * 65, event_id="again"),
                              event(event_id="ok"))
        result = await harness.run()
    finally:
        logger.removeHandler(handler)
    assert (result["invalid_events"], result["events"], result["lines"]) == (3, 1, 4)
    assert [row.event_id for row in (await events_of("ses_1"))[1][1:]] == ["ok"]
    warnings = [record.getMessage() for record in handler.records if "invalid spool event" in record.getMessage()]
    assert len(warnings) == 2
    assert any("field=session_id" in text for text in warnings) and any("field=data" in text for text in warnings)
