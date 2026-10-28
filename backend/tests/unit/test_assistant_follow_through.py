"""A reply that promises later work nothing will do gets one more step (assistant/follow_through.py).

Measured 2026-10-08: after six failed ``schedules.create`` calls the assistant
answered "你先别急，我这就重试，好了第一时间告诉你" and its turn ended; nothing
retried. The failures themselves were a JSON object sent as a string
(tool/argument_repair.py).
"""
import pytest
import asyncio
import json
import re
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from agent import processor
from assistant import follow_through
from tests.unit.assistant_legacy_cards import legacy_send_cards
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.part import Part
from tests.unit.test_assistant_budget import run, runtime
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool import argument_repair

PROMISE = "定时任务没建起来，参数格式没通过。你先别急，我这就重试，好了第一时间告诉你。"
HONEST = "试了另一种写法还是没通过，「测试」项目里还没有这个定时任务；你想换成每小时一次吗？"


def scripted(replies, seen):
    async def stream(**kwargs):
        seen.append(kwargs["messages"][-1])
        yield {"type": "text_delta", "text": replies[min(len(seen), len(replies)) - 1]}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    return stream


async def result_text(main, input_id):
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, input_id)
        parts = (await db.scalars(select(Part).where(Part.message_id == item.result_message_id,
                                                     Part.type == "text"))).all()
    return item, "".join(part.data.get("text") or "" for part in parts)


async def test_confirmed_revision_runs_once_through_real_loop_without_history_lookup(monkeypatch):
    from core.config import get_config
    from db.models.question import QuestionCheckpoint
    from question import question as q
    from question.continuation import apply_answers, resume_main_through_inbox
    from assistant.confirmations import CONFIRM
    owner, main, _, target, binding = await revision_input(monkeypatch, shared=True)
    saved, seen = {}, []
    phase = "prepare"
    async def stream(**kwargs):
        text = json.dumps(kwargs["messages"], ensure_ascii=False)
        visible = {tool.id for tool in kwargs["tools"].values()}
        seen.append((phase, visible))
        wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "requests.revise")
        if phase == "prepare":
            human = re.findall(r"Original human message_id=([^\]]+)", text)[-1]
            saved.update(request_id=binding["request_id"], version=binding["version"],
                instructions="Use the selected photo in a new script. Do not generate media.",
                attachment_ids=[], source_message_ids=[human])
        else:
            assert visible == {"requests.revise"}
            resumed_calls = sum(value[0] == "resume" for value in seen)
            if resumed_calls == 1:
                assert "current answer" in text and saved["request_id"] in text
                yield {"type": "text_delta", "text": "I cannot find which card you answered."}
                yield {"type": "finish", "reason": "stop", "usage": {}}
                return
            if resumed_calls == 2:
                assert "exact saved arguments" in text
            else:
                yield {"type": "text_delta", "text": "The revision was accepted by the original task."}
                yield {"type": "finish", "reason": "stop", "usage": {}}
                return
        yield {"type": "tool_call", "tool": wire, "args": dict(saved), "call_id": f"revise-{len(seen)}", "invalid": False}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    with legacy_send_cards():
        await run(main, owner)
    async with get_db_session() as db:
        card = await db.scalar(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.status == "pending"))
        assert card is not None
    await q.reply(card.id, [[CONFIRM]], user_id=owner)
    generation = await apply_answers(main.id, owner)
    assert await resume_main_through_inbox(main.id, owner, generation)
    phase = "resume"
    config = get_config()
    monkeypatch.setattr(config.memory, "semantic_recall", True)
    monkeypatch.setattr(config.memory, "retrieval_v2", True)
    monkeypatch.setattr(config.memory, "allowed_user_ids", [])
    async def no_router(*_, **__):
        pytest.fail("Card continuation must not classify empty synthetic input")
    monkeypatch.setattr("memory.read_routing.route", no_router)
    await run(main, owner)
    assert [p for p, _ in seen] == ["prepare", "resume", "resume", "resume"]
    async with get_db_session() as db:
        original = await db.get(QuestionCheckpoint, binding["request_id"])
        assert original.status == "superseded" and original.answers is None
        delegated = list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == target.id, AgentInboxItem.origin == "assistant_delegation"))).all())
        assert len(delegated) == 1 and delegated[0].prompt == saved["instructions"]
        cards = list((await db.scalars(select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == main.id))).all())
        assert len(cards) == 1 and cards[0].continuation["assistant_send"]["consumed"]


