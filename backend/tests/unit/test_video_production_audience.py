"""Historical production lists obey the same owner boundary as their details."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
import httpx
import pytest

from api import video_productions as api
from auth import jwt, middleware
from cache.memory_cache import MemoryCache
from db.base import get_db_session
from db.models.user import User
from db.models.video_production import VideoProduction
from db.models.workspace import WorkspaceMember
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture
async def production_world(monkeypatch):
    owner, peer, workspace = await accounts()
    private = await create_session(user_id=owner, workspace_id=workspace, visibility="private")
    shared = await create_session(user_id=owner, workspace_id=workspace)
    peer_session = await create_session(user_id=peer, workspace_id=workspace)
    now = datetime.now(timezone.utc)

    def production(actor, target, title, offset, **changes):
        return VideoProduction(id="production_" + uuid4().hex, user_id=actor, workspace_id=workspace,
            session_id=target.id if target else None, project_id=target.project_id if target else None,
            title=title, brief=title + " original brief", status="init", created_at=now,
            updated_at=now + timedelta(seconds=offset), **changes)

    private_row = production(owner, private, "Private production title 7391", 3)
    shared_row = production(owner, shared, "Owner production in shared Session", 2)
    unfiled = production(owner, None, "Owner unfiled production", 1)
    peer_row = production(peer, peer_session, "Peer own production", -1)
    elsewhere = production(owner, None, "Owner production in another workspace", 4)
    async with get_db_session() as db:
        elsewhere.workspace_id = (await db.get(User, peer)).default_workspace_id
        db.add_all([private_row, shared_row, unfiled, peer_row, elsewhere])
    monkeypatch.setattr(jwt, "_secret", "production-audience-test-signing-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", MemoryCache())
    app = FastAPI()
    app.include_router(api.router)
    world = SimpleNamespace(owner=owner, peer=peer, workspace=workspace,
        private=private_row, shared=shared_row, unfiled=unfiled, peer_row=peer_row, elsewhere=elsewhere)
    world.headers = lambda actor: {"Authorization": "Bearer " + jwt.create_access_token(actor, "user"),
        "X-Workspace-Id": workspace}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://production.test") as client:
        world.client = client
        yield world


async def test_peer_list_cannot_reveal_foreign_private_shared_or_unfiled_productions(production_world):
    w = production_world
    headers = w.headers(w.peer)
    # This is the existing detail contract, independent of the list fix.
    for row in (w.private, w.shared, w.unfiled):
        detail = await w.client.get(f"/api/video-productions/{row.id}", headers=headers)
        assert detail.status_code == 404
    for params in ({}, {"limit": 1}, {"status": "init"}):
        response = await w.client.get("/api/video-productions", params=params, headers=headers)
        assert response.status_code == 200
        assert [row["production_id"] for row in response.json()["productions"]] == [w.peer_row.id]
        for foreign in (w.private, w.shared, w.unfiled):
            assert foreign.id not in response.text and foreign.title not in response.text
            if foreign.session_id:
                assert foreign.session_id not in response.text
    for source in (w.private.session_id, w.shared.session_id, "unknown-session"):
        response = await w.client.get("/api/video-productions", params={"session_id": source}, headers=headers)
        # Exact empty shape: no hidden title, Session id, row count or existence hint.
        assert response.status_code == 200 and response.json() == {"productions": []}


async def test_owner_list_and_detail_remain_available_only_in_the_current_workspace(production_world):
    w = production_world
    headers = w.headers(w.owner)
    response = await w.client.get("/api/video-productions", headers=headers)
    assert response.status_code == 200
    assert [row["production_id"] for row in response.json()["productions"]] == [w.private.id, w.shared.id, w.unfiled.id]
    assert w.peer_row.id not in response.text and w.elsewhere.id not in response.text
    selected = await w.client.get("/api/video-productions", params={"session_id": w.private.session_id}, headers=headers)
    assert [row["production_id"] for row in selected.json()["productions"]] == [w.private.id]
    detail = await w.client.get(f"/api/video-productions/{w.private.id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["brief"] == w.private.brief
    assert (await w.client.get(f"/api/video-productions/{w.elsewhere.id}", headers=headers)).status_code == 404
    assert (await w.client.get("/api/video-productions")).status_code == 401
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
    assert (await w.client.get("/api/video-productions", headers=headers)).status_code == 403
