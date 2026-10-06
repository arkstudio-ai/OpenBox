"""V2 P3 on real SQL: the assistant's own memory writes, project briefs and per-turn memory context.

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 8 (writes on the user's own words, D5 confirmation of
sensitive facts, D1 forgetting is not retroactive) and 9 (per-user project briefs).
Retrieval is lexical only here: the embedding adapter is replaced so no provider is called.
"""
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from agent import inbox, loop, processor
from agent.agent import AgentDef
from agent.driver import reserve_run
from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
from assistant.service import ensure_main_session
from core.config import get_config
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.part import Part
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from memory import retrieval, service
from memory.orchestrator import render_memory_context, run_memory_context
from memory.policy import resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.settings import update_settings
from models.message import TextPart, ToolStatus
from question import question as q
from question.continuation import QuestionContinuationWorker
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, card, finish_turn, main_turn, tool_call
from tool.assistant_tools import assistant_tools

GOALS = "目标：做一个贪吃蛇小游戏。\n技术栈：Vue 3 + Vite。\n当前进度：已完成界面。BRIEFCANARY"
MEMORY_TOOLS = {"memory.remember", "memory.update", "memory.forget", "projects.brief.read", "projects.brief.update"}


class NoEmbedding:
    """Dense retrieval is unavailable in these tests; lexical retrieval stays real."""
    def __init__(self, *_args, **_kwargs):
        pass

    async def embed(self, _texts):
        raise MemoryProviderError("provider_not_configured")


@pytest.fixture(autouse=True)
def quiet_runtime(monkeypatch):
    monkeypatch.setattr(get_config().memory, "retrieval_v2", True)
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-memory-v2-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr(retrieval, "BailianEmbedding", NoEmbedding)
    MAIN_MESSAGES.clear()


async def owned_project(owner, workspace, name):
    project_id = f"prj-{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Project(id=project_id, user_id=owner, workspace_id=workspace, name=name, created_at=now, updated_at=now))
    return project_id


async def memory(memory_id):
    async with get_db_session() as db:
        return await db.get(UserMemory, memory_id)


async def memories_of(user_id):
    async with get_db_session() as db:
        return list((await db.scalars(select(UserMemory).where(UserMemory.user_id == user_id))).all())


async def recalled(ctx, query, **scope):
    """memory.search through the assistant's own tool: the ids of the memories found."""
    value, part = await tool_call(ctx, "memory.search", {"query": query, **scope})
    assert part.status == ToolStatus.COMPLETED, value
    return {item["id"] for item in value["items"] if item["kind"] == "memory"}


async def memory_context(owner, workspace, query, *, project_id=None, include_all_projects=False):
    """The memory_context fragment a turn would carry for this scope (route_jev off: core memories)."""
    config = get_config().memory.model_copy(update={"route_jev": False, "wiki": False, "debug_view": False})
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=owner, workspace_id=workspace, project_id=project_id,
                                           include_all_projects=include_all_projects)
    return render_memory_context(await run_memory_context(query, scope, config))


def remember(summary, quote, **extra):
    return {"summary": summary, "quote": quote, **extra}


# 1. memory.remember ---------------------------------------------------------

