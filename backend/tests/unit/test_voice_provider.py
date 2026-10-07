"""The Bailian adapter: event translation, error classes, the connect plan and the session it asks for."""
import asyncio
import base64
import json

import pytest

from core.config import VoiceConfig
from voice import provider as provider_module
from voice.provider import ProviderUnavailable, RealtimeProvider, classify_error, translate


def test_translation_keeps_only_what_the_bridge_needs():
    pcm = b"\x01\x00" * 10
    assert translate({"type": "input_audio_buffer.speech_started"}).kind == "user_started"
    assert translate({"type": "input_audio_buffer.speech_stopped", "reason": "turn_invalid"}).invalid
    assert not translate({"type": "input_audio_buffer.speech_stopped"}).invalid
    assert translate({"type": "conversation.item.input_audio_transcription.completed", "transcript": "你好"}).text == "你好"
    assert translate({"type": "response.created", "response": {"id": "r1"}}).response_id == "r1"
    delta = translate({"type": "response.audio.delta", "response_id": "r1", "event_id": "e1",
                       "delta": base64.b64encode(pcm).decode()})
    assert (delta.kind, delta.audio, delta.event_id) == ("audio", pcm, "e1")
    assert translate({"type": "response.audio.delta", "delta": "not base64!"}) is None
    call = translate({"type": "response.function_call_arguments.done", "call_id": "c1", "name": "assistant_ask",
                      "arguments": json.dumps({"text": " 我有什么待办 "}, ensure_ascii=False)})
    assert (call.kind, call.call_id, call.name, call.text) == ("tool_call", "c1", "assistant_ask", "我有什么待办")
    assert translate({"type": "response.function_call_arguments.done", "call_id": "c2", "arguments": "{"}).text == ""
    done = translate({"type": "response.done", "response": {"id": "r1", "status": "cancelled", "usage": {"x": 1}}})
    assert (done.kind, done.status, done.usage) == ("response_done", "cancelled", {"x": 1})
    error = translate({"type": "error",
                       "error": {"code": "invalid_value", "message": "Voice 'Cherry' is not supported"}})
    assert (error.kind, error.code, error.reason) == ("provider_error", "invalid_value", "voice_unsupported")
    for ignored in ("response.audio_transcript.delta", "session.updated", "rate_limits.updated"):
        assert translate({"type": ignored}) is None


@pytest.mark.parametrize("message, reason", [
    ("Conversation already has an active response", "active_response"),
    ("Duplicate function call output for call_id", "duplicate_output"),
    ("Voice 'Ethan' is not supported", "voice_unsupported"),
    ("Cannot create response without input, history, or instructions", "no_input"),
    ("Something else", "other"),
])
def test_provider_refusals_are_classified_not_forwarded(message, reason):
    assert classify_error(message) == reason


class FakeSocket:
    def __init__(self, events):
        self.events, self.sent, self.closed = list(events), [], False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            await asyncio.sleep(3600)
        return json.dumps(self.events.pop(0))

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        self.closed = True


async def test_connect_alternates_proxy_and_direct_and_configures_the_session(monkeypatch):
    attempts, sockets = [], []

    async def connect(url, **options):
        attempts.append((url, options["proxy"], options["open_timeout"], options["additional_headers"]))
        if len(attempts) == 1:
            raise TimeoutError("handshake hung")
        sockets.append(FakeSocket([{"type": "session.created"}, {"type": "session.updated"}]))
        return sockets[-1]
    monkeypatch.setattr(provider_module, "RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(provider_module.websockets, "connect", connect)
    provider = RealtimeProvider(VoiceConfig(api_key="secret-key"))
    await provider.open()
    assert [proxy for _, proxy, _, _ in attempts] == [True, None]  # env proxy first, then direct
    assert attempts[0][2] == 5 and attempts[0][3] == {"Authorization": "Bearer secret-key"}
    assert [item["result"] for item in provider.attempts] == ["failed:TimeoutError", "ok"]
    await provider.configure("你是 OpenBox 个人助理的语音前台")
    [update] = sockets[0].sent
    session = update["session"]
    assert update["type"] == "session.update" and session["voice"] == "Serena"
    assert session["turn_detection"] == {"type": "semantic_vad", "threshold": 0.5, "silence_duration_ms": 700}
    assert [tool["function"]["name"] for tool in session["tools"]] == ["assistant_ask"]
    assert "enable_search" not in session
    await provider.create_response("只说这一句")
    await provider.create_response()
    assert sockets[0].sent[-2:] == [{"type": "response.create", "response": {"instructions": "只说这一句"}},
                                    {"type": "response.create"}]


async def test_no_session_after_every_attempt_is_unavailable(monkeypatch):
    async def connect(url, **options):
        return FakeSocket([{"type": "error", "error": {"code": "InvalidApiKey", "message": "bad key"}}])
    monkeypatch.setattr(provider_module, "RETRY_PAUSE_SECONDS", 0)
    monkeypatch.setattr(provider_module.websockets, "connect", connect)
    provider = RealtimeProvider(VoiceConfig(api_key="secret-key", proxy="none", connect_attempts=2))
    with pytest.raises(ProviderUnavailable):
        await provider.open()
    assert [item["mode"] for item in provider.attempts] == ["direct", "direct"]
    assert "secret-key" not in json.dumps(provider.attempts)


async def test_a_hanging_route_does_not_hold_the_call(monkeypatch):
    """Either route can hang for the whole timeout; the other one must win at once."""
    opened = []

    async def connect(url, **options):
        if options["proxy"]:
            await asyncio.sleep(30)  # the proxy route hangs
        opened.append(FakeSocket([{"type": "session.created"}]))
        return opened[-1]
    monkeypatch.setattr(provider_module, "RACE_HEAD_START_SECONDS", 0.01)
    monkeypatch.setattr(provider_module.websockets, "connect", connect)
    provider = RealtimeProvider(VoiceConfig(api_key="secret-key"))
    started = asyncio.get_running_loop().time()
    await provider.open()
    assert asyncio.get_running_loop().time() - started < 1
    assert [(item["mode"], item["result"]) for item in provider.attempts] == [("direct", "ok")]
    assert provider._ws is opened[0] and not opened[0].closed


async def test_the_slower_session_is_closed(monkeypatch):
    sockets = []

    async def connect(url, **options):
        await asyncio.sleep(0.05 if options["proxy"] else 0.06)
        sockets.append(FakeSocket([{"type": "session.created"}]))
        return sockets[-1]
    monkeypatch.setattr(provider_module, "RACE_HEAD_START_SECONDS", 0)
    monkeypatch.setattr(provider_module.websockets, "connect", connect)
    provider = RealtimeProvider(VoiceConfig(api_key="secret-key"))
    await provider.open()
    await asyncio.sleep(0.1)
    assert provider._ws is sockets[0] and not sockets[0].closed
    assert all(sock.closed for sock in sockets[1:])
