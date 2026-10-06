"""Transitive context provenance through the real checkpoint/response/commit path."""
from dataclasses import replace
import json

import pytest
from sqlalchemy import event, select

from agent import inbox
from agent.driver import reserve_run
from agent.loop import _to_llm_messages
from assistant.context_sources import record_provider_context
from assistant.evidence import validate_message_sources
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.projection import project_main_messages
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from models.message import TextPart
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn


async def finish(ctx, lease, message, text):
    part = TextPart(session_id=ctx.session_id, message_id=message.id, text=text)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    message.finish = "stop"
    await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    await lease.release(session_status="idle")
    return part


async def next_turn(ctx, prompt="Restate the previous answer without reading another tool."):
    await inbox.accept_inbox_item(session_id=ctx.session_id, user_id=ctx.user_id, delivery="followup",
        prompt=prompt, origin="human", origin_ref={"actor_user_id": ctx.user_id})
    lease = await reserve_run(ctx.session_id, ctx.user_id)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    fence = (ctx.session_id, lease.run_id, lease.generation)
    message = await create_assistant_message(ctx.session_id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=ctx.user_id, run_fence=fence)
    return replace(ctx, message_id=message.id, part_id="", run_id=lease.run_id, run_generation=lease.generation), lease, message


@pytest.mark.parametrize("during_run", [False, True])
async def test_replayed_answer_cannot_launder_revoked_evidence_into_a_later_answer_or_provider(during_run):
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        await consume_context(ctx)
        original_part = await finish(ctx, lease, answer, "PRIVATE_DERIVED_REPORT")
        ctx, lease, derived = await next_turn(ctx)
        active = derived
        payload = await consume_context(ctx)
        assert "PRIVATE_DERIVED_REPORT" in json.dumps(payload)
        assert any(ref["part_id"] == original_part.id for ref in ctx._assistant_context["source_refs"])
        if not during_run:
            await finish(ctx, lease, derived, "PRIVATE_SECOND_GENERATION_REPORT")
            async with get_db_session() as db:
                await validate_message_sources(db, derived, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            ctx, lease, active = await next_turn(ctx, "What is currently verified?")
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "Replaced original evidence"}
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        if during_run:
            with pytest.raises(AssistantError) as invalid:
                await project_main_messages(list(surface.messages), ctx=ctx)
            assert invalid.value.code == "ASSISTANT_SOURCE_CHANGED"
        else:
            with pytest.raises(AssistantError):
                await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
                    session_id=ctx.session_id, message_ids=[derived.id])
            projected = await project_main_messages(list(surface.messages), ctx=ctx)
            assert "PRIVATE_" not in json.dumps(_to_llm_messages(projected, assistant_projection_verified=True))
        active.finish, active.error = "error", {"name": "SourceChanged", "message": "The original source changed"}
        await update_message_info(active, user_id=ctx.user_id, run_fence=ctx.run_fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=active.id, outcome="error")
        await lease.release(session_status="idle")
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
    finally:
        await lease.release(session_status="idle")


async def test_a_frozen_old_answer_is_not_bound_to_a_newer_sql_hash():
    ctx, lease, answer, _, _ = await read_turn()
    try:
        await consume_context(ctx)
        part = await finish(ctx, lease, answer, "PRIVATE_OLD_TEXT")
        ctx, lease, _ = await next_turn(ctx)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with get_db_session() as db:
            source = await db.get(Part, part.id)
            source.data = {**source.data, "text": "New SQL text"}
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        assert "PRIVATE_OLD_TEXT" not in json.dumps(_to_llm_messages(projected, assistant_projection_verified=True))
        assert not any(ref["part_id"] == part.id for ref in ctx._assistant_context["source_refs"])
    finally:
        await lease.release(session_status="idle")


async def test_preflight_and_mismatched_payload_do_not_certify_an_answer():
    ctx, lease, answer, _, _ = await read_turn()
    try:
        await consume_context(ctx, respond=False)
        with pytest.raises(AssistantError) as mismatch:
            await record_provider_context(ctx, [{"role": "user", "content": "Uncheckpointed text"}])
        assert mismatch.value.code == "ASSISTANT_CONTEXT_UNVERIFIED"
        await finish(ctx, lease, answer, "Unverified answer")
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            manifest = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == answer.id,
                AgentEvent.kind == "assistant.message.committed"))
            assert manifest.payload["provenance_version"] == 2 and manifest.payload["context_verified"] is False
    finally:
        await lease.release(session_status="idle")