async def test_remember_needs_a_quote_of_the_users_own_message_in_this_conversation():
    from assistant.results import deliver_task_result
    from tests.unit.test_assistant_results import result_ready
    owner, workspace, main, accepted, execution, _ = await result_ready()
    await execution.release(session_status="idle")
    from db.models.assistant import TaskResult
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    await deliver_task_result(result_id)
    report = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(report, step=1, include_next_turn=True)
        fence = (main.id, report.run_id, report.generation)
        answer = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=fence)
        await save_part(TextPart(session_id=main.id, message_id=answer.id, text="ASSISTANT_SAID 我喜欢深色主题"),
                        is_new=True, user_id=owner, run_fence=fence)
        answer.finish = "stop"
        await update_message_info(answer, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(report, result_message_id=answer.id, outcome="succeeded")
    finally:
        await report.release(session_status="idle")
    ctx, lease, _ = await main_turn(owner, workspace, main, "请记住：\n我喜欢简洁的回答。")
    try:
        for quote in ("Browser verification is still untested",  # the task_result input of the report turn
                      "ASSISTANT_SAID 我喜欢深色主题",            # the assistant's own answer
                      "Create a report",                          # the user's words in another session
                      "我从来没说过这句话",
                      "我"):                                      # a word or two is not the user's request
            refused, part = await tool_call(ctx, "memory.remember", remember("用户喜欢深色主题", quote))
            assert part.status == ToolStatus.ERROR and refused["error"] == "ASSISTANT_MEMORY_SOURCE", quote
        assert await memories_of(owner) == []
        # Whitespace is normalized on both sides: the line break in the message matches a space.
        saved, part = await tool_call(ctx, "memory.remember", remember("用户喜欢简洁的回答", "  请记住： 我喜欢简洁的回答 "))
        assert part.status == ToolStatus.COMPLETED and saved["state"] == "remembered"
    finally:
        await lease.release(session_status="idle")


async def test_ordinary_preference_is_a_confirmed_personal_memory_recalled_from_any_project():
    owner, _, workspace, main = await assistant()
    project_a = await owned_project(owner, workspace, "Project A")
    project_b = await owned_project(owner, workspace, "Project B")
    ctx, lease, _ = await main_turn(owner, workspace, main, "以后回答我都用表格 TABLEPREF，记住这一点。")
    try:
        saved, part = await tool_call(ctx, "memory.remember",
                                      remember("用户希望回答都用表格 TABLEPREF", "以后回答我都用表格 TABLEPREF"))
        assert saved["state"] == "remembered" and saved["scope"] == "personal" and saved["project_id"] is None
        row = await memory(saved["memory_id"])
        assert (row.status, row.confirmation_status, row.owner, row.project_id) == (
            "ACTIVE", "CONFIRMED", "USER_CONFIRMED", None)
        assert row.value["summary"] == "用户希望回答都用表格 TABLEPREF"
        for scope in ({}, {"project_id": project_a}, {"project_id": project_b}, {"include_all_projects": True}):
            assert saved["memory_id"] in await recalled(ctx, "TABLEPREF 表格", **scope), scope
    finally:
        await lease.release(session_status="idle")
    # An ordinary conversation in project B starts with this personal fact in its memory context.
    assert "TABLEPREF" in await memory_context(owner, workspace, "帮我写一份周报", project_id=project_b)


async def test_project_memory_stays_inside_its_project():
    owner, other, workspace, main = await assistant()
    project_a = await owned_project(owner, workspace, "Snake")
    project_b = await owned_project(owner, workspace, "Blog")
    foreign = await owned_project(other, workspace, "Member project")
    ctx, lease, _ = await main_turn(owner, workspace, main, "贪吃蛇项目统一用 Vue 3 SNAKESTACK。")
    try:
        saved, _ = await tool_call(ctx, "memory.remember", remember(
            "贪吃蛇项目使用 Vue 3 SNAKESTACK", "贪吃蛇项目统一用 Vue 3 SNAKESTACK", project_id=project_a))
        assert saved["state"] == "remembered" and (saved["scope"], saved["project_id"]) == ("project", project_a)
        assert (await memory(saved["memory_id"])).project_id == project_a
        assert saved["memory_id"] in await recalled(ctx, "SNAKESTACK Vue", project_id=project_a)
        assert saved["memory_id"] not in await recalled(ctx, "SNAKESTACK Vue", project_id=project_b)
        assert saved["memory_id"] not in await recalled(ctx, "SNAKESTACK Vue")
        refused, part = await tool_call(ctx, "memory.remember", remember(
            "成员项目使用 Vue 3", "贪吃蛇项目统一用 Vue 3 SNAKESTACK", project_id=foreign))
        assert part.status == ToolStatus.ERROR and refused["error"] == "ASSISTANT_PROJECT_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")
    assert "SNAKESTACK" not in await memory_context(owner, workspace, "写点什么", project_id=project_b)
    assert len(await memories_of(owner)) == 1


