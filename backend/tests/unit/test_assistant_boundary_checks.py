"""One boundary reads each independent fact once; nothing crosses boundaries."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm.attributes import set_committed_value

from agent.loop import _to_llm_messages
from assistant import continuation
from assistant.command_sources import _command_walk
from assistant.context_sources import checked_context_locked
from assistant.evidence import projection_digest, validate_source_ref
from assistant.policy import AssistantError
from assistant.projection import project_main_messages
from assistant.scheduling import task_hold
from assistant.service import ensure_main_session
from assistant.transactions import BoundaryChecks, boundary_checks
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import get_messages
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_monitor_snapshot import automatic_input
from tests.unit.test_assistant_reads import call_tool, cursor_key, read_turn  # noqa: F401
from tests.unit.test_assistant_scheduling import hold


@contextmanager
def unshared(_db):
    yield None


@contextmanager
def statements():
    seen = []

    def count(*args):
        seen.append(args[2].lstrip().upper())

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", count)


def counter():
    calls = []

    async def validate():
        calls.append(None)
        return "verified"
    return calls, validate


async def main_session():
    owner, _, workspace = await accounts()
    return await ensure_main_session(user_id=owner, workspace_id=workspace)


async def test_a_fact_is_read_once_per_boundary_and_never_after_it():
    await main_session()
    calls, validate = counter()
    async with get_db_session() as db:
        await db.execute(select(1))
        with boundary_checks(db) as checks:
            assert isinstance(checks, BoundaryChecks) and checks.reuse_task_facts
            for _ in range(3):
                assert await checks.check(db, "fact", ("scope",), {"id": 1}, validate) == "verified"
            assert len(calls) == 1
            await checks.check(db, "fact", ("scope",), {"id": 2}, validate)
            await checks.check(db, "fact", ("other-scope",), {"id": 1}, validate)
            assert len(calls) == 3
        # The ended boundary's object and a later boundary both read again.
        await checks.check(db, "fact", ("scope",), {"id": 1}, validate)
        with boundary_checks(db) as later:
            await later.check(db, "fact", ("scope",), {"id": 1}, validate)
        assert len(calls) == 5


@pytest.mark.parametrize("mutation", ["flush", "statement", "pending"])
async def test_a_write_in_the_boundary_stops_reuse_for_the_rest_of_it(mutation):
    main = await main_session()
    calls, validate = counter()
    async with get_db_session() as db:
        row = await db.get(Session, main.id)
        with boundary_checks(db) as checks:
            await checks.check(db, "fact", (), 1, validate)
            await checks.check(db, "fact", (), 1, validate)
            assert len(calls) == 1
            if mutation == "flush":
                row.title = "Changed inside this transaction"
                await db.flush()
            elif mutation == "statement":
                await db.execute(update(Session).where(Session.id == main.id).values(title="Changed by SQL"))
            else:
                row.title = "Pending change"
            for _ in range(2):
                await checks.check(db, "fact", (), 1, validate)
            assert len(calls) == 3
            ids = {"facts": []}

            async def load(missing):
                ids["facts"].append(tuple(missing))
                return {identity: identity for identity in missing}
            assert await checks.read_many(db, "task_scope", (), ["a", "a", "b"], load) == {"a": "a", "b": "b"}
            await checks.read_many(db, "task_scope", (), ["a"], load)
            assert ids["facts"] == [("a", "b"), ("a",)]
        await db.rollback()


async def test_pending_changes_or_another_sessions_walk_never_share_reads():
    main = await main_session()
    async with get_db_session() as db:
        assert db.sync_session.get_transaction() is None
        with boundary_checks(db) as checks:
            assert checks is None
        (await db.get(Session, main.id)).title = "Pending"
        with boundary_checks(db) as checks:
            assert checks is None
        await db.rollback()
    async with get_db_session() as other, get_db_session() as db:
        await other.execute(select(1))
        await db.execute(select(1))
        with _command_walk(other) as walk:
            with boundary_checks(db) as checks:
                assert checks is None
            # Another Session's validation keeps its own reusable walk.
            assert walk.enabled and walk.usable(other)


async def test_a_refreshed_held_row_is_never_returned_under_its_earlier_verdict():
    await main_session()
    calls = []
    held = SimpleNamespace(data="original bytes")

    async def validate():
        calls.append(None)
        return held
    fingerprint = lambda value: value.data  # noqa: E731
    async with get_db_session() as db:
        await db.execute(select(1))
        with boundary_checks(db) as checks:
            await checks.check(db, "row", (), 1, validate, fingerprint=fingerprint)
            await checks.check(db, "row", (), 1, validate, fingerprint=fingerprint)
            assert len(calls) == 1
            held.data = "refreshed in place by a later READ COMMITTED read"
            await checks.check(db, "row", (), 1, validate, fingerprint=fingerprint)
            assert len(calls) == 2
            # The boundary stops sharing entirely after a changed identity.
            await checks.check(db, "unrelated", (), 1, validate)
            await checks.check(db, "unrelated", (), 1, validate)
            assert len(calls) == 4


async def test_a_source_row_refreshed_in_place_is_read_and_hashed_again():
    ctx, lease, _, _, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": report.session_id, "message_ids": [report.id]})
        await consume_context(ctx)
        ref = next(ref for ref in ctx._assistant_context["source_refs"] if ref["message_id"] == report.id)
        scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        async with get_db_session() as db:
            await db.execute(select(1))
            with boundary_checks(db) as checks:
                part = await validate_source_ref(db, ref, **scope,
                    validation={"messages": set(), "refs": {}, "snapshot_checks": checks})
                original = deepcopy(part.data)
                with statements() as cached:
                    await validate_source_ref(db, ref, **scope,
                        validation={"messages": set(), "refs": {}, "snapshot_checks": checks})
                # Another read can populate a held identity without dirtying it.
                set_committed_value(part, "data", {**original, "text": "Bytes refreshed in place"})
                with statements() as reread_statements:
                    reread = await validate_source_ref(db, ref, **scope,
                        validation={"messages": set(), "refs": {}, "snapshot_checks": checks})
                assert not any("PARTS" in statement for statement in cached)
                assert any("PARTS" in statement for statement in reread_statements)
                assert len(cached) < len(reread_statements)
                assert reread.data == original
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["none", "paused", "canceled", "source", "membership", "expiry"])
async def test_hold_outcomes_match_unshared_per_edge_checks(monkeypatch, change):
    # Boundary sharing is compared alone; reused verdicts are tested separately.
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "off")
    values, task = await automatic_input(monkeypatch)
    owner, workspace = values[0], values[2]
    if change in {"paused", "canceled"}:
        await hold(task.id, change)
    elif change == "source":
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, task.continuation_policy["grant_command_id"])
            item = await db.get(AgentInboxItem, command.source_ref["continuation_grant"]["inbox_id"])
            part = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
            part.data = {**part.data, "text": "Changed original authorization"}
    elif change == "membership":
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    elif change == "expiry":
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)
        monkeypatch.setattr(continuation, "datetime", Later)
    with statements() as seen:
        shared = await task_hold(task.execution_session_id, owner)
    shared_count = len(seen)
    with monkeypatch.context() as patch, statements() as seen:
        patch.setattr("assistant.transactions.boundary_checks", unshared)
        expected = await task_hold(task.execution_session_id, owner)
    assert shared == expected
    assert (expected is None) == (change == "none")
    if change == "none":
        assert shared_count < len(seen)


async def test_authority_revoked_during_a_boundary_is_read_before_it_decides(monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("An independent committed revocation mid-check requires PostgreSQL")
    values, task = await automatic_input(monkeypatch)
    owner, workspace = values[0], values[2]
    original = continuation.validate_execution_authority
    revoked = []

    async def revoke_then_validate(db, main, task, **kwargs):
        if not revoked:
            async with get_db_session() as other:
                (await other.get(WorkspaceMember, (workspace, owner))).status = "removed"
            revoked.append(True)
        return await original(db, main, task, **kwargs)

    monkeypatch.setattr(continuation, "validate_execution_authority", revoke_then_validate)
    held = await task_hold(task.execution_session_id, owner)
    assert revoked and held is not None and held.state == "unavailable"


async def test_fresh_checkpoint_matches_unshared_checks_and_still_refuses_changed_evidence(monkeypatch):
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "tasks.list", {})
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        for index in range(3):
            await consume_context(ctx)
            await finish(ctx, lease, answer, f"PRIVATE_BOUNDARY_ANSWER_{index}")
            ctx, lease, answer = await next_turn(ctx)
        projected = await project_main_messages(await get_messages(ctx.session_id, user_id=ctx.user_id), ctx=ctx)
        context = deepcopy(ctx._assistant_context)
        context["messages_digest"] = projection_digest(_to_llm_messages(projected, user_id=ctx.user_id,
                                                                        assistant_projection_verified=True))

        async def check():
            async with get_db_session() as db:
                main = await db.get(Session, ctx.session_id)
                return await checked_context_locked(db, main, deepcopy(context), fresh=True)
        with statements() as seen:
            shared = await check()
        shared_count = len(seen)
        with monkeypatch.context() as patch, statements() as seen:
            patch.setattr("assistant.transactions.boundary_checks", unshared)
            expected = await check()
        assert shared == expected and shared_count < len(seen), (shared_count, len(seen))
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "Replaced original evidence"}
        with pytest.raises(AssistantError):
            await check()
    finally:
        await lease.release(session_status="idle")
