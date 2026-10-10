"""PA-24/30: durable isolation through real assistant, Task and cron loops.

The external model and sandbox I/O are deterministic. Inbox, tools, Question
continuation, subagent admission/dispatch, processor/checkpoints and memory
services use their real implementations and the same SQL database. This does
not reopen the legacy memory paths for assistant-isolated sessions.
"""
from collections import Counter
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant.evidence import projection_digest
from assistant.service import ensure_main_session
from cron.validation import ensure_not_cron_session
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.cron import CronJob, CronRun
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionJob, MemoryTurnCompletion
from db.models.memory_v2 import MemoryRevision, MemorySource
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob
from db.models.part import Part
from db.models.session import Session
from db.models.subagent import SubagentActivation, SubagentDescriptor, SubagentOutbox
from memory import context, orchestrator, service
from memory.extraction import MemoryExtractionWorker
from memory.session_policy import MEMORY_CAPABILITIES
from question import question as questions
from question.continuation import apply_answers
from session.agent_event_log import verify_agent_event_parity
from session.session import create_session
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools
from tool.batch import batch_tool
from tool.creator_context import creator_context_tool
from tool.memory_tools import (current_task_state_tool, memory_forget_tool,
                               memory_read_sources_tool, memory_search_tool)
from tool.plan import plan_enter_tool
from tool.task import task_tool


CANARY = "PERSONAL_CONTEXT_CANARY_CHAIN_8142"
HUMAN_PREFERENCES = {"build": "我喜欢简短的中文回复", "plan": "我喜欢分步骤的中文说明"}
FORGED_MEMORY = "AUTOMATED_UNTRUSTED_MEMORY_CHAIN_8142"


async def _memory_counts(owner):
    async with get_db_session() as db:
        return {model.__name__: await db.scalar(select(func.count()).select_from(model).where(
                    model.user_id == owner)) for model in (
            UserMemory, MemoryRevision, MemorySource, MemoryTurnCompletion,
            MemoryExtractionJob, MemoryWikiJob, MemoryWikiCandidate)}


def _call(name, args, call_id):
    return {"type": "tool_call", "tool": name, "args": args,
            "call_id": call_id, "invalid": False}


