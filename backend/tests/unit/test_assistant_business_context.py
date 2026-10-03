"""Historical SQL observations survive progress, never source revocation."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

from assistant.business_context import capture
from assistant.commands import accept_task_command
from assistant.context_sources import checked_context_locked
from assistant.evidence import validate_business_reads, validate_message_sources
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn


async def add_task(ctx, key="new-task"):
    return await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
        main_id=ctx.session_id, project_id=ctx.project_id, idempotency_key=key,
        prompt="A new pure text task", title="New task")


async def add_project(ctx):
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Project(id=uuid4().hex, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            name="New project", created_at=now, updated_at=now))


@pytest.mark.parametrize("operation", ["projects.list", "sessions.list", "tasks.list", "tasks.get"])
async def test_progress_and_new_inventory_refresh_current_reads_without_invalidating_consumed_history(operation):
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        arguments = {"task_id": accepted["task_id"]} if operation == "tasks.get" else {}
        result, _, part = await call_tool(ctx, operation, arguments)
        assert not result.metadata.get("error"), result.output
        from assistant.business_context import refresh
        await refresh(ctx, part.model_dump(), result.metadata["transient_assistant_refs"])
        await consume_context(ctx)
        old = deepcopy(ctx._assistant_context)
        assert old["business_reads"][0]["version"] == 2
        await add_task(ctx)
        await add_project(ctx)
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted["task_id"])
            revision = task.control_revision
        await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, task_id=accepted["task_id"], idempotency_key="progress",
            expected_revision=revision, prompt="Continue the original task")
        await consume_context(ctx)
        assert ctx._assistant_context["business_reads"][0]["digest"] != old["business_reads"][0]["digest"]
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            await checked_context_locked(db, main, old)
            with pytest.raises(AssistantError) as stale:
                await checked_context_locked(db, main, old, fresh=True)
            assert stale.value.code == "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED"
        await finish(ctx, lease, answer, "The original observation remains historical; current SQL facts have advanced.")
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx)
        assert "original observation remains historical" in json.dumps(await consume_context(ctx))
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("operation,change", [
    ("projects.list", "project"), ("sessions.list", "audience"),
    ("tasks.list", "task_scope"), ("tasks.get", "membership"),
])
async def test_current_authority_and_scope_still_gate_historical_business_evidence(operation, change):
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        args = {"task_id": accepted["task_id"]} if operation == "tasks.get" else {}
        await call_tool(ctx, operation, args)
        await consume_context(ctx)
        await finish(ctx, lease, answer, "PRIVATE_BUSINESS_DERIVATION")
        async with get_db_session() as db:
            if change == "project":
                (await db.get(Project, ctx.project_id)).is_deleted = True
            elif change == "audience":
                (await db.get(Session, accepted["execution_session_id"])).visibility = "workspace"
            elif change == "task_scope":
                (await db.get(AssistantTask, accepted["task_id"])).title = "Changed original source"
            else:
                (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status="idle")


async def test_snapshot_tampering_and_legacy_digest_upgrade_are_rejected():
    ctx, lease, _, _, _ = await read_turn()
    try:
        await call_tool(ctx, "tasks.list", {})
        await consume_context(ctx)
        current = ctx._assistant_context["business_reads"][0]
        legacy = {key: current[key] for key in ("operation", "arguments", "digest")}
        damaged = deepcopy(current)
        damaged["projection"]["items"][0]["observed_state"] = "forged"
        async with get_db_session() as db:
            for invalid in (damaged, {**legacy, "version": 2}, {**current, "version": 99}):
                with pytest.raises(AssistantError):
                    await validate_business_reads(db, [invalid], user_id=ctx.user_id,
                        workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await add_task(ctx)
        async with get_db_session() as db:
            await validate_business_reads(db, [current], user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            with pytest.raises(AssistantError) as legacy_changed:
                await validate_business_reads(db, [legacy], user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            assert legacy_changed.value.code == "ASSISTANT_SOURCE_CHANGED"
    finally:
        await lease.release(session_status="idle")


async def test_full_business_page_does_not_spend_model_budget_on_duplicate_provenance(monkeypatch):
    ctx, lease, _, _, _ = await read_turn()
    try:
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            db.add_all([Project(id=uuid4().hex, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                name=f"Visible project {index:02d} " + "x" * 105, created_at=now, updated_at=now) for index in range(49)])
        monkeypatch.setattr("assistant.projection.MAX_CONTEXT_CHARS", 20000)
        await call_tool(ctx, "projects.list", {})
        wire = await consume_context(ctx)
        assert len(ctx._assistant_context["business_reads"][0]["projection"]["items"]) == 50
        assert "Visible project 48" in json.dumps(wire)
        assert "scope_digest" not in json.dumps(wire)
    finally:
        await lease.release(session_status="idle")


async def test_changed_original_observation_is_never_refreshed_into_trusted_evidence():
    ctx, lease, _, _, _ = await read_turn()
    try:
        _, _, part = await call_tool(ctx, "projects.list", {})
        async with get_db_session() as db:
            event = await db.scalar(select(AgentEvent).where(AgentEvent.part_id == part.id,
                AgentEvent.kind == "assistant.business.read"))
            event.payload = {**event.payload, "digest": "0" * 64}
        payload = await consume_context(ctx)
        assert "fresh_read_required" in json.dumps(payload)
        assert ctx._assistant_context["business_reads"] == []
    finally:
        await lease.release(session_status="idle")


async def test_inventory_change_before_checkpoint_rebuilds_without_dispatching_stale_tool_output():
    from agent.loop import _prepare_checkpointed_provider_attempt, _to_llm_messages
    from assistant.context_sources import record_provider_context
    from assistant.evidence import projection_digest
    from assistant.projection import project_main_messages
    from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
    ctx, lease, answer, _, _ = await read_turn()
    attempts = 0

    async def load():
        return await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)

    async def build(surface):
        messages = await project_main_messages(list(surface.messages), ctx=ctx)
        wire = _to_llm_messages(messages, user_id=ctx.user_id, assistant_projection_verified=True)
        ctx._assistant_context["messages_digest"] = projection_digest(wire)
        return wire

    async def checkpoint(surface, tool_digest, prompt_digest):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            await add_task(ctx)
        return await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
            request_id="business-progress-race", model_id="test/model", provider_binding_digest="a" * 64,
            tool_schema_digest=tool_digest, prompt_shape_digest=prompt_digest,
            expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
            message_id=ctx.message_id, assistant_context=ctx._assistant_context)

    try:
        await call_tool(ctx, "tasks.list", {})
        prepared = await _prepare_checkpointed_provider_attempt(load_surface=load, build_messages=build,
            checkpoint=checkpoint, system=["Test request"], tools={}, model_id="test/model",
            provider_binding_digest="a" * 64, payload_dialect="openai", tool_choice=None, user_variant=None)
        assert attempts == 2
        assert len(ctx._assistant_context["business_reads"][0]["projection"]["items"]) == 2
        await record_provider_context(ctx, prepared.llm_messages)
        await finish(ctx, lease, answer, "There are now two authorized tasks.")
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status="idle")


async def test_business_capture_uses_one_postgresql_snapshot(monkeypatch):
    from assistant import business_context
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        async with get_db_session() as db:
            if db.get_bind().dialect.name != "postgresql":
                pytest.skip("Independent MVCC snapshot requires PostgreSQL")
        original = business_context.OPERATIONS["tasks.get"]

        async def interleaved(**kwargs):
            value = await original(**kwargs)
            async with get_db_session() as writer:
                (await writer.get(AssistantTask, accepted["task_id"])).title = "Concurrent title"
            return value

        monkeypatch.setitem(business_context.OPERATIONS, "tasks.get", interleaved)
        _, frozen = await capture(ctx, "tasks.get", {"task_id": accepted["task_id"]})
        monkeypatch.setitem(business_context.OPERATIONS, "tasks.get", original)
        async with get_db_session() as db:
            with pytest.raises(AssistantError) as changed:
                await validate_business_reads(db, [frozen], user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            assert changed.value.code == "ASSISTANT_SOURCE_CHANGED"
    finally:
        await lease.release(session_status="idle")
