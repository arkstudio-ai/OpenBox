"""The Responses channel reuses one pooled HTTP client per event loop.

It used to build an httpx.AsyncClient per request, so every request paid a
new TCP/TLS handshake. A local loopback server shows the real pool: two
consecutive requests share one client and one connection, and a cancelled or
timed-out stream closes its connection instead of holding it.
"""
import asyncio
import json
import re

import httpx
import pytest

from agent import gateway, llm

EVENTS = [
    {"type": "response.output_text.delta", "delta": "Hello"},
    {"type": "response.completed", "response": {"id": "r1", "output": []}},
]


class _Server:
    def __init__(self, *, hang=False):
        self.hang = hang
        self.connections = 0
        self.requests = 0
        self.released = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1

        def chunk(data: bytes):
            writer.write(b"%x\r\n%s\r\n" % (len(data), data))

        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                length = int(re.search(rb"content-length: *(\d+)", head, re.I).group(1))
                await reader.readexactly(length)
                self.requests += 1
                writer.write(b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                             b"transfer-encoding: chunked\r\n\r\n")
                if self.hang:
                    chunk(f"data: {json.dumps(EVENTS[0])}\n\n".encode())
                    await writer.drain()
                    await reader.read()  # Returns once the client drops the connection.
                    return
                for event in EVENTS:
                    chunk(f"data: {json.dumps(event)}\n\n".encode())
                chunk(b"data: [DONE]\n\n")
                writer.write(b"0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            self.released.set()
            writer.close()


@pytest.fixture
async def serve(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    servers = []

    async def start(**kwargs):
        server = _Server(**kwargs)
        listener = await asyncio.start_server(server.handle, "127.0.0.1", 0)
        servers.append(listener)
        port = listener.sockets[0].getsockname()[1]
        monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {
            "api_key": "fixture-key", "api_base": f"http://127.0.0.1:{port}/v1",
        })
        return server

    created = []
    real_client = httpx.AsyncClient

    def counting_client(*args, **kwargs):
        client = real_client(*args, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", counting_client)
    yield start, created
    await gateway.close_clients()
    for listener in servers:
        listener.close()
        await listener.wait_closed()


def _request():
    return llm._stream_responses_api("openai/gpt-5.4", [], [{"role": "user", "content": "hi"}], {})


async def test_consecutive_requests_share_one_client_and_connection(serve):
    start, created = serve
    server = await start()

    for _ in range(2):
        events = [event async for event in _request()]
        assert events[-1]["type"] == "finish"
        assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "Hello"

    assert len(created) == 1
    assert server.requests == 2
    assert server.connections == 1  # The completed stream went back to the pool.
    assert not created[0].is_closed


async def test_cancellation_releases_the_connection(serve):
    start, created = serve
    server = await start(hang=True)
    started = asyncio.Event()

    async def consume():
        async for event in _request():
            if event["type"] == "text_delta":
                started.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(server.released.wait(), timeout=5)
    # The shared client survives the cancelled request.
    assert len(created) == 1 and not created[0].is_closed


async def test_read_timeout_still_applies_per_request(serve, monkeypatch):
    start, created = serve
    server = await start(hang=True)
    monkeypatch.setattr(llm, "request_timeout", lambda **_: httpx.Timeout(5.0, read=0.3))

    events = await asyncio.wait_for(_collect(), timeout=5)
    assert events[-1]["type"] == "error"
    await asyncio.wait_for(server.released.wait(), timeout=5)
    assert len(created) == 1 and not created[0].is_closed


async def _collect():
    return [event async for event in _request()]


async def test_shutdown_closes_the_pool(serve):
    start, created = serve
    await start()
    [event async for event in _request()]
    await gateway.close_clients()
    assert created[0].is_closed
    # A later request on the same loop opens a fresh client.
    [event async for event in _request()]
    assert len(created) == 2 and not created[1].is_closed
