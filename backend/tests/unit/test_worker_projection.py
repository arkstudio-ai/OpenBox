"""Projection worker (SPEC 8.8) against a SQLite trace database and an in-memory blob store."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from tests.unit.test_worker_projection_support import (AT, FIXTURES, JSON, REFERENCES, Ingest, Metrics, add_meta,  # noqa: F401
    add_payload, add_trajectory, archive, blobs, load_fixture, project_all, settings, trace_db)
from trajectory.payload import dedupe_key, is_ref, reset_blob_cache
from trajectory.projector import contribution, replay
from trajectory.repository import get_checkpoint, read_events, state_at
from trajectory.storage import decode_blob
from trajectory.store.database import trace_session
from trajectory.store.models import (SessionTrajectory, TrajectoryCheckpoint, TrajectoryEvent, TrajectoryPayload,
    TrajectoryRecord, TrajectoryRecordEvent, TrajectorySessionSummary)
from trajectory.types import CorruptContent, canonical, iso
from trajectory.worker import projection
from trajectory.worker.projection import ProjectionService, reference_id, search_doc

SESSION, USER, TRAJECTORY = "session_p", "user_p", "trj_p"
RUN = {"run_id": "run_1", "turn_id": "turn_1", "agent_id": "agent_1"}


def _event(seq, kind, data=None, *, at=None, **ids):
    value = {"event_id": ids.pop("event_id", f"evt_p{seq}"), "trajectory_id": TRAJECTORY, "user_id": USER,
             "session_id": SESSION, "source_session_id": ids.pop("source_session_id", SESSION), "seq": str(seq),
             "type": kind, "version": ids.pop("version", 1), "occurred_at": iso(at or AT + timedelta(seconds=seq)),
             "data": data if data is not None else {}}
    value.update(ids)
    return value


def _run_events():
    step = {"step_id": "step_1", **RUN}
    return [
        _event(1, "run.started", **RUN),
        _event(2, "step.started", **step),
        _event(3, "request.prepared", {"input": {"system": "Be exact", "messages": [{"role": "user", "content": "hi"}]}},
               request_id="req_1", **step),
        _event(4, "request.started", {"model": "model-1"}, request_id="req_1", **step),
        *[_event(5 + index, "request.delta", {"chunk_index": index, "delta": f"part {index} "}, request_id="req_1", **step)
          for index in range(4)],
        _event(9, "request.usage", {"usage": {"input_tokens": 7, "output_tokens": 3}}, request_id="req_1", **step),
        _event(10, "request.finished", {"status": "completed"}, request_id="req_1", **step),
        _event(11, "tool.requested", {"name": "read", "requested_arguments": {"path": "a"}}, call_id="call_1",
               request_id="req_1", **RUN),
        _event(12, "tool.started", {}, call_id="call_1", request_id="req_1", **RUN),
        _event(13, "run.interrupted", {"reason": "user stop"}, event_id="evt_interrupt", **RUN),
    ]


@pytest.fixture
async def trajectory(trace_db, blobs):
    await add_meta(SESSION, USER)
    await add_trajectory(TRAJECTORY, SESSION, USER)
    return TRAJECTORY


def _service(blobs, **overrides):
    return ProjectionService(settings(**overrides), blob_store=blobs, metrics=Metrics())


async def _links() -> dict[str, list[int]]:
    async with trace_session() as db:
        rows = (await db.execute(select(TrajectoryRecordEvent.record_id, TrajectoryRecordEvent.seq)
                                 .where(TrajectoryRecordEvent.trajectory_id == TRAJECTORY))).all()
    links: dict[str, list[int]] = {}
    for record_id, seq in sorted(rows):
        links.setdefault(record_id, []).append(seq)
    return links


async def _head(events):
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        return trajectory, await state_at(db, trajectory)


def _contains_ref(value) -> bool:
    if is_ref(value):
        return True
    if isinstance(value, dict):
        return any(_contains_ref(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_ref(child) for child in value)
    return False


async def _fixture_trajectory(blobs, fixture, *, references, record_inline_bytes, checkpoint_interval=5):
    events = [{**event, "occurred_at": iso(event["occurred_at"])} for event in fixture["events"]]
    first = events[0]
    await add_meta(first["session_id"], first["user_id"])
    await add_trajectory(first["trajectory_id"], first["session_id"], first["user_id"])
    ingest = Ingest(blobs, **(REFERENCES if references else {}))
    service = ProjectionService(settings(record_inline_bytes=record_inline_bytes, checkpoint_interval=checkpoint_interval),
                                blob_store=blobs, metrics=Metrics())
    return events, first["trajectory_id"], ingest, service


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
@pytest.mark.parametrize("references", [False, True], ids=["inline", "references"])
async def test_state_at_every_watermark_equals_replay_of_the_original_events(trace_db, blobs, fixture_path, references):
    fixture = load_fixture(fixture_path)
    events, trajectory_id, ingest, service = await _fixture_trajectory(
        blobs, fixture, references=references, record_inline_bytes=64 if references else 16384)
    half = len(events) // 2
    await ingest.append(trajectory_id, events[:half])
    assert await project_all(service, trajectory_id, max_events=3) == half
    await archive(blobs, trajectory_id, half - 2)
    await ingest.append(trajectory_id, events[half:])
    assert await project_all(service, trajectory_id, max_events=4) == len(events) - half
    assert replay(events) == fixture["expected_state"]
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        assert (trajectory.projected_seq, trajectory.archived_seq) == (len(events), half - 2)
        assert trajectory.checkpoint_seq >= 5
        stored = (await db.scalars(select(TrajectoryRecord.data).where(TrajectoryRecord.trajectory_id == trajectory_id))).all()
        assert any(_contains_ref(record) for record in stored) is references
        for through in range(len(events) + 1):
            assert await state_at(db, trajectory, through) == replay(events[:through]), through
            checkpoint = await get_checkpoint(db, trajectory, through)
            if checkpoint is not None:
                assert checkpoint["state"] == replay(events[:int(checkpoint["through_seq"])])
        page = await read_events(db, trajectory, limit=2000)
        assert [event["data"] for event in page["events"]] == [event["data"] for event in events]
        assert [event["event_id"] for event in page["events"]] == [event["event_id"] for event in events]
        checkpoints = await db.scalar(select(func.count()).select_from(TrajectoryCheckpoint)
                                      .where(TrajectoryCheckpoint.trajectory_id == trajectory_id))
        assert checkpoints >= 1


RUN_LINKS = {"run:run_1": [1, 13], "step:step_1": [2, 13], "system:req_1": [3],
             "request:req_1": [3, 4, 5, 6, 7, 8, 9, 10], "assistant:req_1": [5, 6, 7, 8, 10],
             "tool:call_1": [11, 12, 13], "interrupt:evt_interrupt": [13]}


async def test_each_touched_record_is_written_once_per_batch_with_links_for_implicit_targets(trajectory, blobs):
    events = _run_events()
    await Ingest(blobs).append(TRAJECTORY, events)
    service = _service(blobs)
    writes = []
    original = service._write_records

    async def capture(db, trajectory_id, prepared, links):
        writes.append([record["record_id"] for record in prepared])
        await original(db, trajectory_id, prepared, links)
    service._write_records = capture
    assert await service.project(TRAJECTORY) == len(events)
    assert len(writes) == 1 and len(writes[0]) == len(set(writes[0])) == len(RUN_LINKS)
    # request.finished updates the assistant; the interruption closes the open tool and step.
    assert await _links() == RUN_LINKS
    trajectory, state = await _head(events)
    assert trajectory.projected_seq == len(events) and state == replay(events)
    assert state["records"]["tool:call_1"]["status"] == state["records"]["step:step_1"]["status"] == "unknown"


@pytest.mark.parametrize("batch", [1, 3, 4])
async def test_batch_boundaries_change_neither_records_nor_links(trajectory, blobs, batch):
    events = _run_events()
    await Ingest(blobs).append(TRAJECTORY, events)
    assert await project_all(_service(blobs), TRAJECTORY, max_events=batch) == len(events)
    assert await _links() == RUN_LINKS
    assert (await _head(events))[1] == replay(events)


async def test_large_record_values_become_references_that_reuse_stored_content(trajectory, blobs):
    notes = "x" * 300
    attachments = {"notes": notes, "table": [{"cell": "y" * 100}]}
    existing = await add_payload(blobs, TRAJECTORY, canonical(notes), first_seq=1, media_type=JSON)
    events = [_event(1, "input.accepted", {"text": "hello", "attachments": attachments}, message_id="msg_1"),
              _event(2, "input.injected", {"text": "again", "attachments": attachments}, message_id="msg_1")]
    ingest, service = Ingest(blobs), _service(blobs, record_inline_bytes=64)
    await ingest.append(TRAJECTORY, events[:1])
    assert await service.project(TRAJECTORY) == 1
    async with trace_session() as db:
        row = await db.get(TrajectoryRecord, (TRAJECTORY, "user:msg_1"))
        stored_bytes = (await db.get(SessionTrajectory, TRAJECTORY)).stored_bytes
        payloads = {item.payload_id: item for item in (await db.scalars(select(TrajectoryPayload))).all()}
    outer = row.data["data"]["attachments"]["$ref"]
    assert row.data["data"]["text"] == "hello" and row.summary["preview"] == "hello" and outer["kind"] == "value"
    stored = payloads[outer["payload_id"]]
    content = json.loads(decode_blob(blobs.objects[stored.storage_key], stored.encoding))
    # The nested value reuses the row stored before under a random id; new values get content-derived ids.
    assert content["notes"]["$ref"]["payload_id"] == existing.payload_id
    table = content["table"]["$ref"]
    assert table["payload_id"] == reference_id(TRAJECTORY, dedupe_key(table["sha256"], JSON, None, "blob"))
    assert outer["payload_id"] == reference_id(TRAJECTORY, dedupe_key(outer["sha256"], JSON, None, "blob"))
    assert stored_bytes == sum(item.stored_bytes for item in payloads.values() if item.payload_id != existing.payload_id)
    puts, metrics = blobs.puts, service.metrics.counters.get("blob_puts")
    await ingest.append(TRAJECTORY, events[1:])
    assert await service.project(TRAJECTORY) == 1
    async with trace_session() as db:
        assert await db.scalar(select(func.count()).select_from(TrajectoryPayload)) == len(payloads)
        assert (await db.get(SessionTrajectory, TRAJECTORY)).stored_bytes == stored_bytes
    # cell text, table row, table and attachments were uploaded once; the notes reused the stored row.
    assert (blobs.puts, service.metrics.counters.get("blob_puts")) == (puts, metrics) and metrics == 4
    assert (await _head(events))[1] == replay(events)


async def test_a_value_claimed_meanwhile_under_another_id_rolls_the_batch_back(trajectory, blobs, monkeypatch):
    value = "z" * 300
    events = [_event(1, "input.accepted", {"text": "hello", "notes": value}, message_id="msg_1")]
    await Ingest(blobs).append(TRAJECTORY, events)
    service = _service(blobs, record_inline_bytes=64)
    real, claimed = projection.ensure_payload_rows, []

    async def racing(db, trajectory_id, values, *, first_seq):
        if not claimed:
            # Another writer stores the same content under its own id and commits first.
            claimed.append(await add_payload(blobs, TRAJECTORY, canonical(value), first_seq=1, media_type=JSON))
        return await real(db, trajectory_id, values, first_seq=first_seq)
    monkeypatch.setattr(projection, "ensure_payload_rows", racing)
    assert await service.project(TRAJECTORY) == 0
    async with trace_session() as db:
        assert (await db.get(SessionTrajectory, TRAJECTORY)).projected_seq == 0
        assert await db.scalar(select(func.count()).select_from(TrajectoryRecord)) == 0
    assert await service.project(TRAJECTORY) == 1
    async with trace_session() as db:
        row = await db.get(TrajectoryRecord, (TRAJECTORY, "user:msg_1"))
    assert row.data["data"]["notes"]["$ref"]["payload_id"] == claimed[0].payload_id
    assert (await _head(events))[1] == replay(events)


def test_search_doc_joins_the_searchable_fields_within_4000_characters():
    record = {"kind": "tool", "record_id": "tool:call_1", "title": "bash", "preview": "ls -la", "result_preview": "total 0",
              "status_reason": "exit 1", "data": {"tool": "bash", "name": {"nested": True},
                                                  "error": {"message": "boom", "type": "ExitError", "code": 1}}}
    assert search_doc(record) == "tool tool:call_1 bash ls -la total 0 exit 1 bash boom ExitError 1"
    long = search_doc({"kind": "user", "record_id": "user:1", "preview": "a\x00b" + "c" * 5000, "data": {"error": "plain"}})
    assert len(long) == 4000 and "\x00" not in long and long.startswith("user user:1 ab")


async def test_summary_follows_statistics_status_model_gaps_and_event_time(trajectory, blobs):
    later = AT + timedelta(hours=1)
    events = [
        _event(1, "run.started", at=later, **RUN),
        _event(2, "request.started", {"model": "m" * 200}, request_id="req_1", **RUN),
        _event(3, "request.finished", {"status": "failed", "usage": {"input_tokens": 5, "output_tokens": 2}},
               request_id="req_1", **RUN),
        _event(4, "tool.requested", {"name": "bash"}, call_id="call_1", **RUN),
        _event(5, "permission.requested", {"permission_id": "perm_1"}, call_id="call_1", **RUN),
        _event(6, "recording.gap", {"phase": "dropped", "reason": "queue_overflow"}, run_id="run_other"),
        _event(7, "trajectory.renamed", {}, version=2),
    ]
    ingest, service = Ingest(blobs), _service(blobs)
    await ingest.append(TRAJECTORY, events)
    assert await service.project(TRAJECTORY) == 7
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        summary = await db.get(TrajectorySessionSummary, TRAJECTORY)
    totals = contribution(None)
    for record in replay(events)["records"].values():
        for key, value in contribution(record).items():
            totals[key] += value
    assert {key: summary.statistics[key] for key in totals} == totals
    assert (totals["request_count"], totals["error_count"], totals["tool_count"], totals["input_tokens"]) == (1, 1, 1, 5)
    assert summary.statistics["unsupported_events"] == [{"seq": "7", "type": "trajectory.renamed", "version": 2}]
    assert (summary.running_status, summary.model, summary.applied_seq) == ("waiting", "m" * 128, 7)
    assert summary.recording_status == trajectory.recording_status == "gap"
    # Event time, never projection clock time.
    assert summary.last_activity_at.replace(tzinfo=later.tzinfo) == later
    async with trace_session() as db:
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == TRAJECTORY).values(recording_status="paused"))
    more = [_event(8, "recording.gap", {"phase": "dropped", "reason": "queue_overflow"}),
            _event(9, "run.finished", {"status": "completed"}, **RUN)]
    await ingest.append(TRAJECTORY, more)
    assert await service.project(TRAJECTORY) == 2
    trajectory, state = await _head(events + more)
    async with trace_session() as db:
        summary = await db.get(TrajectorySessionSummary, TRAJECTORY)
    assert trajectory.recording_status == summary.recording_status == "paused" and summary.running_status == "idle"
    assert state == replay(events + more)


async def test_system_record_preload_matches_agent_and_source_session(trajectory, blobs):
    ids = {"agent_id": "agent_1", "run_id": "run_1"}
    events = [
        _event(1, "request.prepared", {"input": {"system": "parent prompt"}}, request_id="req_1", **ids),
        _event(2, "request.prepared", {"input": {"system": "child prompt"}}, request_id="req_2",
               source_session_id="session_child", **ids),
        _event(3, "request.prepared", {"input": {"system": "parent prompt"}}, request_id="req_3", **ids),
    ]
    await Ingest(blobs).append(TRAJECTORY, events)
    service = _service(blobs)
    assert (await service.project(TRAJECTORY, max_events=2), await service.project(TRAJECTORY, max_events=2)) == (2, 1)
    state = (await _head(events))[1]
    assert state == replay(events)
    # The same agent's prompt in the root session did not change; the child session's prompt is not compared.
    assert "system:req_3" not in state["records"] and "system:req_2" in state["records"]


async def test_interruptions_close_every_open_record_of_the_run(trajectory, blobs):
    events = [
        _event(1, "tool.requested", {"name": "read"}, call_id="call_q", **RUN),
        _event(2, "tool.finished", {"status": "queued"}, call_id="call_q", **RUN),
        _event(3, "input.accepted", {"text": "hi"}, message_id="msg_1", **RUN),
        _event(4, "run.interrupted", {"reason": "stop"}, event_id="evt_stop", **RUN),
    ]
    await Ingest(blobs).append(TRAJECTORY, events)
    service = _service(blobs)
    assert (await service.project(TRAJECTORY, max_events=3), await service.project(TRAJECTORY)) == (3, 1)
    state = (await _head(events))[1]
    assert state == replay(events) and state["records"]["tool:call_q"]["status"] == "unknown"
    assert (await _links())["tool:call_q"] == [1, 2, 4]


async def test_checkpoints_store_content_addressed_pages_and_reuse_unchanged_ones(trajectory, blobs):
    events = [_event(seq, "input.accepted", {"text": f"message {seq}"}, message_id=f"msg_{seq:03d}") for seq in range(1, 231)]
    ingest, service = Ingest(blobs), _service(blobs, checkpoint_interval=100)
    await ingest.append(TRAJECTORY, events[:120])
    assert await service.project(TRAJECTORY) == 120
    assert (await service.maybe_checkpoint(TRAJECTORY), await service.maybe_checkpoint(TRAJECTORY)) == (True, False)
    async with trace_session() as db:
        first = await db.get(TrajectoryCheckpoint, (TRAJECTORY, 120))
    first_pages = [page["$payload"]["payload_id"] for page in first.state["record_pages"]]
    assert len(first_pages) == 2 and first.state["records"] == {}
    puts = blobs.puts
    await ingest.append(TRAJECTORY, events[120:])
    assert await service.project(TRAJECTORY) == 110
    assert await service.maybe_checkpoint(TRAJECTORY) is True
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        second = await db.get(TrajectoryCheckpoint, (TRAJECTORY, 230))
        pages = [page["$payload"]["payload_id"] for page in second.state["record_pages"]]
        assert trajectory.checkpoint_seq == 230 and len(pages) == 3
        # The unchanged first page is neither uploaded nor stored again.
        assert pages[0] == first_pages[0] and pages[1] != first_pages[1] and blobs.puts - puts == 2
        checkpoint = await get_checkpoint(db, trajectory, 230)
        assert checkpoint["state"] == replay(events) and checkpoint["digest"] == second.digest
        assert (await get_checkpoint(db, trajectory, 229))["through_seq"] == "120"
        page = await db.get(TrajectoryPayload, pages[1])
    reset_blob_cache()
    blobs.objects[page.storage_key] = b"damaged"
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        with pytest.raises(CorruptContent):
            await get_checkpoint(db, trajectory, 230)
        await db.execute(update(TrajectoryCheckpoint).where(TrajectoryCheckpoint.through_seq == 120).values(digest="0" * 64))
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, TRAJECTORY)
        with pytest.raises(CorruptContent, match="digest"):
            await get_checkpoint(db, trajectory, 150)


async def test_run_once_projects_lagging_trajectories_and_isolates_a_failing_one(trace_db, blobs):
    others = {"trj_bad": "session_bad", "trj_gone": "session_gone"}
    for trajectory_id, session_id in {TRAJECTORY: SESSION, **others}.items():
        await add_meta(session_id, USER)
        await add_trajectory(trajectory_id, session_id, USER)
    events = _run_events()
    ingest = Ingest(blobs)
    await ingest.append(TRAJECTORY, events)
    for trajectory_id, session_id in others.items():
        await ingest.append(trajectory_id, [{**_event(1, "input.accepted", {"text": "x"}), "trajectory_id": trajectory_id,
                                             "session_id": session_id, "source_session_id": session_id,
                                             "event_id": f"evt_{trajectory_id}"}])
    missing = {"$payload": {"payload_id": "pld_missing", "sha256": "0" * 64, "size_bytes": 1, "media_type": JSON,
                            "availability": "available"}}
    async with trace_session() as db:
        await db.execute(update(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == "trj_bad").values(data=missing))
        await db.execute(update(SessionTrajectory).where(SessionTrajectory.id == "trj_gone").values(deleted_at=AT))
    metrics = Metrics()
    service = ProjectionService(settings(), blob_store=blobs, metrics=metrics)
    assert await service.run_once() == len(events)
    assert await service.run_once() == 0
    async with trace_session() as db:
        projected = {row.id: row.projected_seq for row in (await db.scalars(select(SessionTrajectory))).all()}
    assert projected == {TRAJECTORY: len(events), "trj_bad": 0, "trj_gone": 0}
    assert metrics.gauges["projection_lag_events"] == 1


async def test_passes_rotate_past_failing_trajectories_that_back_off(trace_db, blobs, monkeypatch):
    monkeypatch.setattr(projection, "PASS_TRAJECTORIES", 2)
    missing = {"$payload": {"payload_id": "pld_missing", "sha256": "0" * 64, "size_bytes": 1, "media_type": JSON,
                            "availability": "available"}}
    names = ["trj_bad_1", "trj_bad_2", "trj_good"]
    ingest = Ingest(blobs)
    for trajectory_id in names:
        session_id = f"session_{trajectory_id}"
        await add_meta(session_id, USER)
        await add_trajectory(trajectory_id, session_id, USER)
        await ingest.append(trajectory_id, [{**_event(1, "input.accepted", {"text": "x"}), "trajectory_id": trajectory_id,
                                             "session_id": session_id, "source_session_id": session_id,
                                             "event_id": f"evt_{trajectory_id}"}])
    async with trace_session() as db:
        await db.execute(update(TrajectoryEvent).where(TrajectoryEvent.trajectory_id.in_(names[:2])).values(data=missing))
    service = _service(blobs)
    attempts, project = [], service.project

    async def counted(trajectory_id, max_events=None):
        attempts.append(trajectory_id)
        return await project(trajectory_id, max_events)
    service.project = counted
    # The failing trajectories fill the first pass; the next one continues after them.
    assert (await service.run_once(), await service.run_once()) == (0, 1)
    assert attempts == names
    # Until their retry is due they are skipped without taking a visit.
    assert await service.run_once() == 0 and attempts == names
    service._retry = {key: (0.0, failures) for key, (_, failures) in service._retry.items()}
    assert await service.run_once() == 0 and attempts == names + names[:2]
    assert service._retry[("project", "trj_bad_1")][1] == 2
    async with trace_session() as db:
        assert (await db.get(SessionTrajectory, "trj_good")).projected_seq == 1
    # Checkpoint failures never escape maybe_checkpoint, and back off as well.
    builds = []

    async def broken(trajectory_id, **kwargs):
        builds.append(trajectory_id)
        raise ConnectionError("blob store unavailable")
    monkeypatch.setattr(projection, "build_checkpoint", broken)
    assert (await service.maybe_checkpoint("trj_good"), await service.maybe_checkpoint("trj_good")) == (False, False)
    assert builds == ["trj_good"] and service._retry[("checkpoint", "trj_good")][1] == 1


async def test_a_checkpoint_row_stored_elsewhere_moves_checkpoint_seq(trajectory, blobs):
    events = [_event(seq, "input.accepted", {"text": f"message {seq}"}, message_id=f"msg_{seq}") for seq in range(1, 4)]
    await Ingest(blobs).append(TRAJECTORY, events)
    service = _service(blobs, checkpoint_interval=3)
    assert await service.project(TRAJECTORY) == 3
    async with trace_session() as db:
        db.add(TrajectoryCheckpoint(trajectory_id=TRAJECTORY, through_seq=3, projector_version=1, state={},
                                    digest="0" * 64, created_at=AT))
    assert await service.maybe_checkpoint(TRAJECTORY) is False
    async with trace_session() as db:
        assert (await db.get(SessionTrajectory, TRAJECTORY)).checkpoint_seq == 3
    # No longer a candidate: an idle pass does nothing.
    assert await service.run_once() == 0


async def test_record_values_equal_to_values_ingested_later_are_visible_where_projected(trajectory, blobs):
    # The output chunks add up to the value that tool.finished carries at seq 4, which ingest
    # stored first; a record projected through seq 3 references that row.
    full = "a" * 30 + "b" * 30
    events = [
        _event(1, "tool.requested", {"name": "bash"}, call_id="call_1", **RUN),
        _event(2, "tool.output", {"chunk_index": 0, "output": full[:30]}, call_id="call_1", **RUN),
        _event(3, "tool.output", {"chunk_index": 1, "output": full[30:]}, call_id="call_1", **RUN),
        _event(4, "tool.finished", {"status": "completed", "output": full}, call_id="call_1", **RUN),
    ]
    await Ingest(blobs, inline_bytes=40).append(TRAJECTORY, events)
    service = _service(blobs, record_inline_bytes=48, checkpoint_interval=3)
    assert await service.project(TRAJECTORY, max_events=3) == 3
    async with trace_session() as db:
        [row] = (await db.scalars(select(TrajectoryPayload))).all()
        record = await db.get(TrajectoryRecord, (TRAJECTORY, "tool:call_1"))
        assert record.data["data"]["output"]["$ref"]["payload_id"] == row.payload_id and row.first_seq == 3
        trajectory_row = await db.get(SessionTrajectory, TRAJECTORY)
        assert await state_at(db, trajectory_row, 3) == replay(events[:3])
    assert await service.maybe_checkpoint(TRAJECTORY) is True
    assert await service.project(TRAJECTORY) == 1
    async with trace_session() as db:
        trajectory_row = await db.get(SessionTrajectory, TRAJECTORY)
        assert (await get_checkpoint(db, trajectory_row, 3))["state"] == replay(events[:3])
        for through in range(len(events) + 1):
            assert await state_at(db, trajectory_row, through) == replay(events[:through]), through
