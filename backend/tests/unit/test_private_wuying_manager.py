"""The real manager/client binds private IO to the original Wuying actor path.

Dormant: the assistant's main conversation is the only private audience left
(see test_private_wuying_runtime), so these cases pin the actor path for it.
Delegated work uses the shared runtime (test_assistant_private_runtime).
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
from sandbox.manager import SandboxManager
from sandbox.privacy import PrivateRuntimeUnavailable
from sandbox.private_runtime import PrivateRuntimeError
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_private_wuying_runtime import assistant_database, wuying_world  # noqa: F401


@pytest.fixture
async def manager_world(wuying_world, monkeypatch):
    w = wuying_world
    w.manager, w.files, w.business_requests, w.signed = SandboxManager(), {}, [], []
    w.revoke_after_read = False

    async def remote(request):
        scope = request.headers["X-OpenBox-Private-Scope"]
        guest = w.proofs[scope]
        prefix = "/private-runtime/" + guest["id"]
        assert request.url.path.startswith(prefix + "/")
        assert request.headers["X-OpenBox-Private-Attempt"] == guest["attempt_id"]
        operation = request.url.path[len(prefix):]
        body = json.loads(request.content) if request.content else {}
        w.business_requests.append((scope, operation))
        store = w.files.setdefault(scope, {})
        if operation == "/alive": result = {"status": "ok"}
        elif operation == "/execute": result = {"exit_code": 0, "stdout": "", "stderr": ""}
        elif operation == "/write_file":
            store[body["path"]] = body["content"]
            result = {"written": True}
        elif operation == "/read_file":
            result = {"content": store.get(body["path"], "")}
            if w.revoke_after_read:
                async with get_db_session() as db:
                    (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
        else: raise AssertionError("Unexpected Wuying actor operation: " + operation)
        return result
    w.remote_handler = remote
    original = httpx.AsyncClient
    monkeypatch.setattr(client_module, "httpx", SimpleNamespace(**{**vars(httpx),
        "AsyncHTTPTransport": lambda **_: w.transport,
        "AsyncClient": lambda **kwargs: original(**{**kwargs, "transport": kwargs.get("transport") or w.transport})}))
    monkeypatch.setattr("sandbox.sandbox_manager", w.manager)

    def presign(key, **_):
        w.signed.append(key)
        return "http://127.0.0.1/fixture-object/" + key
    w.oss = SimpleNamespace(presign_get=presign, host="127.0.0.1", internal_host="127.0.0.1", region="test")
    monkeypatch.setattr("core.oss.get_oss", lambda: w.oss)
    yield w
    for client in w.manager._clients.values():
        await client.aclose()


async def test_manager_preserves_wuying_desktop_and_distinct_actor_paths(manager_world):
    w = manager_world
    owner = await w.manager.get_client(w.session.id, user_id=w.owner)
    peer = await w.manager.get_client(w.peer_session.id, user_id=w.peer)
    assert owner.desktop_id == peer.desktop_id == w.config.wuying_desktop_id
    assert owner.workspace_id == peer.workspace_id == w.workspace
    assert owner.base_url != peer.base_url
    assert owner.private_runtime_route.provider == peer.private_runtime_route.provider == "private_wuying_v1"
    await owner.write_file("/workspace/fixture.txt", "only-actor-fixture")
    assert await owner.read_file("/workspace/fixture.txt") == "only-actor-fixture"
    assert await peer.read_file("/workspace/fixture.txt") == ""
    assert owner is await w.manager.get_client(w.session.id, user_id=w.owner)
    assert await w.manager.get_only_client() is None


@pytest.mark.parametrize("change", ["membership", "response", "path", "scope", "attempt", "trace", "provider"])
async def test_private_client_rejects_changed_authority_and_fixed_transport(manager_world, change):
    w = manager_world
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    before = len(w.business_requests)
    if change == "membership":
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
    if change == "response": w.revoke_after_read = True
    if change == "path": client.base_url = w.config.wuying_endpoint
    if change == "scope": client._headers["X-OpenBox-Private-Scope"] = "f" * 64
    if change == "attempt": client._headers["X-OpenBox-Private-Attempt"] = "replacement_attempt"
    if change == "provider": w.config.sandbox_provider = "docker"
    async with client.request_context(session_id=w.peer_session.id if change == "trace" else w.session.id):
        with pytest.raises((PrivateRuntimeError, PrivateRuntimeUnavailable)):
            await client.read_file("/workspace/fixture.txt")
    assert len(w.business_requests) == before + (change == "response")


async def test_current_driver_cannot_borrow_a_peer_private_guest_scope(manager_world):
    w = manager_world
    lease = await reserve_run(w.session.id, w.owner)
    token = bind_current_lease(lease)
    try:
        with pytest.raises(PrivateRuntimeUnavailable):
            await w.manager.get_client(w.peer_session.id, user_id=w.peer)
        assert w.sent == [] and w.business_requests == []
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")


async def test_attachment_source_is_current_before_signing_and_uses_actor_path(manager_world):
    w = manager_world
    asset = await asset_for(w.owner, w.workspace, session_id=w.session.id, name="fixture.txt")
    assert await deliver_asset_ids(w.session.id, w.owner, [asset.id]) == ["/workspace/uploads/fixture.txt"]
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    assert w.signed == [asset.oss_key]
    assert all(scope == client.private_runtime_route.scope_id for scope, _ in w.business_requests)
    before = (len(w.business_requests), len(w.signed))
    async with get_db_session() as db:
        (await db.get(FileAsset, asset.id)).session_id = w.peer_session.id
    with pytest.raises(PrivateRuntimeUnavailable):
        await deliver(client, "fixture", w.oss, [asset])
    assert (len(w.business_requests), len(w.signed)) == before


@pytest.mark.parametrize("revocation", ["membership", "session"])
async def test_buffered_private_response_revalidates_after_delayed_body(manager_world, revocation):
    w = manager_world
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    events = []

    class DelayedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            events.append("body-started")
            # The HTTPX response hook has already observed the headers here.
            # Commit in a distinct SQL session while the response is incomplete.
            await asyncio.sleep(0)
            async with get_db_session() as db:
                if revocation == "membership":
                    (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
                else:
                    # Only the assistant conversation is private; a Session
                    # that stops being it loses the private audience.
                    (await db.get(Session, w.session.id)).kind = "normal"
            events.append("revoked")
            yield b'{"content":"private delayed body"}'

    async def delayed(request):
        assert request.url.path.endswith("/read_file")
        events.append("response-headers")
        return httpx.Response(200, stream=DelayedBody(), headers={"content-type": "application/json"})

    w.remote_handler = delayed
    with pytest.raises((PrivateRuntimeError, PrivateRuntimeUnavailable)):
        await client.read_file("/workspace/private.txt")
    assert events == ["response-headers", "body-started", "revoked"]


async def test_private_v1_search_uses_only_supported_actor_endpoints(manager_world):
    w = manager_world
    client = await w.manager.get_client(w.session.id, user_id=w.owner)
    paths = []

    async def search(request):
        paths.append(request.url.path)
        if request.url.path.endswith("/openapi.json"):
            return httpx.Response(409, json={"detail": "Unsupported private actor operation"})
        payload = json.loads(request.content)
        assert payload["include_sensitive"] is False
        if request.url.path.endswith("/glob"):
            return {"files": ["/workspace/fixture.txt"]}
        assert request.url.path.endswith("/grep")
        return {"output": "fixture.txt:1:fixture", "exit_code": 0}

    w.remote_handler = search
    assert await client.glob("*.txt") == ["/workspace/fixture.txt"]
    assert await client.grep("fixture") == "fixture.txt:1:fixture"
    prefix = client.private_runtime_route.base_url.split(str(w.config.wuying_endpoint), 1)[1]
    assert paths == [prefix + "/glob", prefix + "/grep"]
