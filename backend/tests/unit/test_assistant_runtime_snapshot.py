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
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from tests.unit.test_assistant_continuation import coordinator, prepare_report, ready, report_read
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


def identity(ctx):
    return dict(session_id=ctx.session_id, user_id=ctx.user_id,
                run_id=ctx.run_id, generation=ctx.run_generation)


@pytest.mark.parametrize("mode", ["report_only", "coordination"])
async def test_mode_projection_is_read_only_and_not_revoked_by_later_source_edits(monkeypatch, mode):
    if mode == "report_only":
        _, ctx, lease, _, result_id, _ = await prepare_report()
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
            if mode == "report_only":
                execution_id = (await db.get(AssistantTask, result.task_id)).execution_session_id
                assert view["source_session_ids"] == {execution_id, ctx.session_id}
            source = await db.get(Part, result.output_refs[-1]["part_id"])
            source.data = {**source.data, "text": "Changed original report"}
        # Revocation is not retroactive (V2 D1): an edited source does not end the bound mode.
        again = await runtime.runtime_view(**identity(ctx))
        assert (again["mode"], again["result_id"]) == (mode, result_id)
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


@pytest.mark.parametrize("revoked", ["membership", "attempt"])
async def test_successful_mode_read_cannot_authorize_a_later_tool(revoked):
    _, ctx, lease, _, result_id, _ = await prepare_report()
    try:
        await report_read(ctx, result_id)
        assert (await runtime.runtime_view(**identity(ctx)))["mode"] == "report_only"
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            if revoked == "membership":
                member = await db.scalar(select(WorkspaceMember).where(
                    WorkspaceMember.user_id == ctx.user_id, WorkspaceMember.workspace_id == ctx.workspace_id))
                member.status = "inactive"
            else:
                result.report_attempt += 1
        # Current authority is still checked at every later step and read.
        with pytest.raises(AssistantError):
            await runtime.runtime_view(**identity(ctx))
        with pytest.raises(AssistantError):
            await read_report_sources(ctx=ctx, result_id=result_id)
    finally:
        await lease.release(session_status="idle")


async def test_mode_read_does_not_block_renewal_or_make_a_released_lease_writable(monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent reader/writer MVCC test requires PostgreSQL")
    _, ctx, lease, _, result_id, _ = await prepare_report()
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
