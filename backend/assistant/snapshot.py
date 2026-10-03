"""Consistent SQL snapshots and actor-bound receipts for visible answer positions."""
import base64
from datetime import datetime, timezone
from hashlib import sha256
import hmac
import json
import time

from sqlalchemy import case, func, select, update

from assistant.commands import _authority, command_digest
from assistant.evidence import validate_message_sources
from assistant.history import _cursor_key
from assistant.policy import AssistantError, main_session_locked, require_membership
from assistant.reads import get_task, list_tasks
from assistant.results import part_hash
from assistant.transactions import begin_snapshot
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantReadCursor
from db.models.message import Message
from db.models.part import Part
from session.internal_parts import begin_session_write

DISPLAY_TTL_SECONDS = 3600
DISPLAY_DOMAIN = b"assistant-display-v1:"


def _sign_display(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_key(), DISPLAY_DOMAIN + raw, sha256).digest()
    return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")


def _verify_display(token, *, user_id, workspace_id, main_id, sequence):
    try:
        if len(token) > 4096:
            raise ValueError()
        value = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw = value[32:]
        signature = hmac.new(_cursor_key(), DISPLAY_DOMAIN + raw, sha256).digest()
        payload = json.loads(raw)
        if (not hmac.compare_digest(signature, value[:32])
                or payload["scope"] != [user_id, workspace_id, main_id]
                or payload["sequence"] != sequence or payload["expires"] < time.time()):
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AssistantError(409, "ASSISTANT_DISPLAY_RECEIPT", "Reload the visible answer before marking it read") from None


async def _answer_digest(db, message, *, user_id, workspace_id, main_id):
    if (message is None or message.session_id != main_id or message.user_id != user_id
            or message.role != "assistant" or message.finish != "stop" or message.error or message.summary):
        raise AssistantError(410, "ASSISTANT_ANSWER_UNAVAILABLE", "Answer is no longer available")
    await validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
        Part.session_id == main_id, Part.user_id == user_id).order_by(Part.id))).all())
    if not any(p.type == "text" and not p.data.get("ignored") and str(p.data.get("text", "")).strip()
               for p in parts):
        raise AssistantError(410, "ASSISTANT_ANSWER_UNAVAILABLE", "Answer has no visible text")
    return command_digest({"message_id": message.id, "parts": [[p.id, part_hash(p)] for p in parts]})


