"""Projection, reads and record search on a real PostgreSQL trace database (SPEC 8.8-8.9).

Skipped unless TRAJECTORY_TRACE_TEST_DATABASE_URL names a local disposable database whose name starts with
``openbox_trace_test_``. Each test drops and recreates that database with the trace schema (pg_trgm,
partitioned events).
"""
import os
from datetime import timedelta

import pytest
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.unit.test_trace_repository import conversation, recorded, statements
from tests.unit.test_worker_projection_support import (AT, FIXTURES, REFERENCES, Ingest, Metrics, add_meta,  # noqa: F401
    add_trajectory, archive, blobs, load_fixture, project_all, settings)
from trajectory.projector import replay
from trajectory.repository import get_record, get_trajectory, list_sessions, read_events, search, state_at
from trajectory.store.database import TraceBase, close_trace_engine, init_trace_engine, trace_dialect, trace_session
from trajectory.store.models import TrajectoryRecord
from trajectory.types import iso
from trajectory.worker.projection import ProjectionService

URL = os.environ.get("TRAJECTORY_TRACE_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")


async def _recreate(url: str) -> None:
    parsed = make_url(url)
    if parsed.host not in {"localhost", "127.0.0.1"} or not (parsed.database or "").startswith("openbox_trace_test_"):
        raise ValueError("PostgreSQL trace tests require a local disposable openbox_trace_test_* database")
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    finally:
        await admin.dispose()


@pytest.fixture
async def pg_trace(blobs):
    await close_trace_engine()
    await _recreate(URL)
    engine = init_trace_engine(URL)
    async with engine.begin() as connection:
        await connection.run_sync(TraceBase.metadata.create_all)
    assert trace_dialect() == "postgresql"
    yield engine
    await close_trace_engine()


def _service(blobs, **overrides):
    return ProjectionService(settings(**overrides), blob_store=blobs, metrics=Metrics())


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda path: path.stem)
async def test_state_at_every_watermark_equals_replay_on_jsonb_and_partitions(pg_trace, blobs, fixture_path):
    # JSONB does not keep key order: previews of structured values must come from the ingest hints.
    fixture = load_fixture(fixture_path)
    events = [{**event, "occurred_at": iso(event["occurred_at"])} for event in fixture["events"]]
    first = events[0]
    await add_meta(first["session_id"], first["user_id"])
    await add_trajectory(first["trajectory_id"], first["session_id"], first["user_id"])
    ingest, service = Ingest(blobs, **REFERENCES), _service(blobs, record_inline_bytes=64, checkpoint_interval=5)
    half = len(events) // 2
    await ingest.append(first["trajectory_id"], events[:half])
    await project_all(service, first["trajectory_id"], max_events=3)
    await archive(blobs, first["trajectory_id"], half - 2)
    await ingest.append(first["trajectory_id"], events[half:])
    await project_all(service, first["trajectory_id"], max_events=4)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, first["session_id"]))[1]
        assert trajectory.projected_seq == len(events)
        for through in range(len(events) + 1):
            assert await state_at(db, trajectory, through) == replay(events[:through]), through
        page = await read_events(db, trajectory, limit=2000)
    assert [event["data"] for event in page["events"]] == [event["data"] for event in events]


async def test_search_uses_the_trigram_index_escapes_wildcards_and_finds_cjk(pg_trace, blobs):
    extra = [("input.accepted", {"text": "请查看季度报告的第三部分"}, {"message_id": "msg_cjk"})]
    await recorded(blobs, "trj_1", "s1", extra=extra)
    async with trace_session() as db:
        trajectory = (await get_trajectory(db, "s1"))[1]
        assert [item["record_id"] for item in (await search(db, trajectory, q="GREP"))["items"]] == ["tool:call_1"]
        assert [item["record_id"] for item in (await search(db, trajectory, q="季度报告"))["items"]] == ["user:msg_cjk"]
        assert [item["record_id"] for item in (await search(db, trajectory, q="50%_d"))["items"]] == ["tool:call_1"]
        assert (await search(db, trajectory, q="5_%"))["items"] == []
        hit = (await search(db, trajectory, q="报告"))["items"][0]
        assert hit["seq"] == "13" and "季度报告" in hit["preview"]
        await db.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(row[0] for row in (await db.execute(text(
            "EXPLAIN SELECT record_id FROM trajectory_records WHERE search_doc ILIKE '%grep%'"))).all())
    assert "ix_trajectory_records_search_trgm" in plan


async def test_record_detail_statements_do_not_grow_with_records_on_postgresql(pg_trace, blobs):
    counts = {}
    for trajectory_id, fillers in (("trj_small", 10), ("trj_large", 10_000)):
        session_id = f"s_{trajectory_id}"
        events = await recorded(blobs, trajectory_id, session_id)
        async with trace_session() as db:
            await db.execute(insert(TrajectoryRecord), [
                {"trajectory_id": trajectory_id, "record_id": f"user:filler_{index:05d}", "kind": "user", "status": "accepted",
                 "start_seq": 1, "applied_seq": 1, "projector_version": 1, "search_doc": "user",
                 "data": {"record_id": f"user:filler_{index:05d}"}, "summary": {"record_id": f"user:filler_{index:05d}"}}
                for index in range(fillers)])
        async with trace_session() as db:
            trajectory = (await get_trajectory(db, session_id))[1]
            with statements(pg_trace) as seen:
                detail = await get_record(db, trajectory, "assistant:req_1")
            counts[trajectory_id] = len(seen)
        assert detail["record"]["blocks"] == replay(events)["records"]["assistant:req_1"]["blocks"]
        assert [event["seq"] for event in detail["record"]["events"]] == ["3", "4", "5", "6"]
    assert counts["trj_small"] == counts["trj_large"] <= 6


async def test_session_cursors_keep_database_microseconds_and_break_ties_by_id(pg_trace, blobs):
    start = AT + timedelta(microseconds=123457)
    for session_id in ("s_a", "s_b", "s_c"):
        await recorded(blobs, f"trj_{session_id}", session_id, start=start, user=session_id == "s_a", workspace=session_id == "s_a")
    async with trace_session() as db:
        first = await list_sessions(db, limit=2)
        second = await list_sessions(db, limit=2, cursor=first["next_cursor"])
        ascending = await list_sessions(db, limit=1, sort="last_activity_asc")
        after = await list_sessions(db, limit=5, sort="last_activity_asc", cursor=ascending["next_cursor"])
    assert [item["session_id"] for item in first["items"] + second["items"]] == ["s_c", "s_b", "s_a"]
    assert [item["session_id"] for item in ascending["items"] + after["items"]] == ["s_a", "s_b", "s_c"]
    assert conversation("trj_x", "s_x", "user_a")[0]["occurred_at"].endswith("Z")
