"""Private file APIs use live Session authority and a fixed physical route.

SQL, JWT/workspace dependencies, private provisioning/identity validation and
ASGI route dispatch are real. The external Docker SDK and Action Server HTTP
boundary are fixtures; no daemon, provider, cloud or user files are accessed.
"""
import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
import httpx
import pytest
from sqlalchemy import func, select, update

from api import containers, dev_browser, files, terminal
from auth import jwt, middleware, ticket
from cache.memory_cache import MemoryCache
from core.config import get_config
from db.base import get_db_session, get_engine
from db.models.container import Container
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from models.container import ContainerInfo, ContainerStatus
from sandbox import private_runtime
from sandbox.docker import DockerManager
from tests.unit.test_private_runtime import assistant_database, private_world  # noqa: F401


@pytest.fixture
async def api_world(private_world, monkeypatch):
    world = private_world
    config = get_config()
    config.private_runtime.allowed_user_ids = [world.owner, world.peer]
    monkeypatch.setattr(jwt, "_secret", "private-runtime-api-test-signing-key")
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(middleware, "_cache", MemoryCache())
    monkeypatch.setattr(ticket, "_cache", MemoryCache())
    route = await private_runtime.resolve_private_runtime(session_id=world.session.id, user_id=world.owner,
                                                          workspace_id=world.workspace)
    state = SimpleNamespace(world=world, route=route, requests=[], after_request=None, failure=None, uploaded={})
    remote = FastAPI()

    @remote.middleware("http")
    async def trace(request: Request, call_next):
        assert request.url.hostname == "127.0.0.1"
        state.requests.append({"path": request.url.path, "port": request.url.port,
                               "headers": dict(request.headers)})
        assert request.headers["X-API-Key"] == ("shared-fixture-key" if request.url.port == 19099 else route.api_key)
        if state.failure:
            from fastapi.responses import Response
            return Response("untrusted service failure", status_code=state.failure[0], headers=state.failure[1])
        response = await call_next(request)
        if state.after_request:
            await state.after_request()
        return response

    @remote.post("/list_files")
    async def list_files():
        return {"path": "/workspace", "entries": [{"name": "owner-private.txt", "is_dir": False, "size": 19}]}

    @remote.post("/read_file")
    async def read_file(request: Request):
        data = await request.json()
        assert data["offset"] == 0 and data["limit"] == 5000
        body = "shared-body" if request.url.port == 19099 else "owner-private-body"
        return {"content": "1\t" + body, "total_lines": 1, "end_line": 1}

    @remote.post("/glob")
    async def glob():
        return {"files": ["/workspace/owner-private.txt", "/workspace/other.txt"]}

    @remote.post("/upload")
    async def upload(file: UploadFile = File(...), destination: str = Form(...)):
        assert destination == "/workspace/uploads"
        value = await file.read()
        state.uploaded[destination + "/" + file.filename] = value
        return {"path": destination + "/" + file.filename, "size": len(value)}

    @remote.get("/proxy/{port}/{path:path}")
    async def preview(port: int, path: str):
        return {"ordinary_preview": True, "port": port, "path": path}

    actual_client = httpx.AsyncClient
    remote_transport = httpx.ASGITransport(app=remote)

    def fixture_client(**kwargs):
        return actual_client(**{**kwargs, "transport": kwargs.get("transport") or remote_transport})

    monkeypatch.setattr(httpx, "AsyncClient", fixture_client)
    # An ordinary registry is intentionally seeded with a private row to prove
    # that legacy API exclusion is independent of discovery/adoption filtering.
    ordinary = object.__new__(DockerManager)
    now = datetime.now(timezone.utc)
    shared = ContainerInfo(id="ordinary-workspace", name="ordinary-workspace", status=ContainerStatus.RUNNING,
        image="fixture", created_at=now, host="127.0.0.1", port=19099, api_key="shared-fixture-key")
    shared_user = shared.model_copy(update={"id": "ordinary-user", "name": "ordinary-user"})
    private = ContainerInfo(id=route.container_id, name="private-title-do-not-list", status=ContainerStatus.RUNNING,
        image="fixture", created_at=now, host=route.host, port=route.port, api_key=route.api_key)
    ordinary._containers = {entry.id: entry for entry in (shared, shared_user, private)}
    ordinary._api_keys = {entry.id: entry.api_key for entry in (shared, shared_user, private)}
    ordinary._container_owners = {shared.id: world.workspace, shared_user.id: world.owner, private.id: world.owner}
    ordinary._container_projects = {}
    monkeypatch.setattr(containers, "provider", ordinary)
    monkeypatch.setattr(files, "provider", ordinary)
    monkeypatch.setattr(terminal, "provider", ordinary)
    monkeypatch.setattr(dev_browser, "provider", ordinary)
    state.provider = ordinary
    async with get_db_session() as db:
        db.add_all([
            Container(id=route.container_id, user_id=world.owner, project_id=world.shared.project_id, docker_id=route.container_id,
                name="private-title-do-not-list", status="running", created_at=now, updated_at=now),
            Container(id="ordinary-admin-" + uuid4().hex, user_id=world.owner, project_id=world.shared.project_id, name="ordinary-admin",
                status="running", created_at=now, updated_at=now),
        ])
    app = FastAPI()
    app.include_router(containers.router)
    app.include_router(containers.preview_router)
    app.include_router(files.router)
    app.include_router(terminal.router)
    app.include_router(dev_browser.router)
    state.app = app

    def headers(user=None, workspace=None, role="user"):
        return {"Authorization": "Bearer " + jwt.create_access_token(user or world.owner, role),
                "X-Workspace-Id": workspace or world.workspace}

    state.headers = headers
    async with actual_client(transport=httpx.ASGITransport(app=app), base_url="http://api.test") as api:
        state.api = api
        yield state


