"""V2 P5: the daily briefing in the one assistant conversation.

On the user's request the assistant sends a briefing every day at a local
time: one platform input per local day, a turn that may only read, an answer
that stands as its own block in the conversation.
"""
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent import loop, processor
from agent.driver import reserve_run
from assistant import briefing
from assistant.service import ensure_main_session
from core.config import get_config
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant_briefing import AssistantBriefing
from memory import service as memories
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, assistant, finish_turn, main_turn, tool_call
from tool.assistant_tools import assistant_tools

#: 2026-10-06 09:00 in Shanghai.
MORNING = datetime(2026, 10, 6, 1, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-briefing-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def briefing_inputs(main):
    async with get_db_session() as db:
        return [item for item in (await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == main.id))).all() if briefing.is_briefing(item.origin_ref)]


async def configure(owner, workspace, main, **arguments):
    ctx, lease, _ = await main_turn(owner, workspace, main, "以后每天早上八点半给我一份简报。")
    try:
        value, _ = await tool_call(ctx, "briefing.configure", arguments)
        await finish_turn(ctx, lease, "好的。")
    finally:
        await lease.release(session_status="idle")
    return value


async def test_the_briefing_is_set_on_request_and_queued_once_a_local_day():
    owner, _, workspace, main = await assistant()
    bad = await configure(owner, workspace, main, enabled=True, time_zone="Mars/Olympus")
    assert bad["error"] == "ASSISTANT_BRIEFING_INVALID"
    saved = await configure(owner, workspace, main, enabled=True, time="08:30", time_zone="Asia/Shanghai")
    assert saved.get("state") == "saved" and saved["enabled"] and saved["time"] == "08:30", saved
    async with get_db_session() as db:
        row = await db.scalar(select(AssistantBriefing).where(AssistantBriefing.user_id == owner))
        row.last_sent_on = None  # as if it was turned on yesterday
    early = MORNING.replace(hour=0, minute=20)  # 08:20 in Shanghai
    assert await briefing.dispatch_due_briefings(now=early) == 0
    assert await briefing.dispatch_due_briefings(now=MORNING) == 1
    assert await briefing.dispatch_due_briefings(now=MORNING) == 0  # once a local day
    [item] = await briefing_inputs(main)
    assert item.origin == "system_recovery" and item.prompt == briefing.PROMPT
    assert item.origin_ref["date"] == "2026-10-06"
    # The next run takes the briefing input first: that turn may only read.
    refused = await configure(owner, workspace, main, enabled=False)
    assert refused["error"] == "ASSISTANT_TOOL_FORBIDDEN"
    off = await configure(owner, workspace, main, enabled=False)
    assert off["enabled"] is False
    assert await briefing.dispatch_due_briefings(now=MORNING.replace(day=7)) == 0


async def test_turning_it_on_after_todays_time_does_not_send_one_right_away():
    owner, _, workspace, main = await assistant()
    late = datetime.now(timezone.utc)
    await configure(owner, workspace, main, enabled=True, time="00:00", time_zone="UTC")
    assert await briefing.dispatch_due_briefings(now=late) == 0
    assert await briefing_inputs(main) == []


async def test_the_briefing_reads_what_finished_waits_and_was_remembered():
    from tests.unit.test_assistant_results import result_ready
    from tests.unit.test_assistant_requests_v2 import ask_in
    from tests.unit.test_assistant_sessions_v2 import conversation
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    waiting = await conversation(owner, workspace, main, "配色讨论", visibility="private")
    await ask_in(waiting, owner)
    await memories.create_note(user_id=owner, workspace_id=workspace, summary="用户喜欢表格 BRIEFNOTE",
                               request_id="briefing-note")
    facts = await briefing.facts(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert facts["finished"] and facts["finished"][0]["outcome"] == "succeeded"
    assert "Browser verification is still untested" in facts["finished"][0]["summary"]
    assert facts["waiting_for_user"][0]["conversation"] == "配色讨论"
    assert facts["waiting_for_user"][0]["link"] == f"/app/s/{waiting.id}"
    assert any("BRIEFNOTE" in item["summary"] for item in facts["remembered"])


async def test_a_briefing_turn_only_reads_and_says_so(monkeypatch):
    owner, _, workspace = await accounts()
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    async with get_db_session() as db:
        db.add(AssistantBriefing(id="briefing-1", user_id=owner, workspace_id=workspace, enabled=True,
            local_time="08:30", time_zone="Asia/Shanghai", revision=1, created_at=MORNING, updated_at=MORNING))
    assert await briefing.dispatch_due_briefings(now=MORNING) == 1
    seen = []

    async def stream(**kwargs):
        seen.append({"tools": sorted(tool.id for tool in kwargs["tools"].values()),
                     "system": json.dumps(kwargs["system"], ensure_ascii=False)})
        yield {"type": "text_delta", "text": "今日简报：昨天没有新结果。"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    [request] = seen
    assert set(request["tools"]) <= briefing.BRIEFING_TOOLS and "status.briefing" in request["tools"]
    assert not {"tasks.submit", "tasks.followup", "requests.answer", "memory.remember"} & set(request["tools"])
    assert "daily briefing" in request["system"]
    [item] = await briefing_inputs(main)
    assert (item.state, item.outcome) == ("settled", "succeeded")
