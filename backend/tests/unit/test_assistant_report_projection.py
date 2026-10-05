"""One report candidate per attempt; actual source checks still fence dispatch."""
import json
import time

import pytest
from sqlalchemy import event, func, select

from agent import loop, processor
from assistant import projection
from assistant.results import deliver_task_result
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.part import Part
from db.models.session import Session
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _assert_balanced_steps
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_report_binding_acceptance import (
    main_events, read_call, result_for, run, setup, task,
)
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts
from tests.unit.test_assistant_walk_originals import repeated_result  # noqa: F401
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401


BODY = "REPORT_PROJECTION_ORIGINAL_419: verification remains incomplete; no deployment was performed."


async def test_real_report_sizes_once_per_step_and_preserves_sources_and_receipts(monkeypatch, record_property):
    await _report_scenario(monkeypatch, record_property)


async def test_derived_report_sizes_once_without_rewriting_execution(monkeypatch, record_property, repeated_result):
    await _report_scenario(monkeypatch, record_property, repeated_result)


async def _report_scenario(monkeypatch, record_property, prepared=None):
    owner, workspace, main = await setup(monkeypatch)
    from core.config import get_config
    get_config().compaction.auto = False
    if prepared:
        from db.models.assistant import AssistantTask
        scope, result_id = prepared
        owner, workspace = scope["user_id"], scope["workspace_id"]
        async with get_db_session() as db:
            main = await db.get(Session, scope["main_id"])
            result = await db.get(TaskResult, result_id)
            current_task = await db.get(AssistantTask, result.task_id)
            accepted = {"task_id": result.task_id, "execution_session_id": current_task.execution_session_id}
            original_part = await db.scalar(select(Part).where(Part.message_id == result.result_message_id, Part.type == "text"))
            body = original_part.data["text"]
    else:
        accepted = await task(owner, workspace, main, "single-report-projection",
                              "Keep the original report's uncertainty and do not claim deployment.")
        result, body = None, BODY
    report_calls, provider_times, errors = [], [], []

    async def stream(**kwargs):
        try:
            if kwargs["ctx"].session_id != main.id:
                yield {"type": "text_delta", "text": body}
            else:
                payload = json.dumps(kwargs["messages"])
                report_calls.append(payload)
                provider_times.append(time.perf_counter())
                if len(report_calls) == 1:
                    assert body not in payload
                    yield read_call(kwargs, result.id, "read-original-report")
                    yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                    return
                assert body in payload
                if len(report_calls) == 2:
                    wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "tasks.get")
                    yield {"type": "tool_call", "tool": wire, "args": {"task_id": accepted["task_id"]},
                           "call_id": "read-original-task", "invalid": False}
                    yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                    return
                yield {"type": "text_delta", "text": "The original execution report says: " + body}
            yield {"type": "finish", "reason": "stop", "usage": {}}
        except (AssertionError, KeyError, TypeError) as exc:
            errors.append(repr(exc))
            raise

    monkeypatch.setattr(processor, "stream_llm", stream)
    if not prepared:
        await run(accepted["execution_session_id"], owner)
        result = await result_for(accepted)
    original = await _execution_facts(accepted["task_id"])
    delivery = await deliver_task_result(result.id)
    measured, queries = [], []
    real_project = projection.project_main_messages

    def query(*_args):
        queries.append(1)

    async def project(messages, **kwargs):
        started, count = time.perf_counter(), len(queries)
        try:
            return await real_project(messages, **kwargs)
        finally:
            measured.append({"seconds": time.perf_counter() - started,
                             "sql": len(queries) - count, "for_compaction": kwargs.get("for_compaction", False)})

    monkeypatch.setattr(projection, "project_main_messages", project)
    event.listen(get_engine().sync_engine, "before_cursor_execute", query)
    started = time.perf_counter()
    try:
        lease = await run(main.id, owner)
    finally:
        duration = time.perf_counter() - started
        event.remove(get_engine().sync_engine, "before_cursor_execute", query)
    evidence = {"derived_history": bool(prepared), "provider_calls": len(report_calls), "projection_calls": len(measured),
        "sql": len(queries), "seconds": duration, "first_provider_seconds": provider_times[0] - started,
        "projection": measured}
    print("REPORT_PROJECTION " + json.dumps(evidence))
    record_property("report_projection", json.dumps(evidence))
    assert not errors
    assert len(report_calls) == 3
    assert await _execution_facts(accepted["task_id"]) == original
    async with get_db_session() as db:
        saved = await db.get(TaskResult, result.id)
        item = await db.get(AgentInboxItem, delivery["inbox_id"])
        assert saved.delivery_state == "processed" and saved.processed_message_id
        assert item.state == "settled" and item.outcome == "succeeded"
    requests = [row for row in await main_events(main.id, "model.requested") if row.run_id == lease.run_id]
    admissions = [row for row in await main_events(main.id, "assistant.budget.request") if row.run_id == lease.run_id]
    assert len(requests) == len(admissions) == 3
    assert all(row.payload["assistant_context"]["mode"] == "report_only" for row in requests)
    assert len(await main_events(main.id, "assistant.result.processed")) == 1
    assert len(await main_events(main.id, "assistant.report.sources_projected")) == 2
    if not prepared:
        await _assert_balanced_steps(main.id, owner, 3)
    else:
        async with get_db_session() as db:
            for row in requests:
                boundaries = (await db.scalars(select(Part.type).where(Part.message_id == row.message_id,
                    Part.type.in_(("step-start", "step-finish"))))).all()
                assert sorted(boundaries) == ["step-finish", "step-start"]
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert len(measured) == 3


