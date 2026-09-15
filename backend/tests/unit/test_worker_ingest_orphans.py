"""Blob objects of batches that do not commit as planned (SPEC §8.3, §8.4, §8.11): ids, GC entries, degrade."""
import hashlib
import re

from trajectory.store.models import TrajectoryGcQueue, TrajectoryPayload
from trajectory.types import canonical
from trajectory.worker.ingest import IngestService, session_trajectory_id
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
