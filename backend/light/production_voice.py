"""System and user-owned narration voices for guided productions."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import struct
import wave
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from sqlalchemy import select

from core.config import get_config
from core.oss import get_oss
from db.base import get_db_session
from db.models.preference import UserPreference
from db.models.user import User
from light.mode import LightError
from light import production_usage

MODEL = "qwen-audio-3.1-tts-flash"
PRESETS = {
    "zh": ("anmingyuan_v3.1", "yezhiqing_v3.1", "xiaxiaochen_v3.1", "longshu_v3.1"),
    "en": ("Brian_v3.1", "Emily_v3.1", "Luna_v3.1", "Eric_v3.1"),
    "vi": ("longanhuan_v3.1", "longanlingxin_v3.1", "longanfengyue_v3.1", "xunanchuan_v3.1"),
}
STYLE = ("documentary", "bright", "energetic", "steady")
SAMPLE = {
    "zh": "欢迎来到塞外朵莱娜，一起来看看今天的招牌菜。",
    "en": "Welcome! Let's take a look at today's signature dish.",
    "vi": "Xin chào, hãy cùng xem món đặc trưng hôm nay của chúng tôi.",
}
_PREF = "production_voices"
MAX_SAMPLE_BYTES = 10 * 1024 * 1024
_STATUS = {"OK": "ready", "DEPLOYING": "processing", "UNDEPLOYED": "failed"}


def language(value: str | None) -> str:
    return (value or "zh").split("-", 1)[0].lower()


def _error(code: str, message: str, status: int = 422) -> LightError:
    return LightError(code, message, status)


async def _call(path: str, model: str, data: dict, *, receipt: bool = False) -> dict:
    config = get_config().video_transcription
    base = (os.getenv("OPENBOX_TTS_BASE_URL") or "https://dashscope.aliyuncs.com").rstrip("/")
    key = os.getenv("OPENBOX_TTS_API_KEY") or os.getenv("DASHSCOPE_API_KEY") or config.api_key
    if not key:
        raise _error("PRODUCTION_VOICE_UNAVAILABLE", "Voice synthesis is unavailable.", 503)
    try:
        async with httpx.AsyncClient(timeout=35) as client:
            response = await client.post(f"{base}/api/v1/services/audio/tts/{path}", json={"model": model, "input": data},
                                         headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        raise _error("PRODUCTION_VOICE_PROVIDER", "Voice service did not respond. Try again.", 503) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.is_success or payload.get("code") or not isinstance(payload.get("output"), dict):
        code = str(payload.get("code") or response.status_code)
        raise _error("PRODUCTION_VOICE_PROVIDER", f"Voice service could not process the request ({code}).",
                     429 if response.status_code == 429 or code.startswith("Throttling") else 422)
    return payload if receipt else payload["output"]


async def _records(user: str) -> list[dict]:
    async with get_db_session() as db:
        row = await db.scalar(select(UserPreference).where(UserPreference.user_id == user))
        return list(((row.extra if row else None) or {}).get(_PREF) or [])


async def _update(user: str, voice: dict, *, reserve: bool = False) -> dict:
    async with get_db_session() as db:
        await db.scalar(select(User.id).where(User.id == user).with_for_update())
        pref = await db.scalar(select(UserPreference).where(UserPreference.user_id == user).with_for_update())
        if pref is None:
            pref = UserPreference(id=uuid4().hex, user_id=user, extra={})
            db.add(pref)
        extra = dict(pref.extra or {})
        rows = list(extra.get(_PREF) or [])
        existing = next((row for row in rows if row["id"] == voice["id"]), None)
        if reserve and existing:
            if existing.get("sample_sha") != voice.get("sample_sha") or existing.get("name") != voice.get("name"):
                raise _error("PRODUCTION_VOICE_CHANGED", "This upload request already belongs to another sample.", 409)
            return existing
        record = {**(existing or {}), **voice}
        extra[_PREF] = [record, *(row for row in rows if row["id"] != voice["id"])]
        pref.extra = extra
        return record


def _public(row: dict) -> dict:
    return {key: row[key] for key in ("id", "name", "status", "language")}


async def _status(user: str, row: dict) -> dict:
    if row.get("status") != "processing" or not row.get("provider_id"):
        return row
    try:
        response = await _call("customization", "voice-enrollment", {
            "action": "query_voice", "voice_id": row["provider_id"],
        })
    except (httpx.HTTPError, LightError):
        return row
    state = _STATUS.get(response.get("status"), "processing")
    if state != row["status"]:
        return await _update(user, {"id": row["id"], "status": state})
    return row


async def catalogue(user: str, locale: str | None) -> dict:
    lang = language(locale)
    mine = await asyncio.gather(*(_status(user, row) for row in await _records(user)))
    return {"system": [{"id": f"system:{style}", "style": style, "status": "ready"} for style in STYLE]
            if lang in PRESETS else [], "mine": [_public(row) for row in mine]}


async def create(user: str, name: str, audio: bytes, mime: str, key: str, locale: str | None) -> dict:
    lang = language(locale)
    record = {"id": f"voice:{key}", "name": name, "language": lang, "model": MODEL,
              "status": "processing", "sample_sha": hashlib.sha256(audio).hexdigest(), "reservation": uuid4().hex}
    saved = await _update(user, record, reserve=True)
    if saved["reservation"] != record["reservation"]:
        return _public(await _status(user, saved))
    try:
        response = await _call("customization", "voice-enrollment", {
            "action": "create_voice", "target_model": MODEL, "prefix": f"ob{key[:8]}",
            "audio": f"data:{mime};base64,{base64.b64encode(audio).decode('ascii')}",
            "language_hints": [lang], "enable_volume_normalization": "true",
        })
        provider_id = response.get("voice_id")
        if not provider_id:
            raise _error("PRODUCTION_VOICE_PROVIDER", "The voice service did not return a voice.")
    except (httpx.HTTPError, LightError):
        await _update(user, {"id": record["id"], "status": "failed"})
        raise
    saved = await _update(user, {"id": record["id"], "provider_id": provider_id})
    return _public(await _status(user, saved))


async def resolve(user: str, voice: str, locale: str | None) -> tuple[str, str]:
    lang = language(locale)
    if voice.startswith("system:"):
        style = voice.partition(":")[2]
        if style in STYLE and lang in PRESETS:
            return MODEL, PRESETS[lang][STYLE.index(style)]
    else:
        row = next((row for row in await _records(user) if row["id"] == voice), None)
        if row is not None:
            row = await _status(user, row)
            if row["status"] == "ready" and row.get("provider_id"):
                return row["model"], row["provider_id"]
    raise _error("PRODUCTION_VOICE_UNAVAILABLE", "Choose an available voice.")


async def _synthesize(model: str, voice: str, text: str, locale: str | None) -> dict:
    lang = language(locale)
    data = {"text": text, "voice": voice, "format": "wav", "sample_rate": 24000}
    if lang == "vi":
        data["language_hints"] = ["vi"]
    for attempt in range(4):
        try:
            return await _call("SpeechSynthesizer", model, data, receipt=True)
        except LightError as exc:
            if exc.status != 429 or attempt == 3:
                raise
            await asyncio.sleep((1, 2, 4)[attempt])


async def _read_audio(audio: dict) -> bytes:
    if audio.get("data"):
        return base64.b64decode(audio["data"])
    url = str(audio.get("url") or "")
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not (parts.hostname or "").endswith(".aliyuncs.com"):
        raise _error("PRODUCTION_VOICE_PROVIDER", "The voice service did not return playable audio.")
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(parts._replace(scheme="https").geturl())
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise _error("PRODUCTION_VOICE_PROVIDER", "Could not retrieve the synthesized voice.") from exc
    return response.content


def _canonical_wav(data: bytes) -> tuple[bytes, float]:
    # Streaming WAVs can report placeholder RIFF/data sizes even after download.
    try:
        with wave.open(io.BytesIO(data)) as sample:
            params = sample.getparams()
            frame_size = sample.getnchannels() * sample.getsampwidth()
            if sample.getnframes() * frame_size <= len(data) and struct.unpack_from("<I", data, 4)[0] + 8 == len(data):
                return data, sample.getnframes() / sample.getframerate()
            frames = sample.readframes(min(sample.getnframes(), len(data) // frame_size))
        count = len(frames) // frame_size
        if count:
            output = io.BytesIO()
            with wave.open(output, "wb") as normalized:
                normalized.setparams(params)
                normalized.writeframes(frames[:count * frame_size])
            return output.getvalue(), count / params.framerate
    except (EOFError, wave.Error) as exc:
        raise _error("PRODUCTION_VOICE_PROVIDER", "The voice service did not return playable audio.") from exc
    raise _error("PRODUCTION_VOICE_PROVIDER", "The voice service did not return playable audio.")


async def render_clip(*, user: str, workspace: str, session: str | None, model: str, voice: str,
                      text: str, locale: str | None) -> str:
    from billing import media

    digest = hashlib.sha256(f"{workspace}\0{model}\0{voice}\0{locale}\0{text}".encode()).hexdigest()
    key = f"assets/{user}/production-voice/{digest}.wav"
    oss = get_oss()
    async with production_usage.once(f"synthesis:{user}:{digest}"):
        if await oss.head_object_info(key):
            return key
        # Admission uses a conservative estimate only. The ledger records the provider's exact token counts.
        estimate = production_usage.tts_quote(model, {"input_tokens": len(text) * 3, "output_tokens": 32 * len(text)})
        async with production_usage.admit(workspace, session, estimate, "语音合成"):
            receipt = await _synthesize(model, voice, text, locale)
        usage = receipt.get("usage") or {}
        known = bool(usage.get("output_tokens"))
        price = production_usage.tts_quote(model, usage)
        request_id = str(receipt.get("request_id") or uuid4().hex)
        await media.settle(key=f"voice_synthesis:{request_id}", workspace_id=workspace, user_id=user, session_id=session,
                           price=price, kind="voice_synthesis", quantity_known=known,
                           tokens=usage, default_title="语音合成", title="语音合成")
        sound, _duration = _canonical_wav(await _read_audio(receipt["output"].get("audio") or {}))
        await oss.put_object(key, sound, content_type="audio/wav", forbid_overwrite=True)
    return key


async def audition(user: str, workspace: str, selected: str, locale: str | None) -> dict:
    model, voice = await resolve(user, selected, locale)
    text = SAMPLE.get(language(locale), SAMPLE["en"])
    key = await render_clip(user=user, workspace=workspace, session=None, model=model, voice=voice,
                            text=text, locale=locale)
    return {"url": get_oss().presign_get(key), "text": text}
