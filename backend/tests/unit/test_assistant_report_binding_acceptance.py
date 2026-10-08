"""PA-12/13: real report dispatch binds one result and preserves source scope.

The provider is deterministic external I/O. These tests establish transport,
persistence and receipt authority, not arbitrary model understanding. In V2 the
report input carries the result summary; reads are optional and not coverage.
"""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant.commands import accept_task_command, command_digest
from assistant.reporting import REPORT_TOOLS
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts
from tool.assistant_tools import assistant_tools


async def setup(monkeypatch):
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def no_external(*args, **kwargs):
        return None

    async def tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools} if agent.name == "assistant" else {},
                               catalogue_availability="available")

    monkeypatch.setattr(loop, "_ensure_title", no_external)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_external)
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    return owner, workspace, main


async def run(session_id, owner):
    lease = await reserve_run(session_id, owner)
    try:
        await loop.run_loop(session_id, user_id=owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    return lease


async def task(owner, workspace, main, key, prompt):
    return await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        project_id=main.project_id, idempotency_key=key, title=key, prompt=prompt)


async def result_for(receipt):
    async with get_db_session() as db:
        return await db.scalar(select(TaskResult).where(TaskResult.task_id == receipt["task_id"]))


async def main_events(main_id, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main_id,
            AgentEvent.kind == kind).order_by(AgentEvent.sequence))).all())


async def counts(owner, main_id):
    async with get_db_session() as db:
        return {name: await db.scalar(select(func.count()).select_from(model).where(predicate))
            for name, model, predicate in (
                ("commands", AssistantCommand, AssistantCommand.actor_user_id == owner),
                ("tasks", AssistantTask, AssistantTask.user_id == owner),
                ("execution_sessions", Session, (Session.user_id == owner) & (Session.id != main_id)),
                ("submissions", TaskSubmission, TaskSubmission.task_id.in_(select(AssistantTask.id).where(AssistantTask.user_id == owner))),
                ("execution_inputs", AgentInboxItem, (AgentInboxItem.user_id == owner) & (AgentInboxItem.session_id != main_id)),
                ("results", TaskResult, TaskResult.task_id.in_(select(AssistantTask.id).where(AssistantTask.user_id == owner))),
                ("report_inputs", AgentInboxItem, (AgentInboxItem.session_id == main_id) & (AgentInboxItem.origin == "task_result")),
                ("processed_events", AgentEvent, (AgentEvent.session_id == main_id) & (AgentEvent.kind == "assistant.result.processed")),
            )}


def read_call(kwargs, result_id, call_id):
    wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "results.read")
    return {"type": "tool_call", "tool": wire, "args": {"result_id": result_id}, "call_id": call_id, "invalid": False}