async def test_a_reply_promising_a_retry_gets_one_more_step_with_a_reminder(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, model_requests=6)
    seen = []
    monkeypatch.setattr(processor, "stream_llm", scripted([PROMISE, HONEST], seen))
    await run(main, owner)
    assert len(seen) == 2
    assert seen[1] == {"role": "user", "content": follow_through.REMINDER}  # right after the promise
    item, text = await result_text(main, first["inbox_id"])
    assert item.outcome == "succeeded" and text == HONEST  # the voice front desk reads this one


async def test_the_reminder_comes_once_and_work_started_in_the_turn_keeps_its_promise(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, model_requests=6)
    seen = []
    monkeypatch.setattr(processor, "stream_llm", scripted([PROMISE, PROMISE], seen))
    await run(main, owner)
    assert len(seen) == 2  # it promised again: the turn ends, no loop
    _, owner2, _, main2, first2 = await runtime(monkeypatch, model_requests=6)
    seen2 = []

    async def started(*args):
        return True  # e.g. tasks.submit handed it to a project: its report comes back by itself
    monkeypatch.setattr(follow_through, "started_background_work", started)
    monkeypatch.setattr(processor, "stream_llm", scripted(["已经交给「测试」项目去做了，有结果我第一时间告诉你。"], seen2))
    await run(main2, owner2)
    assert len(seen2) == 1


async def test_no_reminder_near_the_request_limit(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, model_requests=2)
    seen = []
    monkeypatch.setattr(processor, "stream_llm", scripted([PROMISE, HONEST], seen))
    await run(main, owner)
    assert len(seen) == 1  # one more request would exhaust the turn: the reply stands


async def revision_input(monkeypatch, *, shared=False):
    from tests.unit.test_assistant_sessions_v2 import conversation
    from tests.unit.test_assistant_requests_v2 import ask_in
    from assistant.requests import question_version
    from db.models.question import QuestionCheckpoint
    _, owner, workspace, main, first = await runtime(monkeypatch, model_requests=6)
    session = await conversation(owner, workspace, main, "Autumn fruit video",
        visibility="workspace" if shared else "private")
    question, _ = await ask_in(session, owner)
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, question)
        binding = {"request_id": question, "version": question_version(row), "session_id": session.id}
        item = await db.get(AgentInboxItem, first["inbox_id"])
        item.prompt = "Change that autumn video to use the store photo. Show the script before generating anything."
        item.origin_ref = {**item.origin_ref, "entrypoint": "assistant_voice",
                           "voice_context": {"revision_request": binding, "heard": item.prompt}}
    return owner, main, first, session, binding


@pytest.mark.parametrize("shared", [False, True])
async def test_bound_revision_without_attempt_gets_one_step_and_preserves_consent(monkeypatch, shared):
    from db.models.question import QuestionCheckpoint
    owner, main, first, target, binding = await revision_input(monkeypatch, shared=shared)
    seen = []
    async def stream(**kwargs):
        seen.append(kwargs["messages"][-1])
        if len(seen) == 1:
            assert {tool.id for tool in kwargs["tools"].values()} == {
                "requests.get", "requests.revise", "assets.list", "history.read"}
            # No known promise phrase: an ordinary draft cannot satisfy this action.
            yield {"type": "text_delta", "text": "Here is a new draft. Say yes and I will replace the old one."}
        elif len(seen) == 2:
            assert "has not attempted requests.revise" in seen[-1]["content"]
            human = re.findall(r"Original human message_id=([^\]]+)", json.dumps(kwargs["messages"]))[-1]
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "requests.revise")
            yield {"type": "tool_call", "tool": wire, "args": {
                "request_id": binding["request_id"], "version": binding["version"],
                "instructions": "Prepare a new script using the selected store photo; do not generate media.",
                "attachment_ids": [], "source_message_ids": [human]}, "call_id": "revision", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
            return
        else:
            yield {"type": "text_delta", "text": "The planning change was submitted to the original task."}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, binding["request_id"])
        delegated = list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == target.id, AgentInboxItem.origin == "assistant_delegation"))).all())
        cards = list((await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.status == "pending"))).all())
    assert len(seen) == 3 and not cards
    assert row.status == "superseded" and row.answers is None and len(delegated) == 1
    assert "do not generate media" in delegated[0].prompt


