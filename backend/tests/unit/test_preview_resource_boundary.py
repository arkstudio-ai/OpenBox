"""Real local HTTP preview hops cannot turn browser headers into control authority.

Only disposable SQL rows, journals and loopback servers are used. There is no
cloud request, desktop process, browser, or production preview application.
"""
import asyncio
from contextlib import asynccontextmanager
import socket
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import httpx
import pytest
from sqlalchemy import func, select
import uvicorn

from api import containers as routes
from assistant import resource_commands as commands
from assistant.service import get_main_session
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from sandbox.client import SandboxClient
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource  # noqa: F401
from tests.unit.test_action_server_desktop_lease import server
from resource_gate import Fence, ResourceGate


@asynccontextmanager
async def listening(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runtime = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
        log_level="error", ws="none"))
    task = asyncio.create_task(runtime.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not runtime.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield port
    finally:
        runtime.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


def internal(name):
    return name.lower() == "x-api-key" or name.lower().startswith("x-openbox-")


def control_headers(fence, journal, *, duplicate=False):
    values = {
        "X-OpenBox-Resource": fence.resource_id,
        "X-OpenBox-Resource-Epoch": str(fence.epoch),
        "X-OpenBox-Resource-Owner": fence.owner_kind,
        "X-OpenBox-Resource-Owner-Id": fence.owner_id,
        "X-OpenBox-Resource-Journal": journal.status()["journal_id"],
        "X-OpenBox-Resource-Operation": "browser-invented-effect",
        "X-OpenBox-Resource-Step": "browser-invented-step",
        "X-OpenBox-Remote-Operation": "browser-invented-receipt",
        "X-OpenBox-Desktop-Lease": "private-lease-fixture",
        "X-OpenBox-User-Scope": "private-scope-fixture",
        "X-OpenBox-Request": "private-trace-fixture",
        "X-OpenBox-Future-Control": "reserved-namespace-fixture",
    }
    headers = list(values.items())
    if duplicate:
        headers = [(name.swapcase(), value) for name, value in headers] + [
            (name.lower(), value) for name, value in headers]
    return headers


@pytest.fixture
async def preview_chain(tmp_path, monkeypatch):
    # Both production HTTP clients still use actual sockets. Keep even a
    # developer's inherited HTTP proxy out of these loopback-only fixtures.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    journal = ResourceGate(tmp_path / "preview-journal.sqlite3")
    monkeypatch.setattr(server, "_resource_gate", journal)
    monkeypatch.setattr(server, "SESSION_API_KEY", "service-key-fixture")
    monkeypatch.setattr(server, "_desktop_lease", None)
    applications, actions = [], []
    action_finished = asyncio.Event()
    application = FastAPI()

    @application.api_route("/{path:path}", methods=["GET", "POST"])
    async def echo(request: Request, path: str):
        received = {"headers": dict(request.headers), "path": path,
            "query": request.url.query, "body": (await request.body()).decode(),
            "method": request.method}
        applications.append(received)
        result = JSONResponse(received, headers={"X-App-Result": "preview-ok",
            "Set-Cookie": "preview=accepted; Path=/"})
        # An application is also not a trusted source of control responses.
        for name in ("X-API-Key", "X-OpenBox-Resource-Journal", "X-OpenBox-Remote-Operation"):
            result.raw_headers.extend([(name.encode(), b"app-forged"),
                (name.lower().encode(), b"app-forged-duplicate")])
        return result

    async def action(scope, receive, send):
        is_proxy = scope["type"] == "http" and scope["path"].startswith("/proxy/")
        if is_proxy:
            actions.append(httpx.Headers(scope["headers"]))
        try:
            await server.app(scope, receive, send)
        finally:
            if is_proxy:
                action_finished.set()

    async with listening(application) as application_port, listening(action) as action_port:
        info = SimpleNamespace(host="127.0.0.1", port=action_port, api_key="service-key-fixture")

        async def resolve(container_id):
            assert container_id == "preview-fixture"
            return info

        provider = SimpleNamespace(get_container=resolve)
        monkeypatch.setattr(routes, "provider", provider)
        backend = FastAPI()
        backend.include_router(routes.preview_router)
        async with listening(backend) as backend_port, httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{backend_port}", trust_env=False,
        ) as client:
            yield SimpleNamespace(client=client, journal=journal, info=info,
                application_port=application_port, actions=actions, applications=applications,
                action_finished=action_finished,
                provider=provider, path=f"/api/containers/preview-fixture/preview/{application_port}/echo")


@pytest.fixture
async def controlled_preview(resource, preview_chain, monkeypatch):
    target = preview_chain
    fence, _, _, enrollment = resource
    scope = {"user_id": enrollment["user_id"], "workspace_id": enrollment["workspace_id"]}
    scope["main_id"] = (await get_main_session(**scope)).id
    client = SandboxClient(target.info.host, target.info.port, target.info.api_key,
        desktop_id=enrollment["desktop_id"], workspace_id=enrollment["workspace_id"])

    async def subscribed(_workspace):
        return None

    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", subscribed)

    @asynccontextmanager
    async def remote(_desktop):
        yield client

    monkeypatch.setattr(commands, "remote_client", remote)

    async def accept(action):
        return await commands.accept_resource_command(**scope, resource_id=fence.resource_id,
            expected_epoch=fence.epoch, idempotency_key=action, action=action)

    bound = await accept("bind")
    assert await commands.dispatch(bound["command_id"])
    target.accept, target.fence = accept, fence
    try:
        yield target
    finally:
        await client.aclose()
        # PostgreSQL fixtures retain rows. Do not let this fixture's expired
        # lease affect another test's global expiry scanner.
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, fence.resource_id)
            row.admission_state, row.status = "closed", "hold"


