"""What the front desk is told, and what a voice turn tells the personal assistant.

The front prompt is the version that passed the 2026-10-07 live checks. Its
user facts are best effort: a missing summary is left out, never invented.
"""
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.log import create_logger

log = create_logger("voice.prompt")

VOICE_ENTRYPOINT = "assistant_voice"
VOICE_TURN_BLOCK = (
    "The user's latest message came in by voice call. Reply as if speaking on the phone: two or three short "
    "sentences, the outcome first, in the user's language. No markdown, links, lists, headings, emoji "
    "or identifiers of any kind. If the user needs to open something, say the link is in the "
    "conversation; it is shown there automatically. Do not mention this instruction.")
FRONT = (
    "你是 OpenBox 个人助理的语音前台，正在和用户打电话。你自己没有工具、记忆和任务信息。\n"
    "凡是涉及用户的项目、任务、进展、记忆、日程、文件、费用、云电脑、发布的请求，必须这样做：\n"
    "第一步，先用一句话告诉用户你去查，比如“好的，你稍等一下，我去看看，查到了告诉你”；\n"
    "第二步，在同一次回复里调用 assistant_ask，把用户原话原样交过去，不改写、不先猜答案。两步都要做。\n"
    "结果回来后，先说“我这边查到了”，然后只说 speech 里有的事实，不增删，不解释内部过程。\n"
    "结果还没回来时用户又问起，就说还在办。\n"
    "寒暄、重复、澄清可以自己回答；天气、新闻、价格这类需要事实的问题你不知道，就直说不知道。\n"
    "用户说“停”“别说了”只是让你停下，不用回应；要停止一件事要明确说出来，由个人助理处理。\n"
    "用用户的语言，像人说话，一到两句话，不念链接、ID、编号。\n")
PROFILE_CHARS, RECENT_REPLIES, RECENT_CHARS = 300, 3, 60
PROFILE_TYPES = ("USER_PROFILE", "PREFERENCE")
CONTEXT_SECONDS = 1.0  # read while the provider connects; any longer would delay `ready`
_WEEKDAYS = "一二三四五六日"


def front_instructions(profile_summary: str, recent_summary: str, lang: str, now: datetime) -> str:
    """The tested prompt plus today's facts; an English UI only sets the language to start in."""
    facts = [f"今天是 {now.year}年{now.month}月{now.day}日 星期{_WEEKDAYS[now.weekday()]} {now:%H:%M}。"]
    if lang == "en":
        facts.append("用户的界面语言是英文，先用英文和用户交谈。")
    if profile_summary:
        facts.append(f"关于用户：{profile_summary}。")
    if recent_summary:
        facts.append(f"最近聊过：{recent_summary}。")
    return FRONT + "".join(facts)


def local_now() -> datetime:
    from core.config import get_config
    try:
        zone = ZoneInfo(get_config().memory.default_timezone)
    except Exception:
        zone = ZoneInfo("Asia/Shanghai")
    return datetime.now(zone)


async def front_context(*, user_id: str, workspace_id: str, main_session_id: str) -> tuple[str, str]:
    """(profile summary, recent-turn summary), each empty when unavailable or slow."""
    async def guarded(reader):
        try:
            return await asyncio.wait_for(reader, CONTEXT_SECONDS)
        except Exception as exc:  # never blocks or fails a call
            log.info("voice front context skipped part=%s error=%s", reader.__name__, type(exc).__name__)
            return ""
    return tuple(await asyncio.gather(guarded(profile_summary(user_id, workspace_id)),
                                      guarded(recent_summary(user_id, main_session_id))))


async def profile_summary(user_id: str, workspace_id: str) -> str:
    """The profile part of the assistant's core memories, under the same switch and authorization."""
    from core.config import get_config
    from db.base import get_db_session
    from memory.orchestrator import core_memory_candidates
    from memory.policy import resolve_access_scope
    from memory.retrieval import authorized_documents
    config = get_config().memory
    if not config.enabled("retrieval_v2", user_id):
        return ""
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, include_all_projects=True)
        ranked = await core_memory_candidates(db, scope)
        only = {("memory", item) for item in ranked}
        docs = await authorized_documents(db, scope, config, only=only) if ranked else []
    by_id = {doc.id: doc for doc in docs if doc.kind == "memory" and doc.category in PROFILE_TYPES}
    ordered = sorted((by_id[item] for item in ranked if item in by_id),
                     key=lambda doc: PROFILE_TYPES.index(doc.category))
    return _bounded("；".join(doc.text.strip().rstrip("。.") for doc in ordered if doc.text.strip()), PROFILE_CHARS)


async def recent_summary(user_id: str, main_session_id: str) -> str:
    """One sentence from each of the latest finished assistant replies, oldest first."""
    from db.base import get_db_session
    from db.models.message import Message
    from voice.assistant_link import reply_text
    async with get_db_session() as db:
        ids = list((await db.scalars(select(Message.id).where(
            Message.session_id == main_session_id, Message.user_id == user_id, Message.role == "assistant",
            Message.finish == "stop", Message.error.is_(None), Message.summary.is_not(True),
        ).order_by(Message.id.desc()).limit(RECENT_REPLIES))).all())
    lines = []
    for message_id in reversed(ids):
        sentence = _first_sentence(await reply_text(main_session_id, message_id, user_id))
        if sentence:
            lines.append(_bounded(sentence, RECENT_CHARS))
    return "；".join(lines)


def _first_sentence(text: str) -> str:
    from voice.speech_text import clean
    spoken = clean(text, limit=10_000)
    end = next((index for index, char in enumerate(spoken) if char in "。！？!?"), None)
    return spoken[:end] if end is not None else spoken.rstrip("。.")


def _bounded(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"