async def test_fact_key_keeps_one_fact_per_key():
    """The same key and summary returns the same memory; a different summary must use memory.update."""
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "我的回复格式偏好是表格；不对，改成列表。")
    try:
        args = remember("用户偏好用表格回复", "我的回复格式偏好是表格", fact_key="personal.reply_format")
        first, _ = await tool_call(ctx, "memory.remember", args)
        again, _ = await tool_call(ctx, "memory.remember", args)
        assert first["state"] == again["state"] == "remembered" and again["memory_id"] == first["memory_id"]
        changed, part = await tool_call(ctx, "memory.remember", {**args, "summary": "用户偏好用列表回复",
                                                                  "quote": "不对，改成列表"})
        assert part.status == ToolStatus.ERROR and changed["error"] == "ASSISTANT_MEMORY_CONFLICT"
    finally:
        await lease.release(session_status="idle")
    rows = await memories_of(owner)
    assert [(row.id, row.fact_key, row.value["summary"]) for row in rows] == [
        (first["memory_id"], "personal.reply_format", "用户偏好用表格回复")]


@pytest.mark.parametrize("summary", ["用户的手机号是13812345678", "用户的工资卡号是6222021234567890123"],
                         ids=["phone", "card-with-soft-keyword"])
async def test_hard_sensitive_content_is_refused_and_nothing_is_stored(summary):
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "记住我的信息：" + summary)
    try:
        refused, part = await tool_call(ctx, "memory.remember", remember(summary, summary))
    finally:
        await lease.release(session_status="idle")
    assert part.status == ToolStatus.COMPLETED and refused["state"] == "refused" and refused["reason"]
    assert await memories_of(owner) == []
    async with get_db_session() as db:
        saved = await db.get(Part, part.id)
    assert saved.data["metadata"]["assistant_memory"] == {"state": "refused"}


async def answer_card(main, owner, request_id, label):
    """The user answers on the card; the real continuation worker applies it and resumes the main session."""
    await q.reply(request_id, [[label]], owner)
    await QuestionContinuationWorker().tick()
    async with get_db_session() as db:
        execution = await db.get(SessionExecution, main.id)
        assert execution.resume_pending is False
        resumed = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.origin == "system_recovery"))).all())
    assert [item.origin_ref["entrypoint"] for item in resumed] == ["question_answer"]


@pytest.mark.parametrize("label,status", [("记住", ("ACTIVE", "CONFIRMED")), ("不用记", ("DEPRECATED", "REJECTED"))])
async def test_soft_sensitive_fact_waits_for_the_users_card(label, status):
    owner, _, workspace, main = await assistant()
    summary = "用户正在服药控制血压 HEALTHNOTE"
    ctx, lease, _ = await main_turn(owner, workspace, main, "请记住我正在服药控制血压 HEALTHNOTE。")
    try:
        asked, part = await tool_call(ctx, "memory.remember", remember(summary, "我正在服药控制血压 HEALTHNOTE"))
        request = await card(asked["suspended"])
        assert (request.session_id, request.status, request.part_id) == (main.id, "pending", part.id)
        assert request.continuation["kind"] == "memory_proposal" and summary in request.questions[0]["question"]
        assert [option["label"] for option in request.questions[0]["options"]] == ["记住", "不用记"]
        proposal = await memory(request.continuation["memory_id"])
        assert proposal.status == "CANDIDATE" and proposal.type == "PENDING_NOTE"
        assert proposal.id not in await recalled(ctx, "HEALTHNOTE 服药", include_all_projects=True)
    finally:
        await lease.release(session_status="idle")
    await answer_card(main, owner, request.id, label)
    row = await memory(request.continuation["memory_id"])
    assert (row.status, row.confirmation_status) == status
    async with get_db_session() as db:
        call = await db.get(Part, part.id)
    assert call.data["status"] == "completed"
    assert call.data["metadata"]["decision"] == ("confirmed" if label == "记住" else "rejected")
    assert ("HEALTHNOTE" in await memory_context(owner, workspace, "今天吃什么")) is (label == "记住")