@pytest.mark.parametrize("source", ["body", "audience"])
async def test_report_reused_bytes_still_reject_independent_postgres_revocation(monkeypatch, source):
    from assistant import context_sources
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires a distinct committed PostgreSQL writer during the real checkpoint")
    owner, workspace, main = await setup(monkeypatch)
    from core.config import get_config
    get_config().compaction.auto = False
    accepted = await task(owner, workspace, main, "fresh-report-source", "Only return a text report.")
    result, calls, proofs, writes = None, [], [], []

    async def stream(**kwargs):
        if kwargs["ctx"].session_id != main.id:
            yield {"type": "text_delta", "text": BODY}
        else:
            calls.append(json.dumps(kwargs["messages"]))
            yield read_call(kwargs, result.id, "fresh-report-read")
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
            return
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(accepted["execution_session_id"], owner)
    result = await result_for(accepted)
    delivery = await deliver_task_result(result.id)
    real_proof, real_check = loop._assistant_admission_only, context_sources.checked_context_locked

    async def proof(*args, **kwargs):
        answer = await real_proof(*args, **kwargs)
        assert kwargs["report_view"]["result_id"] == result.id
        proofs.append(answer)
        return answer

    async def check(db, current_main, context, **kwargs):
        if kwargs.get("fresh") and len(calls) == 1 and not writes:
            assert proofs[-1] is True, "The exact report candidate was reused before this fresh boundary"
            if source == "body":
                held = await db.scalar(select(Part).where(Part.message_id == result.result_message_id,
                                                         Part.type == "text"))
                target, identity, field = Part, held.id, "data"
                value = {**held.data, "text": "The currently stored report changed after candidate reuse."}
            else:
                target, identity, field, value = Session, accepted["execution_session_id"], "visibility", "workspace"
                held = await db.get(target, identity)
            old = getattr(held, field)
            reader = await db.scalar(select(func.pg_backend_pid()))
            async with get_db_session() as writer:
                writer_pid = await writer.scalar(select(func.pg_backend_pid()))
                assert reader != writer_pid
                setattr(await writer.get(target, identity), field, value)
            assert getattr(held, field) == old and old != value
            writes.append((reader, writer_pid))
        return await real_check(db, current_main, context, **kwargs)

    monkeypatch.setattr(loop, "_assistant_admission_only", proof)
    monkeypatch.setattr(context_sources, "checked_context_locked", check)
    await run(main.id, owner)
    assert len(writes) == 1 and proofs == [True, True]
    assert len(calls) == 1 and BODY not in calls[0]
    assert len(await main_events(main.id, "model.requested")) == 1
    assert len(await main_events(main.id, "assistant.budget.request")) == 2
    assert not await main_events(main.id, "assistant.result.processed")
    await _assert_balanced_steps(main.id, owner, 2)
    async with get_db_session() as db:
        saved = await db.get(TaskResult, result.id)
        item = await db.get(AgentInboxItem, delivery["inbox_id"])
        assert saved.delivery_state == "blocked" and saved.processed_message_id is None
        assert saved.report_attempt == 1 and item.outcome == "error"
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok


