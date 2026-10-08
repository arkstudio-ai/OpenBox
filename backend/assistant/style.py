"""How this user likes to be helped, as they said it or showed it: the "style card".

Two sources, both the user's own:
- memories about how the assistant should talk or work with them ("太长了，说重点", "别问那么多"),
  which extraction files under ``personal.style.*`` (memory/extraction.py);
- why they turned one of its answers down (the reason picked with a thumbs-down in the assistant's
  chat), when the same reason keeps coming back.

The card goes into the assistant's prompt (assistant/profile.py ``prompt_section``) and the phone
front desk's (voice/prompt.py), which both follow it. A preference for one kind of work only
(``personal.task.*``, "做视频时…") stays out: recall brings it when that work comes up.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("assistant.style")

STYLE_PREFIX = "personal.style."
LEARNED_LIMIT, LEARNED_CHARS, ITEM_CHARS = 8, 600, 120
# Reasons offered with a thumbs-down (web and mobile), and how the card says them.
REACTION_REASONS = ("too_long", "too_short", "off_topic", "wrong", "tone")
REASON_TEXT = {"too_long": "回答太长", "too_short": "回答太简略", "off_topic": "答非所问",
               "wrong": "内容有错", "tone": "语气不合适"}
REACTION_DAYS, REACTION_MIN = 30, 2  # a reason given at least twice in a month is a pattern


@dataclass(frozen=True)
class Card:
    learned: tuple[str, ...] = ()
    reactions: tuple[tuple[str, int], ...] = ()  # (reason, times) in the last month

    def lines(self) -> list[str]:
        """One line each, in the user's language: learned preferences first, then feedback patterns."""
        return [*self.learned, *(f"最近 {REACTION_DAYS} 天有 {count} 次点踩，原因是{REASON_TEXT[reason]}"
                                 for reason, count in self.reactions)]


def _bounded(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


async def style_card(user_id: str, workspace_id: str | None) -> Card:
    """The card for one user; an empty one when nothing can be read (never blocks a reply or a call)."""
    try:
        learned = await _learned(user_id, workspace_id) if workspace_id else ()
        return Card(learned, await _reactions(user_id))
    except Exception as exc:
        log.warning("style card unread user=%s error=%s", user_id, type(exc).__name__)
        return Card()


async def learned(user_id: str, workspace_id: str | None) -> dict:
    """Settings → 个人助理 shows what was learned, each removable like any memory, and the reasons
    given with thumbs-downs this month."""
    rows = await _learned_rows(user_id, workspace_id) if workspace_id else []
    return {"learned": [{"id": row.id, "revision": row.revision, "summary": str((row.value or {}).get("summary") or ""),
                         "updated_at": row.updated_at.isoformat() if row.updated_at else None} for row in rows],
            "reactions": [{"reason": reason, "count": count} for reason, count in await _reactions(user_id, at_least=1)]}


async def _learned_rows(user_id: str, workspace_id: str) -> list:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.memory import UserMemory
    from memory.policy import active_memory_predicates, resolve_access_scope
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id)
        return list((await db.scalars(select(UserMemory).where(
            *access.predicates(UserMemory), *active_memory_predicates(),
            UserMemory.fact_key.like(STYLE_PREFIX + "%")).order_by(
            UserMemory.updated_at.desc(), UserMemory.id).limit(LEARNED_LIMIT))).all())


async def _learned(user_id: str, workspace_id: str) -> tuple[str, ...]:
    items, used = [], 0
    for row in await _learned_rows(user_id, workspace_id):
        text = _bounded(str((row.value or {}).get("summary") or ""), ITEM_CHARS)
        if not text or text in items or used + len(text) > LEARNED_CHARS:
            continue
        items.append(text)
        used += len(text)
    return tuple(items)


async def _reactions(user_id: str, *, at_least: int = REACTION_MIN) -> tuple[tuple[str, int], ...]:
    from sqlalchemy import func, select
    from db.base import get_db_session
    from db.models.message import Message
    from db.models.session import Session
    since = datetime.now(timezone.utc) - timedelta(days=REACTION_DAYS)
    async with get_db_session() as db:
        # The assistant's own answers: how a work chat answered says nothing about how it talks.
        rows = (await db.execute(select(Message.reaction_reason, func.count()).join(
            Session, Session.id == Message.session_id).where(
            Message.user_id == user_id, Message.role == "assistant", Message.reaction == "down",
            Message.reaction_reason.in_(REACTION_REASONS), Message.created_at >= since,
            Session.kind == "assistant").group_by(Message.reaction_reason))).all()
    return tuple(sorted(((reason, int(count)) for reason, count in rows if count >= at_least),
                        key=lambda item: (-item[1], item[0])))