async def test_the_turn_resumed_after_a_card_still_sees_the_cards_tool_call_and_answer(monkeypatch):
    """The answer lives in the suspended run's tool result; the resumed run must see it, or it asks again."""
    owner, _, workspace = await accounts()
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup",
        prompt="顺便记一下我在吃降压药 RESUMECARD。", agent="assistant", origin="human",
        origin_ref={"actor_user_id": owner})
    payloads = []

    async def stream(**kwargs):
        payloads.append(json.dumps(kwargs["messages"], ensure_ascii=False))
        if len(payloads) == 1:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "memory.remember")
            yield {"type": "tool_call", "tool": wire, "call_id": "remember-card", "invalid": False,
                   "args": {"summary": "用户在吃降压药 RESUMECARD", "quote": "我在吃降压药 RESUMECARD"}}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
            return
        yield {"type": "text_delta", "text": "好的，不记。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        request = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == main.id,
                                                                   QuestionCheckpoint.status == "pending"))
    assert request is not None and request.continuation["kind"] == "memory_proposal"
    await answer_card(main, owner, request.id, "不用记")
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert len(payloads) == 2
    assert "The user declined. Do not save or re-propose this memory." in payloads[1]
    assert "我在吃降压药 RESUMECARD" in payloads[1]


async def test_a_declined_fact_is_not_proposed_again_and_that_is_not_an_error():
    owner, _, workspace, main = await assistant()
    summary = "用户在还房贷 LOANNOTE"
    declined = await service.propose_note(user_id=owner, workspace_id=workspace, summary=summary)
    await service.reject_note(user_id=owner, workspace_id=workspace, proposal_id=declined["id"])
    ctx, lease, _ = await main_turn(owner, workspace, main, "记住我在还房贷 LOANNOTE。")
    try:
        again, part = await tool_call(ctx, "memory.remember", remember(summary, "记住我在还房贷 LOANNOTE"))
        assert part.status == ToolStatus.COMPLETED and again["state"] == "declined_before", again
        async with get_db_session() as db:
            pending = await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.session_id == main.id,
                QuestionCheckpoint.status == "pending"))
        assert pending is None
    finally:
        await lease.release(session_status="idle")


async def test_paused_saving_stores_nothing():
    owner, _, workspace, main = await assistant()
    await update_settings(owner, auto_save=False)
    ctx, lease, _ = await main_turn(owner, workspace, main, "以后回答我都用表格。")
    try:
        paused, part = await tool_call(ctx, "memory.remember", remember("用户希望回答都用表格", "以后回答我都用表格"))
        sensitive, _ = await tool_call(ctx, "memory.remember", remember("用户在服药", "以后回答我都用表格",
                                                                       sensitive=True))
    finally:
        await lease.release(session_status="idle")
    assert paused["state"] == sensitive["state"] == "paused"
    assert await memories_of(owner) == []


# 2. memory.update / memory.forget --------------------------------------------