def session_path(w, suffix):
    return f"/api/agent/session/{w.world.session.id}/files/{suffix}"


async def test_owner_file_routes_read_and_upload_exact_bytes_without_shell_or_shared_lookup(api_world):
    w = api_world
    headers = w.headers()
    for method, suffix, kwargs in [
        ("POST", "list", {"json": {"path": "/workspace"}}),
        ("GET", "search", {"params": {"q": "private", "limit": 1}}),
        ("GET", "content", {"params": {"path": "/workspace/owner-private.txt"}}),
    ]:
        response = await w.api.request(method, session_path(w, suffix), headers=headers, **kwargs)
        assert response.status_code == 200, response.text
        assert "private" in response.text
        assert w.route.api_key not in response.text and "127.0.0.1" not in response.text
    raw = b"\x00private\xffbinary\nbytes"
    response = await w.api.post(session_path(w, "upload"), headers=headers,
        files={"file": ("private.bin", raw, "application/octet-stream")})
    assert response.status_code == 200, response.text
    assert w.uploaded == {"/workspace/uploads/private.bin": raw}
    assert [item["path"] for item in w.requests] == ["/list_files", "/glob", "/read_file", "/upload"]
    assert all(item["port"] == w.route.port for item in w.requests)


@pytest.mark.parametrize("audience", ["peer", "noauth", "wrong-workspace", "missing-session", "ordinary-session"])
async def test_private_session_file_routes_do_not_reveal_or_touch_an_unavailable_audience(api_world, audience):
    w = api_world
    headers = w.headers(w.world.peer) if audience == "peer" else w.headers()
    target = w.world.session.id
    if audience == "noauth":
        headers = {}
    elif audience == "wrong-workspace":
        headers = w.headers(workspace="unrelated-workspace")
    elif audience == "missing-session":
        target = "not-a-session"
    elif audience == "ordinary-session":
        target = w.world.shared.id
    for method, suffix, kwargs in [
        ("POST", "list", {"json": {"path": "/workspace"}}),
        ("GET", "content", {"params": {"path": "/workspace/owner-private.txt"}}),
        ("GET", "search", {"params": {"q": "private"}}),
        ("POST", "upload", {"files": {"file": ("must-not-land.txt", b"private")}}),
    ]:
        response = await w.api.request(method, f"/api/agent/session/{target}/files/{suffix}", headers=headers, **kwargs)
        assert response.status_code in {401, 403, 404}, response.text
        assert "owner-private-body" not in response.text and w.route.api_key not in response.text
    assert w.requests == [] and w.uploaded == {}


