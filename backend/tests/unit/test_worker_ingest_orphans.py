"""Blob objects of batches that do not commit as planned (SPEC §8.3, §8.4, §8.11): ids, GC entries, degrade."""
import hashlib
import re

from trajectory.store.models import TrajectoryGcQueue, TrajectoryPayload
from trajectory.types import canonical
from trajectory.worker.content import BLOB_UNAVAILABLE
from trajectory.worker.ingest import MAX_UPLOAD_ATTEMPTS, IngestService, session_trajectory_id
from tests.unit.test_worker_ingest import event, events_of, harness, rows, settings, trace_db  # noqa: F401

SYSTEM = "S" * 3000


def _sha(value) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _prefixes(store) -> set[str]:
    return {key.split("/")[1] for key in store.objects}


def test_the_trajectory_id_of_a_root_session_is_derived_from_the_session():
    identifier = session_trajectory_id("ses_1")
    assert re.fullmatch(r"trj_[0-9a-f]{32}", identifier)
    assert identifier == "trj_" + hashlib.sha256(b"openbox-trajectory\0ses_1").hexdigest()[:32]
    assert session_trajectory_id("ses_2") != identifier


async def test_blobs_uploaded_before_a_failed_commit_belong_to_the_trajectory_created_after_a_restart(
        harness, monkeypatch):
    harness.writer.events(event("request.prepared", session="ses_p", event_id="p1", data={"input": {"system": SYSTEM}}))
    commit = IngestService._commit
    calls = []

    async def failing_once(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("commit failed")
        return await commit(self, *args, **kwargs)

    monkeypatch.setattr(IngestService, "_commit", failing_once)
    assert (await harness.run())["failed_batches"] == 1
    assert _prefixes(harness.store) == {session_trajectory_id("ses_p")}
    harness.configure()  # a restart: nothing the worker kept in memory survives
    assert (await harness.run())["events"] == 1
    trajectory, _ = await events_of("ses_p")
    assert trajectory.id == session_trajectory_id("ses_p") and _prefixes(harness.store) == {trajectory.id}
    [payload] = await rows(TrajectoryPayload)
    assert payload.storage_key == f"trajectories/{trajectory.id}/blobs/{_sha(SYSTEM)}"
    assert await rows(TrajectoryGcQueue) == []


def _release(service):
    """Let every batch and file that backs off retry on the next pass."""
    for held in [*service._backoff.values(), *service._failures.values()]:
        held.next_at = 0.0


async def test_only_references_to_objects_that_kept_failing_become_not_recorded(harness):
    tools = [{"name": "t", "description": "D" * 3000}]
    tools_sha = _sha(tools)

    def failing_tools(key):
        if key.endswith(tools_sha):
            raise ConnectionError("the tools upload fails")

    harness.store.faults["put"] = failing_tools
    harness.writer.events(event("request.prepared", session="ses_a", request_id="r1", event_id="big",
                                data={"model": "m", "input": {"system": SYSTEM, "tools": tools}}))
    for _ in range(MAX_UPLOAD_ATTEMPTS):
        _release(harness.service)
        await harness.run()
    trajectory, stored = await events_of("ses_a")
    big = next(row for row in stored if row.event_id == "big")
    system_key = f"trajectories/{trajectory.id}/blobs/{_sha(SYSTEM)}"
    assert big.data["input"]["tools"] == BLOB_UNAVAILABLE
    assert big.data["input"]["system"]["$ref"]["sha256"] == _sha(SYSTEM)
    [payload] = await rows(TrajectoryPayload)
    assert (payload.storage_key, payload.availability) == (system_key, "available")
    assert sorted(harness.store.objects) == [system_key] and await rows(TrajectoryGcQueue) == []
    gap = stored[-1]
    assert (gap.type, gap.data["reason"], gap.data["event_ids"]) == ("recording.gap", "blob_store_unavailable", ["big"])
    assert gap.data["dropped_bytes"] == len(canonical(tools)) and harness.service._backoff == {}


async def _queued() -> list[tuple[str, str, str]]:
    return [(row.kind, row.storage_key, row.reason) for row in await rows(TrajectoryGcQueue)]


async def test_an_object_stored_before_a_degrade_that_no_row_references_is_queued_for_gc(harness):
    harness.configure(inline_bytes=4096)
    tools = [{"name": "t", "description": "D" * 3000}]
    tools_sha = _sha(tools)

    def failing_tools(key):
        if key.endswith(tools_sha):
            raise ConnectionError("the tools upload fails")

    harness.store.faults["put"] = failing_tools
    fields = {f"k{index}": "v" * 90 for index in range(60)}
    harness.writer.events(event("request.prepared", session="ses_w", request_id="r1", event_id="wide",
                                data={"model": "m", "input": {"system": SYSTEM, "tools": tools}, **fields}))
    await harness.run()
    # Stored so far: the system object and the whole data as first planned, with a reference to the tools object.
    [whole] = [key for key in harness.store.objects if not key.endswith(_sha(SYSTEM))]
    for _ in range(MAX_UPLOAD_ATTEMPTS - 1):
        _release(harness.service)
        await harness.run()
    _, stored = await events_of("ses_w")
    wide = next(row for row in stored if row.event_id == "wide")
    referenced = {row.storage_key for row in await rows(TrajectoryPayload)}
    assert wide.data["$payload"]["availability"] == "available" and whole not in referenced
    assert referenced == set(harness.store.objects) - {whole} and len(referenced) == 2
    assert await _queued() == [("key", whole, "content_not_referenced")]


async def test_objects_of_an_event_its_transaction_drops_are_queued_for_gc(harness):
    writer = harness.writer
    writer.file([
        writer.line("control", {"type": "session.meta", "session": {"id": "ses_o", "user_id": "u2",
                                                                      "updated_at": "2026-09-14T08:00:00.000Z"}}),
        writer.line("event", event("request.prepared", session="ses_o", request_id="r1", event_id="r1",
                                   data={"model": "m", "input": {"system": SYSTEM}}))])
    result = await harness.run()
    key = f"trajectories/{session_trajectory_id('ses_o')}/blobs/{_sha(SYSTEM)}"
    # Planned and uploaded, then dropped by the transaction: since the control the session belongs to another user.
    assert result["ownership_drops"] == 1 and key in harness.store.objects
    assert (await events_of("ses_o"))[0] is None and await rows(TrajectoryPayload) == []
    assert await _queued() == [("key", key, "content_not_referenced")] and harness.service._stored == {}


async def test_objects_stored_by_a_quarantined_batch_are_queued_for_gc(harness, monkeypatch):
    harness.configure(ingest_max_batch_failures=2)

    async def failing_commit(self, *args, **kwargs):
        raise RuntimeError("value out of range for type integer")

    monkeypatch.setattr(IngestService, "_commit", failing_commit)
    path = harness.writer.events(event("request.prepared", session="ses_q", request_id="r1", event_id="r1",
                                       data={"model": "m", "input": {"system": SYSTEM}}))
    await harness.run()
    assert await _queued() == [] and harness.service._stored
    _release(harness.service)
    result = await harness.run()
    key = f"trajectories/{session_trajectory_id('ses_q')}/blobs/{_sha(SYSTEM)}"
    assert result["quarantined_files"] == 1 and not path.exists() and key in harness.store.objects
    assert await _queued() == [("key", key, "content_not_referenced")] and harness.service._stored == {}


async def test_an_object_an_earlier_attempt_stored_is_stored_again_once_deleted(harness):
    tools = [{"name": "t", "description": "D" * 3000}]
    tools_sha = _sha(tools)
    failures = ["tools"]

    def tools_fail_once(key):
        if key.endswith(tools_sha) and failures:
            failures.pop()
            raise ConnectionError("the tools upload fails once")

    harness.store.faults["put"] = tools_fail_once
    harness.writer.events(event("request.prepared", session="ses_d", request_id="r1", event_id="r1",
                                data={"model": "m", "input": {"system": SYSTEM, "tools": tools}}))
    await harness.run()
    system_key = f"trajectories/{session_trajectory_id('ses_d')}/blobs/{_sha(SYSTEM)}"
    # A GC entry for the key, queued by another batch and processed between the attempts, deleted the object.
    del harness.store.objects[system_key]
    _release(harness.service)
    assert (await harness.run())["events"] == 1
    [row] = await rows(TrajectoryPayload, TrajectoryPayload.storage_key == system_key)
    assert row.availability == "available" and system_key in harness.store.objects
