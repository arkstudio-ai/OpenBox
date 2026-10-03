"""Real SQL/Driver/loop boundaries for durable coordination turn limits."""
import asyncio
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant import budget
from assistant.inputs import accept_turn
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from core.config import AssistantTurnLimits
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready
from tests.unit.test_agent_recovery import _expire
from tool.assistant_tools import assistant_tools


async def runtime(monkeypatch, **limits):
    config = _loop_config()
    config.compaction.auto = False
    config.permission = {"*": "allow"}
    config.assistant.ordinary = AssistantTurnLimits(**limits)
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    async def tools(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    first = await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
        client_id="first", text="Create at most one short report in the default project, then acknowledge it.")
    return config, owner, workspace, main, first


async def run(main, owner):
    lease = await reserve_run(main.id, owner)
    try:
        return await asyncio.wait_for(loop.run_loop(main.id, owner, lease=lease), timeout=12)
    finally:
        await lease.release(session_status="idle")


async def assert_failed(main, owner, input_id):
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, input_id)
        assert item.state == "settled" and item.outcome == "error"
        message = await db.get(Message, item.result_message_id)
        assert message.finish == "error" and message.error["code"] == budget.CODE
        assert (await db.get(AgentDriverState, main.id)).phase == "idle"
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert budget.current.get() is None


async def test_model_request_cap_closes_turn_and_allows_the_next_human_input(monkeypatch):
    _, owner, workspace, main, first = await runtime(monkeypatch, model_requests=2)
    second = await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
        client_id="second", text="Say queue continued, without tools.")
    calls = []
    async def stream(**kwargs):
        calls.append(1)
        if len(calls) <= 2:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.list")
            yield {"type": "tool_call", "tool": wire, "args": {}, "call_id": f"list-{len(calls)}", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "queue continued"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    assert len(calls) == 2
    await assert_failed(main, owner, first["inbox_id"])
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, second["inbox_id"])).state == "accepted"
    await run(main, owner)
    assert len(calls) == 3
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, second["inbox_id"])).outcome == "succeeded"


async def test_stalled_stream_deadline_preserves_partial_text_and_closes_provider(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, wall_time_seconds=2)
    closed = asyncio.Event()
    async def stream(**kwargs):
        try:
            yield {"type": "text_delta", "text": "Partial evidence remains."}
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    assert closed.is_set()
    await assert_failed(main, owner, first["inbox_id"])
    async with get_db_session() as db:
        parts = list((await db.scalars(select(Part).where(Part.session_id == main.id, Part.type == "text"))).all())
        assert any(p.data.get("text") == "Partial evidence remains." for p in parts)


async def test_budget_failure_has_a_safe_durable_public_receipt(monkeypatch):
    from assistant.public_history import public_messages
    from session.session import get_messages, get_session
    _, owner, _, main, first = await runtime(monkeypatch, wall_time_seconds=2)
    async def stream(**kwargs):
        yield {"type": "text_delta", "text": "UNVERIFIED_PARTIAL"}
        await asyncio.Event().wait()
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    await assert_failed(main, owner, first["inbox_id"])
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, first["inbox_id"])
        message_id = item.result_message_id
        message = await db.get(Message, message_id)
        # Neither a provider message nor a stale client cache supplies the text
        # of the public server receipt, even with the recognized error code.
        message.error = {"code": budget.CODE, "message": "PRIVATE_ERROR_DETAILS"}
    session = await get_session(main.id, user_id=owner)
    cached = await get_messages(main.id, user_id=owner)
    projected = await public_messages(session, cached, actor_user_id=owner)
    receipt = next(row for row in projected if row["id"] == message_id)
    assert receipt["source_status"] == "available" and receipt["finish"] == "error"
    assert receipt["error"] == {"code": budget.CODE, "message": budget.PUBLIC_MESSAGE}
    assert receipt["parts"] == []
    assert "UNVERIFIED_PARTIAL" not in json.dumps(projected)
    assert "PRIVATE_ERROR_DETAILS" not in json.dumps(projected)
    # An unbound/unfinished error cannot impersonate a settled budget receipt.
    async with get_db_session() as db:
        (await db.get(AgentInboxItem, first["inbox_id"])).error = {"code": "UNRELATED_ERROR"}
    projected = await public_messages(session, cached, actor_user_id=owner)
    assert next(row for row in projected if row["id"] == message_id)["source_status"] == "unavailable"
    assert budget.PUBLIC_MESSAGE not in json.dumps(projected)