async def test_update_and_forget_need_the_users_words_and_forgetting_is_not_retroactive():
    from agent.loop import _to_llm_messages
    from assistant.projection import project_main_messages
    from session.agent_event_log import load_canonical_model_surface
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "以后回答都用表格 FORMATPREF。")
    try:
        saved, remember_part = await tool_call(ctx, "memory.remember",
            remember("用户希望回答都用表格 FORMATPREF", "以后回答都用表格 FORMATPREF"))
        await finish_turn(ctx, lease, "好的，已记住。")
    finally:
        await lease.release(session_status="idle")
    memory_id = saved["memory_id"]
    assert "FORMATPREF" in await memory_context(owner, workspace, "写一份周报")
    ctx, lease, _ = await main_turn(owner, workspace, main, "不对，以后用列表 FORMATPREF，然后忘掉这条。")
    try:
        denied, part = await tool_call(ctx, "memory.update", {"memory_id": memory_id, "summary": "用户希望用列表",
                                                              "quote": "这句话不是用户说的"})
        assert part.status == ToolStatus.ERROR and denied["error"] == "ASSISTANT_MEMORY_SOURCE"
        updated, _ = await tool_call(ctx, "memory.update", {"memory_id": memory_id, "expected_revision": 1,
            "summary": "用户希望回答都用列表 FORMATPREF", "quote": "不对，以后用列表 FORMATPREF"})
        assert updated == {"state": "updated", "memory_id": memory_id,
                           "summary": "用户希望回答都用列表 FORMATPREF", "revision": 2}
        stale, part = await tool_call(ctx, "memory.update", {"memory_id": memory_id, "expected_revision": 1,
            "summary": "用户希望回答都用段落 FORMATPREF", "quote": "不对，以后用列表 FORMATPREF"})
        assert part.status == ToolStatus.ERROR and stale["error"] == "ASSISTANT_MEMORY_CONFLICT"
        denied, part = await tool_call(ctx, "memory.forget", {"memory_id": memory_id, "quote": "请忘掉所有事"})
        assert part.status == ToolStatus.ERROR and denied["error"] == "ASSISTANT_MEMORY_SOURCE"
        assert memory_id in await recalled(ctx, "FORMATPREF 列表")
        forgotten, _ = await tool_call(ctx, "memory.forget", {"memory_id": memory_id, "quote": "然后忘掉这条"})
        assert forgotten["state"] == "forgotten" and forgotten["memory_id"] == memory_id
        assert memory_id not in await recalled(ctx, "FORMATPREF 列表", include_all_projects=True)
        await finish_turn(ctx, lease, "已经忘掉了。")
    finally:
        await lease.release(session_status="idle")
    row = await memory(memory_id)
    assert row.status == "DEPRECATED" and row.deleted_at is not None
    # The next turn's memory context no longer carries it ...
    assert "FORMATPREF" not in await memory_context(owner, workspace, "写一份周报")
    # ... while the transcript keeps what was already said (D1): earlier tool calls are not rewritten.
    async with get_db_session() as db:
        kept = await db.get(Part, remember_part.id)
    assert kept.data["status"] == "completed" and "用户希望回答都用表格 FORMATPREF" in kept.data["output"]
    ctx, lease, _ = await main_turn(owner, workspace, main, "我们继续。")
    try:
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        rendered = json.dumps(_to_llm_messages(projected, user_id=owner, assistant_projection_verified=True),
                              ensure_ascii=False)
        # Earlier answers are replayed as written; earlier runs' tool observations are not.
        assert "好的，已记住。" in rendered and "已经忘掉了。" in rendered
        assert "用户希望回答都用列表 FORMATPREF" not in rendered
    finally:
        await lease.release(session_status="idle")


async def test_update_of_a_forgotten_or_unknown_memory_is_unavailable():
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "记住我喜欢表格；然后忘掉它，再把那条记忆改成列表。")
    try:
        saved, _ = await tool_call(ctx, "memory.remember", remember("用户喜欢表格", "记住我喜欢表格"))
        await tool_call(ctx, "memory.forget", {"memory_id": saved["memory_id"], "quote": "然后忘掉它"})
        for memory_id in (saved["memory_id"], "mem_unknown"):
            value, part = await tool_call(ctx, "memory.update", {"memory_id": memory_id, "summary": "用户喜欢列表",
                                                                 "quote": "再把那条记忆改成列表"})
            assert part.status == ToolStatus.ERROR and value["error"] == "ASSISTANT_MEMORY_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")


