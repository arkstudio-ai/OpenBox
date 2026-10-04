"""Real worker HTTP/WS and business SQL boundaries for private assistant traces."""
import asyncio
from datetime import timedelta

import pytest

from bus import bus
from db.models.session import Session
from db.models.user import User
from db.models.workspace import WorkspaceMember
from tests.unit.test_worker_app_harness import (PREFIX, PRIVATE, SESSION, SYSTEM_SHA, admin_env, auth_stores,  # noqa: F401
    business_db, internal_backend, token, trace_url, worker)
from tests.unit.test_worker_ws import ticket_for
from tests.unit.test_worker_archive_support import FakeMetrics, worker_settings
from trajectory import repository
from trajectory.auth import LocalBackend
from trajectory.export import ExportService
from trajectory.store.database import trace_session
from trajectory.store.models import TrajectoryExport, TrajectoryMetaSession
from trajectory.types import now

real_list_sessions = repository.list_sessions


async def make_private(worker, session_id="session_a_1"):
    async with worker.business.begin() as db:
        (await db.get(Session, session_id)).visibility = "private"


@pytest.mark.parametrize("mode", ["http", "embedded"])
async def test_all_trace_reads_exports_and_subscriptions_reject_private_sessions_despite_stale_metadata(worker, monkeypatch, mode):
    if mode == "embedded":
        monkeypatch.setattr("trajectory.audience.get_backend", lambda: LocalBackend())
    await make_private(worker)
    paths = ["", "/events", "/records", "/records/request:req_a", "/checkpoint", "/search?q=race",
             "/payloads/pld_media", "/payloads/pld_media?meta=1", f"/blobs/{SYSTEM_SHA}",
             "/exports/exp_ready", "/exports/exp_ready/download"]
    for path in paths:
        response = await worker.client.get(SESSION + path)
        assert response.status_code == 404, (path, response.text)
        assert PRIVATE not in response.content and response.headers["cache-control"] == "no-store"
    assert (await worker.client.post(SESSION + "/export", json={})).status_code == 404
    listing = (await worker.client.get(PREFIX + "/sessions")).json()
    assert [row["session_id"] for row in listing["items"]] == ["session_b_1"]
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert await receive() == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}
    # Unrelated shared diagnostics and the original stored evidence survive.
    assert (await worker.client.get(PREFIX + "/sessions/session_b_1/events")).status_code == 200
    async with trace_session() as db:
        assert not (await db.get(TrajectoryMetaSession, "session_a_1")).is_deleted


async def test_private_owner_keeps_admin_diagnostics_until_current_membership_is_removed(worker):
    await make_private(worker)
    async with worker.business.begin() as db:
        (await db.get(User, "a")).role = "admin"
    headers = {"Authorization": f"Bearer {token('a')}"}
    assert (await worker.client.get(SESSION, headers=headers)).status_code == 200
    assert (await worker.client.get(SESSION + "/payloads/pld_media", headers=headers)).content == PRIVATE
    async with worker.business.begin() as db:
        (await db.get(WorkspaceMember, ("ws_a", "a"))).status = "removed"
    assert (await worker.client.get(SESSION, headers=headers)).status_code == 404
    assert (await worker.client.get(PREFIX + "/sessions/session_b_1", headers=headers)).status_code == 200


@pytest.mark.parametrize("path", ["/payloads/pld_media", "/exports/exp_ready/download", f"/blobs/{SYSTEM_SHA}"])
async def test_slow_content_read_rechecks_private_scope_before_sending_any_bytes(worker, path):
    async def interrupted(_key):
        await make_private(worker)
    worker.blob.get_hook = interrupted
    response = await worker.client.get(SESSION + path)
    assert response.status_code == 404 and PRIVATE not in response.content


@pytest.mark.parametrize("queued", [False, True])
async def test_existing_subscription_drops_idle_and_queued_hints_after_scope_changes(worker, monkeypatch, queued):
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert (await receive())["type"] == "subscribed"
        if queued:
            entered, release = asyncio.Event(), asyncio.Event()
            check = worker.http_backend.session_audience
            async def delayed(viewer, targets):
                entered.set()
                await release.wait()
                return await check(viewer, targets)
            monkeypatch.setattr(worker.http_backend, "session_audience", delayed)
            bus.publish("trajectory.available", {"user_id": "a", "owner_user_id": "a", "session_id": "session_a_1",
                "trajectory_id": "trj_a1", "committed_seq": "999"})
            await asyncio.wait_for(entered.wait(), 2)
        await make_private(worker)
        if queued:
            release.set()
        assert await receive(timeout=3) == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}
        await send({"type": "ping"})
        assert await receive() == {"type": "pong", "data": {}}