@pytest.mark.parametrize("kind", ["member", "user-disabled", "user-deleted", "workspace", "session", "audience", "binding"])
async def test_existing_jwt_cannot_read_after_current_sql_revocation(api_world, kind):
    w = api_world
    headers = w.headers()
    async with get_db_session() as db:
        if kind == "member":
            (await db.get(WorkspaceMember, (w.world.workspace, w.world.owner))).status = "removed"
        elif kind == "user-disabled":
            (await db.get(User, w.world.owner)).is_active = False
        elif kind == "user-deleted":
            (await db.get(User, w.world.owner)).is_deleted = True
        elif kind == "workspace":
            (await db.get(Workspace, w.world.workspace)).is_deleted = True
        elif kind == "session":
            (await db.get(Session, w.world.session.id)).is_deleted = True
        elif kind == "audience":
            row = await db.get(Session, w.world.session.id)
            row.kind, row.visibility, row.memory_policy = "normal", "workspace", "standard"
        else:
            (await db.get(PrivateRuntimeBinding, w.route.binding_id)).status = "blocked"
    response = await w.api.get(session_path(w, "content"), params={"path": "/workspace/owner-private.txt"}, headers=headers)
    assert response.status_code in {403, 404, 409}, response.text
    assert w.requests == [] and "owner-private-body" not in response.text


@pytest.mark.parametrize("alias", ["container_id", "short-id", "binding_id", "route_key", "name"])
async def test_all_legacy_aliases_and_unauthenticated_preview_deny_private_runtime(api_world, alias):
    w = api_world
    identity = w.route.container_id[:12] if alias == "short-id" else getattr(w.route, alias)
    base = f"/api/containers/{identity}"
    operations = [("GET", "", {}), ("GET", "/ports", {}), ("POST", "/start", {}), ("POST", "/stop", {}),
        ("DELETE", "", {}), ("POST", "/preview-token?port=3000", {}),
        ("POST", "/files/list", {"json": {"path": "/workspace"}}),
        ("GET", "/files/content?path=/workspace/owner-private.txt", {}),
        ("GET", "/files/search?q=private", {}), ("GET", "/files/system_info", {}),
        ("POST", "/files/upload", {"files": {"file": ("bad.txt", b"private")}}),
        ("POST", "/dev-browser/start", {}), ("POST", "/dev-browser/stop", {}),
        ("GET", "/dev-browser/status", {})]
    for method, suffix, kwargs in operations:
        response = await w.api.request(method, base + suffix, headers=w.headers(), **kwargs)
        assert response.status_code == 404, response.text
    for method in ("GET", "POST"):
        response = await w.api.request(method, base + "/preview/3000/", headers={"X-API-Key": "forged"})
        assert response.status_code == 404
    assert w.requests == []


async def websocket_messages(app, path, query):
    """Exercise the production websocket endpoint without opening any socket."""
    messages = []
    queue = asyncio.Queue()
    queue.put_nowait({"type": "websocket.connect"})

    async def send(message):
        messages.append(message)

    scope = {"type": "websocket", "asgi": {"version": "3.0"}, "scheme": "ws",
        "path": path, "raw_path": path.encode(), "query_string": query.encode(),
        "root_path": "", "headers": [], "client": ("127.0.0.1", 1234),
        "server": ("api.test", 80), "subprotocols": [], "state": {}}
    await asyncio.wait_for(app(scope, queue.get, send), 3)
    return messages