@pytest.mark.parametrize("retrieval_v2", [False, True], ids=["legacy-context", "v2-prefetch"])
async def test_task_plan_child_and_managed_cron_keep_memory_isolation(
        monkeypatch, record_property, retrieval_v2):
    owner, _, workspace = await accounts()
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.memory = config.memory.model_copy(update={
        "allowed_user_ids": [owner], "retrieval_v2": retrieval_v2,
        "auto_extract": True, "v2_write": True, "wiki": True,
        "automatic_knowledge": False, "route_jev": False, "debug_view": False,
    })
    # Restore these real provider-boundary functions after the shared fixture.
    real_prompt, real_history = loop._build_system_prompt, loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_build_system_prompt", real_prompt)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)

    async def no_title(*args, **kwargs):
        return None

    monkeypatch.setattr(loop, "_ensure_title", no_title)
    monkeypatch.setattr("assistant.schedule_commands.schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr("assistant.schedule_runs.schedule_inbox_wake", lambda *args: None)
    monkeypatch.setattr("cron.service.arm_timer", lambda *args: None)
    # A controlled installed catalogue, with real tool handlers. Including an
    # alias proves that filtering checks canonical IDs, not only dictionary keys.
    ordinary_tools = {tool.id: tool for tool in (
        task_tool, batch_tool, plan_enter_tool, creator_context_tool,
        memory_read_sources_tool, current_task_state_tool, memory_forget_tool)}
    ordinary_tools["masked_memory_lookup"] = memory_search_tool

    async def installed_tools(agent, *args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}
            if agent.name == "assistant" else dict(ordinary_tools),
            catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", installed_tools)
    prefetches, legacy_reads, prefetch_errors = [], [], []
    real_prefetch, real_legacy = orchestrator.run_memory_context, context.assemble_user_context

    async def observed_prefetch(*args, **kwargs):
        prefetches.append(kwargs["session_id"])
        try:
            return await real_prefetch(*args, **kwargs)
        except Exception as exc:
            prefetch_errors.append(exc)
            raise

    async def observed_legacy(*args, **kwargs):
        legacy_reads.append(kwargs["user_id"])
        return await real_legacy(*args, **kwargs)

    monkeypatch.setattr(orchestrator, "run_memory_context", observed_prefetch)
    monkeypatch.setattr(context, "assemble_user_context", observed_legacy)
    note = await service.create_note(user_id=owner, workspace_id=workspace,
        summary=CANARY, request_id="chain-canary")
    baseline = await _memory_counts(owner)
    assert baseline["UserMemory"] == baseline["MemoryRevision"] == baseline["MemorySource"] == 1
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    request_text = ("Create a task to plan and ask a child to check the proposal. "
                    "Also create a disabled ten-minute schedule and run it once now.")
    human = await inbox.accept_inbox_item(session_id=main.id, user_id=owner,
        delivery="followup", agent="assistant", prompt=request_text,
        origin="human", origin_ref={"actor_user_id": owner})
    calls, counts, ordinary_ids, receipts, ordinary_inputs = [], Counter(), set(), {}, {}

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        counts[ctx.session_id] += 1
        step = counts[ctx.session_id]
        tools = {tool.id: wire for wire, tool in kwargs["tools"].items()}
        payload = json.dumps({"system": kwargs["system"], "messages": kwargs["messages"]},
                             ensure_ascii=False)
        is_ordinary = ctx.session_id in ordinary_ids
        if ctx.session_id == main.id:
            # V2 P3: the assistant reads the user's profile and relevant
            # memories every turn (retrieval_v2 only); it keeps its own tools.
            assert (CANARY in payload) is retrieval_v2
            assert (CANARY in json.dumps(kwargs["messages"])) is retrieval_v2
            assert CANARY not in json.dumps(kwargs["system"])
            assert not MEMORY_CAPABILITIES.intersection(tools)
        elif is_ordinary:
            assert CANARY in payload
            assert MEMORY_CAPABILITIES <= set(tools)
            assert (CANARY in json.dumps(kwargs["messages"])) is retrieval_v2
            assert (CANARY in json.dumps(kwargs["system"])) is not retrieval_v2
        else:
            assert CANARY not in payload
            assert not MEMORY_CAPABILITIES.intersection(tools)
            assert "masked_memory_lookup" not in kwargs["tools"]
            assert "<memory_context>" not in payload and "<user_memory>" not in payload
        calls.append({"session_id": ctx.session_id, "agent": ctx.agent_id,
            "run_id": ctx.run_id, "generation": ctx.run_generation,
            "memory_present": CANARY in payload, "tools": sorted(tools),
            "messages_digest": projection_digest(kwargs["messages"])})
        if ctx.session_id == main.id:
            source_id = re.findall(r"Original human message_id=([^\]]+)", payload)[-1]
            if step == 1:
                yield _call(tools["tasks.submit"], {"project_id": main.project_id,
                    "title": "Isolation chain", "instructions": "Check the proposal, including one child.",
                    "source_message_ids": [source_id]}, "chain-task")
            elif step == 2:
                yield _call(tools["schedules.create"], {"project_id": main.project_id,
                    "name": "Isolation scheduled check", "instructions": "Check the scheduled proposal.",
                    "schedule": {"kind": "every", "every_ms": 600000}, "enabled": False,
                    "source_message_ids": [source_id]}, "chain-schedule")
            elif step == 3:
                async with get_db_session() as db:
                    job = await db.scalar(select(CronJob).where(CronJob.assistant_session_id == main.id))
                    assert job is not None and job.id in payload
                yield _call(tools["schedules.run"], {"job_id": job.id, "expected_revision": 1,
                    "source_message_ids": [source_id]}, "chain-schedule-run")
            else:
                assert step == 4
                yield {"type": "text_delta", "text": "The task and one scheduled run are accepted."}
                yield {"type": "finish", "reason": "stop", "usage": {}}
                return
        elif ctx.session_id == receipts["task"]["execution_session_id"]:
            if step == 1:
                assert ctx.agent_id == "build"
                yield _call(tools["batch"], {"invocations": [
                    {"tool": "creator_context", "parameters": {"action": "get_user_context"}},
                    {"tool": "creator_context", "parameters": {"action": "write_memory",
                        "value": {"summary": FORGED_MEMORY}, "owner": "USER_CONFIRMED"}},
                    {"tool": "creator_context", "parameters": {"action": "propose_memory", "summary": FORGED_MEMORY}},
                    {"tool": "masked_memory_lookup", "parameters": {"query": "remember"}},
                    {"tool": "memory_forget", "parameters": {"memory_id": note["id"], "expected_revision": 1}},
                ]}, "chain-denied-memory")
            elif step == 2:
                assert ctx.agent_id == "build"
                yield _call(tools["plan_enter"], {}, "chain-plan-enter")
            elif step == 3:
                assert ctx.agent_id == "plan"
                yield _call(tools["task"], {"description": "Child isolation check",
                    "prompt": "Check the proposal and give a short answer.",
                    "subagent_type": "general", "tools": []}, "chain-child")
            else:
                assert step == 4 and ctx.agent_id == "plan"
                assert "Child checked the proposal" in payload
                yield {"type": "text_delta", "text": "The proposal was checked; planning is complete."}
                yield {"type": "finish", "reason": "stop", "usage": {}}
                return
        elif ctx.session_id == receipts["cron"]["execution_session_id"]:
            assert step == 1 and ctx.agent_id == "build"
            yield {"type": "text_delta", "text": "The scheduled check is complete."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
            return
        elif is_ordinary:
            assert step == 1 and ctx.agent_id in {"build", "plan"}
            yield {"type": "text_delta", "text": "好的。"}
            yield {"type": "finish", "reason": "stop", "usage": {}}
            return
        else:
            assert step == 1 and ctx.agent_id == "general"
            yield {"type": "text_delta", "text": "Child checked the proposal."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
            return
        yield {"type": "finish", "reason": "tool_calls", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)

    async def run(session_id):
        lease = await reserve_run(session_id, owner)
        try:
            result = await loop.run_loop(session_id, user_id=owner, lease=lease)
            if prefetch_errors:
                raise prefetch_errors[0]
            return result
        finally:
            # Idempotent: the loop releases its own lease, including suspension.
            await lease.release(session_status="idle")

    await run(main.id)
    async with get_db_session() as db:
        commands = list((await db.scalars(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == owner))).all())
        assert len(commands) == 3
        receipts["cron"] = dict(next(row.receipt for row in commands if row.action == "schedule_run"))
        receipts["task"] = dict(next(row.receipt for row in commands if "task_id" in row.receipt
                                    and "cron_run_id" not in row.receipt))
        assert len(receipts) == 2
        for receipt in receipts.values():
            session = await db.get(Session, receipt["execution_session_id"])
            assert session.parent_id is None and session.kind == "normal"
            assert session.memory_policy == "assistant_isolated" and session.visibility == "private"
            accepted = await db.get(AgentInboxItem, receipt["inbox_id"])
            assert accepted.origin == "assistant_delegation" and accepted.state == "accepted"
            assert accepted.origin_ref["command_id"] == receipt["command_id"]
    task_id, cron_id = (receipts[name]["execution_session_id"] for name in ("task", "cron"))
    await run(task_id)
    pending = await questions.list_pending(owner)
    assert len(pending) == 1 and pending[0].session_id == task_id
    request = pending[0]
    await questions.reply(request.id, [["Yes"]], owner, reply_id="chain-plan-yes",
        expected_request_revision=request.assistant["request_revision"],
        options_hash=request.assistant["options_hash"], source_ref={"kind": "card"})
    generation = await apply_answers(task_id, owner)
    assert generation == request.generation
    # The resumed plan and not-yet-started cron must use the persisted policy.
    database_url = get_engine().url
    await close_engine()
    init_engine(database_url)
    await loop.run_loop(task_id, user_id=owner, expected_generation=generation)
    await run(cron_id)

    async with get_db_session() as db:
        descriptor = (await db.scalars(select(SubagentDescriptor).where(
            SubagentDescriptor.parent_session_id == task_id))).one()
        activation = (await db.scalars(select(SubagentActivation).where(
            SubagentActivation.descriptor_id == descriptor.id))).one()
        outbox = await db.get(SubagentOutbox, activation.id)
        assert activation.state == "completed" and activation.child_run_id and activation.child_generation
        assert outbox.outcome == "succeeded" and outbox.result_payload
        child_id = activation.child_session_id
        child = await db.get(Session, child_id)
        assert child.parent_id == task_id and child.memory_policy == "assistant_isolated"
        assert child.visibility == "private"
        child_part = await db.get(Part, activation.parent_part_id)
        assert child_part.data["status"] == "completed"
        batch = (await db.scalars(select(Part).where(Part.session_id == task_id,
            Part.type == "tool"))).all()
        denied = next(part for part in batch if part.data.get("tool") == "batch")
        assert denied.data["status"] == "error"
        assert denied.data["error"].count("Nested tool unavailable") == 4
        assert denied.data["error"].count("Nested source read unavailable") == 1
        assert not await questions.list_pending(owner)
        results = list((await db.scalars(select(TaskResult).join(AssistantTask,
            AssistantTask.id == TaskResult.task_id).where(AssistantTask.user_id == owner))).all())
        assert len(results) == 2 and {row.task_id for row in results} == {r["task_id"] for r in receipts.values()}
        assert all(row.outcome == "succeeded" and row.observed_intent_revision == 1 for row in results)
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(
            TaskSubmission.task_id.in_([row.task_id for row in results]))) == 2
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == owner)) == 2
        cron = await db.get(CronRun, receipts["cron"]["cron_run_id"])
        assert cron.status == "ok" and cron.ended_at is not None
        assert cron.assistant_task_id == receipts["cron"]["task_id"] and cron.temp_session_id is None
        job = await db.get(CronJob, cron.job_id)
        assert job.total_runs == 1 and job.revision == 1
        for accepted_id in [human.id, *(receipt["inbox_id"] for receipt in receipts.values())]:
            accepted = await db.get(AgentInboxItem, accepted_id)
            assert accepted.state == "settled" and accepted.outcome == "succeeded"
            assert accepted.result_message_id and accepted.run_id and accepted.generation

    isolated_ids = {main.id, task_id, cron_id, child_id}
    assert {call["session_id"] for call in calls} == isolated_ids
    assert [call["agent"] for call in calls if call["session_id"] == task_id] == ["build", "build", "plan", "plan"]
    # Only the assistant's own turns prefetch (V2 P3); isolated sessions never do.
    assert legacy_reads == [] and set(prefetches) <= {main.id} and bool(prefetches) is retrieval_v2
    # V2 P3: what the person says in the main session is extracted, as personal
    # memory (one receipt and job for its human turn). The isolated Task, child
    # and cron sessions still produce nothing.
    assert await _memory_counts(owner) == {**baseline, "MemoryTurnCompletion": 1, "MemoryExtractionJob": 1}
    async with get_db_session() as db:
        main_receipt = await db.scalar(select(MemoryTurnCompletion).where(MemoryTurnCompletion.user_id == owner))
        assert main_receipt.session_id == main.id and main_receipt.project_id is None
    extractor_calls = []

    def extract(frozen):
        extractor_calls.append(frozen)
        if frozen.session_id == main.id:
            # Only the person's own request reaches extraction; it states no lasting fact.
            assert [source["body"] for source in frozen.sources] == [request_text]
            return {"candidates": []}
        assert frozen.session_id in ordinary_ids
        original = ordinary_inputs[frozen.session_id]
        assert [source["body"] for source in frozen.sources] == [original]
        return {"candidates": [{"type": "PREFERENCE", "summary": "用户" + original[1:],
            "fact_key": None, "confidence": 60, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": original}]}]}

    worker = MemoryExtractionWorker(extractor=extract)
    assert await worker.run_once() == "SUCCEEDED"
    assert [call.session_id for call in extractor_calls] == [main.id]
    assert await worker.run_once() is None
    # Legacy cron creation is deliberately refused for every assistant lineage;
    # the supported scheduled path above is the main assistant command service.
    for session_id in isolated_ids:
        with pytest.raises(ValueError):
            await ensure_not_cron_session(session_id)

    normal_receipts = []
    for agent in ("build", "plan"):
        ordinary = await create_session(user_id=owner, workspace_id=workspace,
            project_id=main.project_id, agent=agent, model=config.model)
        ordinary_ids.add(ordinary.id)
        ordinary_inputs[ordinary.id] = HUMAN_PREFERENCES[agent]
        assert ordinary.memory_policy == "standard"
        accepted = await inbox.accept_inbox_item(session_id=ordinary.id, user_id=owner,
            delivery="followup", prompt=HUMAN_PREFERENCES[agent], agent=agent,
            origin="human", origin_ref={"actor_user_id": owner})
        normal_receipts.append(accepted.id)
        await run(ordinary.id)
        assert await worker.run_once() == "SUCCEEDED"
    assert len(extractor_calls) == 3
    assert set(prefetches) == ((ordinary_ids | {main.id}) if retrieval_v2 else set())
    assert len(legacy_reads) == (0 if retrieval_v2 else 2)
    async with get_db_session() as db:
        main_job = await db.scalar(select(MemoryExtractionJob).where(MemoryExtractionJob.session_id == main.id))
        assert main_job.state == "SUCCEEDED" and main_job.result_memory_ids == []
        completions = list((await db.scalars(select(MemoryTurnCompletion).where(
            MemoryTurnCompletion.user_id == owner, MemoryTurnCompletion.session_id != main.id))).all())
        jobs = list((await db.scalars(select(MemoryExtractionJob).where(
            MemoryExtractionJob.user_id == owner, MemoryExtractionJob.session_id != main.id))).all())
        assert len(completions) == len(jobs) == 2
        assert {row.session_id for row in completions} == ordinary_ids
        assert {row.completion_id for row in jobs} == {row.id for row in completions}
        assert all(row.state == "SUCCEEDED" and row.attempts == 1 and row.lease_generation == 1
                   and len(row.result_memory_ids) == 1 for row in jobs)
        assert all(row.ordinal == 1 and row.run_id and row.run_generation and row.source_boundaries
                   for row in completions)
        for accepted_id in normal_receipts:
            accepted = await db.get(AgentInboxItem, accepted_id)
            completion = next(row for row in completions if row.session_id == accepted.session_id)
            assert (completion.logical_turn_id, completion.run_id, completion.run_generation,
                    completion.result_message_id) == (accepted.turn_id, accepted.run_id,
                    accepted.generation, accepted.result_message_id)
        candidate_ids = [row.result_memory_ids[0] for row in jobs]
        candidates = list((await db.scalars(select(UserMemory).where(UserMemory.id.in_(candidate_ids)))).all())
        assert len(candidates) == 2 and all(row.status == "CANDIDATE" and row.revision == 1 for row in candidates)
        sources = list((await db.scalars(select(MemorySource).where(
            MemorySource.user_id == owner, MemorySource.session_id.isnot(None)))).all())
        assert len(sources) == 2 and {row.session_id for row in sources} == ordinary_ids
        assert all(row.source_kind == "user_statement" and row.body == ordinary_inputs[row.session_id]
                   for row in sources)
        assert (await db.get(UserMemory, note["id"])).revision == 1
        for session_id in isolated_ids | ordinary_ids:
            events = list((await db.scalars(select(AgentEvent).where(
                AgentEvent.session_id == session_id, AgentEvent.kind == "model.requested"))).all())
            session_calls = [call for call in calls if call["session_id"] == session_id]
            assert len(events) == len(session_calls)
            assert {(event.run_id, event.generation) for event in events} == {
                (call["run_id"], call["generation"]) for call in session_calls}
            if session_id == main.id:
                # V2 checkpoints record only the turn mode; nothing is re-validated or consumed later.
                assert [event.payload["assistant_context"] for event in events] == [
                    {"version": 2, "mode": "ordinary"}] * len(session_calls)
    for session_id in isolated_ids | ordinary_ids:
        assert (await verify_agent_event_parity(session_id, user_id=owner)).ok
    record_property("chain_evidence", json.dumps({
        "main_id": main.id, "task_receipt": receipts["task"], "cron_receipt": receipts["cron"],
        "question_id": request.id, "question_generation": generation,
        "child_activation_id": activation.id, "child_id": child_id,
        "child_run_id": activation.child_run_id, "child_generation": activation.child_generation,
        "provider_calls": calls, "isolated_memory_counts": baseline,
        "standard_completion_ids": [row.id for row in completions],
        "standard_jobs": [{"id": row.id, "attempts": row.attempts,
                           "lease_generation": row.lease_generation, "memory_ids": row.result_memory_ids} for row in jobs],
        "standard_source_ids": [row.id for row in sources],
    }, ensure_ascii=False))
