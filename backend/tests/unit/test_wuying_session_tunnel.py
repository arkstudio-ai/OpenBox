"""Exercise renewal and draining with real TCP sockets and child processes."""
import asyncio
from contextlib import asynccontextmanager
import sys

import httpx
import pytest

from scripts.wuying_session_tunnel import SessionTunnel


FAKE_CLI = r'''
import asyncio, os, sys
async def handle(reader, writer):
    try:
        header = await reader.readuntil(b"\r\n\r\n")
        path = header.split(b" ")[1]
        length = next((int(line.split(b":")[1]) for line in header.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        if length:
            await reader.readexactly(length)
        if path == b"/disconnect":
            with open(sys.argv[2], "a") as f:
                f.write("executed\n")
            return
        body = str(os.getpid()).encode()
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode()
                     + b"\r\nConnection: close\r\n\r\n")
        await writer.drain()
        if path == b"/slow":
            await reader.readexactly(1)
        writer.write(body)
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()
async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", int(sys.argv[1]))
    async with server:
        await server.serve_forever()
asyncio.run(main())
'''


@asynccontextmanager
async def running(tmp_path, **kwargs):
    child = tmp_path / "fake_cli.py"
    child.write_text(FAKE_CLI)
    tunnel = SessionTunnel([sys.executable, str(child), "{port}", str(tmp_path / "executions")],
                           port=0, **kwargs)
    await tunnel.start()
    try:
        yield tunnel, tunnel.server.sockets[0].getsockname()[1]
    finally:
        await tunnel.close()
        assert not tunnel.generations
        assert not tunnel.connections


async def wait_until(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


async def test_renewal_keeps_inflight_response_and_uses_new_session_for_next_request(tmp_path):
    async with running(tmp_path) as (tunnel, port):
        old = tunnel.current
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        await tunnel.rotate()
        assert old.process.returncode is None
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{port}/next")
            assert response.text == str(tunnel.current.process.pid)
        writer.write(b"x")
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 3) == str(old.process.pid).encode()
        writer.close()
        await writer.wait_closed()
        await wait_until(lambda: old.stopped)
        assert old.process.returncode is not None


async def test_expiring_session_is_renewed_automatically(tmp_path):
    async with running(tmp_path, refresh_after=.4, probe_interval=.05) as (tunnel, port):
        old = tunnel.current
        await wait_until(lambda: tunnel.current is not old)
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f"http://127.0.0.1:{port}/alive")).status_code == 200
        await wait_until(lambda: old.stopped)


async def test_dead_forwarder_is_replaced_without_restarting_listener(tmp_path):
    async with running(tmp_path, probe_interval=.05) as (tunnel, port):
        old = tunnel.current
        old.process.terminate()
        await old.process.wait()
        await wait_until(lambda: tunnel.current is not old)
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f"http://127.0.0.1:{port}/alive")).status_code == 200


async def test_failed_candidate_does_not_replace_working_session(tmp_path):
    async with running(tmp_path) as (tunnel, port):
        old = tunnel.current
        tunnel.command = [sys.executable, "-c", "raise SystemExit(2)", "{port}"]
        with pytest.raises(RuntimeError, match="exited with code 2"):
            await tunnel.rotate()
        assert tunnel.current is old and not old.retired
        assert len(tunnel.generations) == 1
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(f"http://127.0.0.1:{port}/alive")).status_code == 200


async def test_disconnect_after_command_does_not_replay_it(tmp_path):
    async with running(tmp_path) as (_, port):
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await client.post(f"http://127.0.0.1:{port}/disconnect", content="command")
        assert (tmp_path / "executions").read_text() == "executed\n"


async def test_shutdown_closes_inflight_connections_and_children(tmp_path):
    async with running(tmp_path) as (tunnel, port):
        old = tunnel.current
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /slow HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        await tunnel.close()
        assert await asyncio.wait_for(reader.read(), 3) == b""
        assert old.process.returncode is not None
        writer.close()
        await writer.wait_closed()


async def test_sandbox_disconnect_reports_unknown_outcome_without_recreating_or_retrying(monkeypatch):
    from types import SimpleNamespace
    from agent.hooks import ToolHooks
    from tool.tool import ToolContext
    calls = 0
    hooks = ToolHooks("session", "user")

    async def allow(*args):
        return None

    async def execute(args, ctx):
        nonlocal calls
        calls += 1
        raise httpx.RemoteProtocolError("connection closed", request=httpx.Request(
            "POST", "http://127.0.0.1:18002/execute"))

    monkeypatch.setattr(hooks, "authorize_tool", allow)
    ctx = ToolContext(session_id="session", user_id="user",
                      sandbox=SimpleNamespace(base_url="http://127.0.0.1:18002"))
    result = await hooks.wrap_execute("bash", execute, {}, ctx, part_id="call")
    assert calls == 1
    assert result.metadata["outcome_unknown"]
    assert result.metadata["failure_code"] == "sandbox_transport_error"
    assert "recreated" not in result.output
    assert "outcome is unknown" in result.output