async def test_verified_source_chain_survives_more_than_four_turns_and_legacy_manifest_is_not_upgraded():
    ctx, lease, answer, _, _ = await read_turn()
    try:
        for index in range(7):
            payload = await consume_context(ctx)
            if index:
                assert f"Verified answer {index - 1}" in json.dumps(payload)
            await finish(ctx, lease, answer, f"Verified answer {index}")
            async with get_db_session() as db:
                await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            if index < 6:
                ctx, lease, answer = await next_turn(ctx)
        async with get_db_session() as db:
            manifest = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == answer.id,
                AgentEvent.kind == "assistant.message.committed"))
            manifest.payload = {key: value for key, value in manifest.payload.items() if key != "provenance_version"}
        async with get_db_session() as db:
            row = await db.get(Message, answer.id)
            with pytest.raises(AssistantError) as legacy:
                await validate_message_sources(db, row, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            assert legacy.value.code == "ASSISTANT_SOURCE_UNVERIFIED"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("character,length,over_budget", [("x", 32000, False), ("\u0001", 16000, True)])
async def test_current_input_is_preserved_or_rejected_without_silently_truncating_constraints(character, length, over_budget):
    ctx, lease, answer, _, _ = await read_turn()
    try:
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Ready for the next request")
        prompt = character * length + "\nKeep this final constraint: do not publish anything."
        ctx, lease, _ = await next_turn(ctx, prompt)
        if over_budget:
            with pytest.raises(AssistantError) as oversized:
                await consume_context(ctx)
            assert oversized.value.code == "ASSISTANT_CONTEXT_BUDGET"
            assert ctx._assistant_context is None
        else:
            assert prompt in json.dumps(await consume_context(ctx)).replace("\\n", "\n")
    finally:
        await lease.release(session_status="idle")


async def test_changed_current_input_stops_instead_of_replacing_it_with_a_placeholder():
    ctx, lease, answer, _, _ = await read_turn()
    try:
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == answer.parent_id, Part.type == "text"))
            source.data = {**source.data, "text": "Changed current input"}
        with pytest.raises(AssistantError) as changed:
            await project_main_messages(list(surface.messages), ctx=ctx)
        assert changed.value.code == "ASSISTANT_SOURCE_CHANGED"
        assert ctx._assistant_context is None
    finally:
        await lease.release(session_status="idle")


async def test_multipart_answer_checks_its_source_graph_once_per_snapshot_and_rechecks_after_change(monkeypatch):
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {
            "session_id": accepted["execution_session_id"], "message_ids": [report.id],
        })
        await consume_context(ctx)
        answer_parts = []
        for index in range(3):
            part = TextPart(session_id=ctx.session_id, message_id=answer.id,
                text=f"PRIVATE_MULTIPART_ANSWER_{index}")
            await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
            answer_parts.append(part.id)
        answer_parts.append((await finish(ctx, lease, answer, "PRIVATE_MULTIPART_ANSWER_END")).id)
        ctx, lease, _ = await next_turn(ctx)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        manifest_reads = []

        def count_manifest_reads(connection, cursor, statement, parameters, context, executemany):
            values = parameters.values() if isinstance(parameters, dict) else parameters
            if "assistant.message.committed" in values and answer.id in values:
                manifest_reads.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count_manifest_reads)
        try:
            # Counts sharing within one snapshot; a reused verdict's own
            # capture is a separate read (test_assistant_evidence_cache).
            with monkeypatch.context() as uncached:
                uncached.setenv("ASSISTANT_EVIDENCE_CACHE", "off")
                projected = await project_main_messages(list(surface.messages), ctx=ctx)
        finally:
            event.remove(engine, "before_cursor_execute", count_manifest_reads)
        payload = json.dumps(_to_llm_messages(projected, assistant_projection_verified=True))
        assert all(f"PRIVATE_MULTIPART_ANSWER_{i}" in payload for i in range(3))
        assert "PRIVATE_MULTIPART_ANSWER_END" in payload
        assert len(manifest_reads) == 1, "Each answer body must reuse its already checked source graph"

        # Reuse is limited to that SQL snapshot. A later provider step must
        # notice a revoked transitive source and hide every part of the answer.
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "The original evidence changed"}
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        assert "PRIVATE_MULTIPART_ANSWER" not in json.dumps(
            _to_llm_messages(projected, assistant_projection_verified=True))
        assert not set(answer_parts) & {ref["part_id"] for ref in ctx._assistant_context["source_refs"]}
    finally:
        await lease.release(session_status="idle")