async def test_forget_of_an_unknown_or_foreign_memory_is_not_reported_as_forgotten():
    owner, other, workspace, main = await assistant()
    theirs = await service.create_note(user_id=other, workspace_id=workspace, summary="成员的偏好", request_id="theirs")
    ctx, lease, _ = await main_turn(owner, workspace, main, "忘掉那条记忆。")
    try:
        for memory_id in ("mem_unknown", theirs["id"]):
            value, part = await tool_call(ctx, "memory.forget", {"memory_id": memory_id, "quote": "忘掉那条记忆"})
            assert (await memory(theirs["id"])).status == "ACTIVE"
            assert part.status == ToolStatus.ERROR and value["error"] == "ASSISTANT_MEMORY_UNAVAILABLE", value
    finally:
        await lease.release(session_status="idle")


# 3. projects.brief.read / projects.brief.update ------------------------------

async def test_brief_tools_create_conflict_refuse_and_inject_only_into_their_project():
    from agent.loop import _build_system_prompt
    owner, other, workspace, main = await assistant()
    snake = await owned_project(owner, workspace, "Snake")
    blog = await owned_project(owner, workspace, "Blog")
    foreign = await owned_project(other, workspace, "Member project")
    ctx, lease, _ = await main_turn(owner, workspace, main, "把贪吃蛇项目的档案更新一下。")
    try:
        empty, part = await tool_call(ctx, "projects.brief.read", {"project_id": snake})
        assert part.status == ToolStatus.COMPLETED and empty == {"project_id": snake, "brief": None,
                                                                 "untrusted_data": True}
        created, _ = await tool_call(ctx, "projects.brief.update", {"project_id": snake, "content": GOALS,
                                                                   "expected_revision": 0})
        assert (created["revision"], created["updated_by"], created["content"]) == (1, "assistant", GOALS)
        stale, part = await tool_call(ctx, "projects.brief.update", {"project_id": snake, "content": GOALS + "\n更多",
                                                                    "expected_revision": 0})
        assert part.status == ToolStatus.ERROR and stale["error"] == "ASSISTANT_BRIEF_CONFLICT"
        secret, part = await tool_call(ctx, "projects.brief.update", {"project_id": snake,
            "content": GOALS + "\n联系人手机：13812345678", "expected_revision": 1})
        assert part.status == ToolStatus.ERROR and secret["error"] == "ASSISTANT_BRIEF_SENSITIVE"
        for operation, arguments in (("projects.brief.read", {"project_id": foreign}),
                                     ("projects.brief.update", {"project_id": foreign, "content": GOALS,
                                                                "expected_revision": 0})):
            refused, part = await tool_call(ctx, operation, arguments)
            assert part.status == ToolStatus.ERROR and refused["error"] == "ASSISTANT_PROJECT_UNAVAILABLE", operation
        read, _ = await tool_call(ctx, "projects.brief.read", {"project_id": snake})
        assert read["brief"]["content"] == GOALS and read["brief"]["revision"] == 1
    finally:
        await lease.release(session_status="idle")

    async def prompt(project_id):
        return "\n".join(await _build_system_prompt(AgentDef(name="build", description=""), "test/model",
            user_id=owner, project_id=project_id, workspace_id=workspace, include_user_memory=False,
            session_kind="normal"))
    assert "<project_brief>" in await prompt(snake) and "BRIEFCANARY" in await prompt(snake)
    assert "BRIEFCANARY" not in await prompt(blog) and "<project_brief>" not in await prompt(blog)


