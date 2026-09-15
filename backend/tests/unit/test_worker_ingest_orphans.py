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
    for held in service._backoff.values():
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
