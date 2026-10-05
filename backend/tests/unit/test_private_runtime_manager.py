"""Positive private execution through real SQL, manager, tools and HTTP hooks.

Only Docker daemon I/O and Action Server transport are substituted. Scope,
provisioning, per-request physical validation and asset audience are real.
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from agent.driver import bind_current_lease, reserve_run, reset_current_lease
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from sandbox import client as client_module
from sandbox.assets import deliver, deliver_asset_ids
from sandbox.client import SandboxClient
from sandbox.manager import SandboxManager
from sandbox.privacy import PrivateRuntimeUnavailable
from sandbox.private_runtime import PrivateRuntimeError
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_private_runtime import private_world  # noqa: F401
from tool.tool import ToolContext
from tool.write import WriteArgs, execute as write


@pytest.fixture
async def manager_world(private_world, monkeypatch):
    w = private_world
    w.manager, w.sent, w.files, w.signed = SandboxManager(), [], {}, []
    w.revoke_on_response = False
    w.starting_probes = 0
    w.connecting_probe = False

    async def remote(request):
        assert request.url.host == "127.0.0.1"
        rows = [row for row in w.daemon.container_rows.values()
            if row["NetworkSettings"]["Ports"]["8000/tcp"][0]["HostPort"] == str(request.url.port)]
        assert len(rows) == 1
        row = rows[0]
        secret = next(value.split("=", 1)[1] for value in row["Config"]["Env"] if value.startswith("SESSION_API_KEY="))
        assert request.headers["X-API-Key"] == secret
        body = json.loads(request.content) if request.content else {}
        w.sent.append((row["Id"], request.url.path, body))
        store = w.files.setdefault(row["Id"], {})
        if request.url.path == "/alive":
            if w.starting_probes:
                w.starting_probes -= 1
                if w.connecting_probe:
                    raise httpx.ConnectError("Original server is starting", request=request)
                return httpx.Response(503, json={"starting": True})
            result = {"alive": True}
        elif request.url.path == "/write_file":
            store[body["path"]] = body["content"]
            result = {"ok": True}
        elif request.url.path == "/read_file":
            result = {"content": store.get(body["path"], "")}
        elif request.url.path == "/execute":
            result = {"exit_code": 0, "stdout": "", "stderr": ""}
        else:
            raise AssertionError("Unexpected private transport: " + request.url.path)
        if w.revoke_on_response:
            async with get_db_session() as db:
                (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        return httpx.Response(200, json=result)

    transport = httpx.MockTransport(remote)
    http_namespace = dict(vars(httpx))
    original = httpx.AsyncClient
    http_namespace["AsyncHTTPTransport"] = lambda **_: transport
    http_namespace["AsyncClient"] = lambda **args: original(**{**args, "transport": args.get("transport") or transport})
    monkeypatch.setattr(client_module, "httpx", SimpleNamespace(**http_namespace))
    monkeypatch.setattr("sandbox.sandbox_manager", w.manager)

    class NoSharedProvider:
        def __getattr__(self, name):
            raise AssertionError("Private execution reached the ordinary provider: " + name)
    monkeypatch.setattr("sandbox.provider", NoSharedProvider())

    def presign(key, **_):
        w.signed.append(key)
        return "http://127.0.0.1/private-test-object/" + key
    w.oss = SimpleNamespace(presign_get=presign, host="127.0.0.1", internal_host="127.0.0.1", region="test")
    monkeypatch.setattr("core.oss.get_oss", lambda: w.oss)
    yield w
    for client in w.manager._clients.values():
        await client.aclose()


async def test_real_write_tool_uses_dedicated_actor_storage_and_no_public_provider(manager_world):
    w = manager_world
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    other = await w.manager.get_client(w.peer_session.id, user_id=w.peer)
    ctx = ToolContext(session_id=w.session.id, user_id=w.owner, workspace_id=w.workspace, sandbox=client)
    result = await write(WriteArgs(file_path="/workspace/private.txt", content="PRIVATE_CANARY"), ctx)
    assert result.metadata.get("error") is not True
    assert await client.read_file("/workspace/private.txt") == "PRIVATE_CANARY"
    assert await other.read_file("/workspace/private.txt") == ""
    assert client.private_runtime_route.container_id != other.private_runtime_route.container_id
    assert client is await w.manager.get_client(w.session.id, user_id=w.owner)
    assert len(w.daemon.container_rows) == 2
    assert await w.manager.get_only_client() is None


@pytest.mark.parametrize("connecting", [False, True])
async def test_private_cold_start_probes_original_runtime_before_directory_write(manager_world, connecting):
    w = manager_world
    w.starting_probes = 1
    w.connecting_probe = connecting
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    assert [entry[1] for entry in w.sent[:3]] == ["/alive", "/alive", "/execute"]
    assert len(w.daemon.container_rows) == 1
    assert all(entry[0] == client.private_runtime_route.container_id for entry in w.sent)


async def test_private_readiness_does_not_timeout_current_driver_authority(manager_world, monkeypatch):
    w = manager_world
    original = SandboxClient._authorize_request

    async def authorize(client, request):
        if request.url.path == "/alive":
            # A real Task's current-source checks may outlast the HTTP budget.
            # They must finish before deciding whether the server is listening.
            await asyncio.sleep(2.05)
        await original(client, request)

    monkeypatch.setattr(SandboxClient, "_authorize_request", authorize)
    lease = await reserve_run(w.session.id, w.owner)
    token = bind_current_lease(lease)
    try:
        client = await w.manager.get_client(w.session.id, user_id=w.owner)
        assert [entry[1] for entry in w.sent] == ["/alive", "/execute"]
        assert len(w.daemon.container_rows) == 1
        assert all(entry[0] == client.private_runtime_route.container_id for entry in w.sent)
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")


async def test_private_readiness_preserves_authority_refusal_without_dispatch(manager_world, monkeypatch):
    w = manager_world

    async def refused(client, request):
        raise PrivateRuntimeUnavailable("CURRENT_AUTHORITY_REVOKED")

    monkeypatch.setattr(SandboxClient, "_authorize_request", refused)
    with pytest.raises(PrivateRuntimeUnavailable, match="CURRENT_AUTHORITY_REVOKED"):
        await w.manager.get_client(w.session.id, user_id=w.owner)
    assert w.sent == []
    assert w.manager._clients == {}
    assert len(w.daemon.container_rows) == 1


@pytest.mark.parametrize("change", ["membership", "session", "physical", "trace", "response"])
async def test_cached_client_revalidates_both_sides_of_request(manager_world, change):
    w = manager_world
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    before = len(w.sent)
    async with get_db_session() as db:
        if change == "membership": (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        if change == "session": (await db.get(Session, w.session.id)).visibility = "workspace"
    if change == "physical": w.daemon.container_rows[client.private_runtime_route.container_id]["Id"] = "f" * 64
    if change == "response": w.revoke_on_response = True
    async with client.request_context(session_id=w.peer_session.id if change == "trace" else w.session.id):
        with pytest.raises((PrivateRuntimeError, PrivateRuntimeUnavailable)):
            await client.read_file("/workspace/private.txt")
    assert len(w.sent) == before + (change == "response")


async def test_private_driver_cannot_select_a_peer_private_runtime_or_shared_trace(manager_world):
    w = manager_world
    lease = await reserve_run(w.session.id, w.owner)
    token = bind_current_lease(lease)
    try:
        before = len(w.daemon.calls)
        with pytest.raises(PrivateRuntimeUnavailable):
            await w.manager.get_client(w.peer_session.id, user_id=w.peer)
        assert len(w.daemon.calls) == before
        client = await w.manager.get_client(w.session.id, user_id=w.owner)
        await client.write_file("/workspace/owned.txt", "owned")
        async with client.request_context(session_id=w.shared.id):
            with pytest.raises(PrivateRuntimeUnavailable):
                await client.write_file("/workspace/wrong.txt", "wrong")
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")


async def test_private_assets_deliver_only_to_current_owner_runtime(manager_world):
    w = manager_world
    asset = await asset_for(w.owner, w.workspace, session_id=w.session.id, name="private.txt")
    assert await deliver_asset_ids(w.session.id, w.owner, [asset.id]) == ["/workspace/uploads/private.txt"]
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    assert w.signed == [asset.oss_key]
    assert all(cid == client.private_runtime_route.container_id for cid, _, _ in w.sent)
    before = (len(w.sent), len(w.signed))
    async with get_db_session() as db:
        (await db.get(FileAsset, asset.id)).session_id = w.peer_session.id
    with pytest.raises(PrivateRuntimeUnavailable):
        await deliver(client, "private-fixture", w.oss, [asset])
    assert (len(w.sent), len(w.signed)) == before


async def test_a_route_header_cannot_promote_an_unbound_client(manager_world):
    w = manager_world
    owner = await w.manager.get_client(w.session.id, user_id=w.owner)
    route = owner.private_runtime_route
    unbound = SandboxClient(route.host, route.port, route.api_key)
    before = len(w.sent)
    try:
        async with unbound.request_context(session_id=w.session.id):
            with pytest.raises(PrivateRuntimeUnavailable):
                await unbound.write_file("/workspace/not-allowed.txt", "never sent")
        assert len(w.sent) == before
    finally:
        await unbound.aclose()
