"""A skipped or expired card is not a go-ahead for paid generation.

gw2 2026-11-12: a helper posted its 「确认视频方案」 card, the owner tapped 跳过 on the phone, the tool
answered "No approval was granted" and the helper generated the 30-second clip anyway (7.78 credits). The
poster helper next to it did the same with its 「请上传菜品照片」 card. Words in a tool result do not hold
a model that was told "the task is done only when the finished piece exists"; the spend itself has to be
refused. The card stands until the person says something new in the conversation (their own message, or
their assistant relaying one): that is the only thing that can change what they want.
"""
from sqlalchemy import select

from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session

#: Tool id -> the actions that spend (None: every call does).
SPENDING = {"image_gen": None, "video_generate": {"submit"}, "video_compose": {"submit"}}
WORDS = {"human", "assistant_delegation"}
FAILURE_CODE = "question_skipped"
MESSAGE = ("Not executed: the user skipped your last card (or let it expire) and has said nothing since, so nothing "
           "was approved. Do not generate, compose or retry on your own choices. Tell the user in one short line "
           "that nothing has been made or charged and what you need from them to start, then stop and wait.")


def spends(tool_id: str, args) -> bool:
    if tool_id not in SPENDING:
        return False
    actions = SPENDING[tool_id]
    return actions is None or (isinstance(args, dict) and args.get("action") in actions)


async def standing_skip(db, session_id: str, user_id: str) -> str | None:
    """The id of the newest question card when it was skipped or expired and nobody has spoken since."""
    session = await db.get(Session, session_id)
    if session is None or session.kind != "normal":
        return None
    card = (await db.scalars(select(QuestionCheckpoint).where(
        QuestionCheckpoint.session_id == session_id, QuestionCheckpoint.user_id == user_id,
        QuestionCheckpoint.detached.is_(False),
        QuestionCheckpoint.status.in_(("pending", "answered", "rejected", "expired")),
    ).order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()).limit(1))).first()
    if card is None or card.status not in ("rejected", "expired"):
        return None
    # Registered cards (a model switch, a takeover…) and memory/plan confirmations define their own "no".
    if (card.continuation or {}).get("kind") != "question" or any(
            isinstance(item, dict) and (item.get("detail") or {}).get("kind") for item in card.questions or []):
        return None
    later = (await db.execute(select(Part.data).join(Message, Message.id == Part.message_id).where(
        Part.session_id == session_id, Part.user_id == user_id, Part.type == "text",
        Message.role == "user", Part.created_at > card.updated_at,
    ).order_by(Part.created_at.desc()).limit(20))).scalars().all()
    if any(isinstance(data, dict) and data.get("origin") in WORDS and not data.get("synthetic")
           and (data.get("text") or "").strip() for data in later):
        return None
    return card.id
