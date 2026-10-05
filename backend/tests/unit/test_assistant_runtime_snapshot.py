"""Runtime mode reads must stay fresh without locking out lease/control writes."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from agent.driver import LeaseLostError, reserve_run
from assistant import runtime
from assistant.policy import AssistantError
from assistant.reporting import read_report_sources
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.assistant import TaskResult
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
from tests.unit.test_assistant_continuation import coordinator, ready
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_reporting import prepare_report, seen_read


def identity(ctx):
    return dict(session_id=ctx.session_id, user_id=ctx.user_id,
                run_id=ctx.run_id, generation=ctx.run_generation)


@pytest.mark.parametrize("mode", ["report_only", "coordination"])
async def test_mode_projection_retains_source_scope_without_a_write_or_row_lock(monkeypatch, mode):
    if mode == "report_only":
        ctx, lease, _, _, result_id, _ = await prepare_report()
    else:
        values, task, result_id = await ready(monkeypatch)
        ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    statements = []
    def observed(*args):
        statements.append(args[2].lstrip().upper())
    engine = get_engine().sync_engine
    try:
        event.listen(engine, "before_cursor_execute", observed)
        try:
            view = await runtime.runtime_view(**identity(ctx))
        finally:
            event.remove(engine, "before_cursor_execute", observed)
        assert view["mode"] == mode and view["result_id"] == result_id
        assert "results.read" in view["tool_ids"] and "tasks.submit" not in view["tool_ids"]
        assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) or "FOR UPDATE" in s for s in statements)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert {ref["session_id"] for ref in result.output_refs} <= view["source_session_ids"]
            source = await db.get(Part, result.output_refs[-1]["part_id"])
            source.data = {**source.data, "text": "Changed original report"}
        # A completed projection does not cache authority for the next call.
        with pytest.raises(AssistantError):
            await runtime.runtime_view(**identity(ctx))
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("stale", ["user", "run", "generation", "idle", "missing_expiry", "expired"])
async def test_mode_projection_requires_exact_live_run(stale):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    lease = await reserve_run(main.id, owner)
    args = dict(session_id=main.id, user_id=owner, run_id=lease.run_id, generation=lease.generation)
    try:
        assert (await runtime.runtime_view(**args))["mode"] == "ordinary"
        if stale in {"user", "run", "generation"}:
            key, value = {"user": ("user_id", other), "run": ("run_id", "another-run"),
                          "generation": ("generation", lease.generation + 1)}[stale]
            args[key] = value
        else:
            async with get_db_session() as db:
                driver = await db.get(AgentDriverState, main.id)
                if stale == "idle":
                    driver.phase = "idle"
                else:
                    driver.lease_expires_at = (None if stale == "missing_expiry" else
                        datetime.now(timezone.utc) - timedelta(seconds=1))
        with pytest.raises(LeaseLostError):
            await runtime.runtime_view(**args)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("revoked", ["membership", "source", "attempt"])
async def test_successful_mode_read_cannot_authorize_a_later_tool_or_provider_checkpoint(revoked):
    ctx, lease, _, part, result_id, _ = await prepare_report()
    try:
        await seen_read(ctx, part, result_id=result_id)
        assert (await runtime.runtime_view(**identity(ctx)))["mode"] == "report_only"
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            if revoked == "membership":
                member = await db.scalar(select(WorkspaceMember).where(
                    WorkspaceMember.user_id == ctx.user_id, WorkspaceMember.workspace_id == ctx.workspace_id))
                member.status = "inactive"
            elif revoked == "source":
                source = await db.get(Part, result.output_refs[-1]["part_id"])
                source.data = {**source.data, "text": "Changed original report"}
            else:
                result.report_attempt += 1
        with pytest.raises(AssistantError):
            await runtime.runtime_view(**identity(ctx))
        with pytest.raises(AssistantError):
            await read_report_sources(ctx=ctx, result_id=result_id)
        with pytest.raises(AssistantError):
            await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
                request_id="after-revocation", model_id="test/model", provider_binding_digest="a" * 64,
                tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
                expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
                message_id=ctx.message_id, assistant_context=ctx._assistant_context)
        async with get_db_session() as db:
            requests = (await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
                AgentEvent.kind == "model.requested"))).all()
            assert not any(row.payload["request_id"] == "after-revocation" for row in requests)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("changed", ["mode", "binding", "expired"])
async def test_provider_checkpoint_rechecks_coordination_mode_binding_and_expiry(monkeypatch, changed):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    try:
        assert (await runtime.runtime_view(**identity(ctx)))["mode"] == "coordination"
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        context = ctx._assistant_context
        if changed == "mode":
            context = {**context, "mode": "ordinary"}
        elif changed == "binding":
            context = {**context, "continuation_ref": {**context["continuation_ref"], "result_id": "another-result"}}
        else:
            monkeypatch.setattr("assistant.continuation._expired", lambda grant: True)
        with pytest.raises(AssistantError):
            await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
                request_id="stale-coordinator", model_id="test/model", provider_binding_digest="a" * 64,
                tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
                expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
                message_id=ctx.message_id, assistant_context=context)
    finally:
        await lease.release(session_status="idle")


async def test_mode_read_does_not_block_renewal_or_make_a_released_lease_writable(monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent reader/writer MVCC test requires PostgreSQL")
    ctx, lease, _, _, result_id, _ = await prepare_report()
    entered, resume = asyncio.Event(), asyncio.Event()
    original = runtime.bound_report_locked
    async def waiting(*args, **kwargs):
        entered.set()
        await resume.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(runtime, "bound_report_locked", waiting)
    reading = asyncio.create_task(runtime.runtime_view(**identity(ctx)))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await asyncio.wait_for(lease.renew(), 5)
        assert await asyncio.wait_for(lease.release(session_status="idle"), 5)
        resume.set()
        assert (await reading)["mode"] == "report_only"
        # The older snapshot is only a mode view. The actual operation owns
        # its fresh transaction and cannot use the released generation.
        with pytest.raises(LeaseLostError):
            await read_report_sources(ctx=ctx, result_id=result_id)
        with pytest.raises(LeaseLostError):
            await runtime.runtime_view(**identity(ctx))
    finally:
        resume.set()
        await asyncio.gather(reading, return_exceptions=True)
        await lease.release(session_status="idle")
