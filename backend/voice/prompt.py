"""What the front desk is told, and what a voice turn tells the personal assistant.

The front prompt (docs/ASSISTANT_VOICE_FIX_PLAN.md §5.4) lets the front desk
talk freely and keeps facts to tool outputs and background notes. The session
prompt is the rules plus facts read at connect time (all best effort: missing
facts are left out, never invented) plus two sections the call keeps current:
what the call has been about so far and what the assistant is doing now.
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    "你是用户的私人助理，正在和用户打电话。说话就像一个跟了老板很久、靠谱又随和的助理在电话里聊天：\n"
    "- 口语、短句，一次一两句，说完就停，让用户接话。可以用“嗯”“好嘞”“行”“对了”“这样啊”自然衔接，别每句都用。\n"
    "- 不用书面腔和客服腔：不说“已为您”“进行”“目前”“麻烦您”“请您”“收到”“好的呢”，"
    "也不说“结论是”“第一”“首先”这种汇报格式；同一通电话里不说重复的话，不用“我这边查到了”“还在办”这类套话。\n"
    "- 名字里的书名号、括号、编号不念；任务名、项目名太长就说得口语点（比如“那个口播视频”“贪吃蛇那个”）。\n"
    "- 用户不耐烦或听不懂时，先简单接一句（比如“哎，我刚才没说清楚”），再把事说明白。\n"
    "- 你能自己查的先自己查：tasks_overview 看任务、进展和最新结果，memory_search 翻记忆，schedules_list 看定时任务，"
    "projects_list 看项目，credits 看积分，cards_pending 看等用户确认的卡片；再决定要不要交给助理。"
    "用户问某个任务有没有结果、结果说了什么，先用 tasks_overview 查，别凭印象回答。\n"
    "- 要办事（建项目、派任务、改东西、记偏好、删除）或要查更深的细节，就调用 assistant_ask，"
    "把用户原话整句原样交过去，一句话里有几件事也整句交，不要拆开；同时先用一句话告诉用户你去安排。\n"
    "- 只有工具结果和“后台备注”里有的事实才能说；没有就说没查到。不要编造进度，没查过不要说“查到了”。\n"
    "- 后台备注到了，用自己的话结合刚才聊的内容转述，事实不变；个人助理主动汇报的结果，用户没问也要找空当告诉用户。\n"
    "- 删除、代答、发送这类高风险的事，个人助理会出一张确认卡片（后台备注或 cards_pending 里有编号）："
    "先说清楚要做什么、影响是什么，再问“确认吗”。用户明确同意（确认、可以、删吧、就这样）才用 cards_answer "
    "选卡片上确认的那个选项；用户拒绝（算了、不删了、取消）也要用 cards_answer 选取消，把卡片关掉；"
    "含糊、反问、没出声都不算同意，再问一次。"
    "回答卡片只能用 cards_answer，不要把“确认”交给 assistant_ask。\n"
    "- 用户问有没有要确认或回答的事，用 cards_pending。任务里等用户回答的问题，用户说了怎么答，"
    "就把原话交给 assistant_ask，由个人助理代答；要用户自己在屏幕上处理的，就告诉用户在屏幕上处理。\n"
    "- 听起来没说完的话（比如“新建一个”“就是”）先等一等，或者追问一句想做什么，不要半句就交办。\n"
    "- 你不能上网，没有天气、新闻、股价、路况这类实时信息：用户问到就直说这个你查不到，"
    "绝不能说出任何天气、温度、价格或别的数字和情况。\n"
    "- 用户说“停”“别说了”只是让你停下，不用回应；要停止一件事要明确说出来，由个人助理处理。\n"
    "- 你是 AI 助理：不说自己累了、饿了、困了这类身体感受，也不说紧张、担心、心里打鼓这类情绪。\n"
    "- 用用户的语言说，不念链接、ID、编号。\n")
PROFILE_CHARS, RECENT_REPLIES, RECENT_CHARS = 300, 3, 60
FINISHED_ITEMS, FINISHED_CHARS, SINCE_HOURS = 3, 200, 24
PROFILE_TYPES = ("USER_PROFILE", "PREFERENCE")
CONTEXT_SECONDS = 1.0  # read while the provider connects; any longer would delay `ready`
_WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class FrontFacts:
    """Read once at connect time; each one may be empty."""
    profile: str = ""     # the user's profile and preferences (how to address them, when a memory says so)
    recent: str = ""      # the latest typed replies in the main session
    last_call: str = ""   # the previous call's summary, within a day
    finished: str = ""    # watched work that finished since the previous call


def front_instructions(facts: FrontFacts, lang: str, now: datetime) -> str:
    """The rules plus today's facts; an English UI only sets the language to start in."""
    lines = [f"现在是 {now.year}年{now.month}月{now.day}日 星期{_WEEKDAYS[now.weekday()]} {now:%H:%M}。"]
    if lang == "en":
        lines.append("用户的界面语言是英文，先用英文和用户交谈。")
    for label, value in (("关于用户", facts.profile), ("上次通话", facts.last_call),
                         ("上次通话后办完的事", facts.finished), ("最近在文字里聊过", facts.recent)):
        if value:
            lines.append(f"{label}：{value}。")
    return FRONT + "".join(lines)