async def test_brief_read_is_used_only_by_its_run_while_the_memory_receipt_is_not_a_read():
    from agent.loop import _to_llm_messages
    from assistant.projection import project_main_messages
    from project.brief import update_brief
    from session.agent_event_log import load_canonical_model_surface
    owner, _, workspace, main = await assistant()
    snake = await owned_project(owner, workspace, "Snake")
    await update_brief(user_id=owner, workspace_id=workspace, project_id=snake, content=GOALS,
                       expected_revision=0, updated_by="user")
    ctx, lease, _ = await main_turn(owner, workspace, main, "看看贪吃蛇的档案；以后都用表格。")
    try:
        read, _ = await tool_call(ctx, "projects.brief.read", {"project_id": snake})
        assert "BRIEFCANARY" in read["brief"]["content"]
        await tool_call(ctx, "memory.remember", remember("用户希望用表格 RECEIPTSUMMARY", "以后都用表格"))
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=ctx.run_fence)
        current = json.dumps(_to_llm_messages(await project_main_messages(list(surface.messages), ctx=ctx),
                             user_id=owner, assistant_projection_verified=True), ensure_ascii=False)
        assert "BRIEFCANARY" in current and "RECEIPTSUMMARY" in current  # The run that read it uses it.
        # A stored brief read replayed without the current-run marker is only a request to read again
        # (guard_assistant_read; projects.brief.read is now a read tool). The write receipt is not a read.
        unmarked = json.dumps(_to_llm_messages(list(surface.messages)), ensure_ascii=False)
        assert "BRIEFCANARY" not in unmarked and "fresh_read_required" in unmarked
        assert "RECEIPTSUMMARY" in unmarked
        await finish_turn(ctx, lease, "已读取档案并记住。")
    finally:
        await lease.release(session_status="idle")
    ctx, lease, _ = await main_turn(owner, workspace, main, "我们继续。")
    try:
        surface = await load_canonical_model_surface(main.id, user_id=owner, run_fence=ctx.run_fence)
        later = json.dumps(_to_llm_messages(await project_main_messages(list(surface.messages), ctx=ctx),
                           user_id=owner, assistant_projection_verified=True), ensure_ascii=False)
    finally:
        await lease.release(session_status="idle")
    # A later run keeps the earlier answer's text but none of that run's tool observations.
    assert "已读取档案并记住。" in later and "BRIEFCANARY" not in later and "RECEIPTSUMMARY" not in later


# 4. Report turns -------------------------------------------------------------

