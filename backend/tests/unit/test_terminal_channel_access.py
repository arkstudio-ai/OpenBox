"""A real local terminal relay must not outlive its durable channel authority.

Ticket consumption, SQL authority/subscription checks, provider resolution and
channel revocation are real. Only the cloud tunnel-stop IO is made to fail;
both WebSocket transports stay on loopback and no shell or desktop is started.
"""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import pytest
from sqlalchemy import event
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from api import terminal
from auth import middleware, ticket
from cache.memory_cache import MemoryCache
from core.config import OpenBoxConfig
from db.base import get_db_session, get_engine
from db.models.billing import BillingSubscription, PaymentOrder
from db.models.cloud_desktop import CloudDesktop
from db.models.workspace import WorkspaceMember
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, entitlement, terminal_channel
from sandbox.wuying import WuyingProvider
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@asynccontextmanager
async def listening(app):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runtime = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
        log_level="error", ws="websockets"))
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
        allow_handshake=asyncio.Event(), connected=asyncio.Event(), finished=asyncio.Event())
    data.allow_handshake.set()
    remote = FastAPI()
    data.remote_app = remote

    @remote.websocket("/terminal")
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
        monkeypatch.setattr(terminal, "provider", provider)
        app = FastAPI()
        app.include_router(terminal.router)
        async with listening(app) as backend_port:
            async def connect():
                issued = await ticket.create_ticket(owner, workspace_id=workspace)
                return await websockets.connect(
                    f"ws://127.0.0.1:{backend_port}/ws/terminal/{data.record['desktop_id']}?ticket={issued}",
                    open_timeout=3, close_timeout=1, proxy=None)
            data.connect, data.provider = connect, provider
            try:
                yield data
            finally:
                data.allow_handshake.set()
                # These are this test's isolated rows/listeners, never an ECD
                # tunnel. Release the unique fixture port after each case.
                await cloud_desktop_repo.update(data.record["id"], tunnel_port=None)


async def ready(target, client):
    await client.send(b"\x00before")
    assert await asyncio.wait_for(client.recv(), 3) == b"\x00before"
    assert target.received == [b"\x00before"]


async def revoke(target):
    await channel.wuying_channel.revoke(target.record)
    target.stop.assert_awaited_once()
    async with get_db_session() as db:
        record = await db.get(CloudDesktop, target.record["id"])
        assert record.tunnel_state == "revoked" and record.tunnel_port == target.record["tunnel_port"]


async def denied(client):
    with pytest.raises(ConnectionClosed) as closed:
        payload = await asyncio.wait_for(client.recv(), 3)
        pytest.fail(f"revoked channel still emitted payload: {payload!r}")
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4003


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_committed_revocation_stops_next_frame_despite_failed_cloud_stop(target, direction):
    client = await target.connect()
    try:
        await ready(target, client)
        await revoke(target)
        if direction == "input":
            await client.send(b"\x00after-revoke")
        else:
            await target.socket.send_bytes(b"\x00after-revoke")
        await denied(client)
        await asyncio.wait_for(target.finished.wait(), 3)
        assert target.received == [b"\x00before"]
    finally:
        await client.close()


@pytest.mark.parametrize("when", ["after_resolution", "during_remote_handshake"])
async def test_revocation_while_connecting_cannot_adopt_or_use_stale_channel(target, monkeypatch, when):
    waiting, release = asyncio.Event(), asyncio.Event()
    normal = entitlement.require_sandbox_subscription
    calls = 0

    async def subscribed(workspace):
        nonlocal calls
        result = await normal(workspace)
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
        if when == "during_remote_handshake":
            # Route and authorization reads ended before waiting on network IO.
            assert get_engine().sync_engine.pool.checkedout() == 0
        await revoke(target)
        release.set()
        target.allow_handshake.set()
        await client.send(b"\x00late-input")
        await denied(client)
        assert target.received == []
        assert target.connections == (0 if when == "after_resolution" else 1)
    finally:
        release.set()
        target.allow_handshake.set()
        await client.close()


async def test_idle_channel_is_closed_without_another_client_or_remote_frame(target, monkeypatch):
    monkeypatch.setattr(terminal_channel, "WATCH_INTERVAL_SECONDS", .02)
    client = await target.connect()
    try:
        await ready(target, client)
        await revoke(target)
        await denied(client)
        await asyncio.wait_for(target.finished.wait(), 3)
        assert target.received == [b"\x00before"]
    finally:
        await client.close()