async def get_snapshot(*, user_id: str, workspace_id: str, task_cursor=None,
                       before_sequence: int | None = None, limit: int = 50) -> dict:
    if type(limit) is not int or not 1 <= limit <= 50 or (before_sequence is not None and before_sequence < 1):
        raise ValueError("Invalid snapshot window")
    async with get_db_session() as db:
        await begin_snapshot(db)
        await require_membership(db, user_id, workspace_id)
        main = await main_session_locked(db, user_id, workspace_id)
        if main is None:
            return {"state": "not_created", "session": None, "high_water_mark": 0,
                    "last_seen_sequence": 0, "answers": [], "tasks": [], "unread_count": 0,
                    "next_task_cursor": None, "next_before_sequence": None, "unread_count_is_lower_bound": False}
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main.id)
        high_water = int(await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(
            AgentEvent.session_id == main.id, AgentEvent.user_id == user_id)))
        cursor = await db.get(AssistantReadCursor, (main.id, user_id))
        seen = cursor.last_seen_sequence if cursor else 0
        page = await list_tasks(user_id=user_id, workspace_id=workspace_id, main_id=main.id,
                                cursor=task_cursor, limit=limit, db=db)
        tasks = [await get_task(user_id=user_id, workspace_id=workspace_id, main_id=main.id,
                                task_id=row["id"], db=db) for row in page["items"]]
        # One original terminal occurrence per message, including after recovery.
        terminals = select(AgentEvent.message_id.label("message_id"), func.min(AgentEvent.sequence).label("sequence")).where(
            AgentEvent.session_id == main.id, AgentEvent.user_id == user_id,
            AgentEvent.kind == "turn.finished", AgentEvent.sequence <= high_water,
        ).group_by(AgentEvent.message_id).subquery()
        query = select(Message, terminals.c.sequence).join(terminals, terminals.c.message_id == Message.id).where(
            Message.session_id == main.id, Message.user_id == user_id, Message.role == "assistant",
            Message.finish == "stop", Message.summary.is_not(True))
        if before_sequence is not None:
            query = query.where(terminals.c.sequence < before_sequence)
        candidates = (await db.execute(query.order_by(terminals.c.sequence.desc()).limit(limit + 1))).all()
        answers = []
        for message, sequence in candidates[:limit]:
            answer = {"message_id": message.id, "sequence": sequence, "available": False}
            try:
                digest = await _answer_digest(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main.id)
            except AssistantError:
                # The caller can replace a stale transcript answer with an unavailable notice.
                pass
            else:
                answer.update(available=True, display_token=_sign_display({
                    "scope": [user_id, workspace_id, main.id], "sequence": sequence,
                    "message_id": message.id, "digest": digest, "expires": int(time.time()) + DISPLAY_TTL_SECONDS,
                }))
            answers.append(answer)
        has_more = len(candidates) > limit
        return {"state": "ready", "session": {key: getattr(main, key) for key in (
                    "id", "user_id", "workspace_id", "project_id", "kind", "agent", "model", "variant", "status")},
                "high_water_mark": high_water, "last_seen_sequence": seen,
                "tasks": tasks, "next_task_cursor": page["next_cursor"], "answers": answers,
                "next_before_sequence": answers[-1]["sequence"] if answers and has_more else None,
                "unread_count": sum(a["available"] and a["sequence"] > seen for a in answers),
                "unread_count_is_lower_bound": has_more and answers[-1]["sequence"] > seen}


async def advance_read_cursor(*, user_id: str, workspace_id: str, main_id: str,
                              last_seen_sequence: int, display_token: str) -> dict:
    if type(last_seen_sequence) is not int or last_seen_sequence < 1:
        raise ValueError("A displayed answer sequence is required")
    payload = _verify_display(display_token, user_id=user_id, workspace_id=workspace_id,
                              main_id=main_id, sequence=last_seen_sequence)
    async with get_db_session() as db:
        await begin_session_write(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        # Also serializes insertion of the first cursor on independent workers.
        await main_session_locked(db, user_id, workspace_id, lock=True)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main_id,
            AgentEvent.user_id == user_id, AgentEvent.sequence == last_seen_sequence,
            AgentEvent.message_id == payload["message_id"], AgentEvent.kind == "turn.finished"))
        if event is None:
            raise AssistantError(409, "ASSISTANT_DISPLAY_RECEIPT", "Displayed event is no longer available")
        message = await db.get(Message, payload["message_id"])
        digest = await _answer_digest(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if digest != payload["digest"]:
            raise AssistantError(409, "ASSISTANT_DISPLAY_RECEIPT", "Displayed answer has changed")
        now = datetime.now(timezone.utc)
        cursor = await db.get(AssistantReadCursor, (main_id, user_id))
        if cursor is None:
            cursor = AssistantReadCursor(assistant_session_id=main_id, user_id=user_id,
                                         last_seen_sequence=last_seen_sequence, updated_at=now)
            db.add(cursor)
            await db.flush()
        else:
            await db.execute(update(AssistantReadCursor).where(
                AssistantReadCursor.assistant_session_id == main_id, AssistantReadCursor.user_id == user_id,
            ).values(last_seen_sequence=case(
                (AssistantReadCursor.last_seen_sequence < last_seen_sequence, last_seen_sequence),
                else_=AssistantReadCursor.last_seen_sequence), updated_at=now))
            await db.refresh(cursor)
        return {"assistant_session_id": main_id, "last_seen_sequence": cursor.last_seen_sequence}