@pytest.mark.parametrize("change", ["result-version", "transport-retry"])
async def test_report_rebuilds_changed_binding_or_new_transport_attempt_without_extra_charge(monkeypatch, change):
    from agent.retry import RetryableError
    from assistant import budget as assistant_budget
    owner, workspace, main = await setup(monkeypatch)
    from core.config import get_config
    get_config().compaction.auto = False
    accepted = await task(owner, workspace, main, "report-candidate-rebuild", "Return only a text report.")
    result, calls, projects, proofs, changed = None, [], [], [], []

    async def stream(**kwargs):
        if kwargs["ctx"].session_id != main.id:
            yield {"type": "text_delta", "text": BODY}
        else:
            calls.append(json.dumps(kwargs["messages"]))
            if change == "transport-retry" and len(calls) == 1:
                yield {"type": "error", "error": RetryableError("503 Service Unavailable", status_code=503)}
                return
            if BODY not in calls[-1]:
                yield read_call(kwargs, result.id, "read-after-rebuild")
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            yield {"type": "text_delta", "text": "The source states: " + BODY}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(accepted["execution_session_id"], owner)
    result = await result_for(accepted)
    await deliver_task_result(result.id)
    real_project, real_proof = projection.project_main_messages, loop._assistant_admission_only
    real_admit, real_attempts = assistant_budget.TurnBudget.admit, loop._run_provider_attempts

    async def project(*args, **kwargs):
        projects.append(1)
        return await real_project(*args, **kwargs)

    async def proof(*args, **kwargs):
        value = await real_proof(*args, **kwargs)
        proofs.append(value)
        return value

    async def admit(self, kind, identity):
        await real_admit(self, kind, identity)
        if kind == "request" and change == "result-version" and not changed:
            async with get_db_session() as db:
                saved = await db.get(TaskResult, result.id)
                assert len(saved.output_refs) >= 2
                saved.output_refs = list(reversed(saved.output_refs))
                changed.append(saved.output_refs)

    async def attempts(*args, **kwargs):
        return await real_attempts(*args, **kwargs, delay_for=lambda *_: 0)

    monkeypatch.setattr(projection, "project_main_messages", project)
    monkeypatch.setattr(loop, "_assistant_admission_only", proof)
    monkeypatch.setattr(assistant_budget.TurnBudget, "admit", admit)
    monkeypatch.setattr(loop, "_run_provider_attempts", attempts)
    await run(main.id, owner)
    assert len(calls) == (2 if change == "result-version" else 3)
    assert len(projects) == 3
    assert proofs == ([False, True] if change == "result-version" else [True, True])
    assert len(await main_events(main.id, "model.requested")) == len(calls)
    assert len(await main_events(main.id, "assistant.budget.request")) == len(calls)
    async with get_db_session() as db:
        saved = await db.get(TaskResult, result.id)
        assert saved.delivery_state == "processed" and saved.report_attempt == 1
        if changed:
            assert saved.output_refs == changed[0]
    await _assert_balanced_steps(main.id, owner, 2)
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
