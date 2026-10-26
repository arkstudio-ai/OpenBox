"""Real SDK stream ownership, first-progress deadlines and safe transport evidence."""
import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent import gateway, llm


def wire(content=None, finish=None):
    return ("data: " + json.dumps({"id": "qa", "created": 1, "model": "qa",
        "object": "chat.completion.chunk", "choices": [{"index": 0,
        "delta": {"content": content} if content else {"role": "assistant"},
        "finish_reason": finish}]}) + "\n\n").encode()


class LiveStream(httpx.AsyncByteStream):
    def __init__(self, chunks, *, hang=False):
        self.chunks, self.hang, self.closed = chunks, hang, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.hang:
            await asyncio.Event().wait()
        else:
            yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


async def test_early_consumer_exit_closes_underlying_sdk_response(monkeypatch):
    body = LiveStream([wire("开头")], hang=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=body))) as http:
        async with AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", http_client=http) as sdk:
            monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {"client": sdk})
            stream = llm._stream_litellm_direct("openai/qwen3.8-flash", [], [], {})
            assert (await anext(stream))["text"] == "开头"
            assert not body.closed
            await stream.aclose()
            assert body.closed  # before GC and before the shared client exits


@pytest.mark.parametrize("partial", [False, True])
async def test_stalled_stream_closes_without_retry_or_partial_success(monkeypatch, partial):
    monkeypatch.setattr(gateway, "CAPTION_FIRST_PROGRESS_SECONDS", 0.08)
    monkeypatch.setattr(gateway, "IDLE_PROGRESS_SECONDS", 0.08)
    body = LiveStream([wire("未完成" if partial else None)], hang=True)
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=body)
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        async with AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1", http_client=http) as sdk:
            monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {"client": sdk})
            async with asyncio.timeout(2):
                events = [e async for e in llm._stream_litellm_direct(
                    "openai/qwen3.8-flash", [], [], {}, purpose="asset_description")]
            assert events[-1]["type"] == "error"
            assert isinstance(events[-1]["error"], gateway.GatewayTimeout)
            from agent.retry import is_retryable
            assert is_retryable(events[-1]["error"]) is None
            assert events[-1]["error"].phase == ("stream_idle" if partial else "first_progress")
            assert not any(e["type"] in {"finish", "tool_call"} for e in events)
            assert body.closed and len(calls) == 1


async def test_consumer_work_does_not_consume_gateway_idle_budget(monkeypatch):
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
    monkeypatch.setattr(gateway, "IDLE_PROGRESS_SECONDS", .01)
    async def source():
        for value in ("one", "two"):
            yield ModelResponseStream(choices=[StreamingChoices(delta=Delta(content=value))])
    observation = gateway.Observation("qwen", "chat")
    seen = []
    async for value in observation.chunks(source()):
        seen.append(value)
        await asyncio.sleep(.02)
    assert len(seen) == 2


async def test_wire_trace_distinguishes_upstream_wait_and_never_logs_secrets(caplog):
    received = asyncio.Event()
    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            received.set()
            await reader.read()  # accepted TCP, but gateway sends no headers
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    observation = gateway.Observation("qwen", "asset_description")
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=.08,
                event_hooks={"request": [observation.on_request]}) as client:
            with pytest.raises(httpx.ReadTimeout) as error:
                await client.get(f"http://127.0.0.1:{port}/private?key=NEVER_LOG",
                                 headers={"Authorization": "Bearer SECRET"})
        observation.finish("failed", error.value)
        assert received.is_set()
        assert observation.failure_phase == "upstream_headers"
        assert observation.spans["connect"] < observation.spans["upstream_headers"]
        assert "NEVER_LOG" not in caplog.text and "SECRET" not in caplog.text
    finally:
        server.close()
        await server.wait_closed()


async def test_cancel_during_dispatch_is_not_converted_to_retry():
    observation = gateway.Observation("qwen", "chat")
    entered = asyncio.Event()
    async def request():
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(observation.dispatch(request))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway._current.get() is None


