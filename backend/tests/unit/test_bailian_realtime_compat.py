import importlib.util
import json
import asyncio
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[3] / "deploy/bailian-realtime-compat/bridge.py"
spec = importlib.util.spec_from_file_location("bailian_realtime_compat", path)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def test_bailian_usage_aliases_preserve_all_original_values():
    usage = {"input_tokens": 53, "output_tokens": 40,
             "input_tokens_details": {"text_tokens": 45, "audio_tokens": 8},
             "output_tokens_details": {"text_tokens": 10, "audio_tokens": 29}}
    event = {"type": "response.done", "response": {"usage": usage, "output": [{"type": "function_call"}]}}
    result = json.loads(bridge.compatible_event(json.dumps(event)))
    for key, value in usage.items():
        assert result["response"]["usage"][key] == value
    assert result["response"]["usage"]["input_token_details"] == usage["input_tokens_details"]
    assert result["response"]["usage"]["output_token_details"] == usage["output_tokens_details"]
    assert result["response"]["output"] == event["response"]["output"]


@pytest.mark.parametrize("event", [
    {"type": "session.updated", "session": {"input_audio_format": "pcm"}},
    {"type": "response.audio.delta", "delta": "AQABAA=="},
    {"type": "response.function_call_arguments.done", "arguments": "{\"id\":1}"},
    {"type": "response.done", "response": None},
    {"type": "response.done", "response": {"usage": {"input_token_details": {"text_tokens": 3},
                                                       "input_tokens_details": {"text_tokens": 5}}}},
])
def test_non_usage_events_and_existing_aliases_are_unchanged(event):
    raw = json.dumps(event, indent=2)
    assert bridge.compatible_event(raw) == raw


def test_destination_and_model_cannot_be_supplied_by_untrusted_clients():
    from types import SimpleNamespace
    for route in ("/v1/chat/completions", "/v1/realtime?model=unknown",
                  "/v1/realtime?model=qwen-audio-3.1-realtime-plus&model=evil"):
        assert bridge.request_model(SimpleNamespace(path=route)) is None


async def test_actual_websocket_frames_auth_and_disconnect_are_preserved(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    received = []
    closed = asyncio.Event()
    output = {"type": "response.done", "response": {"usage": {
        "input_tokens_details": {"audio_tokens": 8}, "output_tokens_details": {"audio_tokens": 29}}}}
    async def provider(ws):
        try:
            assert ws.request.headers["Authorization"] == "Bearer gateway-upstream-fixture"
            assert ws.request.path == "/api-ws/v1/realtime?model=qwen-audio-3.1-realtime-plus"
            received.append(await ws.recv())
            await ws.send(json.dumps(output))
            await ws.wait_closed()
        finally:
            closed.set()
    async with serve(provider, "127.0.0.1", 0) as upstream:
        monkeypatch.setattr(bridge, "UPSTREAM", f"ws://127.0.0.1:{upstream.sockets[0].getsockname()[1]}/api-ws/v1/realtime")
        async with serve(bridge.relay, "127.0.0.1", 0, process_request=bridge.preflight) as relay:
            url = f"ws://127.0.0.1:{relay.sockets[0].getsockname()[1]}/v1/realtime?model=qwen-audio-3.1-realtime-plus"
            async with connect(url, additional_headers={"Authorization": "Bearer gateway-upstream-fixture"}, proxy=None) as ws:
                frame = '{"type":"conversation.item.create","item":{"type":"function_call_output","output":"完成"}}'
                await ws.send(frame)
                result = json.loads(await asyncio.wait_for(ws.recv(), 2))
                assert received == [frame]
                assert result["response"]["usage"]["output_token_details"] == {"audio_tokens": 29}
            await asyncio.wait_for(closed.wait(), 2)


async def test_missing_credentials_are_rejected_before_upstream_connect(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus
    calls = []
    monkeypatch.setattr(bridge, "connect", lambda *args, **kwargs: calls.append(args))
    async with serve(bridge.relay, "127.0.0.1", 0, process_request=bridge.preflight) as relay:
        url = f"ws://127.0.0.1:{relay.sockets[0].getsockname()[1]}/v1/realtime?model=qwen-audio-3.1-realtime-plus"
        with pytest.raises(InvalidStatus) as exc:
            async with connect(url, proxy=None):
                pytest.fail("Unauthenticated connection accepted")
        assert exc.value.response.status_code == 401
        assert not calls


async def test_provider_exception_cannot_expose_authorization(monkeypatch):
    from websockets.asyncio.client import connect
    from websockets.asyncio.server import serve
    def unavailable(*args, **kwargs):
        raise RuntimeError("Authorization: Bearer private-upstream-credential")
    monkeypatch.setattr(bridge, "connect", unavailable)
    async with serve(bridge.relay, "127.0.0.1", 0, process_request=bridge.preflight) as relay:
        url = f"ws://127.0.0.1:{relay.sockets[0].getsockname()[1]}/v1/realtime?model=qwen-audio-3.1-realtime-plus"
        async with connect(url, additional_headers={"Authorization": "Bearer fixture"}, proxy=None) as ws:
            event = await asyncio.wait_for(ws.recv(), 2)
            assert json.loads(event) == {"type": "error", "error": {
                "code": "upstream_unavailable", "message": "Realtime provider unavailable"}}
            await asyncio.wait_for(ws.wait_closed(), 2)
