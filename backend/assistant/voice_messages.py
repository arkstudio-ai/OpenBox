"""Short audio -> cached ASR -> an ordinary, idempotent human assistant turn.

Only server-produced transcription metadata can mark a message as voice.
The recording stays in the owner's asset store; no cloud desktop is needed.
"""
import asyncio
import base64
import hashlib
import os
from urllib.parse import urlsplit

import httpx

from assistant.policy import AssistantError
from core.config import get_config
from core.oss import get_oss, OssError, OssNotConfigured
from db.base import get_db_session
from db.models.file_asset import FileAsset
from assets.service import owned_ready_query

MAX_BYTES = 2 * 1024 * 1024
MAX_SECONDS = 60
META_KEY = "voice_message"
FORMATS = {"audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/m4a": "m4a",
           "audio/wav": "wav", "audio/x-wav": "wav"}


def api_key(config=None) -> str:
    config = config or get_config()
    settings = config.voice_input
    if settings.api_key:
        return settings.api_key
    host = (urlsplit(settings.base_url).hostname or "").lower()
    if host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or host.endswith(".maas.aliyuncs.com"):
        return os.getenv("DASHSCOPE_API_KEY", "") or (
            config.video_transcription.api_key
            if config.video_transcription.base_url.rstrip("/") == settings.base_url.rstrip("/") else "")
    return ""


def enabled(config=None) -> bool:
    config = config or get_config()
    return bool(config.voice_input.enabled and api_key(config))


async def owned_audio(db, asset_id, user_id, workspace_id, *, lock=False):
    query = owned_ready_query(user_id, workspace_id).where(FileAsset.id == asset_id)
    row = await db.scalar(query.with_for_update() if lock else query)
    if row is None or row.source != "user":
        raise AssistantError(404, "VOICE_MESSAGE_NOT_FOUND", "Recording not found")
    if row.mime not in FORMATS or not 0 < row.size <= MAX_BYTES:
        raise AssistantError(422, "VOICE_MESSAGE_FORMAT", "Use an M4A or WAV recording up to 2 MB")
    return row


def cached(row):
    value = (row.meta or {}).get(META_KEY)
    return value if isinstance(value, dict) and value.get("text") and value.get("sha256") else None


async def recognize(data: bytes, mime: str) -> dict:
    config = get_config()
    if not enabled(config):
        raise AssistantError(503, "VOICE_INPUT_UNAVAILABLE", "Voice input is not configured")
    settings = config.voice_input
    payload = {"model": settings.model, "input": {"messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {
            "data": f"data:{mime};base64," + base64.b64encode(data).decode()}}
    ]}]}, "parameters": {"format": FORMATS[mime], "keep_dialect": False}}
    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds, trust_env=False) as client:
            response = await client.post(settings.base_url.rstrip("/") +
                "/api/v1/services/aigc/multimodal-generation/generation",
                headers={"Authorization": "Bearer " + api_key(config), "X-DashScope-SSE": "disable"},
                json=payload)
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise AssistantError(502, "VOICE_TRANSCRIPTION_FAILED", "Could not transcribe; retry this recording") from None
    if not isinstance(result, dict):
        raise AssistantError(502, "VOICE_TRANSCRIPTION_FAILED", "Could not transcribe; retry this recording")
    if "ASR_RESPONSE_HAVE_NO_WORDS" in str(result.get("message", "")):
        raise AssistantError(422, "VOICE_NO_SPEECH", "No speech detected; record again")
    if not response.is_success:
        raise AssistantError(502, "VOICE_TRANSCRIPTION_FAILED", "Could not transcribe; retry this recording")
    output = result.get("output") or {}
    if not isinstance(output, dict):
        raise AssistantError(502, "VOICE_TRANSCRIPTION_FAILED", "Could not transcribe; retry this recording")
    text = str(output.get("text") or "").strip()
    if not text:
        raise AssistantError(422, "VOICE_NO_SPEECH", "No speech detected; record again")
    try:
        duration = float((result.get("usage") or {})["duration"])
        if not 0 < duration <= MAX_SECONDS + 1:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise AssistantError(422, "VOICE_MESSAGE_DURATION", "Record up to 60 seconds") from None
    if len(text) > 8000:
        raise AssistantError(422, "VOICE_MESSAGE_DURATION", "Recording is too long")
    return {"text": text, "duration_ms": round(duration * 1000), "model": settings.model,
            "sha256": hashlib.sha256(data).hexdigest()}


async def transcribe(*, user_id, workspace_id, main_id, asset_id) -> dict:
    # Serialize per asset across workers. A failed request rolls back; a retry
    # reads the committed transcript instead of calling ASR again.
    async with get_db_session() as db:
        row = await owned_audio(db, asset_id, user_id, workspace_id, lock=True)
        value = cached(row)
        if value is None:
            try:
                data = bytearray()
                async with asyncio.timeout(20):
                    async for chunk in get_oss().get_object_chunks(row.oss_key, chunk_bytes=65536, timeout=15):
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise AssistantError(413, "VOICE_MESSAGE_FORMAT", "Recording exceeds 2 MB")
            except (OssError, OssNotConfigured, FileNotFoundError, TimeoutError):
                raise AssistantError(503, "VOICE_UPLOAD_UNAVAILABLE", "Could not read recording; retry") from None
            value = await recognize(bytes(data), row.mime)
            row.meta = {**(row.meta or {}), META_KEY: value}
        row.session_id = row.session_id or main_id
        return {"asset_id": row.id, "text": value["text"], "duration_ms": value["duration_ms"]}


async def bind_locked(db, *, asset_id, text, user_id, workspace_id) -> dict:
    row = await owned_audio(db, asset_id, user_id, workspace_id, lock=True)
    value = cached(row)
    if not value or value["text"] != text:
        raise AssistantError(409, "VOICE_TRANSCRIPT_MISMATCH", "Transcribe this recording before sending")
    return {"asset_id": row.id, "duration_ms": value["duration_ms"],
            "sha256": value["sha256"], "model": value["model"]}
