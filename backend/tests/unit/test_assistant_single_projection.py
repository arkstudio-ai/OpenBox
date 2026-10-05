"""Real loop/processor/SQL coverage for sizing one owned assistant candidate."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from agent import loop, processor
from agent.driver import reserve_run
from assistant import budget as assistant_budget, projection
from assistant.inputs import accept_turn
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import (
    _assert_balanced_steps, _loop_config, _patch_real_loop_runtime,
)
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


async def _runtime(monkeypatch):
    config = _loop_config()
    config.compaction.auto = False
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def tools(*_args, **_kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    return SimpleNamespace(config=config, owner=owner, workspace=workspace, main=main)


async def _accept(state, text="Answer briefly using the verified history."):
    return await accept_turn(user_id=state.owner, workspace_id=state.workspace,
        main_id=state.main.id, client_id=f"single-{time.monotonic_ns()}", text=text)


async def _run(state):
    lease = await reserve_run(state.main.id, state.owner)
    try:
        return await asyncio.wait_for(loop.run_loop(state.main.id, state.owner, lease=lease), timeout=45)
    finally:
        await lease.release(session_status="idle")


async def _events(state, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == state.main.id, AgentEvent.kind == kind,
        ).order_by(AgentEvent.sequence))).all())


@pytest.mark.parametrize("with_history", [False, True])
async def test_one_full_projection_for_one_dispatched_step(monkeypatch, with_history):
    state = await _runtime(monkeypatch)
    provider_calls, dispatch_times = [], []

    async def stream(**kwargs):
        provider_calls.append(kwargs)
        dispatch_times.append(time.perf_counter())
        if with_history and len(provider_calls) == 1:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.list")
            yield {"type": "tool_call", "tool": wire, "args": {}, "call_id": "read-list", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "The currently verified task inventory was checked."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    if with_history:
        from assistant.commands import accept_task_command
        for index in range(12):
            await accept_task_command(user_id=state.owner, workspace_id=state.workspace,
                main_id=state.main.id, project_id=state.main.project_id,
                idempotency_key=f"projection-task-{index}", prompt="Only draft a short local report.",
                title=f"Verified task {index}")
        await _accept(state, "Read the current task list, then answer briefly.")
        await _run(state)
        assert len(provider_calls) == 2
        for _ in range(3):
            await _accept(state, "Restate the verified task facts without dispatching work.")
            await _run(state)
    projections, sql = [], []
    real_project = projection.project_main_messages

    def query(*_args):
        sql.append(1)

    async def measured(*args, **kwargs):
        started, count = time.perf_counter(), len(sql)
        try:
            return await real_project(*args, **kwargs)
        finally:
            projections.append({"seconds": time.perf_counter() - started,
                                "sql": len(sql) - count})

    monkeypatch.setattr(projection, "project_main_messages", measured)
    event.listen(get_engine().sync_engine, "before_cursor_execute", query)
    try:
        accepted = await _accept(state)
        before = len(provider_calls)
        started = time.perf_counter()
        await _run(state)
        duration = time.perf_counter() - started
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", query)
    print("SINGLE_PROJECTION " + json.dumps({"history": with_history,
        "provider_calls": len(provider_calls) - before, "seconds": duration,
        "pre_dispatch_seconds": dispatch_times[-1] - started,
        "projection": projections, "sql": len(sql)}))
    assert len(provider_calls) == before + 1
    assert len(projections) == 1
    requests, budgets = await _events(state, "model.requested"), await _events(state, "assistant.budget.request")
    assert len(requests) == len(budgets) == len(provider_calls)
    assert all(budget.sequence < request.sequence for budget, request in zip(budgets, requests))
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, accepted["inbox_id"])).outcome == "succeeded"
    await _assert_balanced_steps(state.main.id, state.owner, len(provider_calls))
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok


@pytest.mark.parametrize("arrival", ["before_admission", "after_admission", "after_proof"])
async def test_another_input_event_rebuilds_instead_of_rebinding_owned_bytes(monkeypatch, arrival):
    state = await _runtime(monkeypatch)
    await _accept(state)
    real_admit = assistant_budget.TurnBudget.admit
    real_proof = loop._assistant_admission_only
    real_project = projection.project_main_messages
    projections, proofs, provider_calls, next_input = [], [], [], []

    async def project(*args, **kwargs):
        projections.append(1)
        return await real_project(*args, **kwargs)

    async def arrive():
        if not next_input:
            next_input.append(await _accept(state, "A later human input must remain queued."))

    async def admit(self, kind, identity):
        if kind == "request" and arrival == "before_admission":
            await arrive()
        await real_admit(self, kind, identity)
        if kind == "request" and arrival == "after_admission":
            await arrive()

    async def proof(*args, **kwargs):
        answer = await real_proof(*args, **kwargs)
        proofs.append(answer)
        if arrival == "after_proof":
            assert answer
            await arrive()
        return answer

    async def stream(**kwargs):
        provider_calls.append(kwargs)
        assert "A later human input" not in json.dumps(kwargs["messages"])
        yield {"type": "text_delta", "text": "The current turn completed."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(assistant_budget.TurnBudget, "admit", admit)
    monkeypatch.setattr(loop, "_assistant_admission_only", proof)
    monkeypatch.setattr(projection, "project_main_messages", project)
    monkeypatch.setattr(processor, "stream_llm", stream)
    await _run(state)
    assert len(projections) == 2 and len(provider_calls) == 1
    assert proofs == [arrival == "after_proof"]
    assert len(await _events(state, "model.requested")) == 1
    assert len(await _events(state, "assistant.budget.request")) == 1
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, next_input[0]["inbox_id"])).state == "accepted"
    await _assert_balanced_steps(state.main.id, state.owner, 1)
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok


async def test_one_non_budget_event_cannot_impersonate_an_idempotent_admission(monkeypatch):
    state = await _runtime(monkeypatch)
    await _accept(state)
    real_prepare, real_admit = loop._prepare_checkpointed_provider_attempt, assistant_budget.TurnBudget.admit
    real_proof, real_project = loop._assistant_admission_only, projection.project_main_messages
    projections, proofs, calls, arrivals = [], [], [], []

    async def prepare(**kwargs):
        load = kwargs["load_surface"]
        paid = False

        async def load_with_existing_receipt():
            nonlocal paid
            candidate = await load()
            if not paid:
                active = assistant_budget.current.get()
                current_message = next(m for m in reversed(candidate.messages) if m.role == "assistant")
                await real_admit(active, "request", f"{current_message.id}:1")
                paid = True
                candidate = await load()
            return candidate

        return await real_prepare(**{**kwargs, "load_surface": load_with_existing_receipt})

    async def admit(self, kind, identity):
        await real_admit(self, kind, identity)  # The exact receipt already exists.
        if not arrivals:
            arrivals.append(await _accept(state, "A separate queued input is exactly one new Event."))

    async def proof(before, after, *args):
        assert after.event_sequence == before.event_sequence + 1
        assert after.messages == before.messages
        answer = await real_proof(before, after, *args)
        proofs.append(answer)
        return answer

    async def project(*args, **kwargs):
        projections.append(1)
        return await real_project(*args, **kwargs)

    async def stream(**kwargs):
        calls.append(kwargs)
        yield {"type": "text_delta", "text": "Only the current turn was handled."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(loop, "_prepare_checkpointed_provider_attempt", prepare)
    monkeypatch.setattr(assistant_budget.TurnBudget, "admit", admit)
    monkeypatch.setattr(loop, "_assistant_admission_only", proof)
    monkeypatch.setattr(projection, "project_main_messages", project)
    monkeypatch.setattr(processor, "stream_llm", stream)
    await _run(state)
    assert proofs == [False] and len(projections) == 2 and len(calls) == 1
    assert len(await _events(state, "assistant.budget.request")) == 1
    assert len(await _events(state, "model.requested")) == 1
    await _assert_balanced_steps(state.main.id, state.owner, 1)


@pytest.mark.parametrize("revoke", [False, True])
async def test_transport_retry_builds_again_and_rechecks_current_human_source(monkeypatch, revoke):
    from agent.retry import RetryableError
    from db.models.part import Part
    state = await _runtime(monkeypatch)
    accepted = await _accept(state)
    real_project, real_attempts = projection.project_main_messages, loop._run_provider_attempts
    projections, provider_calls = [], []

    async def project(*args, **kwargs):
        projections.append(1)
        return await real_project(*args, **kwargs)

    async def attempts(*args, **kwargs):
        return await real_attempts(*args, **kwargs, delay_for=lambda *_: 0)

    async def stream(**kwargs):
        provider_calls.append(kwargs)
        if len(provider_calls) == 1:
            if revoke:
                async with get_db_session() as db:
                    item = await db.get(AgentInboxItem, accepted["inbox_id"])
                    part = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
                    part.data = {**part.data, "text": "The original input changed in SQL."}
            yield {"type": "error", "error": RetryableError("503 Service Unavailable", status_code=503)}
        else:
            yield {"type": "text_delta", "text": "The retry succeeded."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(projection, "project_main_messages", project)
    monkeypatch.setattr(loop, "_run_provider_attempts", attempts)
    monkeypatch.setattr(processor, "stream_llm", stream)
    await _run(state)
    assert len(projections) == 2
    assert len(provider_calls) == (1 if revoke else 2)
    assert len(await _events(state, "model.requested")) == len(provider_calls)
    assert len(await _events(state, "assistant.budget.request")) == 2
    await _assert_balanced_steps(state.main.id, state.owner, 1)


@pytest.mark.parametrize("source", ["human", "actor", "task", "knowledge", "derived_knowledge"])
async def test_postgres_fresh_checkpoint_rejects_independent_revocation_after_reuse(monkeypatch, source):
    from sqlalchemy import func
    from assistant import context_sources
    from assistant.commands import accept_task_command
    from db.models.memory_v2 import MemorySource
    from db.models.part import Part
    from db.models.session import Session
    from db.models.workspace import WorkspaceMember
    from tests.unit.test_assistant_knowledge import page_for

    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires an independent PostgreSQL transaction holding a stale ORM object")
    state = await _runtime(monkeypatch)
    target, target_id, field, value = None, None, None, None
    if source == "actor":
        target, target_id, field, value = WorkspaceMember, (state.workspace, state.owner), "status", "removed"
    elif source == "task":
        task = await accept_task_command(user_id=state.owner, workspace_id=state.workspace,
            main_id=state.main.id, project_id=state.main.project_id,
            idempotency_key="source-task", prompt="Only draft locally.", title="PRIVATE_TASK_TITLE")
        target, target_id, field, value = Session, task["execution_session_id"], "visibility", "workspace"
    elif "knowledge" in source:
        state.config.memory.wiki = state.config.memory.v2_write = True
        state.config.memory.automatic_knowledge = False
        state.config.memory.allowed_user_ids = [state.owner]
        state.config.jwt_secret = "single-projection-test-cursor"
        monkeypatch.setattr("assistant.knowledge.get_config", lambda: state.config)
        page, _ = await page_for({"user_id": state.owner, "workspace_id": state.workspace,
                                 "main_id": state.main.id}, None, "PRIVATE_KNOWLEDGE_TITLE")
        target, target_id, field, value = MemorySource, page.source_manifest[0]["id"], "status", "REVOKED"
    provider_calls, proofs, changed = [], [], []

    async def stream(**kwargs):
        provider_calls.append(kwargs)
        if "knowledge" in source and len(provider_calls) == 1:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "knowledge.directory")
            yield {"type": "tool_call", "tool": wire, "args": {}, "call_id": "read-knowledge", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "PRIVATE_DERIVED_KNOWLEDGE_TITLE"}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    if source == "derived_knowledge":
        await _accept(state, "Consult the current knowledge directory, then summarize its title.")
        await _run(state)
        assert len(provider_calls) == 2
    accepted = await _accept(state)
    prior_calls = len(provider_calls)
    real_check, real_proof = context_sources.checked_context_locked, loop._assistant_admission_only

    async def proof(*args, **kwargs):
        result = await real_proof(*args, **kwargs)
        proofs.append(result)
        return result

    async def check(db, main, context, **kwargs):
        nonlocal target, target_id, field, value
        ready = source != "knowledge" or any(
            read.get("operation") == "knowledge.directory" for read in context["business_reads"])
        if kwargs.get("fresh") and ready and not changed:
            assert proofs[-1] is True, "The optimization reused bytes before this real fresh checkpoint"
            if source == "human":
                item = await db.get(AgentInboxItem, accepted["inbox_id"])
                held = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
                target, target_id, field = Part, held.id, "data"
                value = {**held.data, "text": "The current original human source changed."}
            held = await db.get(target, target_id)
            old_value = getattr(held, field)
            reader_pid = await db.scalar(select(func.pg_backend_pid()))
            async with get_db_session() as writer:
                writer_pid = await writer.scalar(select(func.pg_backend_pid()))
                assert reader_pid != writer_pid
                setattr(await writer.get(target, target_id), field, value)
            assert getattr(held, field) == old_value and old_value != value
            changed.append((reader_pid, writer_pid))
        return await real_check(db, main, context, **kwargs)

    monkeypatch.setattr(loop, "_assistant_admission_only", proof)
    monkeypatch.setattr(context_sources, "checked_context_locked", check)
    await _run(state)
    assert len(changed) == 1
    expected_calls = prior_calls + (1 if source == "knowledge" else 0)
    assert len(provider_calls) == expected_calls
    assert len(await _events(state, "model.requested")) == expected_calls
    assert len(await _events(state, "assistant.budget.request")) == expected_calls + 1
    await _assert_balanced_steps(state.main.id, state.owner, expected_calls + 1)
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, accepted["inbox_id"])).outcome == "error"


@pytest.mark.parametrize("stays_large,max_retries", [(False, 0), (True, 0), (True, 1)])
async def test_pressure_compaction_spends_only_summary_requests_and_preserves_retry_limit(
    monkeypatch, stays_large, max_retries,
):
    from agent import compaction
    from core.config import ModelConfig
    from db.models.message import Message
    from db.models.part import Part
    state = await _runtime(monkeypatch)
    state.config.models = [ModelConfig(id=state.config.model, context_limit=18000)]
    state.config.compaction.max_tokens = 512
    monkeypatch.setattr("agent.llm._get_max_output_tokens", lambda _: 2048)
    provider_calls, summary_calls, prunes = [], [], []
    seeding = True

    async def stream(**kwargs):
        provider_calls.append(kwargs)
        yield {"type": "text_delta", "text": "Preserve the original local-only constraint."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 127000 if seeding else 100, "output": 10}}

    async def summary(**kwargs):
        summary_calls.append(kwargs)
        yield {"type": "text_delta", "text": "Earlier work was local only; preserve the original constraints."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 100, "output": 10}}

    async def prune(*args, **kwargs):
        if kwargs.get("aggressive"):
            prunes.append(1)
        await compaction.prune_tool_outputs(*args, **kwargs)

    monkeypatch.setattr(processor, "stream_llm", stream)
    monkeypatch.setattr("agent.llm.stream_llm", summary)
    for _ in range(2):
        await _accept(state, "Record the original local-only constraint. Do not publish anything.")
        await _run(state)
    seeding = False
    state.config.compaction.auto = True
    if stays_large:
        # Summaries legitimately shrink the source and fit the hard model
        # window, but the fixed tool/schema prefix alone exceeds this policy.
        state.config.compaction.threshold_ratio = 0.2
    state.config.compaction.max_retries = max_retries
    state.config.compaction.preserve_recent_tokens = 0
    state.config.compaction.tail_turns = 0
    monkeypatch.setattr(loop, "prune_tool_outputs", prune)
    current_text = "UNIQUE_CURRENT_INPUT: Restate the verified original constraints, without executing work."
    accepted = await _accept(state, current_text)
    await _run(state)
    assert sum(call["billing_kind"] == "compaction" for call in summary_calls) == (max_retries + 1 if stays_large else 1)
    assert len(provider_calls) == (2 if stays_large else 3)
    assert len(prunes) == (max_retries + 2 if stays_large else 1)
    requests = await _events(state, "assistant.budget.request")
    assert len(requests) == len(provider_calls) + len(summary_calls)
    assert len(await _events(state, "model.requested")) == len(provider_calls)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
        assert item.outcome == ("error" if stays_large else "succeeded")
        if not stays_large:
            wire = json.dumps(provider_calls[-1]["messages"])
            assert current_text in wire and item.message_id in wire
            current_part_id = await db.scalar(select(Part.id).where(
                Part.message_id == item.message_id, Part.type == "text"))
            model_requests = await _events(state, "model.requested")
            refs = model_requests[-1].payload["assistant_context"]["source_refs"]
            assert any(ref["message_id"] == item.message_id and ref["part_id"] == current_part_id for ref in refs)
        assistants = list((await db.scalars(select(Message).where(
            Message.session_id == state.main.id, Message.agent == "assistant", Message.role == "assistant",
        ))).all())
        for assistant in assistants:
            parts = list((await db.scalars(select(Part.type).where(Part.message_id == assistant.id))).all())
            assert parts.count("step-start") == parts.count("step-finish") == 1
            assert assistant.finish is not None
        if stays_large:
            assert (await db.get(Message, item.result_message_id)).error["code"] == "COMPACTION_FAILED"
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok
