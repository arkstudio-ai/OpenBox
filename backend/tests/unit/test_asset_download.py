"""Stable asset download links resolve to fresh, owner-bound OSS URLs."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, Request, Response
import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api import assets as asset_api
from auth import jwt, middleware
from cache.memory_cache import MemoryCache
from db.base import get_db_session, get_engine
from db.models.file_asset import FileAsset
from db.models.workspace import WorkspaceMember
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.mark.asyncio
async def test_asset_download_redirects_to_fresh_disposition_url(monkeypatch):
    row = SimpleNamespace(
        id="asset_ready",
        status="ready",
        oss_key="assets/user/asset_ready/final.mp4",
        name="final.mp4",
    )

    class Oss:
        def presign_get(self, key, *, download_name=None):
            assert key == row.oss_key
            assert download_name == row.name
            return "https://oss.example.test/final.mp4?download=fresh"

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    async def fake_owned_asset(_db, asset_id, user_id):
        assert asset_id == row.id
        assert user_id == "user-1"
        return row

    monkeypatch.setattr(asset_api, "_oss_or_503", lambda: Oss())
    monkeypatch.setattr(asset_api, "get_db_session", fake_db_session)
    monkeypatch.setattr(asset_api, "_owned_asset", fake_owned_asset)

    response = await asset_api.asset_download(
        row.id,
        token="",
        current_user={"user_id": "user-1"},
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://oss.example.test/final.mp4?download=fresh"


@pytest.mark.asyncio
async def test_asset_download_accepts_owner_bound_capability(monkeypatch):
    row = SimpleNamespace(
        id="asset_ready",
        status="ready",
        oss_key="assets/user/asset_ready/final.mp4",
        name="final.mp4",
    )

    class Oss:
        def presign_get(self, key, *, download_name=None):
            return "https://oss.example.test/final.mp4?download=fresh"

    @asynccontextmanager
    async def fake_db_session():
        yield object()

    async def fake_owned_asset(_db, asset_id, user_id):
        assert asset_id == row.id
        assert user_id == "user-1"
        return row

    monkeypatch.setattr(asset_api, "_oss_or_503", lambda: Oss())
    monkeypatch.setattr(asset_api, "get_db_session", fake_db_session)
    monkeypatch.setattr(asset_api, "_owned_asset", fake_owned_asset)
    monkeypatch.setattr(
        asset_api,
        "decode_asset_download_token",
        lambda token, asset_id: {"sub": "user-1"}
        if token == "valid" and asset_id == row.id
        else None,
    )

    response = await asset_api.asset_download(row.id, token="valid", current_user=None)

    assert response.status_code == 307


@pytest.fixture
async def asset_text_world(monkeypatch):
    """Real JWT/workspace/asset SQL and ASGI; only OSS signing/I/O is fake."""
    owner, peer, workspace = await accounts()
    private = await create_session(user_id=owner, workspace_id=workspace, visibility="private")
    shared = await create_session(user_id=owner, workspace_id=workspace)
    asset = FileAsset(id="asset_" + uuid4().hex, user_id=owner, workspace_id=workspace,
        session_id=private.id, project_id=private.project_id, name="private-original.txt", mime="text/plain",
        oss_key="assets/preview/" + uuid4().hex, size=26, status="ready", source="user", transient=False,
        is_deleted=False, created_at=datetime.now(timezone.utc))
    async with get_db_session() as db:
        db.add(asset)
    state = SimpleNamespace(owner=owner, peer=peer, workspace=workspace, asset=asset, shared=shared,
        entered=asyncio.Event(), release=asyncio.Event(), oss_reads=[], reader_pids=[])
    monkeypatch.setattr(jwt, "_secret", "asset-text-only-test-signing-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", MemoryCache())

    class Oss:
        def presign_get(self, key, *, expires_sec):
            assert key == asset.oss_key and expires_sec == 300
            return "http://127.0.0.1:19998/" + key

    monkeypatch.setattr(asset_api, "_oss_or_503", lambda: Oss())
    actual_owned = asset_api._owned_asset

    async def observed_owned(db, *args):
        row = await actual_owned(db, *args)
        if db.bind.dialect.name == "postgresql":
            state.reader_pids.append(await db.scalar(select(func.pg_backend_pid())))
        return row

    monkeypatch.setattr(asset_api, "_owned_asset", observed_owned)
    remote = FastAPI()

    @remote.get("/{path:path}")
    async def oss_get(path: str, request: Request):
        assert path == asset.oss_key and request.url.hostname == "127.0.0.1"
        state.oss_reads.append(path)
        state.entered.set()
        await state.release.wait()
        return Response("original-private-body-7391", media_type="text/plain")

    actual_client = httpx.AsyncClient
    remote_transport = httpx.ASGITransport(app=remote)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        actual_client(**{**kwargs, "transport": kwargs.get("transport") or remote_transport}))
    app = FastAPI()
    app.include_router(asset_api.router)
    state.headers = lambda user=owner: {"Authorization": "Bearer " + jwt.create_access_token(user, "user"),
        "X-Workspace-Id": workspace}
    async with actual_client(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as api:
        state.api = api
        yield state


@pytest.mark.parametrize("change, status", [
    ("membership", 404), ("deleted", 404), ("owner", 404),
    ("key", 409), ("source_session", 409), ("pending", 409),
])
async def test_asset_text_withholds_delayed_oss_body_after_current_scope_or_source_change(
        asset_text_world, change, status, record_property):
    world = asset_text_world
    # Hold a separate real connection before HTTP admission, so a PostgreSQL
    # writer cannot accidentally be the same backend as either audience read.
    async with get_engine().connect() as writer:
        writer_pid = await writer.scalar(select(func.pg_backend_pid())) if writer.dialect.name == "postgresql" else None
        await writer.commit()
        response_task = asyncio.create_task(world.api.get(f"/api/assets/{world.asset.id}/text", headers=world.headers()))
        try:
            await asyncio.wait_for(world.entered.wait(), 10)
            async with AsyncSession(bind=writer, expire_on_commit=False) as db:
                row = await db.get(FileAsset, world.asset.id)
                if change == "membership":
                    (await db.get(WorkspaceMember, (world.workspace, world.owner))).status = "removed"
                elif change == "deleted": row.is_deleted = True
                elif change == "owner": row.user_id = world.peer
                elif change == "key": row.oss_key = "assets/replaced/object"
                elif change == "source_session": row.session_id = world.shared.id
                elif change == "pending": row.status = "pending"
                await db.commit()
        finally:
            world.release.set()
            response = await asyncio.wait_for(response_task, 10)
        assert response.status_code == status, response.text
        assert "original-private-body-7391" not in response.text
        assert world.oss_reads == [world.asset.oss_key]
        if writer_pid is not None:
            assert world.reader_pids and writer_pid not in world.reader_pids
        record_property("delayed_asset_text", json.dumps({"change": change, "status": response.status_code,
            "writer_pid": writer_pid, "reader_pids": world.reader_pids, "oss_reads": len(world.oss_reads)}))


@pytest.mark.parametrize("visibility", ["private", "workspace"])
async def test_asset_text_retains_owner_preview_and_denies_peer_before_oss(asset_text_world, visibility):
    world = asset_text_world
    if visibility == "workspace":
        async with get_db_session() as db:
            (await db.get(FileAsset, world.asset.id)).session_id = world.shared.id
    peer = await world.api.get(f"/api/assets/{world.asset.id}/text", headers=world.headers(world.peer))
    assert peer.status_code == 404 and world.oss_reads == []
    world.release.set()
    response = await world.api.get(f"/api/assets/{world.asset.id}/text", headers=world.headers())
    assert response.status_code == 200
    assert response.json() == {"name": world.asset.name, "mime": "text/plain",
        "text": "original-private-body-7391", "truncated": False}
    assert world.oss_reads == [world.asset.oss_key]