@pytest.mark.parametrize("target", ["terminal-full", "terminal-short", "browser-auto"])
async def test_legacy_websockets_cannot_use_private_identity_from_stale_registry(api_world, monkeypatch, target):
    w = api_world
    identity = w.route.container_id[:12] if target == "terminal-short" else w.route.container_id
    info = w.provider._containers[w.route.container_id].model_copy(update={"id": identity})
    # Even a private entry incorrectly attributed to the current workspace
    # must not supply a service key to either legacy websocket relay.
    w.provider._containers = {identity: info}
    w.provider._container_owners = {identity: w.world.workspace}
    w.provider._api_keys = {identity: w.route.api_key}
    attempted = []

    def forbidden_connect(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Private runtime must not reach legacy websocket transport")

    monkeypatch.setattr(terminal.websockets, "connect", forbidden_connect)
    one_time = await ticket.create_ticket(w.world.owner, workspace_id=w.world.workspace)
    path = "/ws/dev-browser/auto" if target == "browser-auto" else "/ws/terminal/" + identity
    messages = await websocket_messages(w.app, path, "ticket=" + one_time)
    assert any(item["type"] == "websocket.accept" for item in messages)
    assert messages[-1]["type"] == "websocket.close"
    if target == "browser-auto":
        assert messages[-1]["code"] == 4004
    else:
        assert any("Container not found" in item.get("text", "") for item in messages)
    assert not attempted and not w.requests
    assert w.route.api_key not in json.dumps(messages)


async def test_private_alias_never_falls_through_to_the_legacy_shared_wuying_desktop(api_world, monkeypatch):
    from sandbox.wuying import WuyingProvider
    w = api_world
    # This provider historically ignores its container_id in shared mode.
    # Keep the actual provider and real routes; replace only its transports.
    w.world.config.wuying_routing = "shared"
    w.world.config.wuying_endpoint = "http://127.0.0.1:19099"
    w.world.config.wuying_api_key = "shared-fixture-key"
    shared = WuyingProvider()
    monkeypatch.setattr(terminal, "provider", shared)
    monkeypatch.setattr(dev_browser, "provider", shared)
    attempted = []

    def forbidden_connect(*args, **kwargs):
        attempted.append(True)
        raise AssertionError("Private identity must not fall back to a shared socket")

    monkeypatch.setattr(terminal.websockets, "connect", forbidden_connect)
    for identity in (w.route.container_id, w.route.route_key, w.route.name):
        for method, suffix in (("POST", "start"), ("POST", "stop"), ("GET", "status")):
            response = await w.api.request(method, f"/api/containers/{identity}/dev-browser/{suffix}", headers=w.headers())
            assert response.status_code == 404
        one_time = await ticket.create_ticket(w.world.owner, workspace_id=w.world.workspace)
        messages = await websocket_messages(w.app, "/ws/terminal/" + identity, "ticket=" + one_time)
        assert any("Container not found" in item.get("text", "") for item in messages)
    assert not attempted and not w.requests


async def test_legacy_lists_and_admin_hide_private_titles_ids_and_counts_but_shared_stays_usable(api_world):
    w = api_world
    response = await w.api.get("/api/containers", headers=w.headers())
    assert response.status_code == 200 and response.json()["total"] == 1
    assert [row["id"] for row in response.json()["containers"]] == ["ordinary-user"]
    response = await w.api.get("/api/containers/admin/all", headers=w.headers(role="admin"))
    async with get_db_session() as db:
        ordinary_count = await db.scalar(select(func.count()).select_from(Container).where(Container.name == "ordinary-admin"))
    assert response.status_code == 200 and [row["name"] for row in response.json()] == ["ordinary-admin"] * ordinary_count
    assert "private-title" not in response.text and w.route.container_id not in response.text
    response = await w.api.get("/api/containers/ordinary-workspace/files/content", headers=w.headers(),
                               params={"path": "/workspace/shared.txt"})
    assert response.status_code == 200 and response.json()["content"] == "shared-body"
    response = await w.api.get("/api/containers/ordinary-workspace/preview/3000/test")
    assert response.status_code == 200 and response.json()["ordinary_preview"] is True


async def test_response_is_withheld_when_authority_is_revoked_during_private_read(api_world):
    w = api_world
    async def revoke():
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (w.world.workspace, w.world.owner))).status = "removed"
    w.after_request = revoke
    response = await w.api.get(session_path(w, "content"), headers=w.headers(), params={"path": "/workspace/owner-private.txt"})
    assert response.status_code == 403 and "owner-private-body" not in response.text
    assert [item["path"] for item in w.requests] == ["/read_file"]


