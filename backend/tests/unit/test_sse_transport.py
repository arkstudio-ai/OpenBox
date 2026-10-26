"""Exercise HTTP/1.1 reuse through the real SDK plus abnormal SSE cleanup."""
import asyncio
import json

import httpx
import pytest

from agent import gateway, llm, sse_transport


async def test_completed_sdk_streams_reuse_tcp_connection(monkeypatch):
    # One-time SDK imports are outside this network lifecycle deadline.
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    __import__("litellm")
    # Developer .env may route even loopback HTTP through a system proxy.
    # This test measures our pool, not that proxy's upstream connection pool.
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    connections = requests = 0
    tasks = set()
    responses = []
    original_hook = sse_transport.preserve_completed_sse
    async def capture_response(response):
        await original_hook(response)
        responses.append(response)
    monkeypatch.setattr(sse_transport, "preserve_completed_sse", capture_response)

    async def serve(reader, writer):
        nonlocal connections, requests
        connections += 1
        tasks.add(asyncio.current_task())
        try:
            while True:
                headers = await reader.readuntil(b"\r\n\r\n")
                size = next((int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                             if line.lower().startswith(b"content-length:")), 0)
                await reader.readexactly(size)
                requests += 1
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n")
                for content, finish in [("complete", None), (None, "stop")]:
                    event = {"id": "test", "created": 1, "model": "fixture", "object": "chat.completion.chunk",
                             "choices": [{"index": 0, "delta": {"content": content} if content else {}, "finish_reason": finish}]}
                    chunk = ("data: " + json.dumps(event) + "\n\n").encode()
                    writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                chunk = b"data: [DONE]\n\n"
                writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n0\r\n\r\n")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    await gateway.close_clients()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {
        "api_base": f"http://127.0.0.1:{port}/v1", "api_key": "fixture"})
    try:
        async with asyncio.timeout(5):
            for _ in range(3):
                events = [event async for event in llm._stream_litellm_direct(
                    "openai/qwen3.8-flash", [], [{"role": "user", "content": "hello"}], {})]
                assert events[-1]["type"] == "finish"
                assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "complete"
            assert requests == 3 and connections == 1, [
                (type(r.stream).__name__, getattr(r.stream, "done", None),
                 getattr(r.stream, "eof", None), r.headers.get("content-encoding")) for r in responses]
    finally:
        await gateway.close_clients()
        server.close()
        await server.wait_closed()
        await asyncio.gather(*list(tasks))


class TailStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, tail="end"):
        self.chunks, self.tail = chunks, tail
        self.closed = False
        self.tail_reads = 0
        self.entered = asyncio.Event()

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        self.entered.set()
        if self.tail == "hang":
            await asyncio.Event().wait()
        elif self.tail == "error":
            raise httpx.ReadError("broken tail")
        elif self.tail == "infinite":
            while True:
                self.tail_reads += 1
                yield b" " * 1024

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("chunks", [
    [b"data: [DONE]\n\n"], [b"data:[DONE]\r\n\r\n"],
    [b"data: [DO", b"NE]\n", b"\n"],
])
async def test_marker_can_span_network_chunks(chunks):
    inner = TailStream(chunks)
    stream = sse_transport.CompletedSSEStream(inner)
    iterator = stream.__aiter__()
    for chunk in chunks:
        assert await anext(iterator) == chunk
    await stream.aclose()
    assert stream.done and stream.eof and inner.closed


@pytest.mark.parametrize("tail", ["hang", "error", "infinite"])
async def test_bad_tail_is_bounded_and_never_invalidates_completed_answer(monkeypatch, tail):
    monkeypatch.setattr(sse_transport, "DRAIN_SECONDS", .01)
    monkeypatch.setattr(sse_transport, "DRAIN_BYTES", 2048)
    inner = TailStream([b"data: [DONE]\n\n"], tail=tail)
    stream = sse_transport.CompletedSSEStream(inner)
    await anext(stream.__aiter__())
    async with asyncio.timeout(.5):
        await stream.aclose()
    assert inner.closed and not stream.eof
    assert inner.tail_reads <= 3


async def test_cancelled_or_unfinished_stream_never_drains():
    inner = TailStream([b'data: {"text":"[DONE]"}\n\n'], tail="hang")
    stream = sse_transport.CompletedSSEStream(inner)
    await anext(stream.__aiter__())
    await stream.aclose()
    assert inner.closed and not inner.entered.is_set()

    inner = TailStream([b"data: [DONE]\n\n"], tail="hang")
    stream = sse_transport.CompletedSSEStream(inner)
    await anext(stream.__aiter__())
    started = asyncio.Event()
    async def operation():
        try:
            started.set()
            await asyncio.Event().wait()
        finally:
            await stream.aclose()
    task = asyncio.create_task(operation())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.closed and not inner.entered.is_set()


async def test_cancel_during_drain_propagates_and_closes():
    inner = TailStream([b"data: [DONE]\n\n"], tail="hang")
    stream = sse_transport.CompletedSSEStream(inner)
    await anext(stream.__aiter__())
    task = asyncio.create_task(stream.aclose())
    await inner.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner.closed


@pytest.mark.parametrize("status,content_type,encoding", [
    (503, "text/event-stream", "identity"), (200, "application/json", "identity"),
    (200, "text/event-stream", "gzip"),
])
async def test_other_responses_are_not_wrapped(status, content_type, encoding):
    inner = TailStream([])
    response = httpx.Response(status, stream=inner, headers={
        "content-type": content_type, "content-encoding": encoding})
    await sse_transport.preserve_completed_sse(response)
    assert response.stream is inner
    await response.aclose()