@pytest.mark.parametrize("condition", ["changed", "expired", "foreign", "different_run", "failed_attempt"])
async def test_revision_guard_does_not_revive_changed_or_unavailable_work(monkeypatch, condition):
    from datetime import timedelta
    from db.models.question import QuestionCheckpoint
    from db.models.part import Part
    from agent.driver import reserve_run
    from agent.inbox import claim_inbox_boundary
    from types import SimpleNamespace
    from question.runtime import now
    owner, main, first, target, binding = await revision_input(monkeypatch)
    lease = await reserve_run(main.id, owner)
    try:
        await claim_inbox_boundary(lease, step=1, include_next_turn=True)
        ctx = SimpleNamespace(session_id=main.id, user_id=owner, workspace_id=main.workspace_id,
                              run_id=lease.run_id, run_generation=lease.generation)
        ids = []
        assert await follow_through.unfinished_revision(ctx, ids)
        async with get_db_session() as db:
            row = await db.get(QuestionCheckpoint, binding["request_id"])
            if condition == "changed": row.status = "answered"
            if condition == "expired": row.expires_at = now() - timedelta(seconds=1)
            if condition == "foreign": row.user_id = "different-owner"
            if condition == "different_run": ctx.run_id = "not-current"
            if condition == "failed_attempt":
                from session.session import create_assistant_message, save_part
                from models.message import ToolPartData, ToolStatus
        if condition == "failed_attempt":
            message = await create_assistant_message(main.id, None, agent="assistant", model_id=main.model,
                user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
            await save_part(ToolPartData(session_id=main.id, message_id=message.id,
                tool="requests.revise", status=ToolStatus.ERROR, input={}, error="Unknown outcome"),
                is_new=True, user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
            ids.append(message.id)
        assert await follow_through.unfinished_revision(ctx, ids) is None
    finally:
        await lease.release(session_status="idle")


async def test_corrective_step_keeps_the_original_deadline_watcher(monkeypatch):
    from tests.unit.test_assistant_budget import assert_failed
    _, owner, _, main, first = await runtime(monkeypatch, model_requests=6, wall_time_seconds=2)
    seen = []
    closed = asyncio.Event()
    async def stream(**kwargs):
        seen.append(kwargs["messages"][-1])
        if len(seen) == 1:
            yield {"type": "text_delta", "text": PROMISE}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        else:
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    assert len(seen) == 2 and closed.is_set()
    await assert_failed(main, owner, first["inbox_id"])


async def test_revision_scope_cannot_inspect_media_create_tasks_or_change_target(monkeypatch):
    from agent.driver import reserve_run
    from agent.inbox import claim_inbox_boundary
    from assistant.runtime import runtime_view, _authorize_tool_scope
    from assistant.policy import AssistantError
    owner, main, _, _, binding = await revision_input(monkeypatch)
    lease = await reserve_run(main.id, owner)
    try:
        await claim_inbox_boundary(lease, step=1, include_next_turn=True)
        view = await runtime_view(session_id=main.id, user_id=owner,
                                  run_id=lease.run_id, generation=lease.generation)
        assert view["revision_target"] == binding
        for tool, args in [("assets.describe", {}), ("tasks.submit", {}), ("requests.list", {}),
                           ("requests.revise", {**binding, "request_id": "other-card"}),
                           ("requests.revise", {**binding, "version": "b" * 64}),
                           ("history.read", {"session_id": "other-session"})]:
            with pytest.raises(AssistantError):
                _authorize_tool_scope(view, tool, args)
        _authorize_tool_scope(view, "requests.revise", binding)
        _authorize_tool_scope(view, "history.read", {"session_id": binding["session_id"]})
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("text, promised", [
    (PROMISE, True),
    ("我换个写法再试一次，好了马上告诉你。", True),
    ("弄好之后我会第一时间通知你。", True),
    ("I'll try again and let you know.", True),
    ("云桌面开通后告诉我，我再接着发。", False),  # asks the user, promises nothing of its own
    ("好的，每日简报关掉了。", False),
    ("定时任务建好了，每五分钟回你一句 hello。", False),
])
def test_what_counts_as_a_promise_of_later_work(text, promised):
    assert follow_through.promises_later(text) is promised


async def test_only_the_personal_assistant_and_only_without_background_work(monkeypatch):
    async def none(*args):
        return False
    monkeypatch.setattr(follow_through, "started_background_work", none)
    kwargs = dict(session_id="s", user_id="u", message_ids=["m"])
    assert await follow_through.needs_another_step(session_kind="assistant", text=PROMISE, **kwargs)
    assert not await follow_through.needs_another_step(session_kind="normal", text=PROMISE, **kwargs)
    assert not await follow_through.needs_another_step(session_kind="assistant", text=HONEST, **kwargs)

    async def broken(*args):
        raise RuntimeError("db")
    monkeypatch.setattr(follow_through, "started_background_work", broken)
    assert not await follow_through.needs_another_step(session_kind="assistant", text=PROMISE, **kwargs)


class Every(BaseModel):
    kind: str
    every_ms: int


class Create(BaseModel):
    name: str
    schedule: Every
    tags: list[str] = []


def test_a_json_object_sent_as_a_string_is_decoded_where_an_object_was_expected():
    args = {"name": "每五分钟回复hello", "schedule": '{"kind": "every", "every_ms": 300000}', "tags": '["a", "b"]'}
    value = argument_repair.validate(Create, args, tool_id="schedules.create")
    assert value.schedule == Every(kind="every", every_ms=300000) and value.tags == ["a", "b"]
    assert args["schedule"] == '{"kind": "every", "every_ms": 300000}'  # the caller's arguments are untouched
    for bad in ("every 5 minutes", '["not", "an", "object"]', '{"kind": "every"}'):
        with pytest.raises(ValidationError):
            argument_repair.validate(Create, dict(args, schedule=bad))


def test_the_real_schedule_tool_accepts_the_arguments_that_failed_six_times():
    from tool.assistant_tools import ScheduleCreateArgs
    args = {"name": "每五分钟回复hello", "schedule": '{"kind": "every", "every_ms": 300000}',
            "project_id": "01M3X58NCSAWD6WFC9Q469AADY", "instructions": "回复一句 hello。",
            "source_message_ids": ["message_01M4CXND4EP5ACX7R62TZ5DN7K"]}
    assert argument_repair.validate(ScheduleCreateArgs, args).schedule.every_ms == 300000


def test_a_union_of_objects_is_advertised_as_an_object():
    """The root cause: schedules.create's ``schedule`` was a bare oneOf, and qwen3.8-flash filled it with text."""
    from agent.llm import _simplify_schema, _tool_parameters_schema
    from tool.assistant_tools import assistant_tools
    tools = {tool.id: tool for tool in assistant_tools}
    create = _tool_parameters_schema(tools["schedules.create"])["properties"]["schedule"]
    assert create["type"] == "object" and len(create["oneOf"]) == 3
    update = _tool_parameters_schema(tools["schedules.update"])["properties"]["patch"]["properties"]["schedule"]
    assert update["type"] == "object"
    assert update["oneOf"][1]["properties"]["anchor_ms"]["type"] == "integer"  # nullable branches simplified too
    assert _simplify_schema({"anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]}) == {
        "anyOf": [{"type": "number"}, {"type": "string"}, {"type": "null"}]}  # mixed types: left alone
    assert _simplify_schema({"oneOf": [{"type": "string", "const": "a"}, {"type": "string", "const": "b"}]})["type"] == "string"


def test_gateway_repr_question_is_decoded_without_changing_strings_or_schema():
    from tool.question_tool import QuestionArgs
    question = {"question": "新的脚本可以吗？", "header": "脚本确认", "custom": True,
                "options": [{"label": "可以", "description": "只确认脚本"}]}
    args = {"questions": repr([question])}
    parsed = argument_repair.validate(QuestionArgs, args, tool_id="question")
    assert parsed.questions[0].question == question["question"]
    assert args["questions"] == repr([question])
    assert parsed.questions[0].options[0].description == "只确认脚本"
    with pytest.raises(ValidationError):
        argument_repair.validate(QuestionArgs, {"questions": "[{'header': '缺少问题'}]"})


@pytest.mark.parametrize("value", ["[open('/tmp/no-write','w')]", "[x for x in []]", "[unknown]",
    "[('tuple',)]", "[{'set'}]", "[{1: 'non-string-key'}]", "[{'a': 1, 'a': 2}]",
    "[b'bytes']", "[1e999]", "['x' * 1000000]", "[" * 40 + "'x'" + "]" * 40,
    "['" + "x" * 65536 + "']"])
def test_container_repair_rejects_code_and_non_json_or_unbounded_literals(value):
    class Values(BaseModel):
        values: list
    with pytest.raises(ValidationError):
        argument_repair.validate(Values, {"values": value})