async def test_redirect_is_not_followed_and_internal_headers_are_not_browser_control(api_world):
    w = api_world
    # An ASGI handler under the external boundary can issue a redirect, but
    # the production adapter must neither send its key there nor return it.
    w.failure = (307, {"Location": "http://127.0.0.1:19998/must-not-follow"})
    response = await w.api.get(session_path(w, "content"), headers={**w.headers(),
        "X-API-Key": "browser-forged", "X-OpenBox-Resource": "browser-forged"},
        params={"path": "/workspace/owner-private.txt"})
    assert response.status_code == 502
    assert len(w.requests) == 1 and w.requests[0]["headers"]["x-api-key"] == w.route.api_key
    assert "x-openbox-resource" not in w.requests[0]["headers"]
    assert w.route.api_key not in response.text and "127.0.0.1" not in response.text


async def test_large_private_upload_is_rejected_before_any_runtime_dispatch(api_world):
    w = api_world
    response = await w.api.post(session_path(w, "upload"), headers=w.headers(),
        files={"file": ("too-large.bin", b"x" * (files._UPLOAD_MAX_BYTES + 1))})
    assert response.status_code == 413 and w.requests == [] and w.uploaded == {}


async def test_get_does_not_provision_missing_binding_or_fall_back_to_workspace(api_world):
    w = api_world
    before = list(w.world.daemon.calls)
    response = await w.api.get(f"/api/agent/session/{w.world.peer_session.id}/files/content",
        params={"path": "/workspace/owner-private.txt"}, headers=w.headers(w.world.peer))
    assert response.status_code == 404
    assert w.requests == [] and w.world.daemon.calls == before


async def test_postgres_distinct_writer_revokes_before_private_response_can_be_released(api_world, monkeypatch, record_property):
    w = api_world
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("Independent backend PID proof requires PostgreSQL")
    readers = set()
    original_scope = private_runtime._scope

    async def observed_scope(db, *args, **kwargs):
        readers.add(await db.scalar(select(func.pg_backend_pid())))
        return await original_scope(db, *args, **kwargs)

    monkeypatch.setattr(private_runtime, "_scope", observed_scope)
    async with engine.connect() as writer:
        writer_pid = (await writer.execute(select(func.pg_backend_pid()))).scalar_one()
        async def revoke():
            await writer.execute(update(Session).where(Session.id == w.world.session.id).values(is_deleted=True))
            await writer.commit()
        w.after_request = revoke
        response = await w.api.get(session_path(w, "content"), params={"path": "/workspace/owner-private.txt"}, headers=w.headers())
        assert response.status_code == 403 and "owner-private-body" not in response.text
        assert readers and writer_pid not in readers
    assert [item["path"] for item in w.requests] == ["/read_file"]
    record_property("private_response_revocation", json.dumps({"writer_pid": writer_pid,
        "reader_pids": sorted(readers), "http_status": response.status_code,
        "response_disclosed": False, "physical_requests": 1}))
