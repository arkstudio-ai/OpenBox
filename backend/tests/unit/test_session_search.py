"""The sidebar search: conversations by title and by what was said in them (session.search_sessions)."""
from sqlalchemy import update

from api import sessions as routes
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.session import Session
from models.message import TextPart
from session import session as sessions
from session.session import create_assistant_message, create_user_message, save_part
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def said(session_id, user_id, question, answer=None, agent="build"):
    user = await create_user_message(session_id, question, agent=agent, user_id=user_id)
    if answer is not None:
        reply = await create_assistant_message(session_id, user.id, user_id=user_id)
        await save_part(TextPart(text=answer, session_id=session_id, message_id=reply.id), is_new=True, user_id=user_id)


async def test_titles_first_then_messages_with_the_words_around_the_match():
    owner, _, workspace = await accounts()
    snake = await sessions.create_session(user_id=owner, workspace_id=workspace, title="贪吃蛇界面讨论")
    plan = await sessions.create_session(user_id=owner, workspace_id=workspace, title="周计划")
    answer = ("上周复盘：官网首页文案已经改完，投放数据也看过了。\n\n下周开始优化贪吃蛇的计分逻辑，之后再看下个月的选题"
              "安排和预算拆分，顺便把素材库整理一遍，再约设计对一下新的配色和图标风格，最后整理成周报发给大家。"
              "下下周再定年度计划的初稿和各项目负责人名单，月底前交给老板过目。")
    await said(plan.id, owner, "下周要做什么？", answer)
    await said(snake.id, owner, "贪吃蛇的配色换成暗色")
    other = await sessions.create_session(user_id=owner, workspace_id=workspace, title="五子棋")
    await said(other.id, owner, "棋盘做成 15 路")

    hits = await sessions.search_sessions("  贪吃蛇 ", user_id=owner, workspace_id=workspace)
    assert [(hit["session_id"], hit["match"]) for hit in hits] == [(snake.id, "title"), (plan.id, "content")]
    assert hits[0]["snippet"] == "贪吃蛇的配色换成暗色" and hits[0]["role"] == "user"
    assert hits[1]["title"] == "周计划" and hits[1]["role"] == "assistant"
    flat = " ".join(answer.split())  # one line: the blank line between paragraphs becomes a space
    at = flat.index("贪吃蛇")
    assert hits[1]["snippet"] == "…" + flat[at - 24:at + 3 + 72] + "…"
    assert await sessions.search_sessions("   ", user_id=owner, workspace_id=workspace) == []
    route = await routes.search_sessions(q="15 路", current_user={"user_id": owner, "workspace_id": workspace})
    assert [hit["session_id"] for hit in route] == [other.id]


async def test_only_what_the_chat_shows_and_what_the_sidebar_lists():
    owner, member, workspace = await accounts()
    chat = await sessions.create_session(user_id=owner, workspace_id=workspace, title="对话")
    await create_user_message(chat.id, "系统提醒：暗号是蓝莓", synthetic=True, user_id=owner)
    await create_user_message(chat.id, "帮我看看蓝莓的报价", synthetic=True, origin="assistant_delegation", user_id=owner)
    cron = await sessions.create_session(user_id=owner, workspace_id=workspace, title="定时运行", kind="cron",
                                         parent_id=chat.id)
    await said(cron.id, owner, "蓝莓日报")
    child = await sessions.create_session(user_id=owner, workspace_id=workspace, title="子任务", parent_id=chat.id)
    await said(child.id, owner, "蓝莓子任务")
    gone = await sessions.create_session(user_id=owner, workspace_id=workspace, title="蓝莓旧对话")
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == gone.id).values(is_deleted=True))
    hits = await sessions.search_sessions("蓝莓", user_id=owner, workspace_id=workspace)
    assert [(hit["session_id"], hit["snippet"]) for hit in hits] == [(chat.id, "帮我看看蓝莓的报价")]

    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    await said(main.id, owner, "提醒我周五交房租", agent="assistant")
    private = await sessions.create_session(user_id=owner, workspace_id=workspace, title="房租私事",
                                           visibility="private")
    shared = await sessions.create_session(user_id=owner, workspace_id=workspace, title="房租分摊")
    mine = await sessions.search_sessions("房租", user_id=owner, workspace_id=workspace)
    assert {hit["session_id"]: hit["kind"] for hit in mine} == {private.id: "normal", shared.id: "normal",
                                                               main.id: "assistant"}
    theirs = await sessions.search_sessions("房租", user_id=member, workspace_id=workspace)
    assert [hit["session_id"] for hit in theirs] == [shared.id]  # never another person's assistant or private chat


async def test_like_wildcards_are_plain_characters():
    owner, _, workspace = await accounts()
    chat = await sessions.create_session(user_id=owner, workspace_id=workspace, title="转化率")
    await said(chat.id, owner, "转化率涨了 5%，file_name 也改了")
    await said((await sessions.create_session(user_id=owner, workspace_id=workspace, title="别的")).id, owner, "filexname")
    assert [hit["session_id"] for hit in await sessions.search_sessions("5%", user_id=owner, workspace_id=workspace)] == [chat.id]
    assert [hit["session_id"] for hit in await sessions.search_sessions("file_name", user_id=owner,
                                                                          workspace_id=workspace)] == [chat.id]
    assert [hit["session_id"] for hit in await sessions.search_sessions("%", user_id=owner, workspace_id=workspace)] == [chat.id]
    assert await sessions.search_sessions("_x", user_id=owner, workspace_id=workspace) == []


def test_the_search_route_comes_before_the_session_id_route():
    order = [route.path for route in routes.router.routes]
    assert order.index("/session/search") < order.index("/session/{session_id}")


async def test_snippets_are_plain_text():
    owner, _, workspace = await accounts()
    chat = await sessions.create_session(user_id=owner, workspace_id=workspace, title="界面讨论")
    await said(chat.id, owner, "给个暗色方案", "### 核心配色表\n| 元素 | 颜色 |\n| :--- | :--- |\n| 背景 | **深岩灰** `#0f172a` |\n"
                                           "---\n- **整体配色**换成深岩灰\n> 先别改文件")
    [hit] = await sessions.search_sessions("深岩灰", user_id=owner, workspace_id=workspace)
    assert hit["snippet"] == "核心配色表 元素 颜色 背景 深岩灰 #0f172a 整体配色换成深岩灰 先别改文件"
