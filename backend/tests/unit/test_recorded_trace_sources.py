"""Recorded sources survive reparenting, archival and partial legacy backfill."""
import pytest
from sqlalchemy import select, update

from api.internal import TrajectoryAudienceQuery, trajectory_session_audience
from db.models.session import Session
from tests.unit.test_worker_app_harness import (PREFIX, PRIVATE, SESSION, admin_env, auth_stores,  # noqa: F401
    business_db, internal_backend, token, trace_url, worker)
from tests.unit.test_worker_ingest import (event, events_of, harness, rows, settings, trace_db)  # noqa: F401
from tests.unit.test_worker_projection_support import archive
from tests.unit.test_worker_ws import ticket_for
from trajectory import repository
from trajectory.store.database import trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryEvent, TrajectorySessionSource
from trajectory.types import CorruptContent, now
from trajectory.worker.source_index import backfill_sources, index_events

real_list_sessions = repository.list_sessions


async def capture_child(worker):
    stamp = now()
    async with worker.business.begin() as db:
        db.add(Session(id="recorded_child", user_id="a", workspace_id="ws_a", project_id="project_a",
            parent_id="session_a_1", visibility="workspace", created_at=stamp, updated_at=stamp))
    async with trace_session() as db:
        await db.execute(update(TrajectoryEvent).where(TrajectoryEvent.trajectory_id == "trj_a1",
            TrajectoryEvent.seq == 2).values(source_session_id="recorded_child"))
        (await db.get(SessionTrajectory, "trj_a1")).audience_seq = 0
    assert await backfill_sources("trj_a1", blob_store=worker.blob, batch_events=200) == 3


async def detach_private(worker):
    async with worker.business.begin() as db:
        child = await db.get(Session, "recorded_child")
        child.parent_id, child.visibility = None, "private"


async def test_recorded_child_refuses_reads_list_export_and_client_omitted_sources_after_reparenting(worker, monkeypatch):
    await capture_child(worker)
    assert (await worker.client.get(SESSION)).status_code == 200
    await detach_private(worker)
    # Reproduce why checking only current ancestry was insufficient.
    target = {"session_id": "session_a_1", "user_id": "a", "workspace_id": "ws_a"}
    assert (await trajectory_session_audience(TrajectoryAudienceQuery(user_id="admin", targets=[target])))["allowed"]
    for path in ("", "/events", "/records", "/checkpoint", "/payloads/pld_media", "/exports/exp_ready/download"):
        response = await worker.client.get(SESSION + path)
        assert response.status_code == 404 and PRIVATE not in response.content
    assert (await worker.client.post(SESSION + "/export", json={})).status_code == 404
    # The public endpoint must obtain historical bindings itself, ignoring client omissions/forgeries.
    for sources in ([], [{"session_id": "session_b_1", "user_id": "b", "workspace_id": "ws_b"}]):
        response = await worker.client.post(PREFIX + "/audience", json={"targets": [{**target, "sources": sources}]})
        assert response.json() == {"version": 1, "allowed": []}
    monkeypatch.setattr(repository, "list_sessions", real_list_sessions)
    assert [row["session_id"] for row in (await worker.client.get(PREFIX + "/sessions")).json()["items"]] == ["session_b_1"]
    assert (await worker.client.get(PREFIX + "/sessions/session_b_1")).status_code == 200


