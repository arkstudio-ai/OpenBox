"""A reply that promises later work nothing will bring back: the turn goes on instead of ending on it.

A reply ends the assistant's turn. It runs again only when the user writes or
when work it started reports back (a task it handed over, a scheduled job).
Measured 2026-10-08: after ``schedules.create`` failed six times the assistant
answered "你先别急，我这就重试，好了第一时间告诉你" and stopped; nothing ever
retried, and the voice front desk passed the promise on to the user. Such a
reply gets one more step with a reminder (agent/loop.py): do it now, or say
plainly what failed and what is needed. A promise to report back after work
was started in the same turn is kept by that work and is left alone.
"""
import re

from core.log import create_logger

log = create_logger("assistant.follow_through")

# "I will retry / do it again", "I'll tell you once it's done", "don't worry": promises about later.
LATER = re.compile(
    r"(?:我|这就|马上|立刻|稍后|待会儿?|回头|等会儿?|一会儿?)(?:就)?(?:再|重新)?(?:去)?"
    r"(?:重试|再试|试一次|试一下|重新(?:提交|创建|建|发|跑|试|来))"
    r"|(?:好了|弄好|办好|做好|建好|完成|有结果|搞定|成功)(?:之后|以后|后|了)?[，,]?\s*(?:我)?(?:会)?"
    r"(?:第一时间|马上|立刻|再|就)?(?:告诉|通知|回复|同步)你"
    r"|你先别急"
    r"|\bI'?ll (?:try again|retry|let you know|get back to you|report back)\b",
    re.IGNORECASE)
# Work that reports back on its own once started: a promise to tell the user later is then kept.
BACKGROUND_TOOLS = frozenset({"tasks.submit", "tasks.followup", "tasks.next_step", "tasks.resume",
                              "schedules.create", "schedules.update", "schedules.run"})
REMINDER = (
    "<system-reminder>\n"
    "Your reply promises to retry or to report back later, but nothing started in this turn will bring a result "
    "back: this reply ends your turn, and you will not act again until the user writes. Do it now instead: retry "
    "with corrected arguments or another way. If you cannot, tell the user plainly what failed and what you need "
    "from them, and do not promise later work. Do not mention this reminder.\n"
    "</system-reminder>")


def promises_later(text: str | None) -> bool:
    return bool(text and LATER.search(text))


async def started_background_work(session_id: str, user_id: str, message_ids) -> bool:
    """Whether this run started work that reports back: a background tool that completed without error."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    ids = [message_id for message_id in message_ids if message_id]
    if not ids:
        return False
    async with get_db_session() as db:
        rows = (await db.scalars(select(Part.data).where(
            Part.session_id == session_id, Part.user_id == user_id, Part.message_id.in_(ids),
            Part.type == "tool"))).all()
    return any((data or {}).get("tool") in BACKGROUND_TOOLS and (data or {}).get("status") == "completed"
               and not (data or {}).get("error") for data in rows)


async def needs_another_step(*, session_kind: str | None, text: str | None, session_id: str, user_id: str,
                             message_ids) -> bool:
    """The personal assistant's reply promised later work that nothing in this run will do."""
    if session_kind != "assistant" or not promises_later(text):
        return False
    try:
        return not await started_background_work(session_id, user_id, message_ids)
    except Exception as exc:  # unknown: the reply stands as it is
        log.warning("follow-through check skipped session=%s error=%s", session_id, type(exc).__name__)
        return False
