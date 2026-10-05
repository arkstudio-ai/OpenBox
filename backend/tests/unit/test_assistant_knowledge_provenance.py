"""Knowledge titles retain source authority through actual assistant checkpoints."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import func, select

from agent.driver import reserve_run
from agent.loop import _to_llm_messages
from assistant import business_context
from assistant.context_sources import record_provider_context
from assistant.evidence import projection_digest, validate_message_sources
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.projection import project_main_messages
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.memory_v2 import MemorySource
from db.models.part import Part
from db.models.session import Session
from session.agent_event_log import AgentEventPrefixDriftError, checkpoint_model_request, load_canonical_model_surface
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_knowledge import page_for, seed
from tests.unit.test_assistant_reads import call_tool
from tool.tool import ToolContext


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


async def start(monkeypatch, *, title="PRIVATE_KNOWLEDGE_TITLE", project=False):
    identity, _, projects, _ = await seed(monkeypatch)
    async with get_db_session() as db:
        main = await db.get(Session, identity["main_id"])
    ctx = ToolContext(user_id=main.user_id, workspace_id=main.workspace_id, session_id=main.id,
                      project_id=main.project_id, agent_id="assistant")
    ctx, lease, message = await next_turn(ctx, "Consult my knowledge titles for a text-only task. Do not publish.")
    page = (await page_for(identity, projects[0] if project else None, title))[0] if title else None
    return ctx, lease, message, identity, projects, page


async def revoke(page):
    async with get_db_session() as db:
        source = await db.get(MemorySource, page.source_manifest[0]["id"])
        source.status = "REVOKED"


async def events(ctx, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == kind).order_by(AgentEvent.sequence))).all())


async def projected_request(ctx):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = await project_main_messages(list(surface.messages), ctx=ctx)
    messages = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
    ctx._assistant_context["messages_digest"] = projection_digest(messages)
    return surface, messages


async def checkpoint(ctx, surface):
    return await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id=f"knowledge:{ctx.message_id}:{surface.event_sequence}", model_id="test/model",
        provider_binding_digest="a" * 64, tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=ctx.message_id, assistant_context=ctx._assistant_context)


async def test_directory_tool_binds_exact_request_and_response_without_persisting_tool_body(monkeypatch):
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    try:
        result, _, part = await call_tool(ctx, "knowledge.directory", {})
        assert not result.metadata.get("error"), result.output
        assert "PRIVATE_KNOWLEDGE_TITLE" in result.output and "PRIVATE_BODY_" not in result.output
        async with get_db_session() as db:
            stored = await db.get(Part, part.id)
            assert "PRIVATE_KNOWLEDGE_TITLE" not in json.dumps(stored.data)
            assert stored.data["metadata"]["transient_assistant_refs"]["operation"] == "knowledge.directory"
            main = await db.get(Session, ctx.session_id)
            assert main.memory_policy == "assistant_isolated"
        wire = await consume_context(ctx)
        assert "PRIVATE_KNOWLEDGE_TITLE" in json.dumps(wire) and "PRIVATE_BODY_" not in json.dumps(wire)
        requested = (await events(ctx, "model.requested"))[-1]
        assert requested.payload["assistant_context"]["messages_digest"] == projection_digest(wire)
        observed = requested.payload["assistant_context"]["business_reads"]
        assert len(observed) == 1 and observed[0]["operation"] == "knowledge.directory"
        assert observed[0]["projection"]["items"][0]["source_ref"]["id"] == page.id
        consumed = (await events(ctx, "assistant.context.consumed"))[-1]
        assert consumed.payload["request_sequence"] == requested.sequence
        await finish(ctx, lease, answer, "I found PRIVATE_KNOWLEDGE_TITLE; its document body was not read.")
        committed = (await events(ctx, "assistant.message.committed"))[-1]
        assert committed.payload["context_verified"] is True
        assert committed.payload["business_reads"] == observed
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["source", "empty_inventory"])
async def test_capture_to_actual_checkpoint_rechecks_title_sources_and_empty_inventory(monkeypatch, change):
    ctx, lease, _, identity, _, page = await start(monkeypatch, title=None if change == "empty_inventory" else "PRIVATE_TITLE")
    try:
        result, _, _ = await call_tool(ctx, "knowledge.directory", {})
        assert not result.metadata.get("error"), result.output
        surface, _ = await projected_request(ctx)
        if page:
            await revoke(page)
        else:
            await page_for(identity, None, "NEWLY_VISIBLE_TITLE")
        with pytest.raises(AgentEventPrefixDriftError if change == "empty_inventory" else AssistantError):
            await checkpoint(ctx, surface)
        assert not await events(ctx, "model.requested")
        assert not await events(ctx, "assistant.context.consumed")
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("during_run", [False, True])
async def test_knowledge_derived_answer_and_second_generation_cannot_survive_revocation(monkeypatch, during_run):
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        await finish(ctx, lease, answer, "PRIVATE_DERIVED_TITLE")
        ctx, lease, derived = await next_turn(ctx)
        assert "PRIVATE_DERIVED_TITLE" in json.dumps(await consume_context(ctx))
        if not during_run:
            await finish(ctx, lease, derived, "PRIVATE_SECOND_GENERATION_TITLE")
            ctx, lease, _ = await next_turn(ctx)
        await revoke(page)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        if during_run:
            with pytest.raises(AssistantError):
                await project_main_messages(list(surface.messages), ctx=ctx)
        else:
            with pytest.raises(AssistantError):
                await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
                    session_id=ctx.session_id, message_ids=[answer.id, derived.id])
            projected = await project_main_messages(list(surface.messages), ctx=ctx)
            assert "PRIVATE_" not in json.dumps(_to_llm_messages(projected, assistant_projection_verified=True))
    finally:
        await lease.release(session_status="idle")


async def test_inflight_revocation_does_not_certify_or_replay_a_late_answer(monkeypatch):
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        await revoke(page)
        await finish(ctx, lease, answer, "PRIVATE_LATE_ANSWER")
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx)
        assert "PRIVATE_LATE_ANSWER" not in json.dumps(await consume_context(ctx))
    finally:
        await lease.release(session_status="idle")


async def test_preflight_and_wrong_provider_payload_cannot_certify_knowledge_answer(monkeypatch):
    ctx, lease, answer, _, _, _ = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx, respond=False)
        with pytest.raises(AssistantError):
            await record_provider_context(ctx, [{"role": "user", "content": "Substituted title"}])
        await finish(ctx, lease, answer, "PRIVATE_UNCONSUMED_ANSWER")
        committed = (await events(ctx, "assistant.message.committed"))[-1]
        assert committed.payload["context_verified"] is False
        assert not await events(ctx, "assistant.context.consumed")
    finally:
        await lease.release(session_status="idle")


async def test_public_history_rehydrates_old_pages_before_display_or_copy(monkeypatch):
    from assistant.public_history import public_messages
    from session.session import get_messages, get_session
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        await finish(ctx, lease, answer, "PRIVATE_PUBLIC_ANSWER")
        main = await get_session(ctx.session_id, user_id=ctx.user_id)
        frozen = await get_messages(ctx.session_id, user_id=ctx.user_id)
        assert "PRIVATE_PUBLIC_ANSWER" in json.dumps(await public_messages(main, frozen, actor_user_id=ctx.user_id), default=str)
        await revoke(page)
        rendered = await public_messages(main, frozen, actor_user_id=ctx.user_id)
        assert "PRIVATE_PUBLIC_ANSWER" not in json.dumps(rendered, default=str)
        assert next(item for item in rendered if item["id"] == answer.id)["source_status"] == "unavailable"
    finally:
        await lease.release(session_status="idle")


async def test_compaction_stream_keeps_knowledge_dependencies_and_rejects_revoked_summary(monkeypatch):
    from assistant.compaction import COMMITTED
    from tests.unit.test_assistant_compaction import compact
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    sent = []

    async def provider(**kwargs):
        sent.append(json.dumps(kwargs["messages"]))
        yield {"type": "text_delta", "text": "PRIVATE_KNOWLEDGE_SUMMARY"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        await finish(ctx, lease, answer, "PRIVATE_ORIGINAL_TITLE_ANSWER")
        ctx, lease, answer = await next_turn(ctx, "Summarize the earlier knowledge observations.")
        await compact(ctx)
        assert len(sent) == 1 and "PRIVATE_ORIGINAL_TITLE_ANSWER" in sent[0]
        saved = (await events(ctx, COMMITTED))[0]
        assert saved.payload["context"]["source_refs"]
        assert "PRIVATE_KNOWLEDGE_SUMMARY" in json.dumps(await consume_context(ctx))
        await finish(ctx, lease, answer, "PRIVATE_SUMMARY_DERIVED_ANSWER")
        await revoke(page)
        from db.models.message import Message
        async with get_db_session() as db:
            summary = await db.get(Message, saved.message_id)
            with pytest.raises(AssistantError):
                await validate_message_sources(db, summary, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx)
        assert "PRIVATE_" not in json.dumps(await consume_context(ctx))
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("isolated", [False, True])
async def test_directory_does_not_open_on_normal_or_delegated_sessions(monkeypatch, isolated):
    from memory.policy import MemoryAccessDenied
    from memory.session_policy import require_context_memory
    from session.session import create_session
    from tests.unit.test_assistant_reads import TOOLS
    identity, _, _, _ = await seed(monkeypatch)
    session = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        visibility="private", memory_policy="assistant_isolated" if isolated else "standard")
    ctx = ToolContext(user_id=session.user_id, workspace_id=session.workspace_id, session_id=session.id,
        project_id=session.project_id, agent_id="assistant")
    result = await TOOLS["knowledge.directory"].execute({}, ctx)
    assert result.metadata.get("error"), result.output
    if isolated:
        with pytest.raises(MemoryAccessDenied):
            await require_context_memory(ctx)
    else:
        assert (await require_context_memory(ctx)).id == session.id


@pytest.mark.parametrize("damage", ["metadata", "scope", "proof_extra", "cursor_type", "oversize"])
async def test_exact_observation_rejects_tampered_projection_scope_and_proof(monkeypatch, damage):
    ctx, lease, _, _, _, _ = await start(monkeypatch)
    try:
        _, original = await business_context.capture(ctx, "knowledge.directory", {})
        changed = deepcopy(original)
        if damage == "metadata":
            changed["projection"]["items"][0]["title"] = "Substituted title"
        elif damage == "scope":
            changed["arguments"]["include_all_projects"] = True
        elif damage == "proof_extra":
            changed["sources"]["resources"][0]["unchecked"] = "Unbound information"
        elif damage == "cursor_type":
            changed["projection"]["next_cursor"] = {"after": "A malformed transport token"}
        else:
            changed["sources"]["resources"][0]["entries"] = ["x" * 120001]
        # Even a recomputed value digest cannot authenticate a changed source.
        from assistant.commands import command_digest
        changed["digest"] = command_digest(changed["projection"])
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            with pytest.raises(AssistantError):
                await business_context.validate(db, main, changed, fresh=True)
    finally:
        await lease.release(session_status="idle")


async def test_postgres_checkpoint_cannot_reuse_a_held_source_after_independent_revocation(monkeypatch):
    from db import base
    if base._engine.dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL writer during actual checkpoint")
    from assistant import context_sources
    ctx, lease, _, _, _, page = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        surface, _ = await projected_request(ctx)
        original = context_sources.checked_context_locked
        held_objects = []

        async def revoke_after_load(db, main, context, **kwargs):
            held = await db.get(MemorySource, page.source_manifest[0]["id"])
            held_objects.append(held)
            assert held.status == "ACTIVE"
            await revoke(page)
            assert held.status == "ACTIVE", "Outer transaction deliberately retains stale ORM state"
            return await original(db, main, context, **kwargs)

        monkeypatch.setattr(context_sources, "checked_context_locked", revoke_after_load)
        with pytest.raises(AssistantError):
            await checkpoint(ctx, surface)
        assert held_objects and not await events(ctx, "model.requested")
    finally:
        await lease.release(session_status="idle")


async def test_encrypted_cursor_nonce_does_not_change_exact_provider_observation(monkeypatch):
    ctx, lease, _, identity, _, _ = await start(monkeypatch)
    try:
        await page_for(identity, None, "SECOND_TITLE")
        result, _, _ = await call_tool(ctx, "knowledge.directory", {"limit": 1})
        assert json.loads(result.output)["next_cursor"]
        surface, messages = await projected_request(ctx)
        before = deepcopy(ctx._assistant_context)
        assert before["business_reads"][0]["projection"]["next_cursor"]
        await checkpoint(ctx, surface)
        await record_provider_context(ctx, messages)
        request = (await events(ctx, "model.requested"))[-1]
        assert request.payload["assistant_context"] == before
        cursor = before["business_reads"][0]["projection"]["next_cursor"]
        result, _, _ = await call_tool(ctx, "knowledge.directory", {"limit": 1, "cursor": cursor})
        assert not result.metadata.get("error"), result.output
        assert len(json.loads(result.output)["items"]) == 1
        await consume_context(ctx)
    finally:
        await lease.release(session_status="idle")


async def test_cursor_expiry_does_not_expire_historical_answers_but_revocation_still_does(monkeypatch):
    from types import SimpleNamespace
    from assistant import knowledge
    ctx, lease, answer, identity, _, _ = await start(monkeypatch)
    try:
        await page_for(identity, None, "SECOND_TITLE")
        await call_tool(ctx, "knowledge.directory", {"limit": 1})
        await consume_context(ctx)
        observed = deepcopy(ctx._assistant_context["business_reads"][0])
        assert observed["projection"]["next_cursor"]
        await finish(ctx, lease, answer, "HISTORICAL_PAGED_TITLE_OBSERVATION")
        later = knowledge.time.time() + knowledge.CURSOR_TTL + 1
        monkeypatch.setattr(knowledge, "time", SimpleNamespace(time=lambda: later))
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        # The expired transport token is still forbidden for a new continuation.
        with pytest.raises(AssistantError) as expired:
            await knowledge.directory(**identity, limit=1, cursor=observed["projection"]["next_cursor"])
        assert expired.value.code == "ASSISTANT_KNOWLEDGE_CURSOR"
        ctx, lease, _ = await next_turn(ctx)
        assert "HISTORICAL_PAGED_TITLE_OBSERVATION" in json.dumps(await consume_context(ctx))
        from db.models.memory_wiki import MemoryWikiPage
        async with get_db_session() as db:
            original = await db.get(MemoryWikiPage, observed["projection"]["items"][0]["id"])
        await revoke(original)
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status="idle")


async def test_empty_directory_remains_a_historical_observation_after_inventory_changes(monkeypatch):
    ctx, lease, answer, identity, _, _ = await start(monkeypatch, title=None)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        assert ctx._assistant_context["business_reads"][0]["projection"]["items"] == []
        await finish(ctx, lease, answer, "HISTORICAL_EMPTY_OBSERVATION: no titles were available at that time.")
        await page_for(identity, None, "NEW_CURRENT_TITLE")
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx, "Distinguish the previous historical observation from current inventory.")
        wire = json.dumps(await consume_context(ctx))
        assert "HISTORICAL_EMPTY_OBSERVATION" in wire and "NEW_CURRENT_TITLE" not in wire
        assert ctx._assistant_context["business_reads"] == []
        current, _, _ = await call_tool(ctx, "knowledge.directory", {})
        assert "NEW_CURRENT_TITLE" in current.output
    finally:
        await lease.release(session_status="idle")


async def test_project_scope_is_explicit_through_the_actual_tool(monkeypatch):
    ctx, lease, _, identity, projects, page = await start(monkeypatch, project=True)
    try:
        personal, _ = await page_for(identity, None, "PERSONAL_TITLE")
        other_project, _ = await page_for(identity, projects[1], "OTHER_PROJECT_TITLE")
        default, _, _ = await call_tool(ctx, "knowledge.directory", {})
        assert [item["id"] for item in json.loads(default.output)["items"]] == [personal.id]
        selected, _, _ = await call_tool(ctx, "knowledge.directory", {"project_id": projects[0]})
        assert {item["id"] for item in json.loads(selected.output)["items"]} == {personal.id, page.id}
        all_projects, _, _ = await call_tool(ctx, "knowledge.directory", {"include_all_projects": True})
        assert {item["id"] for item in json.loads(all_projects.output)["items"]} == {personal.id, page.id, other_project.id}
        await consume_context(ctx)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("mode", ["report_only", "coordination"])
async def test_report_and_coordination_do_not_gain_directory_access(monkeypatch, mode):
    if mode == "report_only":
        from tests.unit.test_assistant_reporting import prepare_report
        ctx, lease, *_ = await prepare_report()
    else:
        from tests.unit.test_assistant_continuation import coordinator, ready
        values, task, result_id = await ready(monkeypatch)
        ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    try:
        result, _, _ = await call_tool(ctx, "knowledge.directory", {})
        assert result.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
        assert not await events(ctx, "assistant.business.read")
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("accepted", [False, True])
async def test_knowledge_derivation_blocks_task_admission_or_later_execution_after_revocation(monkeypatch, accepted):
    ctx, lease, answer, _, _, page = await start(monkeypatch)
    try:
        await call_tool(ctx, "knowledge.directory", {})
        await consume_context(ctx)
        if not accepted:
            await revoke(page)
        result, _, _ = await call_tool(ctx, "tasks.submit", {"project_id": ctx.project_id, "title": "Title-derived task",
            "instructions": "Only produce text about PRIVATE_KNOWLEDGE_TITLE", "source_message_ids": [answer.parent_id]})
        if not accepted:
            assert result.metadata.get("error"), result.output
            async with get_db_session() as db:
                assert await db.scalar(select(func.count()).select_from(AssistantTask).where(
                    AssistantTask.user_id == ctx.user_id)) == 0
            return
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            assert command.source_ref["derivation"]["business_reads"][0]["operation"] == "knowledge.directory"
            execution = await db.get(Session, receipt["execution_session_id"])
            assert execution.memory_policy == "assistant_isolated"
        from assistant.scheduling import TaskSchedulingHeld, require_runnable
        from session.session import create_session
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                     parent_id=receipt["execution_session_id"])
        await require_runnable(child.id, ctx.user_id)
        await revoke(page)
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(receipt["execution_session_id"], ctx.user_id)
        with pytest.raises(TaskSchedulingHeld):
            await require_runnable(child.id, ctx.user_id)
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")
