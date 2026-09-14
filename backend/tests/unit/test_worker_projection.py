"""Projection worker (SPEC 8.8) against a SQLite trace database and an in-memory blob store."""
import pytest
from sqlalchemy import func, select

from tests.unit.test_worker_projection_support import (FIXTURES, REFERENCES, Ingest, Metrics, add_meta,  # noqa: F401
    add_trajectory, archive, blobs, load_fixture, project_all, settings, trace_db)
from trajectory.payload import is_ref
from trajectory.projector import replay
from trajectory.repository import get_checkpoint, read_events, state_at
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryCheckpoint, TrajectoryRecord
from trajectory.types import iso
from trajectory.worker.projection import ProjectionService


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