async def test_deadline_during_tool_preserves_the_effect_without_repeating_it(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, wall_time_seconds=2)
    tool = next(tool for tool in assistant_tools if tool.id == "tasks.submit")
    real_execute = tool.execute
    committed, closed = asyncio.Event(), asyncio.Event()
    async def delayed_receipt(args, ctx):
        try:
            await real_execute(args, ctx)
            committed.set()
            await asyncio.Event().wait()
        finally:
            closed.set()
    monkeypatch.setattr(tool, "execute", delayed_receipt)
    async def stream(**kwargs):
        wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.submit")
        human = re.findall(r"Original human message_id=([^\]]+)", json.dumps(kwargs["messages"]))[-1]
        yield {"type": "tool_call", "tool": wire, "args": {"project_id": main.project_id,
            "title": "Committed before timeout", "instructions": "Only draft a short report.",
            "source_message_ids": [human]}, "call_id": "submit", "invalid": False}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    assert committed.is_set() and closed.is_set()
    await assert_failed(main, owner, first["inbox_id"])
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == owner)) == 1
        part = await db.scalar(select(Part).where(Part.session_id == main.id, Part.type == "tool"))
        assert part.data["status"] == "error"
        assert part.data["metadata"]["execution_outcome"] == "unknown"


async def test_tool_cap_preserves_one_committed_task_and_never_dispatches_a_second(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, tool_calls=1)
    async def stream(**kwargs):
        wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.submit")
        human = re.findall(r"Original human message_id=([^\]]+)", json.dumps(kwargs["messages"]))[-1]
        for index in range(2):
            yield {"type": "tool_call", "tool": wire, "args": {"project_id": main.project_id,
                "title": f"Report {index}", "instructions": "Only draft a short report.",
                "source_message_ids": [human]}, "call_id": f"submit-{index}", "invalid": False}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    await assert_failed(main, owner, first["inbox_id"])
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == owner)) == 1
        tools = list((await db.scalars(select(Part).where(Part.session_id == main.id, Part.type == "tool")
            .order_by(Part.created_at, Part.id))).all())
        assert [p.data["status"] for p in tools] == ["completed", "error"]
        assert tools[1].data["metadata"]["execution_outcome"] == "not_started"
        assert tools[1].data["metadata"]["failure_code"] == budget.CODE


async def test_budget_policy_and_spent_attempts_survive_engine_restart(monkeypatch):
    config, owner, _, main, _ = await runtime(monkeypatch, model_requests=1)
    lease = await reserve_run(main.id, owner)
    first_budget = second_budget = None
    try:
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        first_budget = await budget.start(lease)
        await first_budget.admit("request", "original-attempt")
        await first_budget.admit("request", "original-attempt")
        await first_budget.close()
        async with get_db_session() as db:
            url = db.get_bind().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        config.assistant.ordinary.model_requests = 50
        second_budget = await budget.start(lease)
        assert second_budget.deadline == first_budget.deadline
        assert second_budget.limits["model_requests"] == 1
        with pytest.raises(budget.AssistantBudgetExceeded):
            await second_budget.admit("request", "new-attempt")
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == main.id,
                AgentEvent.kind == "assistant.budget.request")) == 1
    finally:
        if first_budget: await first_budget.close()
        if second_budget: await second_budget.close()
        await lease.release(session_status="idle")


async def test_exact_recovery_generation_does_not_reset_the_original_budget(monkeypatch):
    from agent.driver import recover_expired_driver_records, reserve_recovered_run
    _, owner, _, main, _ = await runtime(monkeypatch, model_requests=1)
    old = await reserve_run(main.id, owner)
    replacement = first_budget = restored = None
    try:
        await inbox.claim_inbox_boundary(old, step=1, include_next_turn=True)
        first_budget = await budget.start(old)
        await first_budget.admit("request", "accepted-before-crash")
        await first_budget.close()
        await _expire(old)
        record = next(r for r in await recover_expired_driver_records() if r.session_id == main.id)
        replacement = await reserve_recovered_run(record, initial_phase="reserved")
        assert replacement.generation > old.generation
        assert await inbox.rebind_recovered_claims(record, replacement) == 1
        restored = await budget.start(replacement)
        assert restored.turn_id == first_budget.turn_id and restored.deadline == first_budget.deadline
        with pytest.raises(budget.AssistantBudgetExceeded):
            await restored.admit("request", "after-recovery")
    finally:
        if restored: await restored.close()
        if first_budget: await first_budget.close()
        if replacement: await replacement.release(session_status="idle")
        await old.release(session_status="idle")


