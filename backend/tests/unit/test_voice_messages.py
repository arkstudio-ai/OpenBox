"""Voice input: real scoped SQL, cached ASR, immutable text and replay identity."""
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from agent import inbox
from agent.driver import reserve_run
from assistant import voice_messages as voice
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from core.config import OpenBoxConfig
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.file_asset import FileAsset
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def recording(owner, workspace, id="voice-asset", **changes):
    row = FileAsset(id=id, user_id=owner, workspace_id=workspace, name="voice.m4a", mime="audio/mp4",
        size=100, oss_key="test-only/voice", status="ready", source="user", transient=False,
        is_deleted=False, created_at=datetime.now(timezone.utc), **changes)
    async with get_db_session() as db:
        db.add(row)
    return row


async def test_voice_transcription_cache_scope_and_exactly_once_turn(monkeypatch):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    await ensure_main_session(user_id=other, workspace_id=workspace)
    await recording(owner, workspace)
    calls = []
    async def chunks(*args, **kwargs):
        yield b'audio'
    async def recognize(data, mime):
        calls.append((data, mime))
        return {"text": "明天8点半提醒我", "duration_ms": 4000, "sha256": "a" * 64, "model": "test-asr"}
    monkeypatch.setattr(voice, "get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr(voice, "recognize", recognize)
    async with client_for(owner, workspace, monkeypatch) as client:
        body = {"asset_id": "voice-asset", "assistant_session_id": main.id}
        first = await client.post('/api/assistant/voice-messages/transcribe', json=body)
        assert first.status_code == 200, first.text
        assert (await client.post('/api/assistant/voice-messages/transcribe', json=body)).json() == first.json()
        assert len(calls) == 1
        payload = {"client_id": "phone-voice-1", "delivery": "followup", "text": first.json()["text"],
                   "voice_asset_id": "voice-asset", "assistant_session_id": main.id}
        bad = await client.post('/api/assistant/turns', json={**payload, "text": "forged instruction"})
        assert bad.status_code == 409
        accepted = await client.post('/api/assistant/turns', json=payload)
        assert accepted.status_code == 202, accepted.text
        assert (await client.post('/api/assistant/turns', json=payload)).json() == accepted.json()
        # Reusing the id as an ordinary text send cannot lose its audio binding.
        plain = {k: v for k, v in payload.items() if k != "voice_asset_id"}
        assert (await client.post('/api/assistant/turns', json=plain)).status_code == 409
    async with client_for(other, workspace, monkeypatch) as client:
        other_main = await ensure_main_session(user_id=other, workspace_id=workspace)
        assert (await client.post('/api/assistant/voice-messages/transcribe', json={
            **body, 'assistant_session_id': other_main.id})).status_code == 404
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert len(batch.messages) == 1
        assert batch.messages[0].client_message_id == "phone-voice-1"
        parts = batch.messages[0].parts
        text = next(p for p in parts if getattr(p, 'type', None) == 'text')
        assert text.text == first.json()["text"]
        assert text.origin_ref['voice_message']['asset_id'] == 'voice-asset'
        assert not batch.attachment_ids  # No sandbox/audio tool roundtrip.
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('status,body,expected', [
    (400, {'message': 'ASR_RESPONSE_HAVE_NO_WORDS'}, 'VOICE_NO_SPEECH'),
    (401, {'message': 'private upstream key or account details'}, 'VOICE_TRANSCRIPTION_FAILED'),
    (200, [], 'VOICE_TRANSCRIPTION_FAILED'),
    (200, {'output': []}, 'VOICE_NO_SPEECH'),
    (200, {'output': {'text': 'too long'}, 'usage': {'duration': 90}}, 'VOICE_MESSAGE_DURATION'),
    (200, {'output': {'text': ''}, 'usage': {'duration': 3}}, 'VOICE_NO_SPEECH'),
])
async def test_asr_errors_do_not_dispatch_or_leak_provider_details(monkeypatch, status, body, expected):
    config = OpenBoxConfig(voice_input={'api_key': 'private-test-key'})
    monkeypatch.setattr(voice, 'get_config', lambda: config)
    cls = httpx.AsyncClient
    monkeypatch.setattr(voice.httpx, 'AsyncClient', lambda **kwargs: cls(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json=body)), **kwargs))
    with pytest.raises(AssistantError) as failure:
        await voice.recognize(b'not-sent-to-network', 'audio/mp4')
    assert failure.value.code == expected
    assert 'private' not in str(failure.value)


async def test_asr_sends_only_audio_and_preserves_negation(monkeypatch):
    config = OpenBoxConfig(voice_input={'api_key': 'private-test-key'})
    monkeypatch.setattr(voice, 'get_config', lambda: config)
    cls = httpx.AsyncClient
    def provider(request):
        import json
        body = json.loads(request.content)
        assert body['model'] == 'qwen-audio-3.1-asr-flash'
        assert body['parameters']['keep_dialect'] is False
        assert body['input']['messages'][0]['content'][0]['input_audio']['data'].startswith('data:audio/mp4;base64,')
        return httpx.Response(200, json={'output': {'text': '不要取消会议。'}, 'usage': {'duration': 4}})
    monkeypatch.setattr(voice.httpx, 'AsyncClient', lambda **kwargs: cls(transport=httpx.MockTransport(provider), **kwargs))
    assert (await voice.recognize(b'audio', 'audio/mp4'))['text'] == '不要取消会议。'


def test_custom_gateway_never_receives_official_fallback(monkeypatch):
    monkeypatch.setenv('DASHSCOPE_API_KEY', 'official-secret')
    config = OpenBoxConfig(voice_input={'base_url': 'https://relay.invalid'})
    assert voice.api_key(config) == ''
    assert not voice.enabled(config)
