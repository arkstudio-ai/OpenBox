"""A reply that promises later work nothing will do gets one more step (assistant/follow_through.py).

Measured 2026-10-08: after six failed ``schedules.create`` calls the assistant
answered "你先别急，我这就重试，好了第一时间告诉你" and its turn ended; nothing
retried. The failures themselves were a JSON object sent as a string
(tool/argument_repair.py).
"""
import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from agent import processor
from assistant import follow_through
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