@pytest.mark.parametrize("revoked_at", ["before_build", "after_archive", "after_upload", None])
async def test_background_export_rechecks_current_audience_before_build_upload_and_completion(worker, monkeypatch, revoked_at):
    service = ExportService(worker_settings(), blob_store=worker.blob, metrics=FakeMetrics(), owner_id="audience-test")
    written, uploaded = [], []
    write, upload = service._write_archive, worker.blob.put_file
    async def archive(*args, **kwargs):
        written.append(True)
        await write(*args, **kwargs)
        if revoked_at == "after_archive":
            await make_private(worker)
    async def put_file(*args, **kwargs):
        uploaded.append(True)
        await upload(*args, **kwargs)
        if revoked_at == "after_upload":
            await make_private(worker)
    monkeypatch.setattr(service, "_write_archive", archive)
    monkeypatch.setattr(worker.blob, "put_file", put_file)
    if revoked_at == "before_build":
        await make_private(worker)
    assert await service.build("exp_pending") == ("completed" if revoked_at is None else "failed")
    assert bool(written) == (revoked_at != "before_build")
    assert bool(uploaded) == (revoked_at not in {"before_build", "after_archive"})
    async with trace_session() as db:
        row = await db.get(TrajectoryExport, "exp_pending")
        assert row.error == (None if revoked_at is None else "LookupError")
        if revoked_at is not None:
            assert row.storage_key is None
        assert not (await db.get(TrajectoryMetaSession, "session_a_1")).is_deleted
    if revoked_at is not None:
        assert (await worker.client.get(SESSION + "/exports/exp_pending/download")).status_code == 404


async def test_real_list_filters_before_paging_and_does_not_emit_a_private_cursor_or_tail(worker, monkeypatch):
    monkeypatch.setattr(repository, "list_sessions", real_list_sessions)
    stamp = now() + timedelta(days=1)
    async with worker.business.begin() as db:
        for i in range(210):
            db.add(Session(id=f"private_{i:03}", user_id="a", workspace_id="ws_a", project_id="project_a",
                visibility="private", title="PRIVATE_ONLY_TITLE", created_at=stamp, updated_at=stamp))
    async with trace_session() as db:
        for i in range(210):
            db.add(TrajectoryMetaSession(id=f"private_{i:03}", user_id="a", workspace_id="ws_a", kind="normal",
                title="PRIVATE_ONLY_TITLE", created_at=stamp, updated_at=stamp, synced_at=stamp))
    ids, cursor = [], None
    for _ in range(4):
        response = await worker.client.get(PREFIX + "/sessions", params={
            "include_unrecorded": "true", "limit": 1, **({"cursor": cursor} if cursor else {})})
        assert response.status_code == 200, response.text
        page = response.json()
        assert "PRIVATE_ONLY_TITLE" not in response.text and "private_" not in response.text
        ids += [row["session_id"] for row in page["items"]]
        cursor = page["next_cursor"]
        if cursor:
            assert repository.cursor_decode(cursor)[2] in ids
        if not page["has_more"]:
            assert cursor is None
            break
    assert set(ids) == {"session_a_1", "session_a_2", "session_b_1"} and len(ids) == 3
    hidden = await worker.client.get(PREFIX + "/sessions", params={"include_unrecorded": "true", "q": "PRIVATE_ONLY_TITLE"})
    assert hidden.json() == {"items": [], "next_cursor": None, "has_more": False}


async def test_list_lookahead_is_rechecked_so_a_revoked_tail_cannot_set_has_more(worker, monkeypatch):
    monkeypatch.setattr(repository, "list_sessions", real_list_sessions)
    check = worker.http_backend.session_audience
    calls = []
    async def revoke_after_first_check(viewer, targets):
        result = await check(viewer, targets)
        calls.append(targets)
        if len(calls) == 1:
            await make_private(worker)  # a_1 is the lookahead after visible b_1.
        return result
    monkeypatch.setattr(worker.http_backend, "session_audience", revoke_after_first_check)
    response = await worker.client.get(PREFIX + "/sessions", params={"limit": 1})
    assert response.status_code == 404 and response.json() == {"detail": "Session not found"}


async def test_synced_metadata_cannot_rebind_original_trace_content_to_another_workspace(worker, monkeypatch):
    monkeypatch.setattr(repository, "list_sessions", real_list_sessions)
    async with worker.business.begin() as db:
        (await db.get(Session, "session_a_1")).workspace_id = "ws_b"
    async with trace_session() as db:
        (await db.get(TrajectoryMetaSession, "session_a_1")).workspace_id = "ws_b"
    assert (await worker.client.get(SESSION)).status_code == 404
    assert (await worker.client.get(SESSION + "/payloads/pld_media")).status_code == 404
    listing = await worker.client.get(PREFIX + "/sessions")
    assert [row["session_id"] for row in listing.json()["items"]] == ["session_b_1"]
    ticket = await ticket_for(worker)
    async with worker.socket(ticket) as (send, receive):
        assert (await receive())["type"] == "websocket.accept"
        await send({"type": "subscribe", "session_id": "session_a_1"})
        assert await receive() == {"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": "session_a_1"}}


async def test_audience_service_failure_or_wrong_scope_has_no_replica_fallback(worker, monkeypatch):
    assert (await worker.client.get(SESSION)).status_code == 200
    async def wrong(_viewer, _targets):
        return {"version": 1, "user_id": "someone-else", "allowed": ["session_a_1"]}
    monkeypatch.setattr(worker.http_backend, "session_audience", wrong)
    assert (await worker.client.get(SESSION)).status_code == 503