async def test_deadline_interrupts_retry_after_without_starting_another_attempt(monkeypatch):
    _, owner, _, main, first = await runtime(monkeypatch, wall_time_seconds=2)
    attempts = []
    async def unavailable(**kwargs):
        attempts.append(1)
        return processor.StepResult(outcome=processor.StepOutcome.RETRY, error="busy", retry_reason="busy")
    monkeypatch.setattr(loop, "process_step", unavailable)
    real = loop._run_provider_attempts
    async def retry(*args, **kwargs):
        return await real(*args, **kwargs, delay_for=lambda *_: 30)
    monkeypatch.setattr(loop, "_run_provider_attempts", retry)
    await run(main, owner)
    assert len(attempts) == 1
    await assert_failed(main, owner, first["inbox_id"])


async def test_compaction_requests_share_the_main_budget_and_cannot_cross_sessions(monkeypatch):
    from agent.compaction import _summary_stream, CompactionInterrupted
    from tool.tool import ToolContext
    _, owner, _, main, _ = await runtime(monkeypatch, model_requests=1)
    lease = await reserve_run(main.id, owner)
    value = None
    token = None
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs["ctx"].session_id)
        yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr("agent.llm.stream_llm", stream)
    try:
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        value = await budget.start(lease)
        token = budget.current.set(value)
        args = dict(ctx=ToolContext(session_id=main.id), abort=lease.abort,
                    messages=[], model_id="test/model", billing_kind="compaction")
        assert len([e async for e in _summary_stream(**args)]) == 1
        assert len([e async for e in _summary_stream(**{**args, "ctx": ToolContext(session_id="other-session")})]) == 1
        with pytest.raises(CompactionInterrupted):
            _ = [e async for e in _summary_stream(**args)]
        assert calls == [main.id, "other-session"]
    finally:
        if token is not None: budget.current.reset(token)
        if value: await value.close()
        await lease.release(session_status="idle")


async def test_report_budget_exhaustion_retries_reporting_without_reexecuting_task(monkeypatch):
    config = _loop_config()
    config.assistant.report_only.model_requests = 1
    config.permission = {"*": "allow"}
    config.compaction.auto = False
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    async def tools(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, main, accepted, execution, _ = await result_ready()
    await execution.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    receipt = await deliver_task_result(result_id)
    async def stream(**kwargs):
        wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
        yield {"type": "tool_call", "tool": wire, "args": {"result_id": result_id}, "call_id": "read", "invalid": False}
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(main, owner)
    await assert_failed(main, owner, receipt["inbox_id"])
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        assert result.delivery_state == "retry_wait" and result.processed_message_id is None
        assert result.outcome == "succeeded"
        assert (await db.get(AgentDriverState, accepted["execution_session_id"])).generation == 1


async def test_legacy_controls_cannot_bypass_the_main_input_queue_or_allocate_resources(monkeypatch):
    from tests.unit.test_assistant_public_history import client_for
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    async def forbidden(*args, **kwargs):
        raise AssertionError("Main legacy controls must stop before any resource or run mutation")
    monkeypatch.setattr("api.sessions._reserve_prompt_run", forbidden)
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", forbidden)
    paths = [("POST", "plan/accept", None), ("POST", "plan/reject", None),
             ("POST", "summarize", None), ("POST", "command", {"command": "test"}),
             ("POST", "todo/items", {"subject": "must not add"}),
             ("DELETE", "todo/items/nonexistent", None),
             ("GET", "plan", None), ("PUT", "plan", {"content": "must not write"})]
    async with client_for(owner, workspace) as client:
        for method, suffix, body in paths:
            response = await client.request(method, f"/api/agent/session/{main.id}/{suffix}", json=body)
            assert response.status_code == 409, response.text
            assert response.json()["detail"]["code"] == "ASSISTANT_INPUT_REQUIRED"
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message).where(Message.session_id == main.id)) == 0
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.session_id == main.id)) == 0
        assert await db.get(AgentDriverState, main.id) is None


async def test_recovered_legacy_main_trigger_closes_without_an_infinite_retry(monkeypatch):
    from session.session import create_user_message
    config = _loop_config()
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    lease = await reserve_run(main.id, owner)
    try:
        await create_user_message(main.id, "Legacy command", agent="assistant", user_id=owner,
            origin="system_recovery", run_fence=(main.id, lease.run_id, lease.generation), bind_trigger=True)
        async def forbidden(**kwargs):
            raise AssertionError("Legacy main trigger must not dispatch a model")
        monkeypatch.setattr(loop, "process_step", forbidden)
        await loop.run_loop(main.id, owner, lease=lease)
        async with get_db_session() as db:
            assert (await db.get(AgentDriverState, main.id)).phase == "idle"
            terminal = await db.scalar(select(Message).where(Message.session_id == main.id, Message.role == "assistant"))
            assert terminal.finish == "error" and terminal.error["code"] == "ASSISTANT_INPUT_REQUIRED"
        assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    finally:
        await lease.release(session_status="idle")