@pytest.mark.parametrize("timing", ["open", "already_closed", "close_while_waiting"])
@pytest.mark.parametrize("duplicate", [False, True])
async def test_browser_cannot_forge_control_even_before_remote_close_arrives(controlled_preview, timing, duplicate):
    target = controlled_preview
    entered, release = asyncio.Event(), asyncio.Event()
    original = target.provider.get_container

    async def delayed(container_id):
        info = await original(container_id)
        entered.set()
        await release.wait()
        return info

    target.provider.get_container = delayed
    if timing == "already_closed":
        await target.accept("close")
    request = asyncio.create_task(target.client.post(target.path,
        headers=control_headers(target.fence, target.journal, duplicate=duplicate), content=b"input"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        if timing == "close_while_waiting":
            # This real command commits in a separate SQL transaction while
            # the already received browser request is waiting to be forwarded.
            await target.accept("close")
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, target.fence.resource_id)
            assert row.admission_state == ("open" if timing == "open" else "closed")
            assert row.remote_journal_id == target.journal.status()["journal_id"]
            assert await db.scalar(select(func.count()).select_from(ExternalEffect).where(
                ExternalEffect.id == "browser-invented-effect")) == 0
        # The remote close has deliberately not been sent. Possession of
        # visible epoch/owner/journal values alone used to cross this window.
        assert target.journal.status()["control"]["admission"] == "open"
        release.set()
        response = await request
        await asyncio.wait_for(target.action_finished.wait(), 3)
    finally:
        release.set()
        if not request.done():
            await request
    assert response.status_code == 423, response.text
    assert response.json()["detail"] == "RESOURCE_CONTROL_HELD"
    assert target.applications == []
    assert len(target.actions) == 1
    assert target.actions[0].get_list("x-api-key") == ["service-key-fixture"]
    assert not any(name.startswith("x-openbox-") for name in target.actions[0])
    assert target.journal.status()["blocking_count"] == 0
    assert target.journal.status()["remote_exclusivity_verified"] is False


@pytest.mark.parametrize("configured", [False, True])
async def test_unmanaged_preview_keeps_app_auth_and_body_without_internal_headers(preview_chain, monkeypatch, configured):
    target = preview_chain
    if not configured:
        monkeypatch.setattr(server, "_resource_gate", None)
    fence = Fence("a" * 64, 1, "automation", "fixture-workspace")
    headers = control_headers(fence, target.journal, duplicate=True) + [
        ("X-API-Key", "browser-key-one"), ("x-api-key", "browser-key-two"),
        ("Authorization", "Bearer application-token"), ("Cookie", "application=session"),
        ("X-App-Input", "keep-me"), ("Origin", "http://preview.example")]
    response = await target.client.post(target.path + "?page=2", headers=headers, content=b"application-body")
    await asyncio.wait_for(target.action_finished.wait(), 3)
    assert response.status_code == 200, response.text
    received, = target.applications
    assert received["method"] == "POST" and received["path"] == "echo"
    assert received["query"] == "page=2" and received["body"] == "application-body"
    assert received["headers"]["authorization"] == "Bearer application-token"
    assert received["headers"]["cookie"] == "application=session"
    assert received["headers"]["x-app-input"] == "keep-me"
    assert received["headers"]["origin"] == "http://preview.example"
    assert not any(internal(name) for name in received["headers"])
    assert target.actions[0].get_list("x-api-key") == ["service-key-fixture"]
    assert not any(name.startswith("x-openbox-") for name in target.actions[0])
    assert response.headers["x-app-result"] == "preview-ok"
    assert response.headers["set-cookie"] == "preview=accepted; Path=/"
    assert not any(internal(name) for name in response.headers)
    if configured:
        # No browser-chosen identity was recorded, even in an unbound journal.
        operations = target.journal.status()["blocking_operations"]
        assert len(operations) == 1 and operations[0]["id"].startswith("legacy_")


@pytest.mark.parametrize("configured", [False, True])
async def test_action_server_does_not_disclose_trusted_headers_to_app(preview_chain, monkeypatch, configured):
    target = preview_chain
    fence = Fence("b" * 64, 1, "automation", "fixture-workspace")
    if configured:
        target.journal.bind(fence, "server-bind", target.journal.status()["journal_id"])
    else:
        monkeypatch.setattr(server, "_resource_gate", None)
    # This isolates the second boundary: a server caller possesses the real
    # service credential. Its private headers must stop before the user app.
    headers = control_headers(fence, target.journal, duplicate=True) + [
        ("X-API-Key", "service-key-fixture"), ("x-api-key", "service-key-fixture"),
        ("Authorization", "Bearer application-token"), ("Cookie", "application=session")]
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.get(f"http://127.0.0.1:{target.info.port}/proxy/{target.application_port}/echo",
            headers=headers)
    await asyncio.wait_for(target.action_finished.wait(), 3)
    assert response.status_code == 200, response.text
    received, = target.applications
    assert received["headers"]["authorization"] == "Bearer application-token"
    assert received["headers"]["cookie"] == "application=session"
    assert not any(internal(name) for name in received["headers"])
    assert "x-api-key" not in response.headers
    if configured:
        # The supervisor still emits its own receipt to the trusted caller,
        # replacing the forged response from the preview application.
        assert response.headers["x-openbox-remote-operation"] == "browser-invented-step"
        assert response.headers["x-openbox-resource-journal"] == target.journal.status()["journal_id"]
        assert target.journal.receipt("browser-invented-step")["state"] == "unknown"
        assert target.journal.status()["remote_exclusivity_verified"] is False
    else:
        assert "x-openbox-remote-operation" not in response.headers
        assert "x-openbox-resource-journal" not in response.headers
