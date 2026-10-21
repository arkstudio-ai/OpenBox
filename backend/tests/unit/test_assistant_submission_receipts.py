"""ztqz incident: command acceptance is not undone by a coordinator timeout."""
import asyncio
import json
import re

from sqlalchemy import select

from agent import processor
from assistant.receipts import for_inbox
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskSubmission
from db.models.message import Message
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_budget import runtime, run
from voice.assistant_link import AssistantLink, VoiceTurnRef


async def test_submitted_command_then_timed_out_reply_keeps_one_receipt(monkeypatch):
    _, owner, workspace, main, first = await runtime(monkeypatch, wall_time_seconds=2)
    requests = 0

    async def stream(**kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.submit")
            human = re.findall(r"Original human message_id=([^\]]+)", json.dumps(kwargs["messages"]))[-1]
            yield {"type": "tool_call", "tool": wire, "args": {
                "project_id": main.project_id, "title": "御尚喜宴5段口播视频制作",
                "instructions": "重拍第1、2、4、5段，保持原有设置，避免穿模。",
                "source_message_ids": [human], "destination_confirmed": True,
                "model": main.model, "model_confirmed": True}, "call_id": "submit", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            await asyncio.Event().wait()
            yield {}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, first["inbox_id"])
        message = await db.get(Message, item.result_message_id)
        assert item.outcome == "error"  # The reply did fail; don't falsify it.
        assert "已接收并排队" in message.error["message"]
        assert "不会因此撤销" in message.error["message"]
        assert len((await db.scalars(select(TaskSubmission))).all()) == 1
    receipts = await for_inbox(main.id, owner, first["inbox_id"])
    assert len(receipts) == 1 and receipts[0]["state"] == "accepted"
    assert await for_inbox(main.id, "another-user", first["inbox_id"]) == []
    link = AssistantLink(call_id="incident-replay", user_id=owner, workspace_id=workspace,
                         main_session_id=main.id, lang="zh", turn_timeout=2)
    async def recorded(*args, **kwargs):
        pass
    monkeypatch.setattr(link, "record", recorded)
    ref = VoiceTurnRef(id="replay", provider_call_id="call", text="重新提交。",
                       transcript="重新提交。", requested=0, inbox_id=first["inbox_id"])
    reply = await link.wait(ref)
    assert reply["status"] == "ok" and reply["reply_completed"] is False
    assert "已接收并排队" in reply["speech"]
    assert reply["execution_confirmed"] is False
    assert reply["submissions"][0]["submission_id"] == receipts[0]["submission_id"]


def test_slow_model_steps_reserve_time_for_a_reply_without_raising_limit(monkeypatch):
    from types import SimpleNamespace
    from assistant import budget
    monkeypatch.setattr(budget.time, "monotonic", lambda: 1000)
    turn = budget.TurnBudget(SimpleNamespace(), "turn", "ordinary", 1070, 1070,
                             {"wall_time_seconds": 180, "model_requests": 12, "tool_calls": 24})
    assert not turn.needs_reply(3)
    turn.slowest_step_seconds = 52
    assert turn.needs_reply(3)
    assert turn.local_deadline == 1070


async def test_receipt_read_failure_preserves_budget_error_without_claiming_acceptance(monkeypatch):
    from types import SimpleNamespace
    from assistant import budget, receipts
    lease = SimpleNamespace(session_id="main", user_id="owner", run_id="run", generation=1,
                            abort=asyncio.Event())
    turn = budget.TurnBudget(lease, "turn", "ordinary", 0, 0, {})
    turn.exhaust("time")
    attempts = 0

    async def unavailable(*args):
        nonlocal attempts
        attempts += 1
        raise ConnectionError("receipt unavailable")

    monkeypatch.setattr(receipts, "for_run", unavailable)
    await turn.reconcile_error()
    await turn.reconcile_error()
    assert attempts == 1
    assert turn.error == {"code": budget.CODE, "message": budget.MESSAGES["time"]}
    assert lease.abort.is_set()