async def test_two_real_results_and_a_model_named_other_id_process_only_the_bound_result(monkeypatch, record_property):
    owner, workspace, main = await setup(monkeypatch)
    first = await task(owner, workspace, main, "bound-result-A", "ORIGINAL_REQUEST_A: report only task A's text.")
    second = await task(owner, workspace, main, "queued-result-B", "ORIGINAL_REQUEST_B: keep task B separate.")
    main_calls, execution_calls, bindings = [], [], []
    result_a = result_b = None

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        if ctx.session_id != main.id:
            execution_calls.append(ctx.session_id)
            yield {"type": "text_delta", "text": "REPORT_A_SOURCE" if ctx.session_id == first["execution_session_id"] else "REPORT_B_PRIVATE_SOURCE"}
        else:
            main_calls.append(json.dumps(kwargs["messages"]))
            assert ctx.sandbox is None and {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            assert "REPORT_B_PRIVATE_SOURCE" not in main_calls[-1] and "ORIGINAL_REQUEST_B" not in main_calls[-1]
            async with get_db_session() as db:
                claimed = list((await db.scalars(select(AgentInboxItem).where(
                    AgentInboxItem.session_id == main.id, AgentInboxItem.state == "claimed"))).all())
                assert len(claimed) == 1
                assert claimed[0].origin_ref["result_id"] == result_a.id
                assert claimed[0].origin_ref["report_attempt"] == 1
                assert claimed[0].origin_ref["execution_mode"] == "report_only"
                assert (await db.get(AgentInboxItem, result_b.assistant_inbox_id)).state == "accepted"
                bindings.append({"inbox_id": claimed[0].id, "run_id": claimed[0].run_id,
                                 "generation": claimed[0].generation, **claimed[0].origin_ref})
            assert (await inbox.claim_inbox_boundary(
                SimpleNamespace(session_id=ctx.session_id, user_id=ctx.user_id, run_id=ctx.run_id,
                    generation=ctx.run_generation), step=99, include_next_turn=True)).empty
            if len(main_calls) == 1:
                # B exists, is owned and is accepted; only this attempt's
                # binding, not absence or another actor, can reject the read.
                yield read_call(kwargs, result_b.id, "attempt-read-other-result")
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            if len(main_calls) == 2:
                # The hook rejects this before dispatch. Within this turn the
                # provider sees the refusal, never the other result's sources.
                assert "Read is outside the bound result" in main_calls[-1]
                async with get_db_session() as db:
                    blocked = await db.scalar(select(Part).where(Part.session_id == main.id,
                        Part.type == "tool").order_by(Part.created_at.desc()))
                    assert blocked.data["input"]["result_id"] == result_b.id
                    assert blocked.data["metadata"]["blocked"] is True
                yield read_call(kwargs, result_a.id, "read-bound-result")
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert "REPORT_A_SOURCE" in main_calls[-1] and "ORIGINAL_REQUEST_A" in main_calls[-1]
            yield {"type": "text_delta", "text": f"Model-written acknowledgment names {result_b.id}. The bound source reports REPORT_A_SOURCE."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(first["execution_session_id"], owner)
    await run(second["execution_session_id"], owner)
    result_a, result_b = await result_for(first), await result_for(second)
    original_facts = [await _execution_facts(receipt["task_id"]) for receipt in (first, second)]
    receipts = [await deliver_task_result(result_a.id), await deliver_task_result(result_b.id)]
    result_a, result_b = await result_for(first), await result_for(second)
    lease = await run(main.id, owner)
    assert execution_calls == [first["execution_session_id"], second["execution_session_id"]]
    assert len(main_calls) == 3
    assert [await _execution_facts(receipt["task_id"]) for receipt in (first, second)] == original_facts
    async with get_db_session() as db:
        processed, queued = await db.get(TaskResult, result_a.id), await db.get(TaskResult, result_b.id)
        assert processed.delivery_state == "processed" and processed.report_attempt == 1
        assert queued.delivery_state == "accepted" and queued.report_attempt == 1 and queued.processed_message_id is None
        assert (await db.get(AgentInboxItem, receipts[0]["inbox_id"])).state == "settled"
        assert (await db.get(AgentInboxItem, receipts[1]["inbox_id"])).state == "accepted"
        answer = await db.get(Message, processed.processed_message_id)
        body = "\n".join(part.data.get("text", "") for part in (await db.scalars(select(Part).where(
            Part.message_id == answer.id, Part.type == "text"))).all())
        assert queued.id in body and answer.finish == "stop"
    processed_events = await main_events(main.id, "assistant.result.processed")
    assert [event.payload["result_id"] for event in processed_events] == [result_a.id]
    assert processed_events[0].payload["original_report_message_id"] == result_a.result_message_id
    measured = await counts(owner, main.id)
    assert measured == {"commands": 2, "tasks": 2, "execution_sessions": 2, "submissions": 2,
        "execution_inputs": 2, "results": 2, "report_inputs": 2, "processed_events": 1}
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-12", "scope": "server report binding with deterministic provider",
        "receipts": receipts, "bound_result": result_a.id, "queued_result": result_b.id,
        "model_named_other_id": result_b.id, "bindings": bindings,
        "main_run": {"run_id": lease.run_id, "generation": lease.generation}, "counts": measured,
        "execution_facts_unchanged": original_facts, "processed_payload": processed_events[0].payload}))


ORIGINAL_REQUEST = "SOURCE_REQUEST: Preserve the original report's commit, file path, failed test, passed test range and unverified browser status. Do not claim independent verification."
ORIGINAL_REPORT = "\n".join((
    "SOURCE_REPORT: The execution report claims commit 0123456789abcdef0123456789abcdef01234567.",
    "Changed path: backend/example.py; report artifact: artifacts/results/report.json.",
    "Unit suite: tests/unit/test_example.py, 7 passed and 1 failed (test_failed_case).",
    "Browser and integration tests were NOT RUN. Deployment was NOT VERIFIED.",
    "The failed assertion remains unresolved; this is a partial result, not acceptance.",
))
REPORT_DETAILS = ["0123456789abcdef0123456789abcdef01234567", "backend/example.py", "artifacts/results/report.json",
    "tests/unit/test_example.py", "7 passed and 1 failed", "test_failed_case", "NOT RUN", "NOT VERIFIED", "partial result"]
SUMMARY = "The original execution report states:\n" + ORIGINAL_REPORT + "\nThese are reported claims; I have not independently verified the commit, artifact or tests."