async def test_idle_subscription_rechecks_recorded_sources_without_current_ancestry(worker):
    await capture_child(worker)
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert (await receive())["type"] == "subscribed"
        await detach_private(worker)
        assert await receive(timeout=3) == {"type": "error", "data": {
            "code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}


async def test_original_source_is_rechecked_after_a_slow_payload_read(worker):
    await capture_child(worker)
    async def revoke(_key):
        await detach_private(worker)
    worker.blob.get_hook = revoke
    response = await worker.client.get(SESSION + "/payloads/pld_media")
    assert response.status_code == 404 and PRIVATE not in response.content


async def test_a_legacy_callback_cannot_silently_ignore_recorded_source_bindings(worker, monkeypatch):
    await capture_child(worker)
    await detach_private(worker)
    async def old_backend(viewer, targets):
        return {"version": 1, "user_id": viewer, "allowed": [target["session_id"] for target in targets]}
    monkeypatch.setattr(worker.http_backend, "session_audience", old_backend)
    response = await worker.client.get(SESSION)
    assert response.status_code == 503 and PRIVATE not in response.content


async def test_legacy_recordings_wait_for_complete_source_backfill_before_reading(worker):
    async with trace_session() as db:
        (await db.get(SessionTrajectory, "trj_a1")).audience_seq = 0
    assert (await worker.client.get(SESSION)).status_code == 404
    assert await backfill_sources("trj_a1", blob_store=worker.blob, batch_events=1) == 1
    assert (await worker.client.get(SESSION)).status_code == 404
    assert await backfill_sources("trj_a1", blob_store=worker.blob, batch_events=200) == 2
    assert (await worker.client.get(SESSION)).status_code == 200


async def test_ingest_certifies_sources_atomically_and_preserves_conflicting_original_scopes(harness):
    harness.writer.events(event(event_id="parent"), event(event_id="child", source_session_id="child"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    assert trajectory.audience_seq == trajectory.committed_seq == 3
    sources = await rows(TrajectorySessionSource)
    assert {(row.session_id, row.user_id, row.workspace_id) for row in sources} == {
        ("ses_1", "u1", "ws_1"), ("child", "u1", "ws_1")}
    harness.writer.events(event(event_id="child", source_session_id="child"),
                          event(event_id="moved", source_session_id="child", workspace_id="ws_other"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    assert trajectory.audience_seq == trajectory.committed_seq == 4
    assert {(row.session_id, row.workspace_id) for row in await rows(TrajectorySessionSource)} == {
        ("ses_1", "ws_1"), ("child", "ws_1"), ("child", "ws_other")}


async def test_live_appends_do_not_skip_an_unindexed_legacy_prefix(harness):
    harness.writer.events(event(event_id="old", source_session_id="old_child"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    async with trace_session() as db:
        (await db.get(SessionTrajectory, trajectory.id)).audience_seq = 0
    harness.writer.events(event(event_id="new", source_session_id="new_child"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    assert trajectory.audience_seq == 0 and trajectory.committed_seq == 3
    assert await backfill_sources(trajectory.id, blob_store=harness.store, batch_events=1) == 1
    harness.writer.events(event(event_id="later", source_session_id="later_child"))
    await harness.run()
    assert (await events_of("ses_1"))[0].audience_seq == 1
    assert await backfill_sources(trajectory.id, blob_store=harness.store, batch_events=200) == 3
    assert (await events_of("ses_1"))[0].audience_seq == 4
    assert {row.session_id for row in await rows(TrajectorySessionSource)} == {
        "ses_1", "old_child", "new_child", "later_child"}


@pytest.mark.parametrize("corrupt", [False, True])
async def test_backfill_reads_verified_archived_events_and_never_certifies_missing_bytes(harness, corrupt):
    harness.writer.events(event(source_session_id="archived_child"))
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    await archive(harness.store, trajectory.id, trajectory.committed_seq)
    async with trace_session() as db:
        (await db.get(SessionTrajectory, trajectory.id)).audience_seq = 0
    if corrupt:
        from trajectory.repository import reset_segment_cache
        reset_segment_cache()
        async def missing(_key):
            raise FileNotFoundError("test archived segment")
        harness.store.get = missing
        with pytest.raises((FileNotFoundError, CorruptContent)):
            await backfill_sources(trajectory.id, blob_store=harness.store, batch_events=200)
        assert (await events_of("ses_1"))[0].audience_seq == 0
    else:
        assert not await rows(TrajectoryEvent)
        assert await backfill_sources(trajectory.id, blob_store=harness.store, batch_events=200) == 2
        assert (await events_of("ses_1"))[0].audience_seq == 2
        assert "archived_child" in {row.session_id for row in await rows(TrajectorySessionSource)}


async def test_source_index_and_prefix_roll_back_together(harness):
    harness.writer.events(event())
    await harness.run()
    trajectory, _ = await events_of("ses_1")
    with pytest.raises(RuntimeError, match="rollback"):
        async with trace_session() as db:
            row = await db.get(SessionTrajectory, trajectory.id)
            await index_events(db, [{"trajectory_id": row.id, "seq": 3, "source_session_id": "rolled_back",
                                    "user_id": row.user_id}], {row.id: row})
            raise RuntimeError("rollback")
    assert (await events_of("ses_1"))[0].audience_seq == 2
    assert "rolled_back" not in {row.session_id for row in await rows(TrajectorySessionSource)}