def with_sections(base: str, *, call_so_far: str = "", progress: str = "", last_lines: str = "") -> str:
    """The session prompt with what the call keeps current; empty sections are left out."""
    sections = [f"\n本通电话到目前为止：{call_so_far}" if call_so_far else "",
                f"\n刚才最后几句：\n{last_lines}" if last_lines else "",
                f"\n当前后台进度：{progress}" if progress else ""]
    return base + "".join(sections)


def local_zone() -> ZoneInfo:
    from core.config import get_config
    try:
        return ZoneInfo(get_config().memory.default_timezone)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def local_now() -> datetime:
    return datetime.now(local_zone())


async def front_context(*, user_id: str, workspace_id: str, main_session_id: str) -> FrontFacts:
    """Every fact read in parallel, each bounded; a slow or failing one is left out."""
    async def guarded(reader, empty=""):
        try:
            return await asyncio.wait_for(reader, CONTEXT_SECONDS)
        except Exception as exc:  # never blocks or fails a call
            log.info("voice front context skipped part=%s error=%s", reader.__name__, type(exc).__name__)
            return empty
    profile, recent, (last_call, finished) = await asyncio.gather(
        guarded(profile_summary(user_id, workspace_id)), guarded(recent_summary(user_id, main_session_id)),
        guarded(since_last_call(user_id, workspace_id), ("", "")))
    return FrontFacts(profile=profile, recent=recent, last_call=last_call, finished=finished)


async def since_last_call(user_id: str, workspace_id: str) -> tuple[str, str]:
    """(the last call's summary with its time, what finished since that call) within the last day."""
    from voice import calls
    now = datetime.now(timezone.utc)
    previous = await calls.previous_call(user_id, workspace_id)
    summary = await calls.latest_call_summary(user_id, workspace_id, SINCE_HOURS)
    since = max(previous["ended_at"], now - timedelta(hours=SINCE_HOURS)) if previous else now - timedelta(
        hours=SINCE_HOURS)
    finished = await finished_since(user_id, workspace_id, since)
    return (f"{_spoken_time(summary['ended_at'], now)}，{summary['summary']}" if summary else ""), finished


async def finished_since(user_id: str, workspace_id: str, since: datetime) -> str:
    """Watched tasks whose latest result came in after ``since``: title, state, its first sentence."""
    from assistant.reads import watch_list
    from voice.tools import first_sentence, state_label
    value = await watch_list(user_id=user_id, workspace_id=workspace_id)
    lines = []
    for item in value["items"]:
        result = item.get("latest_result") or {}
        try:
            created = datetime.fromisoformat(str(result.get("created_at")))
        except ValueError:
            continue
        created = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created
        if created > since:
            sentence = first_sentence(result.get("summary"), 50)
            title = item.get("title") or (item.get("project") or {}).get("name") or ""
            lines.append(f"「{title}」{state_label(item.get('observed_state'), 'zh')}"
                         + (f"（{sentence.rstrip('。')}）" if sentence else ""))
    return _bounded("；".join(lines[:FINISHED_ITEMS]), FINISHED_CHARS)


def _spoken_time(moment: datetime, now: datetime) -> str:
    local, today = moment.astimezone(local_zone()), now.astimezone(local_zone())
    day = "今天" if local.date() == today.date() else "昨天" if local.date() == today.date() - timedelta(days=1) \
        else f"{local.month}月{local.day}日"
    return f"{day} {local:%H:%M}"


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