@pytest.mark.parametrize("retry", [False, True], ids=["first_success", "retry_after_consumed_failure"])
async def test_first_and_retry_preserve_complex_original_scope_and_exact_report_reference(monkeypatch, record_property, retry):
    owner, workspace, main = await setup(monkeypatch)
    accepted = await task(owner, workspace, main, "source-preserving-report", ORIGINAL_REQUEST)
    result, execution_calls, report_calls = None, [], {}

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        if ctx.session_id != main.id:
            execution_calls.append(ctx.session_id)
            assert ORIGINAL_REQUEST in json.dumps(kwargs["messages"])
            yield {"type": "text_delta", "text": ORIGINAL_REPORT}
        else:
            assert ctx.sandbox is None and {tool.id for tool in kwargs["tools"].values()} == REPORT_TOOLS
            async with get_db_session() as db:
                bound = await db.get(TaskResult, result.id)
                attempt = bound.report_attempt
                item = await db.get(AgentInboxItem, bound.assistant_inbox_id)
                assert item.state == "claimed" and item.run_id == ctx.run_id and item.generation == ctx.run_generation
                assert item.origin == "task_result" and item.origin_ref["execution_mode"] == "report_only"
                assert item.origin_ref["result_id"] == result.id and item.origin_ref["report_attempt"] == attempt
            payload = json.dumps(kwargs["messages"])
            calls = report_calls.setdefault(attempt, [])
            calls.append(payload)
            # V2 7.2: every report input already carries the result summary.
            assert all(detail in payload for detail in REPORT_DETAILS)
            if len(calls) == 1:
                yield read_call(kwargs, result.id, f"read-original-attempt-{attempt}")
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert ORIGINAL_REQUEST in payload
            assert all(detail in payload for detail in REPORT_DETAILS)
            assert result.result_message_id in payload
            if retry and attempt == 1:
                yield {"type": "text_delta", "text": "Interrupted after consuming the original report."}
                raise RuntimeError("Deliberate provider interruption after evidence consumption")
            yield {"type": "text_delta", "text": SUMMARY}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    execution_lease = await run(accepted["execution_session_id"], owner)
    result = await result_for(accepted)
    frozen = {key: getattr(result, key) for key in ("id", "run_id", "generation", "result_message_id",
        "observed_intent_revision", "consumed_inbox_ids", "output_refs")}
    original_facts = await _execution_facts(accepted["task_id"])
    receipts = [await deliver_task_result(result.id)]
    first_lease = await run(main.id, owner)
    if retry:
        async with get_db_session() as db:
            failed = await db.get(TaskResult, result.id)
            assert failed.delivery_state == "retry_wait" and failed.processed_message_id is None
            assert failed.report_attempt == 1 and (await db.get(AgentInboxItem, receipts[0]["inbox_id"])).outcome == "error"
            failed.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            url = db.get_bind().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        receipts.append(await deliver_task_result(result.id))
        assert receipts[-1]["report_attempt"] == 2 and receipts[-1]["inbox_id"] != receipts[0]["inbox_id"]
        last_lease = await run(main.id, owner)
        assert last_lease.generation > first_lease.generation
    else:
        last_lease = first_lease
    assert execution_calls == [accepted["execution_session_id"]]
    assert set(report_calls) == ({1, 2} if retry else {1}) and all(len(calls) == 2 for calls in report_calls.values())
    assert await _execution_facts(accepted["task_id"]) == original_facts
    async with get_db_session() as db:
        saved = await db.get(TaskResult, result.id)
        assert {key: getattr(saved, key) for key in frozen} == frozen
        assert saved.delivery_state == "processed" and saved.report_attempt == (2 if retry else 1)
        assert saved.summary == ORIGINAL_REPORT
        answer = await db.get(Message, saved.processed_message_id)
        assert answer.finish == "stop"
        body = "\n".join(part.data.get("text", "") for part in (await db.scalars(select(Part).where(
            Part.message_id == answer.id, Part.type == "text"))).all())
        assert body == SUMMARY and all(detail in body for detail in REPORT_DETAILS)
        task_row = await db.get(AssistantTask, accepted["task_id"])
        revisions = {"intent": task_row.intent_revision, "control": task_row.control_revision,
                     "result_observed_intent": saved.observed_intent_revision}
    processed = await main_events(main.id, "assistant.result.processed")
    assert len(processed) == 1 and processed[0].payload["result_id"] == result.id
    assert processed[0].payload["original_report_message_id"] == frozen["result_message_id"]
    assert processed[0].payload["report_attempt"] == (2 if retry else 1)
    measured = await counts(owner, main.id)
    assert measured == {"commands": 1, "tasks": 1, "execution_sessions": 1, "submissions": 1,
        "execution_inputs": 1, "results": 1, "report_inputs": 2 if retry else 1, "processed_events": 1}
    assert await deliver_task_result(result.id) is None
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
    assert (await verify_agent_event_parity(accepted["execution_session_id"], user_id=owner)).ok
    record_property("assistant_acceptance", json.dumps({"scenario": "PA-13", "retry": retry,
        "scope": "server summary/source transport and persistence with deterministic provider; no model-quality or physical UI claim",
        "receipts": receipts, "result_id": result.id, "original_report_message_id": frozen["result_message_id"],
        "original_output_refs": frozen["output_refs"],
        "execution_run": {"run_id": execution_lease.run_id, "generation": execution_lease.generation},
        "last_report_run": {"run_id": last_lease.run_id, "generation": last_lease.generation},
        "revisions": revisions, "counts": measured, "execution_facts_unchanged": original_facts,
        "provider_payload_digests": {attempt: [command_digest(payload) for payload in calls] for attempt, calls in report_calls.items()},
        "preserved_report_details": REPORT_DETAILS, "processed_payload": processed[0].payload}))
