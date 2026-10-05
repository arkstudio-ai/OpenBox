"""Result body replay shares one read snapshot, never later admission authority."""
from copy import deepcopy
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event, select

from agent.driver import LeaseLostError
from agent.loop import _to_llm_messages
from assistant import projection, reporting, results
from assistant.evidence import projection_digest
from assistant.policy import AssistantError
from assistant.projection import _fresh_read, project_main_messages
from assistant.reporting import read_report_sources
from db.base import get_db_session, get_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.assistant import TaskResult
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory.tool_projection import _part_dict
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
from tests.unit.test_assistant_continuation import coordinator, ready
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, read_turn
from tests.unit.test_assistant_reporting import prepare_report


async def result_read():
    ctx, lease, _, _, result_id, _ = await prepare_report()
    output, ctx, part = await call_tool(ctx, "results.read", {"result_id": result_id})
    assert not output.metadata.get("error")
    return ctx, lease, _part_dict(part), json.loads(output.output), result_id


async def test_result_body_replay_reuses_original_checks_only_within_one_snapshot(monkeypatch, record_property):
    ctx, lease, part, page, result_id = await result_read()
    original_part = deepcopy(part)
    original = results._result_original
    transactions, statements, validations = [], [], []

    async def checked(db, result, **kwargs):
        assert result.id == result_id
        transactions.append(db.sync_session.get_transaction())
        return await original(db, result, **kwargs)

    def retained_validation(validate):
        async def call(db, result, **kwargs):
            validations.append((db.sync_session.get_transaction(), kwargs.get("snapshot_checks")))
            return await validate(db, result, **kwargs)
        return call

    def observed(_connection, _cursor, statement, *_args):
        statements.append(statement.lstrip().upper())

    monkeypatch.setattr(results, "_result_original", checked)
    monkeypatch.setattr(reporting, "validate_result_source", retained_validation(reporting.validate_result_source))
    monkeypatch.setattr(projection, "validate_result_source", retained_validation(projection.validate_result_source))
    engine = get_engine().sync_engine
    try:
        event.listen(engine, "before_cursor_execute", observed)
        try:
            projected = await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
        finally:
            event.remove(engine, "before_cursor_execute", observed)
        assert json.loads(projected["output"]) == page
        assert projected["metadata"]["_assistant_projection_verified"] is True
        assert part == original_part
        assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) or "FOR UPDATE" in s for s in statements)
        record_property("result_original_checks", len(transactions))
        record_property("result_original_transactions", len(set(transactions)))
        record_property("retained_result_validations", len(validations))
        record_property("selects_per_rematerialization", sum(s.startswith("SELECT") for s in statements))
        assert len(transactions) == 1, "Mode authorization and result replay must share one snapshot"
        assert len(validations) == 2 and validations[0] == validations[1]
        again = await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
        assert json.loads(again["output"]) == page
        assert len(transactions) == 2 and transactions[0] is not transactions[1]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("mode", ["ordinary", "coordination"])
async def test_result_body_replay_preserves_other_assistant_modes(monkeypatch, mode):
    if mode == "ordinary":
        ctx, lease, _, accepted, _ = await read_turn()
        async with get_db_session() as db:
            result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    else:
        values, task, result_id = await ready(monkeypatch)
        ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    try:
        output, ctx, part = await call_tool(ctx, "results.read", {"result_id": result_id})
        assert not output.metadata.get("error")
        replay = await _fresh_read(_part_dict(part), ctx=ctx, for_compaction=False, business_snapshots={})
        assert replay["metadata"]["_assistant_projection_verified"] is True
        assert json.loads(replay["output"]) == json.loads(output.output)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed", ["membership", "source", "audience", "attempt", "version", "target", "workspace", "main_kind"])
