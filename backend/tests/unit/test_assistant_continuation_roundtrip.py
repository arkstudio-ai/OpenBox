"""Real Driver/provider boundary: one human authority, one Task, two executions."""
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import loop, processor
from agent.driver import reserve_run
from agent.inbox import accept_inbox_item
from assistant.continuation import COORDINATION_TOOLS, recover_continuations
from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
from assistant.results import on_execution_result_committed
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


async def test_retained_human_authority_coordinates_two_original_task_runs(monkeypatch):
    monkeypatch.setattr("assistant.results.on_execution_result_committed", on_execution_result_committed)
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")

    async def sandbox(*args, **kwargs):
        return SimpleNamespace()

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", sandbox)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    phase, calls, result_id = "ambient", [], None
    original_message_id = None
    authority = "Continue this same task until both arithmetic steps are complete, at most one followup."

    async def stream(**kwargs):
        nonlocal original_message_id
        ctx = kwargs["ctx"]
        tool_ids = {tool.id for tool in kwargs["tools"].values()}
        calls.append(phase)
        count = calls.count(phase)
        messages = json.dumps(kwargs["messages"], ensure_ascii=False)

        def call(tool, args):
            wire = next(name for name, value in kwargs["tools"].items() if value.id == tool)
            return {"type": "tool_call", "tool": wire, "args": args,
                    "call_id": f"{phase}-{count}", "invalid": False}

        if phase == "ambient":
            yield {"type": "text_delta", "text": "UNRELATED_COORDINATION_HISTORY"}
        elif phase == "delegate" and count == 1:
            assert tool_ids == ASSISTANT_TOOLS and ctx.sandbox is None
            original_message_id = re.findall(r"Original human message_id=([^\]]+)", messages)[-1]
            yield call("tasks.submit", {"project_id": main.project_id, "title": "Two arithmetic steps",
                "instructions": "First calculate 7 + 8; in the following execution multiply that result by 3. "
                                "Pure text only; no external effects. " + authority,
                "source_message_ids": [original_message_id],
                "continuation": {"authorization_quote": authority, "max_followups": 1}})
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
            return
        elif phase == "delegate":
            assert '"state": "accepted"' in messages.replace('\\"', '"')
            yield {"type": "text_delta", "text": "The bounded two-step task was accepted."}
        elif phase.startswith("execute"):
            assert ctx.session_id != main.id and "Pure text only" in messages
            yield {"type": "text_delta", "text": "7 + 8 = 15; multiplication remains." if phase == "execute1"
                   else "15 × 3 = 45; both arithmetic steps are complete."}
        else:
            assert "UNRELATED_COORDINATION_HISTORY" not in messages
            assert tool_ids == (REPORT_TOOLS if phase.startswith("report") else COORDINATION_TOOLS)
            if count == 1:
                yield call("results.read", {"result_id": result_id})
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert "7 + 8 = 15" in messages or "15 × 3 = 45" in messages
            if phase.startswith("coordinate") and count == 2:
                assert authority in messages and "Pure text only" in messages
                yield call("tasks.next_step", {"decision": "continue", "instructions": "Multiply 15 by 3. Pure text only."}
                           if phase == "coordinate1" else {"decision": "complete"})
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            if phase.startswith("coordinate"):
                raise AssertionError("A committed next-step receipt must end coordination without another provider call")
            yield {"type": "text_delta", "text": f"Verified {phase}."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            return await loop.run_loop(session_id, user_id=owner, lease=lease)
        finally:
            await lease.release(session_status="idle")

    for prompt in ("UNRELATED_COORDINATION_HISTORY", authority + " Pure text only; first 7 + 8, then multiply by 3."):
        await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt=prompt,
            agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
        await run(main.id)
        phase = "delegate"
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.user_id == owner))
        assert task is not None
        assert task.continuation_policy["state"] == "active"
        execution_id = task.execution_session_id
    for index in (1, 2):
        phase = f"execute{index}"
        await run(execution_id)
        async with get_db_session() as db:
            task = await db.get(AssistantTask, task.id)
            result_id = task.latest_result_id
            assert (await db.get(TaskResult, result_id)).delivery_state == "accepted"
        assert await recover_continuations() == 0  # Reporting never gains write authority.
        phase = f"report{index}"
        await run(main.id)
        async with get_db_session() as db:
            assert (await db.get(TaskResult, result_id)).delivery_state == "processed"
        assert await recover_continuations() == 1
        assert await recover_continuations() == 0
        phase = f"coordinate{index}"
        await run(main.id)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task.id)
        assert task.continuation_policy["state"] == "completed"
        assert task.continuation_policy["followups_used"] == 1
        for model, expected in ((AssistantTask, 1), (AssistantCommand, 3), (TaskSubmission, 2), (TaskResult, 2)):
            condition = model.user_id == owner if model is AssistantTask else (
                model.actor_user_id == owner if model is AssistantCommand else model.task_id == task.id)
            assert await db.scalar(select(func.count()).select_from(model).where(condition)) == expected
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.origin == "human")) == 2
        # V2 commits answers without provenance manifests and never re-validates them.
        assert await db.scalar(select(func.count()).select_from(AgentEvent).where(
            AgentEvent.session_id == main.id, AgentEvent.kind == "assistant.message.committed")) == 0
    assert await recover_continuations() == 0
    assert calls.count("coordinate1") == calls.count("coordinate2") == 2
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert (await verify_agent_event_parity(execution_id, user_id=owner)).ok