async def test_real_tls_timeout_keeps_presend_evidence_through_sdk(monkeypatch):
    from agent.retry import is_retryable, retry_delay
    from agent.stream_deadline import StreamLimits
    connections = []
    async def serve(reader, writer):
        connections.append(writer)
        try:
            await reader.read()  # Accept TCP without completing the TLS handshake.
        finally:
            writer.close()
            await writer.wait_closed()
    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {
        "api_key": "local-test", "api_base": f"https://127.0.0.1:{port}/v1"})
    monkeypatch.setattr(llm, "request_timeout", lambda **kwargs: httpx.Timeout(.05))
    monkeypatch.setattr("agent.stream_deadline.limits_for", lambda *args: StreamLimits(2, 2, 2))
    try:
        async with asyncio.timeout(3):
            events = [e async for e in llm._stream_litellm_direct("openai/test", [], [], {})]
        error = events[-1]["error"]
        assert is_retryable(error) is not None
        assert error._openbox_pre_dispatch.phase == "tls"
        assert retry_delay(1, error, rand=0) == .1
        assert retry_delay(1, error, rand=1) == .25
        assert retry_delay(2, error, rand=.5) >= 2
        assert len(connections) == 1  # No invisible SDK retry.
    finally:
        await gateway.close_clients()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize("phase,sent,status", [
    (None, False, None), ("upstream_headers", True, None),
    ("tls", True, None), ("connect", False, 429),
])
async def test_short_retry_requires_proven_presend_failure(phase, sent, status):
    from agent.retry import retry_delay
    observation = gateway.Observation("qwen", "chat")
    observation.failure_phase, observation.request_started, observation.status = phase, sent, status
    error = httpx.ReadTimeout("TLS timed out")
    observation.annotate_error(error)
    assert not hasattr(error, "_openbox_pre_dispatch")
    assert retry_delay(1, error, rand=.5) >= 1


def test_presend_retry_does_not_override_retry_after():
    from agent.retry import RetryableError, retry_delay
    error = RetryableError("unavailable", headers={"retry-after": "7"})
    error._openbox_pre_dispatch = gateway.PreDispatchFailure("tls")
    assert retry_delay(1, error, rand=0) == 7


async def test_model_progress_limits_reach_transport_and_observer(monkeypatch):
    from agent.stream_deadline import StreamLimits
    import litellm
    captured = []
    monkeypatch.setattr("agent.stream_deadline.limits_for", lambda *args: StreamLimits(.02, .03, .1))
    async def completion(**kwargs):
        captured.append(kwargs["timeout"].read)
        await asyncio.Event().wait()
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})
    async with asyncio.timeout(1):
        events = [e async for e in llm._stream_litellm_direct("openai/test", [], [], {})]
    assert captured == [.03]
    assert isinstance(events[-1]["error"], gateway.GatewayTimeout)
    assert events[-1]["error"].phase == "first_progress"


async def test_normal_sse_teardown_is_not_reported_as_a_gateway_error(monkeypatch):
    rows = []
    monkeypatch.setattr(gateway.log, "info", lambda _, value: rows.append(json.loads(value)))
    observation = gateway.Observation("qwen", "chat")
    observation.error = "GeneratorExit"
    observation.finish("completed")
    assert rows[0]["outcome"] == "completed" and rows[0]["error"] is None


async def test_voice_summary_has_a_total_deadline_and_closes_the_response(monkeypatch):
    from voice import summary
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {
        "api_base": "https://fixture.invalid/v1", "api_key": "fixture"})
    body = LiveStream([b'{"choices":'], hang=True)
    original = httpx.AsyncClient
    def factory(**kwargs):
        return original(**kwargs, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=body)))
    monkeypatch.setattr(summary.httpx, "AsyncClient", factory)
    with pytest.raises(TimeoutError):
        await summary.complete("openai/qwen3.8-flash", "summary", "hello", .05)
    assert body.closed
