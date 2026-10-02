"""Temporary reads never outlive current authority or enter permanent summary."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import select, update

from agent import compaction
from agent.llm import build_responses_input
from agent.loop import _to_llm_messages
from agent.processor import persisted_tool_metadata
from core import config as config_module
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.part import Part
from db.models.session import Session
from memory import service
from memory.tool_projection import revalidate_memory_tool_messages
from models.message import TextPart, ToolPartData, ToolStateCompleted
from session.agent_event_log import load_canonical_model_surface
from session.session import create_assistant_message, get_messages, save_part, update_message_info
from tests.unit.test_memory_pipeline import _finish_turn, pipeline_database  # noqa: F401
from tests.unit.test_memory_tools import manual_memory, seed_tools, tool_context
from tool import memory_tools as tools

BODY = "temporary-only-memory-content-4823"


def run_fence(lease):
    return lease.session_id, lease.run_id, lease.generation


async def tool_history(seed, operation, args):
    accepted, lease, _ = await _finish_turn(seed, release=False, memory_success=False)
    from agent.inbox import get_inbox_item
    trigger = (await get_inbox_item(accepted.id, user_id=seed[0])).message_id
    assistant = await create_assistant_message(seed[3], trigger, user_id=seed[0], run_fence=run_fence(lease))
    ctx = tool_context(seed)
    ctx.run_id, ctx.run_generation = lease.run_id, lease.generation
    ctx.message_id, ctx._assert_current = assistant.id, lease.assert_current
    executors = {"memory_search": tools.execute_memory_search,
                 "memory_read_sources": tools.execute_memory_read_sources,
                 "current_task_state": tools.execute_current_task_state}
    result = await executors[operation](args, ctx)
    metadata = persisted_tool_metadata(result.metadata)
    assert metadata["transient_memory_refs"]["logical_turn_id"] == trigger
    part = ToolPartData(tool=operation, canonical_tool_id=operation, wire_tool_name=operation,
        provider_binding_digest="f" * 64, provider_dialect="openai-responses", stream_seq=2,
        status="completed", input=args.model_dump(), output=result.output, call_id="call_memory_read",
        metadata=metadata, state=ToolStateCompleted(input=args.model_dump(), output=result.output, metadata=metadata),
        session_id=seed[3], message_id=assistant.id)
    await save_part(part, is_new=True, user_id=seed[0], run_fence=run_fence(lease))
    assistant.finish = "tool_calls"
    await update_message_info(assistant, user_id=seed[0], run_fence=run_fence(lease))
    messages = list((await load_canonical_model_surface(seed[3], user_id=seed[0], run_fence=run_fence(lease))).messages)
    return lease, ctx, messages, part, trigger


def provider_input(messages, *, native=False, verified=False):
    replay = None
    if native:
        owner = next(message for message in messages if any(
            (part.get("type") if isinstance(part, dict) else part.type) == "tool" for part in message.parts))
        replay = {owner.id: [{"stream_seq": 0, "item": {"type": "tool_search_output", "call_id": "search_1",
                   "execution": "server", "status": "completed", "tools": []}}]}
    messages = _to_llm_messages(messages, provider_replay_by_message=replay, memory_projection_verified=verified)
    return json.dumps(build_responses_input(messages) if native else messages, ensure_ascii=False)


async def raw_history_snapshot(part):
    async with get_db_session() as db:
        row = await db.get(Part, part.id)
        events = list((await db.scalars(select(AgentEvent).where(AgentEvent.part_id == part.id)
                       .order_by(AgentEvent.sequence))).all())
        return deepcopy(row.data), [deepcopy(event.payload) for event in events]


@pytest.mark.parametrize("native", [False, True])
async def test_same_turn_is_fresh_but_later_turn_and_compaction_are_citations_only(monkeypatch, native):
    seed = await seed_tools(monkeypatch)
    _, source = await manual_memory(seed, BODY)
    lease, ctx, messages, part, trigger = await tool_history(seed, "memory_read_sources", tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]))
    try:
        before = await raw_history_snapshot(part)
        fresh = await revalidate_memory_tool_messages(messages, ctx=ctx)
        assert BODY in provider_input(fresh, native=native, verified=True)
        projected_part = next(piece for message in fresh for piece in message.parts if piece.get("id") == part.id)
        item = json.loads(projected_part["output"])["items"][0]
        assert item["source_kind"] == source.source_kind
        assert item["session_id"] == source.session_id and item["message_id"] == source.message_id
        later = await revalidate_memory_tool_messages(messages, user_id=seed[0], workspace_id=seed[1],
            project_id=seed[2], session_id=seed[3], logical_turn_id="later-user-message")
        payload = provider_input(later, native=native, verified=True)
        assert BODY not in payload and source.id in payload and "previous_turn_citations_only" in payload
        compacted = await revalidate_memory_tool_messages(messages, ctx=ctx, for_compaction=True)
        payload = provider_input(compacted, native=native, verified=True)
        assert BODY not in payload and source.id in payload and "compaction_citations_only" in payload
        assert await raw_history_snapshot(part) == before
        assert trigger in part.metadata["transient_memory_refs"].values()
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("native", [False, True])
async def test_forgotten_source_cannot_reenter_current_model_or_compaction(monkeypatch, native):
    seed = await seed_tools(monkeypatch)
    memory, source = await manual_memory(seed, BODY)
    lease, ctx, messages, part, _ = await tool_history(seed, "memory_read_sources", tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]))
    try:
        before = await raw_history_snapshot(part)
        assert BODY in provider_input(await revalidate_memory_tool_messages(messages, ctx=ctx), native=native, verified=True)
        assert (await service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory["id"],
            expected_revision=memory["revision"], mode="sources", source_ids=[source.id]))["ok"]
        for for_compaction in (False, True):
            view = await revalidate_memory_tool_messages(messages, ctx=ctx, for_compaction=for_compaction)
            assert BODY not in provider_input(view, native=native, verified=True)
            projected_part = next(piece for message in view for piece in message.parts if piece.get("id") == part.id)
            assert json.loads(projected_part["output"])["references"] == []
        # Clearing source copies does not silently delete original chat events.
        assert await raw_history_snapshot(part) == before
        assert BODY in json.dumps(before, ensure_ascii=False)
    finally:
        await lease.release(session_status="idle")


async def test_corrected_memory_requires_new_exact_revision_read(monkeypatch):
    seed = await seed_tools(monkeypatch)
    memory, _ = await manual_memory(seed, BODY)
    lease, ctx, messages, part, _ = await tool_history(seed, "memory_search", tools.MemorySearchArgs(query=BODY))
    try:
        before = await raw_history_snapshot(part)
        new_body = "current-corrected-memory-7819"
        corrected = await service.edit_note(user_id=seed[0], workspace_id=seed[1], memory_id=memory["id"],
            expected_revision=memory["revision"], summary=new_body)
        assert corrected["revision"] > memory["revision"]
        view = await revalidate_memory_tool_messages(messages, ctx=ctx)
        assert BODY not in provider_input(view, verified=True)
        assert new_body not in provider_input(view, verified=True)
        new_read = await tools.execute_memory_search(tools.MemorySearchArgs(query=new_body), ctx)
        assert new_body in new_read.output and str(corrected["revision"]) in new_read.output
        assert await raw_history_snapshot(part) == before
    finally:
        await lease.release(session_status="idle")


async def test_task_projection_refreshes_business_rows_and_compaction_has_no_snapshot(monkeypatch):
    seed = await seed_tools(monkeypatch)
    lease, ctx, messages, _part, _ = await tool_history(seed, "current_task_state", tools.CurrentTaskStateArgs())
    try:
        async with get_db_session() as db:
            await db.execute(update(Session).where(Session.id == seed[3]).values(status="idle", title="task-status-refresh"))
        view = await revalidate_memory_tool_messages(messages, ctx=ctx)
        fresh = provider_input(view, verified=True)
        assert "fresh_business_sql" in fresh and "task-status-refresh" in fresh
        compacted = await revalidate_memory_tool_messages(messages, ctx=ctx, for_compaction=True)
        payload = provider_input(compacted, verified=True)
        assert "fresh_task_read_required" in payload and "task-status-refresh" not in payload
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("native", [False, True])
async def test_default_serializer_and_disabled_scope_never_trust_saved_tool_text(monkeypatch, native):
    seed = await seed_tools(monkeypatch)
    _, source = await manual_memory(seed, BODY)
    lease, ctx, messages, _part, _ = await tool_history(seed, "memory_read_sources", tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]))
    try:
        assert BODY not in provider_input(messages, native=native)
        assert "temporary_result_not_revalidated" in provider_input(messages, native=native)
        ctx.project_id = "forged-project"
        assert BODY not in provider_input(await revalidate_memory_tool_messages(messages, ctx=ctx), native=native, verified=True)
        ctx.project_id = seed[2]
        config_module.get_config().memory.retrieval_v2 = False
        assert BODY not in provider_input(await revalidate_memory_tool_messages(messages, ctx=ctx), native=native, verified=True)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("forget_source", [False, True])
async def test_real_compaction_fallback_never_sends_temporary_memory_body(monkeypatch, forget_source):
    seed = await seed_tools(monkeypatch)
    memory, source = await manual_memory(seed, BODY)
    lease, _ctx, _messages, part, trigger = await tool_history(seed, "memory_read_sources", tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]))
    final = await create_assistant_message(seed[3], trigger, user_id=seed[0], run_fence=run_fence(lease))
    await save_part(TextPart(text="工具结果已读取。", session_id=seed[3], message_id=final.id),
                    is_new=True, user_id=seed[0], run_fence=run_fence(lease))
    final.finish = "stop"
    await update_message_info(final, user_id=seed[0], run_fence=run_fence(lease))
    await lease.release(session_status="idle")
    before = await raw_history_snapshot(part)
    if forget_source:
        await service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory["id"],
            expected_revision=memory["revision"], mode="sources", source_ids=[source.id])
    assert await compaction.create_compaction(seed[3], auto=False, user_id=seed[0], model_id="test/model")
    requests = []

    async def summary_provider(**kwargs):
        requests.append(kwargs)
        assert BODY not in json.dumps(kwargs["messages"], ensure_ascii=False)
        assert "citations" in json.dumps(kwargs["messages"])
        yield {"type": "text_delta", "text": "只记录仍需读取当前授权引用。"}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 100, "output": 10}}

    monkeypatch.setattr("agent.llm.stream_llm", summary_provider)
    messages = await get_messages(seed[3], user_id=seed[0])
    result = await compaction.process_compaction(seed[3], messages, "test/model", auto=False, user_id=seed[0])
    assert result == "stop" and len(requests) == 1
    assert await raw_history_snapshot(part) == before


def test_ephemeral_projection_approval_is_never_persisted():
    assert persisted_tool_metadata({"transient_memory_refs": {"version": 1}, "_memory_projection_verified": True}) == {
        "transient_memory_refs": {"version": 1}}


async def test_external_tool_metadata_cannot_grant_native_memory_reads(monkeypatch):
    seed = await seed_tools(monkeypatch)
    _, source = await manual_memory(seed, BODY)
    lease, ctx, messages, part, _ = await tool_history(seed, "memory_read_sources", tools.MemoryReadSourcesArgs(
        sources=[{"source_id": source.id, "revision": source.source_revision}]))
    try:
        external = deepcopy(messages)
        for message in external:
            message.parts = [piece.model_dump() if hasattr(piece, "model_dump") else deepcopy(piece) for piece in message.parts]
            for piece in message.parts:
                if piece.get("id") == part.id:
                    # Canonical identity belongs to the registry, not the
                    # external tool's claimed transient-reference metadata.
                    piece["tool"] = "memory_read_sources"
                    piece["canonical_tool_id"] = "mcp:external_memory_like_tool"
        projected = await revalidate_memory_tool_messages(external, ctx=ctx)
        payload = provider_input(projected, verified=True)
        assert BODY not in payload and "unversioned_temporary_result" in payload
    finally:
        await lease.release(session_status="idle")
