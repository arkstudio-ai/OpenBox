"""Real SQL revocation while the production socket pumps are already open."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from auth.socket_access import SocketAccess
from db.base import get_db_session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def revoke(owner, workspace, change="membership"):
    async with get_db_session() as db:
        if change == "membership":
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        elif change == "workspace":
            (await db.get(Workspace, workspace)).is_deleted = True
        elif change == "disabled":
            (await db.get(User, owner)).is_active = False
        elif change == "deleted":
            (await db.get(User, owner)).is_deleted = True


@pytest.mark.parametrize("change", ["membership", "workspace", "disabled", "deleted"])
async def test_access_rechecks_durable_authority_after_handshake(change):
    owner, _, workspace = await accounts()
    access = SocketAccess(owner, workspace, "web", None, True)
    assert await access.check() == "user"
    await revoke(owner, workspace, change)
    with pytest.raises(HTTPException) as denied:
        await access.check()
    assert denied.value.status_code == 403


async def test_ticket_scope_and_current_role_never_follow_mutable_claims_or_default():
    owner, other, workspace = await accounts()
    claims = {"user_id": owner, "workspace_id": workspace, "role": "admin", "client": "web"}
    access = SocketAccess.from_ticket(claims, authenticated=True)
    async with get_db_session() as db:
        alternate = (await db.get(User, other)).default_workspace_id
        (await db.get(User, owner)).default_workspace_id = alternate
    claims["workspace_id"] = alternate
    assert access.workspace_id == workspace
    assert await access.check() == "user"
    async with get_db_session() as db:
        (await db.get(User, owner)).role = "admin"
    assert await access.check() == "admin"
    await revoke(owner, workspace)
    with pytest.raises(HTTPException):
        await access.check()


async def test_legacy_unscoped_ticket_is_refused_and_explicit_single_user_mode_survives():
    with pytest.raises(HTTPException):
        await SocketAccess("default", None, "web", None, True).check()
    assert await SocketAccess("default", None, "web", None, False).check() == "admin"


async def test_mobile_replacement_and_idle_watch_use_the_same_authority():
    from auth.mobile import begin_login
    owner, _, workspace = await accounts()
    sid = await begin_login(owner, "socket-first-installation")
    access = SocketAccess(owner, workspace, "mobile", sid, True)
    assert await access.check() == "user"
    await begin_login(owner, "socket-second-installation")
    with pytest.raises(HTTPException) as denied:
        await access.check()
    assert denied.value.status_code == 401
    watcher = asyncio.create_task(SocketAccess(owner, workspace, "web", None, True).watch(interval=0.01))
    try:
        await revoke(owner, workspace)
        with pytest.raises(HTTPException):
            await asyncio.wait_for(watcher, 2)
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


class BrowserSocket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.outbound = asyncio.Queue()
        self.closed = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        if self.closed:
            raise RuntimeError("Socket already closed")
        self.closed.append(code)
        self.inbound.put_nowait({"type": "websocket.disconnect"})

    async def receive(self):
        return await self.inbound.get()

    async def send_text(self, text):
        self.outbound.put_nowait(text)

    send_bytes = send_text
    send_json = send_text


class RemoteSocket:
    def __init__(self):
        self.inbound = asyncio.Queue()
        self.sent = asyncio.Queue()
        self.closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.inbound.get()

    async def send(self, payload):
        self.sent.put_nowait(payload)


async def relay_fixture(monkeypatch, kind, *, before_resolve=None, authenticated=True, paid=False):
    from api import dev_browser, terminal
    owner, _, workspace = await accounts()
    module = terminal if kind == "terminal" else dev_browser
    identity = {"user_id": owner, "workspace_id": workspace, "role": "user", "client": "web"}
    async def consume(_ticket):
        return identity.copy()
    monkeypatch.setattr(module, "consume_ticket", consume)
    monkeypatch.setattr(module, "is_auth_enabled", lambda: authenticated)
    monkeypatch.setattr(dev_browser, "_active_ws", {})
    browser, remote = BrowserSocket(), RemoteSocket()
    connected = asyncio.Event()
    resolutions = []
    async def resolve(*args, **kwargs):
        resolutions.append((args, kwargs))
        if before_resolve:
            await before_resolve(owner, workspace)
        return SimpleNamespace(id="synthetic-container", status="running", port=9,
                               host="synthetic.invalid", api_key="synthetic-key")
    provider = SimpleNamespace(routes_per_user=paid, get_container=resolve,
                               resolve_user_container=resolve, _api_keys={})
    monkeypatch.setattr(module, "provider", provider)
    @asynccontextmanager
    async def connect(*args, **kwargs):
        connected.set()
        try:
            yield remote
        finally:
            remote.closed.set()
    monkeypatch.setattr(module.websockets, "connect", connect)
    async def route():
        if kind == "terminal":
            await module.terminal_websocket(browser, "synthetic-container", "test-ticket")
        else:
            await module.dev_browser_ws_auto(browser, "test-ticket", "same-browser-client")
    return SimpleNamespace(owner=owner, workspace=workspace, browser=browser, remote=remote,
                           connected=connected, resolutions=resolutions, route=route)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("direction", ["ingress", "egress"])
@pytest.mark.parametrize("binary", [False, True])
async def test_open_relay_stops_next_frame_after_membership_revocation(monkeypatch, kind, direction, binary):
    r = await relay_fixture(monkeypatch, kind)
    task = asyncio.create_task(r.route())
    try:
        await asyncio.wait_for(r.connected.wait(), 2)
        # Both actual pumps work before the revocation commits.
        r.remote.inbound.put_nowait("visible-before")
        assert await asyncio.wait_for(r.browser.outbound.get(), 2) == "visible-before"
        r.browser.inbound.put_nowait({"type": "websocket.receive", "text": "allowed-before"})
        assert await asyncio.wait_for(r.remote.sent.get(), 2) == "allowed-before"
        await revoke(r.owner, r.workspace)
        payload = b"SECRET-AFTER" if binary else "SECRET-AFTER"
        if direction == "ingress":
            r.browser.inbound.put_nowait({"type": "websocket.receive", "bytes" if binary else "text": payload})
        else:
            r.remote.inbound.put_nowait(payload)
        await asyncio.wait_for(task, 2)
        assert r.remote.sent.empty() and r.browser.outbound.empty()
        assert r.remote.closed.is_set() and r.browser.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("when", ["before_handshake", "during_resolution"])
async def test_revocation_blocks_connect_without_opening_remote_transport(monkeypatch, kind, when):
    r = await relay_fixture(monkeypatch, kind, before_resolve=revoke if when == "during_resolution" else None)
    if when == "before_handshake":
        await revoke(r.owner, r.workspace)
    await r.route()
    assert not r.connected.is_set()
    assert r.browser.closed[-1] == 4003
    if when == "before_handshake":
        assert not r.resolutions


@pytest.mark.parametrize("kind", ["terminal", "browser"])
async def test_outbound_relay_also_rechecks_subscription_before_sending_bytes(monkeypatch, kind):
    from sandbox.entitlement import SandboxSubscriptionRequired
    paid = True
    async def subscription(_workspace):
        if not paid:
            raise SandboxSubscriptionRequired()
    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", subscription)
    r = await relay_fixture(monkeypatch, kind, paid=True)
    task = asyncio.create_task(r.route())
    try:
        await asyncio.wait_for(r.connected.wait(), 2)
        r.remote.inbound.put_nowait("before")
        assert await asyncio.wait_for(r.browser.outbound.get(), 2) == "before"
        paid = False
        r.remote.inbound.put_nowait("unpaid-output")
        await asyncio.wait_for(task, 2)
        assert r.browser.outbound.empty() and r.remote.closed.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_agent_ingress_uses_current_role_and_blocks_revoked_membership(monkeypatch):
    from api import ws
    owner, _, workspace = await accounts()
    access = SocketAccess(owner, workspace, "web", None, True)
    received, sent = asyncio.Queue(), asyncio.Queue()
    async def handle(user, role, msg):
        received.put_nowait((user, role, msg))
    monkeypatch.setattr(ws, "_handle_client_message", handle)
    task = asyncio.create_task(ws._receive_loop(SimpleNamespace(receive_text=sent.get), access))
    try:
        sent.put_nowait('{"type":"build.start"}')
        assert await asyncio.wait_for(received.get(), 2) == (owner, "user", {"type": "build.start"})
        await revoke(owner, workspace)
        sent.put_nowait('{"type":"session.abort"}')
        with pytest.raises(HTTPException):
            await asyncio.wait_for(task, 2)
        assert received.empty()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_extension_ticket_is_scoped_when_issued_and_inactive_owner_cannot_get_one(monkeypatch):
    from auth import routes, ticket
    from cache.memory_cache import MemoryCache
    owner, _, workspace = await accounts()
    monkeypatch.setattr(routes, "_cache", MemoryCache())
    monkeypatch.setattr(ticket, "_cache", MemoryCache())
    monkeypatch.setattr(routes, "decode_refresh_token", lambda _: {"sub": owner, "client": "web"})
    first = await routes.extension_auth(routes.ExtensionAuthRequest(refresh_token="test"))
    identity = await ticket.consume_ticket(first["ticket"])
    assert identity["user_id"] == owner and identity["workspace_id"] == workspace
    await revoke(owner, workspace, "disabled")
    with pytest.raises(HTTPException) as denied:
        await routes.extension_auth(routes.ExtensionAuthRequest(refresh_token="test"))
    assert denied.value.status_code == 403


async def test_extension_reconnect_keeps_the_new_socket_and_status_stays_in_its_workspace(monkeypatch):
    from api import browser, dev_browser
    r = await relay_fixture(monkeypatch, "browser")
    first = asyncio.create_task(r.route())
    second = None
    replacement, remote = BrowserSocket(), RemoteSocket()
    connected = asyncio.Event()
    try:
        await asyncio.wait_for(r.connected.wait(), 2)
        @asynccontextmanager
        async def connect(*args, **kwargs):
            connected.set()
            try:
                yield remote
            finally:
                remote.closed.set()
        monkeypatch.setattr(dev_browser.websockets, "connect", connect)
        second = asyncio.create_task(dev_browser.dev_browser_ws_auto(
            replacement, "test-ticket", "same-browser-client"))
        await asyncio.wait_for(connected.wait(), 2)
        await asyncio.wait_for(first, 2)
        assert r.browser.closed[0] == 4001 and r.remote.closed.is_set()
        active = await dev_browser.active_connection(r.owner, r.workspace)
        assert active["ws"] is replacement

        async def local(_user):
            return {"available": False}
        async def preference(_user):
            return "auto"
        monkeypatch.setattr(browser, "_local_status", local)
        monkeypatch.setattr(browser, "get_browser_mode", preference)
        actor = {"user_id": r.owner, "workspace_id": r.workspace}
        assert (await dev_browser.get_extension_status(actor))["connected"] is True
        assert (await browser.get_status(actor))["mode"] == "remote"
        assert (await dev_browser.get_extension_status({**actor, "workspace_id": "another-workspace"}))["connected"] is False
        assert (await browser.get_status({**actor, "workspace_id": "another-workspace"}))["remote"]["connected"] is False

        await revoke(r.owner, r.workspace)
        with pytest.raises(HTTPException):
            await browser.get_status(actor)
        remote.inbound.put_nowait("do-not-forward")
        await asyncio.wait_for(second, 2)
        assert replacement.outbound.empty()
        assert await dev_browser.active_connection(r.owner, r.workspace) is None
    finally:
        for task in (first, second):
            if task:
                task.cancel()
        await asyncio.gather(*(task for task in (first, second) if task), return_exceptions=True)


@pytest.mark.parametrize("kind", ["terminal", "browser"])
@pytest.mark.parametrize("cancellation", ["task", "asgi_scope"])
async def test_cancelled_relay_drains_its_pumps_and_closes_both_sides(monkeypatch, kind, cancellation):
    r = await relay_fixture(monkeypatch, kind)
    if cancellation == "asgi_scope":
        import anyio
        async with anyio.create_task_group() as group:
            group.start_soon(r.route)
            await asyncio.wait_for(r.connected.wait(), 2)
            group.cancel_scope.cancel()
    else:
        task = asyncio.create_task(r.route())
        await asyncio.wait_for(r.connected.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert r.remote.closed.is_set() and r.browser.closed


async def test_revoked_session_workspace_cannot_be_aborted_via_another_valid_socket(monkeypatch):
    from api.ws import _handle_client_message
    from session.session import create_session
    from unittest.mock import AsyncMock
    owner, _, workspace = await accounts()
    session = await create_session(user_id=owner, workspace_id=workspace)
    abort = AsyncMock()
    monkeypatch.setattr("session.abort.abort_session_turn", abort)
    await revoke(owner, workspace)
    await _handle_client_message(owner, "user", {"type": "session.abort", "sessionId": session.id})
    abort.assert_not_awaited()
