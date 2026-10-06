"""A real loop/processor/SQL task roundtrip with only external I/O replaced."""
import json
import re
from types import SimpleNamespace

from sqlalchemy import func, select

from agent import loop, processor
from agent.driver import reserve_run
from agent.inbox import accept_inbox_item
from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
from assistant.results import deliver_task_result, on_execution_result_committed
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.session import Session
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


async def test_private_task_submission_execution_and_read_only_report_roundtrip(monkeypatch):
    monkeypatch.setattr("assistant.results.on_execution_result_committed", on_execution_result_committed)
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)
    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    sandbox_calls = []
    async def sandbox(*args, **kwargs):
        sandbox_calls.append(1)
        return SimpleNamespace()
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", sandbox)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup",
        prompt="UNRELATED_PRIVATE_HUMAN_CONTEXT. Reply briefly without creating a task.",
        agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    phase, calls, result_id, original_message_id = "ambient", [], None, None

    async def stream(**kwargs):
        nonlocal original_message_id
        ctx = kwargs["ctx"]
        tool_ids = {tool.id for tool in kwargs["tools"].values()}
        calls.append((phase, ctx.session_id, tool_ids))
        phase_count = sum(1 for name, _, _ in calls if name == phase)
        if phase == "ambient":
            yield {"type": "text_delta", "text": "UNRELATED_PRIVATE_ASSISTANT_CONTEXT"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        elif phase == "delegate" and phase_count == 1:
            assert tool_ids == ASSISTANT_TOOLS and ctx.sandbox is None
            original_message_id = re.findall(r"Original human message_id=([^\]]+)",
                json.dumps(kwargs["messages"]))[-1]
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.submit")
            yield {"type": "tool_call", "tool": wire, "args": {"project_id": main.project_id,
                "title": "Roundtrip report", "instructions": "Create a report; browser tests are unverified.",
                "source_message_ids": [original_message_id]}, "call_id": "delegate-task", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        elif phase == "delegate":
            assert '"state": "accepted"' in json.dumps(kwargs["messages"]).replace('\\"', '"')
            yield {"type": "text_delta", "text": "Your task was accepted. Execution has not yet been verified."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        elif phase == "execute":
            assert ctx.session_id != main.id
            yield {"type": "text_delta", "text": "Report saved as report.txt. Browser tests were not run; no commit was created."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        elif phase == "report" and phase_count == 1:
            assert tool_ids == REPORT_TOOLS and ctx.sandbox is None
            assert "UNRELATED_PRIVATE_" not in json.dumps(kwargs["messages"])
            # V2 7.2: the report input already carries the final reply summary.
            assert "Report saved as report.txt" in json.dumps(kwargs["messages"])
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
            yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id},
                   "call_id": "read-original-result", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            evidence = json.dumps(kwargs["messages"])
            assert "UNRELATED_PRIVATE_" not in evidence
            assert "Do not claim browser tests were run" in evidence
            assert "Report saved as report.txt" in evidence
            assert "no commit was created" in evidence
            yield {"type": "text_delta", "text": "The execution report says report.txt was saved. Browser tests were not run and no commit was created; I have not independently verified them."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            return await loop.run_loop(session_id, user_id=owner, lease=lease)
        finally:
            await lease.release(session_status="idle")

    await run(main.id)
    phase = "delegate"
    human = await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup",
        prompt="Create a report in the default project. Do not claim browser tests were run.",
        agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    await run(main.id)
    async with get_db_session() as db:
        tasks = list((await db.scalars(select(AssistantTask).where(AssistantTask.user_id == owner))).all())
        assert len(tasks) == 1
        task = tasks[0]
        execution_id = task.execution_session_id
        execution = await db.get(Session, execution_id)
        assert execution.visibility == "private" and execution.parent_id is None
        assert execution.memory_policy == "assistant_isolated"
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(AssistantCommand.actor_user_id == owner)) == 1
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == task.id)) == 1
        assert (await db.get(AgentInboxItem, human.id)).state == "settled"
    phase = "execute"
    await run(execution_id)
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task.id))
        assert result.delivery_state == "accepted" and result.outcome == "succeeded"
        assert any(ref["session_id"] == main.id and ref["message_id"] == original_message_id for ref in result.output_refs)
        result_id = result.id
    receipt = await deliver_task_result(result_id)
    phase = "report"
    await run(main.id)
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "processed" and result.processed_message_id
        assert result.report_attempt == 1
        assert (await db.get(AgentInboxItem, receipt["inbox_id"])).outcome == "succeeded"
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(AssistantCommand.actor_user_id == owner)) == 1
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task.id)) == 1
        assert result.summary == "Report saved as report.txt. Browser tests were not run; no commit was created."
        assert (await db.get(Message, result.processed_message_id)).finish == "stop"
    assert [name for name, _, _ in calls] == ["ambient", "delegate", "delegate", "execute", "report", "report"]
    async with get_db_session() as db:
        requests = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "model.requested").order_by(AgentEvent.sequence))).all())
        budgets = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.budget.request").order_by(AgentEvent.sequence))).all())
        assert len(requests) == len(budgets) == sum(name != "execute" for name, _, _ in calls)
        assert all(budget.sequence < request.sequence for budget, request in zip(budgets, requests))
    assert len(sandbox_calls) == 1  # Only the execution Session prepares a sandbox.
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert (await verify_agent_event_parity(execution_id, user_id=owner)).ok
