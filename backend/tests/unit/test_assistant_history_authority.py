"""UI history reuses authority only within its explicit read-only snapshot."""
import json
from contextlib import contextmanager
from time import perf_counter

import pytest
from sqlalchemy import event, func, select

from assistant import commands, schedule_runs
from assistant.policy import AssistantError
from assistant.public_history import public_messages
from assistant.transactions import begin_snapshot, source_snapshot
from db.base import get_db_session, get_engine
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import get_message_window, get_session
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_snapshot_checks import stable
from tests.unit.test_assistant_source_join_queries import sql_reads
from tests.unit.test_assistant_walk_originals import repeated_result  # noqa: F401


@contextmanager
def history_sql_reads():
    statements = []
    def count(_connection, _cursor, statement, _parameters, _context, _many):
        upper = statement.lstrip().upper()
        if upper.startswith("SELECT"):
            statements.append(statement)
        else:
            assert upper == "BEGIN" or upper.startswith("SET TRANSACTION")
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_actual_history_reuses_authority_without_changing_any_visible_message(repeated_result, monkeypatch, record_property):
    scope, _ = repeated_result
    session = await get_session(scope["main_id"], user_id=scope["user_id"])
    window = await get_message_window(session.id, user_id=scope["user_id"], turns=8)

    async def read():
        started = perf_counter()
        with history_sql_reads() as queries:
            result = await public_messages(session, window.messages, actor_user_id=scope["user_id"])
        return stable(result), {"sql": len(queries), "seconds": perf_counter() - started}

    # Only disable the new explicit _authority argument. Existing original-row
    # and command-proof reuse is unchanged on both sides of this comparison.
    original = commands._authority
    async def no_authority_reuse(db, *, user_id, workspace_id, main_id, snapshot_checks=None):
        return await original(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)

    with monkeypatch.context() as patch:
        patch.setattr(commands, "_authority", no_authority_reuse)
        patch.setattr(schedule_runs, "_authority", no_authority_reuse)
        before, baseline = await read()
    after, candidate = await read()
    assert after == before
    assert candidate["sql"] < baseline["sql"]
    record_property("history_authority_measurement", json.dumps({"baseline": baseline, "candidate": candidate}))


async def test_authority_reuse_requires_explicit_snapshot_and_ends_with_it(repeated_result):
    scope, _ = repeated_result
    async with source_snapshot() as (db, checks):
        with sql_reads() as first:
            main = await commands._authority(db, **scope, snapshot_checks=checks)
            assert await commands._authority(db, **scope, snapshot_checks=checks) is main
        assert len(first) == 1
        with sql_reads() as fresh:
            assert await commands._authority(db, **scope) is main
            assert await commands._authority(db, **scope) is main
        assert len(fresh) == 2  # A default/fresh caller never inherits the memo.
    async with source_snapshot() as (db, fresh_checks):
        with pytest.raises(RuntimeError, match="original read-only snapshot"):
            await commands._authority(db, **scope, snapshot_checks=checks)
        with sql_reads() as following:
            await commands._authority(db, **scope, snapshot_checks=fresh_checks)
        assert len(following) == 1


@pytest.mark.parametrize("changed", ["membership", "main"])
async def test_next_history_snapshot_and_fresh_default_reject_external_revocation(repeated_result, changed):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL transactions prove current revocation")
    scope, _ = repeated_result
    expected = "ASSISTANT_WORKSPACE_FORBIDDEN" if changed == "membership" else "ASSISTANT_UNAVAILABLE"
    async with get_db_session() as db:
        checks = await begin_snapshot(db)
        held = await commands._authority(db, **scope, snapshot_checks=checks)
        pid = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != pid
            if changed == "membership":
                (await writer.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            else:
                (await writer.get(Session, held.id)).memory_policy = "standard"
        await db.rollback()
        with pytest.raises(RuntimeError, match="original read-only snapshot"):
            await commands._authority(db, **scope, snapshot_checks=checks)
        with pytest.raises(AssistantError) as rejected:
            await commands._authority(db, **scope)
        assert rejected.value.code == expected
    async with source_snapshot() as (db, checks):
        with pytest.raises(AssistantError) as rejected:
            await commands._authority(db, **scope, snapshot_checks=checks)
        assert rejected.value.code == expected
