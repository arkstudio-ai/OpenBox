"""Short-lived conversation context, without waking or authorizing the main agent.

Redis shares the recent call across workers; SQL still verifies ownership.
This is navigation only. Durable requests and long-term memories use the inbox.
"""
import asyncio
import json
from datetime import timezone

from sqlalchemy import select

from core.log import create_logger
from memory.redaction import redact_credentials
from voice import calls

log = create_logger("voice.live_context")
TTL_SECONDS, MAX_CHARS = 600, 5000


def key(user_id, workspace_id, main_id):
    return f"voice:context:{user_id}:{workspace_id}:{main_id}"


async def publish(scope, lines):
    recent, size = [], 0
    for line in reversed(lines):
        if line.role not in {"user", "assistant"}:
            continue
        at = line.at if line.at.tzinfo else line.at.replace(tzinfo=timezone.utc)
        value = {"speaker": line.role, "text": redact_credentials(line.text)[:800], "at": at.timestamp()}
        length = len(json.dumps(value, ensure_ascii=False))
        if size + length > MAX_CHARS or len(recent) >= 12:
            break
        recent.append(value)
        size += length
    if not recent:
        return
    async with asyncio.timeout(0.5):
        await calls._cache().set(key(scope.user_id, scope.workspace_id, scope.main_session_id),
                                {"call_id": scope.call_id, "lines": list(reversed(recent))}, ttl=TTL_SECONDS)


async def read(db, main, *, before=None):
    from db.models.voice import VoiceCall
    try:
        async with asyncio.timeout(0.5):
            value = await calls._cache().get(key(main.user_id, main.workspace_id, main.id))
        if not isinstance(value, dict) or not value.get("lines"):
            return None
        if before is not None:
            before = before if before.tzinfo else before.replace(tzinfo=timezone.utc)
            lines = [line for line in value["lines"] if isinstance(line.get("at"), (int, float))
                     and line["at"] <= before.timestamp()]
            if not lines:
                return None
            value = {**value, "lines": lines}
        identity = await db.scalar(select(VoiceCall.id).where(
            VoiceCall.id == value.get("call_id"), VoiceCall.user_id == main.user_id,
            VoiceCall.workspace_id == main.workspace_id, VoiceCall.main_session_id == main.id))
        return value if identity else None
    except Exception as exc:
        log.warning("voice live context unavailable error=%s", type(exc).__name__)
        return None
