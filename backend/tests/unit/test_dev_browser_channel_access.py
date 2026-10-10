"""A real extension relay stays bound to its original SQL desktop channel.

Only cloud-stop I/O is replaced with a failure; tickets, account/subscription,
original Wuying resolution, SQL revocation and both sockets are real loopback.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import pytest
import websockets

from api import dev_browser
from auth import middleware, ticket
from cache.memory_cache import MemoryCache
from core.config import OpenBoxConfig
from db.base import get_db_session, get_engine
from db.models.billing import BillingSubscription, PaymentOrder
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, entitlement, terminal_channel
from sandbox.wuying import WuyingProvider
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_terminal_channel_access import listening, ready, revoke, denied


@pytest.fixture
async def target(monkeypatch):
    owner, _, workspace = await accounts()
    config = OpenBoxConfig(sandbox_provider="wuying", wuying_routing="per_desktop",
        wuying_api_key="unused-shared-fixture", wuying_channel_key="11" * 32,
        jwt_secret="terminal-fixture-signing-key")
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(channel, "get_config", lambda: config)
    monkeypatch.setattr(middleware, "_auth_enabled", True)
    monkeypatch.setattr(ticket, "_cache", MemoryCache())
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    stop = AsyncMock(side_effect=RuntimeError("fixture cloud stop unavailable"))
    monkeypatch.setattr(channel, "run_desktop_command", stop)
    data = SimpleNamespace(owner=owner, workspace=workspace, config=config, stop=stop,
        received=[], connections=0, socket=None, handshake=asyncio.Event(),
        allow_handshake=asyncio.Event(), connected=asyncio.Event(), finished=asyncio.Event(),
        relay_done=asyncio.Event())
    data.allow_handshake.set()
    remote = FastAPI()

    @remote.websocket("/dev-browser/ws")
    async def remote_terminal(ws: WebSocket):
        assert ws.query_params["api_key"] == "terminal-key-fixture"
        data.connections += 1
        data.handshake.set()
        await data.allow_handshake.wait()
        await ws.accept()
        data.socket = ws
        data.connected.set()
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break
                payload = message.get("bytes") or message.get("text")
                data.received.append(payload)
                if isinstance(payload, bytes):
                    await ws.send_bytes(payload)
                else:
                    await ws.send_text(payload)
        except WebSocketDisconnect:
            pass
        finally:
            data.finished.set()

    async with listening(remote) as port:
        stamp = datetime.now(timezone.utc)
        order_id = "terminal-order-" + workspace
        async with get_db_session() as db:
            db.add(PaymentOrder(id=order_id, workspace_id=workspace, user_id=owner,
                request_key=order_id, provider="fixture", amount_fen=100, credits=1,
                currency="CNY", kind="subscription", status="paid", created_at=stamp))
            await db.flush()
            db.add(BillingSubscription(order_id=order_id, workspace_id=workspace, plan_id="pro",
                cycle="monthly", plan={}, starts_at=stamp - timedelta(days=1), ends_at=stamp + timedelta(days=1)))
        data.record = await cloud_desktop_repo.create(workspace, "cn-terminal-fixture", "running",
            user_id=owner, desktop_id="terminal-" + workspace, pool_state="assigned", assigned_at=stamp,
            channel_kind="ssh", tunnel_bind="127.0.0.1", tunnel_port=port, tunnel_state="up",
            tunnel_fingerprint="fixture-" + workspace, tunnel_pubkey="fixture-public-key",
            action_api_key_hash=channel.action_key_hash("terminal-key-fixture"),
            action_api_key_ciphertext=channel.encrypt_action_key("terminal-key-fixture"))
        provider = WuyingProvider()
        monkeypatch.setattr(dev_browser, "provider", provider)
        monkeypatch.setattr(dev_browser, "_active_ws", {})
        app = FastAPI()
        app.include_router(dev_browser.router)

        async def observed_app(scope, receive, send):
            try:
                await app(scope, receive, send)
            finally:
                if scope["type"] == "websocket":
                    data.relay_done.set()

        async with listening(observed_app) as backend_port:
            async def connect():
                issued = await ticket.create_ticket(owner, workspace_id=workspace)
                return await websockets.connect(
                    f"ws://127.0.0.1:{backend_port}/ws/dev-browser/auto?ticket={issued}&client_id=fixture-browser",
                    open_timeout=3, close_timeout=1, proxy=None)
            data.connect, data.provider = connect, provider
            try:
                yield data
            finally:
                data.allow_handshake.set()
                # These are this test's isolated rows/listeners, never an ECD
                # tunnel. Release the unique fixture port after each case.
                await cloud_desktop_repo.update(data.record["id"], tunnel_port=None)


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_browser_relay_rejects_revoked_channel_in_both_directions(target, direction):
    client = await target.connect()
    try:
        await ready(target, client)
        await revoke(target)
        if direction == "input":
            await client.send(b"after-revoke")
        else:
            await target.socket.send_bytes(b"after-revoke")
        await denied(client)
        await asyncio.wait_for(target.finished.wait(), 3)
        await asyncio.wait_for(target.relay_done.wait(), 3)
        assert target.received == [b"\x00before"]
        assert (target.owner, target.workspace) not in dev_browser._active_ws
    finally:
        await client.close()


@pytest.mark.parametrize("when", ["after_resolution", "during_remote_handshake"])
async def test_browser_relay_cannot_use_route_revoked_during_connect(target, monkeypatch, when):
    waiting, release = asyncio.Event(), asyncio.Event()
    original = entitlement.require_sandbox_subscription
    calls = 0

    async def subscribed(workspace):
        nonlocal calls
        result = await original(workspace)
        calls += 1
        if when == "after_resolution" and calls == 2:
            waiting.set()
            await release.wait()
        return result

    monkeypatch.setattr(entitlement, "require_sandbox_subscription", subscribed)
    if when == "during_remote_handshake":
        target.allow_handshake.clear()
    client = await target.connect()
    try:
        await asyncio.wait_for((waiting if when == "after_resolution" else target.handshake).wait(), 3)
        assert get_engine().sync_engine.pool.checkedout() == 0
        await revoke(target)
        release.set()
        target.allow_handshake.set()
        await client.send(b"late-input")
        await denied(client)
        assert target.received == []
        assert target.connections == (0 if when == "after_resolution" else 1)
    finally:
        release.set()
        target.allow_handshake.set()
        await client.close()


async def test_browser_connection_status_does_not_report_a_revoked_channel_connected(target):
    client = await target.connect()
    try:
        await ready(target, client)
        actor = {"user_id": target.owner, "workspace_id": target.workspace}
        assert (await dev_browser.get_extension_status(actor))["connected"] is True
        await revoke(target)
        assert (await dev_browser.get_extension_status(actor)) == {"has_link": False, "connected": False}
    finally:
        await client.close()


async def test_idle_browser_relay_is_closed_after_channel_revocation(target, monkeypatch):
    monkeypatch.setattr(terminal_channel, "WATCH_INTERVAL_SECONDS", .02)
    client = await target.connect()
    try:
        await ready(target, client)
        await revoke(target)
        await denied(client)
        await asyncio.wait_for(target.relay_done.wait(), 3)
        assert target.received == [b"\x00before"]
        assert (target.owner, target.workspace) not in dev_browser._active_ws
    finally:
        await client.close()


async def test_browser_relay_never_adopts_a_rotated_channel_key(target):
    client = await target.connect()
    try:
        await ready(target, client)
        await cloud_desktop_repo.update(target.record["id"],
            action_api_key_ciphertext=channel.encrypt_action_key("replacement-key"))
        await client.send("replacement-must-not-receive")
        await denied(client)
        assert target.received == [b"\x00before"] and target.connections == 1
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["docker_registry", "shared_wuying"])
async def test_browser_ordinary_providers_keep_actual_resolution_and_transport(target, monkeypatch, kind):
    if kind == "docker_registry":
        from sandbox.docker import DockerManager
        provider = object.__new__(DockerManager)
        info = target.provider._record_container(target.record)
        provider._containers = {info.id: info}
        provider._container_owners = {info.id: target.workspace}
    else:
        target.config.wuying_routing = "shared"
        target.config.wuying_endpoint = f"http://127.0.0.1:{target.record['tunnel_port']}"
        target.config.wuying_api_key = "terminal-key-fixture"
        provider = WuyingProvider()
    monkeypatch.setattr(dev_browser, "provider", provider)
    client = await target.connect()
    try:
        await ready(target, client)
        await client.send("ordinary-text")
        assert await asyncio.wait_for(client.recv(), 3) == "ordinary-text"
        assert (await dev_browser.get_extension_status(
            {"user_id": target.owner, "workspace_id": target.workspace}))["connected"] is True
        await client.close()
        await asyncio.wait_for(target.relay_done.wait(), 3)
        assert get_engine().sync_engine.pool.checkedout() == 0
    finally:
        await client.close()
