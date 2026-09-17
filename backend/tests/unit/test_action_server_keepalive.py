"""The deployed entrypoint keeps a real HTTP socket through model think time."""
import asyncio
import socket

import httpx
import uvicorn

from tests.unit.test_action_server_desktop_lease import server


async def test_default_entrypoint_reuses_connection_after_six_seconds(monkeypatch):
    options = {}
    monkeypatch.delenv("ACTION_SERVER_KEEP_ALIVE_TIMEOUT", raising=False)
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: options.update(kwargs))
    server.main(["--host", "127.0.0.1", "--port", "0"])
    peers = []

    async def app(scope, receive, send):
        peers.append(scope["client"])
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"2")]})
        await send({"type": "http.response.body", "body": b"ok"})

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    runtime = uvicorn.Server(uvicorn.Config(app, **options, lifespan="off", access_log=False))
    task = asyncio.create_task(runtime.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not runtime.started:
                await asyncio.sleep(.01)
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", trust_env=False,
            limits=httpx.Limits(keepalive_expiry=60),
        ) as client:
            assert (await client.get("/alive")).status_code == 200
            await asyncio.sleep(6)
            assert (await client.get("/alive")).status_code == 200
        assert len(peers) == 2 and peers[0] == peers[1]
    finally:
        runtime.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


def test_keepalive_can_be_configured_for_an_existing_service(monkeypatch):
    options = {}
    monkeypatch.setenv("ACTION_SERVER_KEEP_ALIVE_TIMEOUT", "90")
    monkeypatch.setattr(server.uvicorn, "run", lambda app, **kwargs: options.update(kwargs))
    server.main([])
    assert options["timeout_keep_alive"] == 90
    server.main(["--timeout-keep-alive", "120"])
    assert options["timeout_keep_alive"] == 120