@pytest.mark.parametrize("field", [
    "id", "desktop_id", "region_id", "workspace_id", "assigned_at", "pool_state",
    "channel_kind", "private_ip", "tunnel_bind", "tunnel_port", "tunnel_fingerprint",
    "tunnel_pubkey", "action_api_key_hash", "action_api_key_ciphertext",
])
async def test_live_socket_never_adopts_a_replacement_physical_channel(target, field):
    client = await target.connect()
    try:
        await ready(target, client)
        value = {
            "id": "replacement-" + target.record["id"],
            "desktop_id": "replacement-" + target.workspace,
            "region_id": "cn-other-fixture", "workspace_id": None,
            "assigned_at": target.record["assigned_at"] + timedelta(seconds=1),
            "pool_state": "released", "channel_kind": "direct",
            "private_ip": "127.0.0.2", "tunnel_bind": "localhost", "tunnel_port": None,
            "tunnel_fingerprint": "rotated-" + target.workspace,
            "tunnel_pubkey": "rotated-fixture-public-key", "action_api_key_hash": "0" * 64,
            # Even rotation with the same plaintext is a different credential version.
            "action_api_key_ciphertext": channel.encrypt_action_key("terminal-key-fixture"),
        }[field]
        await cloud_desktop_repo.update(target.record["id"], **{field: value})
        if field == "id":
            target.record["id"] = value  # Only fixture teardown follows the renamed SQL row.
        await client.send(b"\x00replacement-must-not-receive")
        await denied(client)
        await asyncio.wait_for(target.finished.wait(), 3)
        assert target.received == [b"\x00before"] and target.connections == 1
    finally:
        await client.close()


async def test_unrevoked_channel_keeps_text_binary_and_health_metadata_updates(target):
    client = await target.connect()
    try:
        await ready(target, client)
        await cloud_desktop_repo.update(target.record["id"],
            last_seen_at=datetime.now(timezone.utc), channel_error=None)
        await client.send('resize:{"cols":120,"rows":40}')
        assert await asyncio.wait_for(client.recv(), 3) == 'resize:{"cols":120,"rows":40}'
        await target.socket.send_bytes(b"\x00ordinary-output")
        assert await asyncio.wait_for(client.recv(), 3) == b"\x00ordinary-output"
        assert target.connections == 1
    finally:
        await client.close()
    await asyncio.wait_for(target.finished.wait(), 3)


async def test_real_account_revocation_still_stops_existing_terminal(target):
    client = await target.connect()
    try:
        await ready(target, client)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (target.workspace, target.owner))).status = "removed"
        await target.socket.send_bytes(b"\x00membership-revoked")
        await denied(client)
        assert target.received == [b"\x00before"]
    finally:
        await client.close()


@pytest.mark.parametrize("kind", ["docker_registry", "shared_wuying"])
async def test_providers_without_a_physical_sql_channel_keep_their_contract(target, monkeypatch, kind):
    # Exercise their actual get_container implementations and the real relay.
    # The Docker registry is seeded locally; no daemon/physical host is used.
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
    monkeypatch.setattr(terminal, "provider", provider)
    client = await target.connect()
    try:
        await ready(target, client)
        await client.send("ordinary-text")
        assert await asyncio.wait_for(client.recv(), 3) == "ordinary-text"
    finally:
        await client.close()


async def test_each_channel_check_has_one_unlocked_select_and_returns_its_connection(target):
    _, access = await terminal_channel.resolve_terminal_channel(
        target.provider, target.record["desktop_id"], target.workspace)
    engine = get_engine().sync_engine
    statements = []

    def observe(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.upper())

    event.listen(engine, "before_cursor_execute", observe)
    try:
        for _ in range(3):
            await access.check()
            assert engine.pool.checkedout() == 0
    finally:
        event.remove(engine, "before_cursor_execute", observe)
    assert len(statements) == 3
    assert all(sql.startswith("SELECT ") and "WHERE CLOUD_DESKTOPS.ID =" in sql
               and "FOR UPDATE" not in sql and "FOR SHARE" not in sql for sql in statements)


@pytest.mark.parametrize("cancellation", ["task", "asgi_scope"])
async def test_cancelled_channel_check_drains_its_real_reader_before_return(target, monkeypatch, cancellation):
    import anyio
    _, access = await terminal_channel.resolve_terminal_channel(
        target.provider, target.record["desktop_id"], target.workspace)
    normal = terminal_channel.get_db_session
    read, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def delayed_close():
        async with normal() as db:
            yield db  # The actual primary-key SELECT executes before this barrier.
            assert db.in_transaction()
            read.set()
            await release.wait()
        closed.set()

    monkeypatch.setattr(terminal_channel, "get_db_session", delayed_close)
    scope = None

    async def checking():
        nonlocal scope
        if cancellation == "asgi_scope":
            with anyio.CancelScope() as scope:
                await access.check()
        else:
            await access.check()

    task = asyncio.create_task(checking())
    try:
        await asyncio.wait_for(read.wait(), 3)
        assert get_engine().sync_engine.pool.checkedout() == 1
        if cancellation == "asgi_scope":
            scope.cancel()
        else:
            task.cancel()
        await asyncio.sleep(.01)
        assert not task.done() and not closed.is_set()
        release.set()
        outcome = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        if cancellation == "task":
            assert isinstance(outcome[0], asyncio.CancelledError)
        assert closed.is_set() and get_engine().sync_engine.pool.checkedout() == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
