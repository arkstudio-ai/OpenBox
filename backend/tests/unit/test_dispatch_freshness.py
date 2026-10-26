"""Dispatch stays SQL-fenced without extending a lease at every checkpoint."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, update

from agent import driver
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from tests.unit.test_agent_driver import _seed
from tests.unit.test_durable_questions import state  # noqa: F401


async def test_monitored_checks_read_fresh_sql_without_renewal(monkeypatch):
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await asyncio.sleep(.01)
    before = await driver.get_driver_state(session_id)
    async def unexpected_renew(self):
        raise AssertionError("A full live monitored lease must not renew on dispatch")
    monkeypatch.setattr(driver.RunLease, "renew", unexpected_renew)
    try:
        for _ in range(3):
            await lease.assert_current()
        after = await driver.get_driver_state(session_id)
        assert after.lease_expires_at == before.lease_expires_at
        # A result from the previous check is never an authorization cache.
        async with get_db_session() as db:
            await db.execute(update(AgentDriverState).where(AgentDriverState.session_id == session_id)
                .values(generation=lease.generation + 1))
        with pytest.raises(driver.LeaseLostError):
            await lease.assert_current()
        assert lease.abort.is_set()
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["expired", "owner", "run", "user", "idle"])
async def test_each_dispatch_rejects_changed_authority(change):
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await asyncio.sleep(.01)
    values = {
        "expired": {"lease_expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
        "owner": {"owner_id": "another-process"}, "run": {"run_id": "another-run"},
        "user": {"user_id": "another-user"}, "idle": {"phase": "idle"},
    }[change]
    try:
        async with get_db_session() as db:
            await db.execute(update(AgentDriverState).where(AgentDriverState.session_id == session_id).values(**values))
        with pytest.raises(driver.LeaseLostError):
            await lease.assert_current()
        assert lease.abort.is_set()
    finally:
        await lease.release(session_status="idle")


async def test_live_near_expiry_retains_serialized_renewal():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await asyncio.sleep(.01)
    try:
        async with get_db_session() as db:
            await db.execute(update(AgentDriverState).where(AgentDriverState.session_id == session_id)
                .values(lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=5)))
        await lease.assert_current()
        current = await driver.get_driver_state(session_id)
        assert driver._aware(current.lease_expires_at) > datetime.now(timezone.utc) + timedelta(seconds=40)
    finally:
        await lease.release(session_status="idle")


async def test_unmonitored_check_still_renews():
    user_id, session_id = await _seed()
    lease = await driver.reserve_run(session_id, user_id)
    await lease.stop_monitor()
    try:
        await lease.assert_current()
        assert not lease._lost
    finally:
        await lease.release(session_status="idle")


async def test_nonprogress_checks_do_not_write_and_stale_turn_still_fails(state):
    from question import runtime
    from db.models.question import SessionExecution
    import db.base as database
    ticket = await runtime.start_run("s1", "u1")
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(database._engine.sync_engine, "before_cursor_execute", capture)
    try:
        await runtime._check_run(ticket, "tool", progress=False)
        assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)
        await runtime._check_run(ticket, "request", progress=True)
        async with get_db_session() as db:
            assert (await db.get(SessionExecution, "s1")).run_progress is True
            await db.execute(update(SessionExecution).where(SessionExecution.session_id == "s1")
                .values(generation=ticket.generation + 1))
        with pytest.raises(runtime.RunRevoked):
            await runtime._check_run(ticket, "tool", progress=False)
    finally:
        event.remove(database._engine.sync_engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("change", [None, "generation", "run", "owner", "expired", "deleted", "revoked", "driver"])
async def test_combined_tool_boundary_keeps_both_fences_fresh(state, change):
    from question import runtime
    from db.models.question import SessionExecution
    from tool.tool import ToolContext
    from sqlalchemy import delete
    import db.base as database
    user_id, session_id = "u1", "s1"
    lease = await driver.reserve_run(session_id, user_id)
    ticket = await runtime.start_run(session_id, user_id, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    ctx = ToolContext(session_id=session_id, user_id=user_id,
        _assert_current=lease.assert_current, _assert_all_current=lease.assert_tool_current)
    statements = []
    def capture(_conn, _cursor, statement, _params, _context, _many):
        statements.append(statement)
    try:
        event.listen(database._engine.sync_engine, "before_cursor_execute", capture)
        await ctx.assert_run_current()
        event.remove(database._engine.sync_engine, "before_cursor_execute", capture)
        assert len(statements) == 1 and statements[0].lstrip().upper().startswith("SELECT")
        if change is None:
            return
        values = {
            "generation": {"generation": ticket.generation + 1},
            "run": {"run_id": "replacement"}, "owner": {"user_id": "u2"},
            "expired": {"lease_until": datetime.now(timezone.utc) - timedelta(seconds=1)},
        }
        if change == "revoked":
            runtime.revoke(ticket.run_id, "superseded")
        else:
            async with get_db_session() as db:
                if change == "deleted":
                    await db.execute(delete(SessionExecution).where(SessionExecution.session_id == session_id))
                elif change == "driver":
                    await db.execute(update(AgentDriverState).where(AgentDriverState.session_id == session_id)
                        .values(owner_id="replacement-process"))
                else:
                    await db.execute(update(SessionExecution).where(SessionExecution.session_id == session_id)
                        .values(**values[change]))
        with pytest.raises(driver.LeaseLostError if change == "driver" else runtime.RunRevoked):
            await ctx.assert_run_current()
    finally:
        if event.contains(database._engine.sync_engine, "before_cursor_execute", capture):
            event.remove(database._engine.sync_engine, "before_cursor_execute", capture)
        runtime.current_run.reset(token)
        await lease.release(session_status="idle")


async def test_combined_boundary_near_expiry_renews_without_accepting_a_revoked_turn(state):
    from question import runtime
    from db.models.question import SessionExecution
    user_id, session_id = "u1", "s1"
    lease = await driver.reserve_run(session_id, user_id)
    ticket = await runtime.start_run(session_id, user_id, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        async with get_db_session() as db:
            await db.execute(update(AgentDriverState).where(AgentDriverState.session_id == session_id)
                .values(lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=5)))
        await lease.assert_tool_current()
        current = await driver.get_driver_state(session_id)
        assert driver._aware(current.lease_expires_at) > datetime.now(timezone.utc) + timedelta(seconds=40)
        await lease.stop_monitor()
        async with get_db_session() as db:
            await db.execute(update(SessionExecution).where(SessionExecution.session_id == session_id)
                .values(generation=ticket.generation + 1))
        with pytest.raises(runtime.RunRevoked):
            await lease.assert_tool_current()
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="idle")