async def test_result_replay_checks_current_scope_sources_attempt_and_target(changed):
    ctx, lease, part, _, result_id = await result_read()
    try:
        first = await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
        assert first["metadata"]["_assistant_projection_verified"] is True
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            if changed == "membership":
                (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            elif changed == "source":
                source = await db.get(Part, result.output_refs[-1]["part_id"])
                source.data = {**source.data, "text": "Changed original evidence"}
            elif changed == "audience":
                execution = await db.get(Session, result.output_refs[-1]["session_id"])
                execution.visibility = "workspace"
            elif changed == "attempt":
                result.report_attempt += 1
            elif changed == "version":
                result.output_refs = list(reversed(result.output_refs))
            elif changed == "target":
                part["metadata"]["transient_assistant_refs"]["arguments"]["result_id"] = "outside-bound-result"
            elif changed == "workspace":
                ctx.workspace_id = "outside-bound-workspace"
            else:
                (await db.get(Session, ctx.session_id)).kind = "normal"
        replay = await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
        assert replay["metadata"]["_assistant_projection_verified"] is False
        assert json.loads(replay["output"])["status"] == "fresh_read_required"
        assert "Browser verification is still untested" not in replay["output"]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed", ["run", "generation", "idle", "missing_expiry", "expired"])
async def test_result_replay_keeps_exact_live_driver_requirement(changed):
    ctx, lease, part, _, _ = await result_read()
    try:
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, ctx.session_id)
            if changed == "run":
                driver.run_id = "replacement-run"
            elif changed == "generation":
                driver.generation += 1
            elif changed == "idle":
                driver.phase = "idle"
            else:
                driver.lease_expires_at = (None if changed == "missing_expiry" else
                    datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(LeaseLostError):
            await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed", ["source", "lease"])
async def test_shared_snapshot_does_not_block_writer_or_authorize_next_tool_and_provider(monkeypatch, changed):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent reader/writer MVCC requires PostgreSQL")
    from assistant import projection
    ctx, lease, part, page, result_id = await result_read()
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = await project_main_messages(list(surface.messages), ctx=ctx)
    messages = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
    ctx._assistant_context["messages_digest"] = projection_digest(messages)

    async def checkpoint(request_id):
        await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
            request_id=request_id, model_id="test/model", provider_binding_digest="a" * 64,
            tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
            expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
            message_id=ctx.message_id, assistant_context=ctx._assistant_context)

    # Admit the identical complete payload before revocation. A malformed
    # context must not make the later refusal pass before checking sources.
    await checkpoint("before-rematerialization-revocation")
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    entered, resume = asyncio.Event(), asyncio.Event()
    original = projection.validate_result_source

    async def paused(db, *args, **kwargs):
        entered.set()
        await asyncio.wait_for(resume.wait(), 5)
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(projection, "validate_result_source", paused)
    reading = asyncio.create_task(_fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={}))
    try:
        await asyncio.wait_for(entered.wait(), 5)

        async def independent_write():
            async with get_db_session() as db:
                # The reader must not hold any of the normal source/lease locks.
                await db.scalar(select(Session).where(Session.id == ctx.session_id).with_for_update(nowait=True))
                await db.scalar(select(AgentDriverState).where(
                    AgentDriverState.session_id == ctx.session_id).with_for_update(nowait=True))
                result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update(nowait=True))
                source = await db.scalar(select(Part).where(
                    Part.id == result.output_refs[-1]["part_id"]).with_for_update(nowait=True))
                if changed == "source":
                    source.data = {**source.data, "text": "Revoked original after mode validation"}
            assert await lease.renew()
            if changed == "lease":
                assert await lease.release(session_status="idle")

        await asyncio.wait_for(independent_write(), 5)
        resume.set()
        replay = await asyncio.wait_for(reading, 5)
        # Consistent old snapshot bytes are transient and confer no authority.
        assert json.loads(replay["output"]) == page
        if changed == "source":
            next_replay = await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
            assert next_replay["metadata"]["_assistant_projection_verified"] is False
        else:
            with pytest.raises(LeaseLostError):
                await _fresh_read(part, ctx=ctx, for_compaction=False, business_snapshots={})
        with pytest.raises((AssistantError, LeaseLostError)):
            await read_report_sources(ctx=ctx, result_id=result_id)
        if changed == "source":
            with pytest.raises(AssistantError) as rejected:
                await checkpoint("after-rematerialization-revocation")
            assert rejected.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"
        else:
            with pytest.raises(LeaseLostError):
                await checkpoint("after-rematerialization-revocation")
        async with get_db_session() as db:
            requests = (await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
                AgentEvent.kind == "model.requested"))).all()
            assert any(row.payload["request_id"] == "before-rematerialization-revocation" for row in requests)
            assert not any(row.payload["request_id"] == "after-rematerialization-revocation" for row in requests)
    finally:
        resume.set()
        await asyncio.gather(reading, return_exceptions=True)
        await lease.release(session_status="idle")
