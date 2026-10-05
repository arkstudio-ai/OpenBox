"""PA-26: revoked accepted report sources never reach the next real provider."""
import asyncio
from dataclasses import asdict
import json

import pytest
from sqlalchemy import select

from agent import inbox, processor
from agent.recovery_service import AgentRecoveryService
from assistant.delivery import recover_assistant_results
from assistant.results import deliver_task_result
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.part import Part
from db.models.session import Session
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_report_binding_acceptance import (
    counts, main_events, read_call, result_for, run, setup, task,
)
from tests.unit.test_assistant_report_recovery_acceptance import _execution_facts


PRIVATE_BODY = "PA26_REVOKED_ORIGINAL_BODY_7426: only a text result."


@pytest.mark.parametrize("boundary", ["before-claim", "after-read", "after-projection"])
async def test_accepted_report_revocation_blocks_next_provider_and_survives_recovery(
        monkeypatch, record_property, boundary):
    owner, workspace, main = await setup(monkeypatch)
    accepted = await task(owner, workspace, main, "revoked-report",
                          "Produce a text-only report; do not perform external work.")
    calls, reads, revoked, assertions, pending_contexts = [], [], [], [], []
    result = None

    async def revoke():
        async with get_db_session() as db:
            source = await db.get(Session, accepted["execution_session_id"])
            assert source.visibility == "private"
            source.visibility = "workspace"
        revoked.append(accepted["execution_session_id"])

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        try:
            calls.append({"session_id": ctx.session_id, "run_id": ctx.run_id,
                          "generation": ctx.run_generation,
                          "contains_original_body": PRIVATE_BODY in json.dumps(kwargs["messages"])})
            if ctx.session_id != main.id:
                yield {"type": "text_delta", "text": PRIVATE_BODY}
                yield {"type": "finish", "reason": "stop", "usage": {}}
                return
            assert boundary != "before-claim" and not revoked
            assert not calls[-1]["contains_original_body"]
            yield read_call(kwargs, result.id, "pa26-read-original")
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        except (AssertionError, KeyError, TypeError) as exc:
            assertions.append(exc)
            raise

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(accepted["execution_session_id"], owner)
    result = await result_for(accepted)
    original = await _execution_facts(accepted["task_id"])
    frozen = {key: getattr(result, key) for key in ("id", "run_id", "generation", "result_message_id",
        "observed_intent_revision", "consumed_inbox_ids", "output_refs", "outcome")}
    receipt = await deliver_task_result(result.id)
    assert receipt and receipt["report_attempt"] == 1
    if boundary == "before-claim":
        await revoke()
    else:
        from tool import assistant_tools as tools_module
        real_read = tools_module.read_result_sources

        async def read_then_revoke(**kwargs):
            value = await real_read(**kwargs)
            if kwargs.get("record") and not reads:
                assert PRIVATE_BODY in json.dumps(value)
                reads.append(value["result_id"])
                if boundary == "after-read":
                    await revoke()
            return value

        monkeypatch.setattr(tools_module, "read_result_sources", read_then_revoke)
        if boundary == "after-projection":
            from assistant import projection
            real_project = projection.project_main_messages

            async def project_then_revoke(messages, **kwargs):
                projected = await real_project(messages, **kwargs)
                wire = json.dumps([message.parts for message in projected])
                if PRIVATE_BODY in wire and not revoked:
                    ctx = kwargs["ctx"]
                    assert ctx._assistant_context and reads == [result.id]
                    pending_contexts.append(ctx)
                    await revoke()
                return projected

            monkeypatch.setattr(projection, "project_main_messages", project_then_revoke)
    await run(main.id, owner)
    assert not assertions, assertions
    assert len(revoked) == 1
    assert reads == ([result.id] if boundary != "before-claim" else [])
    assert len(pending_contexts) == (1 if boundary == "after-projection" else 0)
    assert all(ctx._assistant_context is None for ctx in pending_contexts)
    expected_calls = 2 if boundary != "before-claim" else 1
    assert len(calls) == expected_calls and not any(c["contains_original_body"] for c in calls)
    # The actual loop may preserve an early failed Driver for maintenance.
    # Recreate SQL and the real recovery service; never hand-settle its Inbox.
    url = get_engine().url
    await close_engine()
    init_engine(url)
    service = AgentRecoveryService(interval_seconds=3600)
    try:
        first = await service.start()
        assert first is not None
        pending = inbox._wake_tasks.get((owner, main.id))
        if pending is not None:
            await asyncio.wait_for(asyncio.shield(pending), timeout=15)
        second = await service.run_once()
        await recover_assistant_results(result_ids=(result.id,))
        assert len(calls) == expected_calls and not assertions
        async with get_db_session() as db:
            saved = await db.get(TaskResult, result.id)
            item = await db.get(AgentInboxItem, receipt["inbox_id"])
            driver = await db.get(AgentDriverState, main.id)
            assert saved.delivery_state == "blocked" and saved.processed_message_id is None
            assert saved.report_attempt == 1 and saved.assistant_inbox_id == receipt["inbox_id"]
            assert saved.last_error_code.startswith("ASSISTANT_")
            assert item.state in {"settled", "canceled"} and item.outcome != "succeeded"
            assert driver.phase == "idle"
            assert {key: getattr(saved, key) for key in frozen} == frozen
            parts = list((await db.scalars(select(Part).where(Part.session_id == main.id))).all())
            assert PRIVATE_BODY not in json.dumps([part.data for part in parts])
            requested = list((await db.scalars(select(AgentEvent).where(
                AgentEvent.session_id == main.id, AgentEvent.kind == "model.requested"))).all())
            assert len(requested) == (1 if boundary != "before-claim" else 0)
            terminal = {"state": item.state, "outcome": item.outcome,
                        "reason": saved.last_error_code, "driver_generation": driver.generation}
        assert not await main_events(main.id, "assistant.result.processed")
        assert await _execution_facts(accepted["task_id"]) == original
        assert await deliver_task_result(result.id) is None
        third = await service.run_once()
        assert third.assistant_results_recovered == third.resumed_inbox_sessions == 0
        assert len(calls) == expected_calls
        assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
        assert (await verify_agent_event_parity(accepted["execution_session_id"], user_id=owner)).ok
        measured = await counts(owner, main.id)
        assert measured == {"commands": 1, "tasks": 1, "execution_sessions": 1, "submissions": 1,
                            "execution_inputs": 1, "results": 1, "report_inputs": 1, "processed_events": 0}
        record_property("assistant_acceptance", json.dumps({"scenario": "PA-26", "boundary": boundary,
            "task": accepted, "result_id": result.id, "receipt": receipt, "calls": calls,
            "actual_reads": reads, "terminal": terminal, "recovery": [asdict(first), asdict(second)],
            "counts": measured, "execution_facts_unchanged": original}))
    finally:
        await service.stop()