async def test_report_turn_offers_no_memory_or_brief_tools_and_reports_from_the_summary(monkeypatch):
    from assistant import runtime
    from assistant.results import deliver_task_result
    from db.models.assistant import TaskResult
    from tests.unit.test_assistant_results import result_ready
    assert MEMORY_TOOLS <= ASSISTANT_TOOLS and not MEMORY_TOOLS & REPORT_TOOLS
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, workspace, main, accepted, execution, _ = await result_ready()
    await execution.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    await deliver_task_result(result_id)
    seen = []

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        view = await runtime.runtime_view(session_id=ctx.session_id, user_id=ctx.user_id, run_id=ctx.run_id,
                                          generation=ctx.run_generation)
        seen.append({"mode": view["mode"], "tool_ids": set(view["tool_ids"]),
                     "offered": {tool.id for tool in kwargs["tools"].values()}, "system": "\n".join(kwargs["system"])})
        yield {"type": "text_delta", "text": "任务已完成：报告已保存，浏览器验证尚未进行。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    [turn] = seen
    assert turn["mode"] == "report_only" and turn["tool_ids"] == set(REPORT_TOOLS)
    assert turn["offered"] == set(REPORT_TOOLS) and not MEMORY_TOOLS & turn["offered"]
    assert "This turn is report_only for result_id=" + result_id in turn["system"]
    assert "The input already contains the task facts" in turn["system"]
    assert "issue separate results.read and tasks.get" not in turn["system"]
    async with get_db_session() as db:
        assert (await db.get(TaskResult, result_id)).delivery_state == "processed"


# 5. Profile injection into an assistant turn ---------------------------------

async def assistant_turn_with_memory(monkeypatch, prompt):
    """One real assistant main turn with retrieval_v2 enabled; returns the provider payloads."""
    owner, other, workspace = await accounts()
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    config.memory = config.memory.model_copy(update={"allowed_user_ids": [owner, other], "retrieval_v2": True,
        "route_jev": False, "debug_view": False, "wiki": False, "automatic_knowledge": False})
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    snake = await owned_project(owner, workspace, "Snake")
    await service.create_note(user_id=owner, workspace_id=workspace, summary="用户喜欢表格 PROFILECANARY",
                              request_id="profile")
    await service.create_note(user_id=owner, workspace_id=workspace, project_id=snake,
                              summary="贪吃蛇项目使用 Vue 3 PROJECTCANARY", request_id="project")
    await service.create_note(user_id=other, workspace_id=workspace, summary="成员喜欢长文 FOREIGNCANARY",
                              request_id="foreign")
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt=prompt,
        agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    payloads = []

    async def stream(**kwargs):
        payloads.append({"system": json.dumps(kwargs["system"], ensure_ascii=False),
                         "messages": json.dumps(kwargs["messages"], ensure_ascii=False)})
        yield {"type": "text_delta", "text": "好的。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    return payloads


async def test_assistant_turn_carries_the_users_profile_but_never_another_users_memory(monkeypatch):
    [payload] = await assistant_turn_with_memory(monkeypatch, "帮我安排一下今天的工作。")
    assert "<memory_context>" in payload["messages"] and "PROFILECANARY" in payload["messages"]
    assert "PROFILECANARY" not in payload["system"]
    assert "FOREIGNCANARY" not in payload["messages"] + payload["system"]


async def test_memories_sent_with_a_turn_count_as_used_for_recall_order(monkeypatch):
    await assistant_turn_with_memory(monkeypatch, "贪吃蛇项目用什么技术栈？")
    async with get_db_session() as db:
        rows = list((await db.scalars(select(UserMemory))).all())
    hits = {row.value["summary"].split()[-1]: row.hit_count for row in rows
            if row.value.get("summary", "").endswith("CANARY")}
    assert hits == {"PROFILECANARY": 1, "PROJECTCANARY": 1, "FOREIGNCANARY": 0}


async def test_assistant_turn_memory_context_also_covers_owned_projects(monkeypatch):
    [payload] = await assistant_turn_with_memory(monkeypatch, "贪吃蛇项目用什么技术栈？")
    assert "PROJECTCANARY" in payload["messages"]


async def test_memory_tool_receipt_is_kept_on_the_persisted_call(monkeypatch):
    """processor.PERSISTED_TOOL_METADATA_KEYS keeps assistant_memory for the "已记住 · 撤销" chip."""
    owner, _, workspace = await accounts()
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup",
        prompt="以后回答我都用表格 CHIPPREF。", agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    steps = []

    async def stream(**kwargs):
        steps.append(1)
        if len(steps) == 1:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "memory.remember")
            yield {"type": "tool_call", "tool": wire, "call_id": "remember-chip", "invalid": False,
                   "args": {"summary": "用户希望回答都用表格 CHIPPREF", "quote": "以后回答我都用表格 CHIPPREF"}}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
            return
        yield {"type": "text_delta", "text": "好的，已记住。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    [row] = await memories_of(owner)
    async with get_db_session() as db:
        call = await db.scalar(select(Part).where(Part.session_id == main.id, Part.type == "tool"))
    assert call.data["status"] == "completed"
    chip = call.data["metadata"]["assistant_memory"]
    assert {key: chip.get(key) for key in ("state", "memory_id", "summary", "scope", "project_id")} == {
        "state": "remembered", "memory_id": row.id, "summary": "用户希望回答都用表格 CHIPPREF",
        "scope": "personal", "project_id": None}
    assert chip.get("revision", row.revision) == row.revision
