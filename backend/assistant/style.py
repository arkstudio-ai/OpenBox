"""How this user likes to be helped, as they said it or showed it: the "style card".

Two sources, both the user's own:
- memories about how the assistant should talk or work with them ("太长了，说重点", "别问那么多"),
  which extraction files under ``personal.style.*`` (memory/extraction.py);
- why they turned one of its answers down (the reason picked with a thumbs-down in the assistant's
  chat), when the same reason keeps coming back.

The card goes into the assistant's prompt (assistant/profile.py ``prompt_section``) and the phone
front desk's (voice/prompt.py), which both follow it. A preference for one kind of work only
(``personal.task.*``, "做视频时…") stays out: recall brings it when that work comes up.

What Settings has a field for (answer length, tone, emoji, how much is said on calls) is never a
line on the card: said in the assistant's own chat it changes that setting, as the user's decision
(memory/extraction.py, ``setting_patch``), so one aspect always has one value and the card only
holds what no setting covers ("别反复确认", "先说结论再说原因", "用英文回我").
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
# Style facts Settings has a field for, and the field: their value is the setting, never a card line.
SETTABLE_STYLE = {"personal.style.length": "length", "personal.style.tone": "tone",
                  "personal.style.emoji": "emoji", "personal.style.call_detail": "call_detail"}


def settable_field(fact_key: str | None) -> str | None:
    """The Settings field a style fact key stands for ("personal.style.length:3fa1" too), or None."""
    return SETTABLE_STYLE.get((fact_key or "").split(":", 1)[0])


_SETTING_WORDS = {
    "length": {"brief": "answers kept short", "balanced": "answers of medium length", "detailed": "detailed answers"},
    "tone": {"warm": "a warm tone", "professional": "a professional tone", "lively": "a lively, relaxed tone"},
    "emoji": {"on": "an emoji now and then", "off": "no emoji"},
    "call_detail": {"brief": "short answers on phone calls", "detailed": "fuller answers on phone calls"},
}


def setting_claim(fact_key: str | None, value) -> str:
    """What a settable style fact would set, for the verifier to check against the user's words."""
    field = settable_field(fact_key)
    words = _SETTING_WORDS.get(field, {}).get(value) if field else None
    return f" (setting: {words})" if words else ""


def setting_patch(fact_key: str | None, value) -> dict | None:
    """{field: value} when ``value`` is a valid setting for the aspect ``fact_key`` names, else None.
    Emoji is said as "on"/"off"."""
    from assistant.profile import Profile, merge
    field = settable_field(fact_key)
    if field is None:
        return None
    if field == "emoji":
        value = {"on": True, "off": False}.get(value)
    try:
        merge(Profile(), {field: value})
    except ValueError:
        return None
    return {field: value}


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


async def apply_settings(*, user_id: str, session_id: str, input_hash: str, proposals: list[dict],
                         grounding: dict | None) -> dict:
    """Verified feedback on an aspect Settings has a field for, said in the assistant's own chat (typed or
    on a call), is the user's decision there; later statements in the turn win. Returns what was set.
    How a work chat answered says nothing about how the assistant should talk: nothing is set from one."""
    from sqlalchemy import select
    from assistant import profile
    from db.base import get_db_session
    from db.models.session import Session
    from wiki_compiler.hashing import canonical_hash
    if not grounding or grounding.get("input_hash") != input_hash:
        return {}
    supported, patch = set(grounding.get("supported", [])), {}
    for proposal in proposals:
        change = setting_patch(proposal.get("fact_key"), proposal.get("setting"))
        if change and canonical_hash(proposal) in supported:
            patch.update(change)
    if not patch:
        return {}
    async with get_db_session() as db:
        kind = await db.scalar(select(Session.kind).where(Session.id == session_id))
    if kind != "assistant":
        return {}
    await profile.save(user_id, patch, via="chat")
    log.info("style feedback set user=%s fields=%s", user_id, ",".join(sorted(patch)))
    return patch


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
        rows = (await db.scalars(select(UserMemory).where(
            *access.predicates(UserMemory), *active_memory_predicates(),
            UserMemory.fact_key.like(STYLE_PREFIX + "%")).order_by(
            UserMemory.updated_at.desc(), UserMemory.id).limit(LEARNED_LIMIT * 3))).all()
    # Learned before the setting took them over: the setting holds that aspect now.
    return [row for row in rows if settable_field(row.fact_key) is None][:LEARNED_LIMIT]


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
