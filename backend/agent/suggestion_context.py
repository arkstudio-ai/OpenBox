"""Small, human-turn context for composer suggestions, independent of chat pagination."""
from __future__ import annotations

import json
import re

from sqlalchemy import select

from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part

# UTF-8 byte budgets are conservative for both Chinese and English. Leave room
# for the system prompt and result schema within an approximately 4K-token call.
CONTEXT_BYTES = 10_000
_REFERENTIAL = re.compile(r"之前|前面|上面|刚才|最初|第[一二三四五\d]+个|earlier|previous|above|original|second option", re.I)


def clip(text: str, size: int) -> str:
    """Keep the beginning and end (often the conclusion), never split UTF-8."""
    raw = text.encode("utf-8")
    if len(raw) <= size:
        return text
    half = max(0, (size - 8) // 2)
    if not half:
        return ""
    return raw[:half].decode("utf-8", errors="ignore") + "\n…\n" + raw[-half:].decode("utf-8", errors="ignore")


def human_text(parts: list[dict]) -> str:
    return "\n".join(
        p.get("text", "") for p in parts
        if p.get("type") == "text" and not p.get("synthetic") and not p.get("ignored")
    ).strip()


async def _parts(db, ids: list[str], types: tuple[str, ...]) -> dict[str, list[dict]]:
    if not ids:
        return {}
    rows = (await db.execute(select(Part.message_id, Part.data).where(
        Part.message_id.in_(ids), Part.type.in_(types),
    ).order_by(Part.created_at, Part.id))).all()
    grouped: dict[str, list[dict]] = {}
    for message_id, data in rows:
        grouped.setdefault(message_id, []).append(data)
    return grouped


async def _humans(db, session_id: str, user_id: str, *, oldest: bool = False):
    """Synthetic reminders, compaction and stop markers are not user rounds."""
    selected = []
    offset = 0
    while len(selected) < (1 if oldest else 5):
        order = (Message.created_at, Message.id) if oldest else (Message.created_at.desc(), Message.id.desc())
        rows = (await db.scalars(select(Message).where(
            Message.session_id == session_id, Message.user_id == user_id,
            Message.role == "user", Message.summary.is_not(True),
        ).order_by(*order).offset(offset).limit(16))).all()
        if not rows:
            break
        parts = await _parts(db, [m.id for m in rows], ("text", "file"))
        for row in rows:
            data = parts.get(row.id, [])
            if (row.client_message_id or "").startswith("tabort:"):
                continue
            text = human_text(data)
            files = [p.get("path", "") for p in data if p.get("type") == "file" and not p.get("transient")]
            if text or files:
                selected.append((row, text, files))
                if len(selected) == (1 if oldest else 5):
                    return selected
        offset += len(rows)
    return selected


def build_context(rounds: list[dict], goal: str, summary: str) -> str:
    count = 5 if rounds and _REFERENTIAL.search(rounds[-1]["user"]) else 3
    recent = rounds[-count:]
    context = {"initial_goal": clip(goal, 700), "previous_summary": clip(summary, 1_100), "rounds": []}
    # Reserve JSON overhead and state metadata, giving the latest pair twice
    # the space of an older pair. Long answers cannot crowd out user intent.
    share = 6_000 // (len(recent) + 1)
    for index, turn in enumerate(recent):
        budget = share * (2 if index == len(recent) - 1 else 1)
        context["rounds"].append({
            "user": clip(turn["user"], budget // 3),
            "assistant": clip(turn["assistant"], budget * 2 // 3),
            "state": clip(turn.get("state", ""), 220),
        })
    result = json.dumps(context, ensure_ascii=False)
    # Escaping control characters can expand the serialized representation.
    while len(result.encode("utf-8")) > CONTEXT_BYTES:
        for turn in context["rounds"]:
            for field in ("user", "assistant", "state"):
                turn[field] = clip(turn[field], len(turn[field].encode("utf-8")) * 3 // 4)
        context["initial_goal"] = clip(context["initial_goal"], 500)
        context["previous_summary"] = clip(context["previous_summary"], 700)
        result = json.dumps(context, ensure_ascii=False)
    return result


async def load_context(session_id: str, user_id: str, message_id: str) -> str | None:
    async with get_db_session() as db:
        humans = list(reversed(await _humans(db, session_id, user_id)))
        if not humans:
            return None
        oldest = await _humans(db, session_id, user_id, oldest=True)
        final = await db.get(Message, message_id)
        if final is None or final.session_id != session_id or final.user_id != user_id:
            return None
        answers = (await db.scalars(select(Message).where(
            Message.session_id == session_id, Message.user_id == user_id,
            Message.role == "assistant", Message.finish == "stop",
            Message.summary.is_not(True), Message.created_at >= humans[0][0].created_at,
            Message.created_at <= final.created_at,
        ).order_by(Message.created_at, Message.id))).all()
        parts = await _parts(db, [m.id for m in answers], ("text",))
        # Files and todo snapshots usually belong to a tool step, not to the
        # final prose message. Read those small typed parts, never tool logs.
        states = (await db.execute(select(Message.created_at, Message.id, Part.data).join(
            Part, Part.message_id == Message.id,
        ).where(
            Message.session_id == session_id, Message.user_id == user_id, Message.role == "assistant",
            Message.summary.is_not(True), Message.created_at >= humans[0][0].created_at,
            Message.created_at <= final.created_at, Part.type.in_(("file", "todo")),
        ).order_by(Message.created_at, Message.id, Part.created_at, Part.id))).all()
        rounds = []
        for index, (user, text, files) in enumerate(humans):
            end = humans[index + 1][0] if index + 1 < len(humans) else None
            candidates = [m for m in answers if not m.error
                          and (m.created_at, m.id) > (user.created_at, user.id)
                          and (end is None or (m.created_at, m.id) < (end.created_at, end.id))]
            if not candidates:
                continue
            answer = candidates[-1]
            data = parts.get(answer.id, [])
            answer_text = "\n".join(p.get("text", "") for p in data if p.get("type") == "text"
                                    and p.get("channel") != "commentary" and not p.get("ignored"))
            turn_state = [p for created, mid, p in states if (created, mid) > (user.created_at, user.id)
                          and (created, mid) <= (answer.created_at, answer.id)]
            state = [f"file: {p.get('path', '')}" for p in turn_state
                     if p.get("type") == "file" and not p.get("transient")]
            latest_todo = next((p for p in reversed(turn_state) if p.get("type") == "todo"), {})
            state = list(dict.fromkeys(state))[-3:]
            state += [f"{item.get('status')}: {item.get('subject')}" for item in latest_todo.get("items", [])[:3]]
            rounds.append({"user": text + "\n" + "\n".join(files[:3]), "assistant": answer_text,
                           "state": "\n".join(state[:6])})
        if not rounds or not answers or answers[-1].id != message_id:
            return None
        previous = await db.scalar(select(Part.data).where(
            Part.session_id == session_id, Part.user_id == user_id, Part.type == "suggestions",
            Part.created_at < final.created_at,
        ).order_by(Part.created_at.desc(), Part.id.desc()).limit(1))
        return build_context(rounds, oldest[0][1] if oldest else "", (previous or {}).get("context_summary", ""))
