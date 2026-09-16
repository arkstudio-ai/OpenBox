"""Real HTTP/1.1 connections: pooling, request isolation, and lifecycle."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from models.container import ContainerStatus
from sandbox.client import SandboxClient, USER_SCOPE_HEADER, user_scope_for
from sandbox.manager import SandboxInfo, SandboxManager, _map_key


@pytest.fixture
async def action_server():
    state = SimpleNamespace(requests=[], peers=set(), tasks=set(), connection_count=0,
                            blocked=asyncio.Event(), unblock=asyncio.Event())

    async def handle(reader, writer):
        state.connection_count += 1
        connection = state.connection_count
        state.peers.add(writer)
        state.tasks.add(asyncio.current_task())
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                path = lines[0].split()[1]
                headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
                await reader.readexactly(int(headers.get("content-length", 0)))
                state.requests.append((connection, path, headers))
                if path == "/blocked":
                    state.blocked.set()
                    await state.unblock.wait()
                if path == "/slow":
                    await asyncio.sleep(0.15)
                body = json.dumps({"exit_code": 0, "stdout": "ok", "stderr": ""}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"Set-Cookie: operation_cookie=private\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            state.peers.discard(writer)
            state.tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    state.port = server.sockets[0].getsockname()[1]
    try:
        yield state
    finally:
        state.unblock.set()
        server.close()
        await server.wait_closed()
        tasks = list(state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def sandbox_for(server, user="alice", **kwargs):
    return SandboxClient("127.0.0.1", server.port, f"key-{user}",
                         user_scope=user_scope_for(user), reuse_connections=True, **kwargs)


async def wait_for_disconnects(server):
    async with asyncio.timeout(1):
        while server.peers:
            await asyncio.sleep(0)


async def test_sequential_operations_reuse_tcp_without_sharing_cookies(action_server):
    client = sandbox_for(action_server)
    try:
        async with client.request_context(session_id="session-a", tool_call_id="call-a"):
            await client.execute("true")
        assert await client.alive()
        async with client.request_context(session_id="session-b", tool_call_id="call-b"):
            await client.execute("true")
        assert action_server.connection_count == 1
        assert all("cookie" not in headers for _, _, headers in action_server.requests)
        first, _, last = action_server.requests
        assert first[2]["x-openbox-session"] == "session-a"
        assert last[2]["x-openbox-session"] == "session-b"
        assert first[2]["x-openbox-request"] != last[2]["x-openbox-request"]
    finally:
        await client.aclose()
    await wait_for_disconnects(action_server)


async def test_pools_do_not_cross_user_credentials_and_authorize_every_request(action_server, monkeypatch):
    authorize = AsyncMock()
    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", authorize)
    alice = sandbox_for(action_server, workspace_id="workspace-a")
    bob = sandbox_for(action_server, "bob", workspace_id="workspace-b")
    try:
        for client in (alice, bob, alice, bob):
            await client.execute("true")
        assert action_server.connection_count == 2
        for user, request in zip(("alice", "bob", "alice", "bob"), action_server.requests):
            assert request[2]["x-api-key"] == f"key-{user}"
            assert request[2][USER_SCOPE_HEADER.lower()] == user_scope_for(user)
        assert [call.args[0] for call in authorize.await_args_list] == [
            "workspace-a", "workspace-b", "workspace-a", "workspace-b"]
        authorize.side_effect = PermissionError("subscription revoked")
        with pytest.raises(PermissionError):
            await alice.execute("true")
        assert len(action_server.requests) == 4
    finally:
        await alice.aclose()
        await bob.aclose()


async def test_concurrent_operations_keep_independent_timeouts(action_server):
    client = sandbox_for(action_server)
    try:
        short, long = await asyncio.gather(
            client._get("/slow", timeout=0.04), client._get("/slow", timeout=1),
            return_exceptions=True,
        )
        assert isinstance(short, httpx.ReadTimeout)
        assert long["exit_code"] == 0
        assert await client.alive()
    finally:
        await client.aclose()


async def test_retiring_client_drains_active_request_then_closes_pool(action_server):
    client = sandbox_for(action_server)
    request = asyncio.create_task(client._get("/blocked"))
    try:
        await asyncio.wait_for(action_server.blocked.wait(), 1)
        await client.aclose()
        with pytest.raises(RuntimeError, match="closed"):
            await client.execute("true")
        assert not request.done()
        action_server.unblock.set()
        assert (await request)["exit_code"] == 0
        await wait_for_disconnects(action_server)
    finally:
        action_server.unblock.set()
        await asyncio.gather(request, return_exceptions=True)
        await client.aclose()


async def test_cancellation_releases_connection_and_pool_remains_usable(action_server):
    client = sandbox_for(action_server)
    request = asyncio.create_task(client._get("/blocked"))
    try:
        await asyncio.wait_for(action_server.blocked.wait(), 1)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert await client.alive()
    finally:
        action_server.unblock.set()
        await client.aclose()


async def test_unmanaged_short_lived_clients_close_each_operation(action_server):
    client = SandboxClient("127.0.0.1", action_server.port, "key")
    assert await client.alive()
    assert await client.alive()
    assert action_server.connection_count == 2
    await wait_for_disconnects(action_server)


async def test_manager_health_uses_cached_pool_and_shutdown_closes_it(action_server, monkeypatch):
    manager = SandboxManager()
    client = sandbox_for(action_server)
    key = _map_key("alice")
    info = SandboxInfo(container_id="desktop-a", user_id="alice", host="127.0.0.1",
                       port=action_server.port, api_key="key-alice", project_id="default",
                       session_ids={"session-a"})
    manager._project_map[key] = info
    manager._clients[key] = client
    manager._session_project["session-a"] = key
    provider = MagicMock()
    monkeypatch.setattr("sandbox.provider", provider)
    await client.execute("true")
    assert await manager._verify_sandbox_alive(info, key)
    assert action_server.connection_count == 1
    await manager.release_all(destroy=False)
    await wait_for_disconnects(action_server)
    provider.delete_container.assert_not_called()


async def test_manager_rotation_closes_old_pool_before_using_new_credentials(monkeypatch):
    manager = SandboxManager()
    key = _map_key("alice")
    old = SandboxClient("127.0.0.1", 9000, "old-key", reuse_connections=True)
    info = SandboxInfo(container_id="desktop-a", user_id="alice", host="127.0.0.1",
                       port=9000, api_key="old-key", project_id="default", session_ids={"session-a"})
    manager._project_map[key] = info
    manager._clients[key] = old
    manager._session_project["session-a"] = key
    provider = MagicMock()
    provider.routes_per_user = True
    provider.resolve_user_container = AsyncMock(return_value=SimpleNamespace(
        id="desktop-a", host="127.0.0.1", port=9001, api_key="new-key", status=ContainerStatus.RUNNING))
    provider.client_base_url = None
    monkeypatch.setattr("sandbox.provider", provider)
    monkeypatch.setattr(manager, "_ensure_session_dir", AsyncMock())
    await manager._acquire_for_user("session-a", "default", user_id="alice", owner="alice")
    with pytest.raises(RuntimeError, match="closed"):
        await old.execute("true")
    new = manager._clients[key]
    assert new.api_key == "new-key" and new.base_url.endswith(":9001")
    assert new._reuse_connections
    await manager.release_all(destroy=False)
