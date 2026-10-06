"""A validation verdict is reused only while every fact it read is unchanged."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text

from assistant import continuation, evidence_cache
from assistant.scheduling import held_task_locked, task_hold
from assistant.service import ensure_main_session
from db import evidence_schema
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.evidence import AssistantEvidenceEpoch
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from db.repository.part_repo import _to_dict as part_dict
from tests.unit.test_assistant_boundary_checks import statements
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_monitor_snapshot import automatic_input
from tests.unit.test_assistant_scheduling import hold

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def verify_every_reuse(monkeypatch):
    # Each reuse below is also checked against the original validation.
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "verify")


def postgres():
    return get_engine().dialect.name == "postgresql"


async def epochs():
    async with get_db_session() as db:
        rows = await db.execute(select(AssistantEvidenceEpoch.scope_key, AssistantEvidenceEpoch.version))
        return dict(rows.all())


async def grant_message(task):
    async with get_db_session() as db:
        command = await db.get(AssistantCommand, task.continuation_policy["grant_command_id"])
        item = await db.get(AgentInboxItem, command.source_ref["continuation_grant"]["inbox_id"])
        part = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
        return item.message_id, part.id


async def warmed(monkeypatch):
    values, task = await automatic_input(monkeypatch)
    owner = values[0]
    assert await task_hold(task.execution_session_id, owner) is None
    assert await task_hold(task.execution_session_id, owner) is None
    assert evidence_cache.stats["task_sources.hit"] >= 1
    return values, task


async def test_a_repeated_hold_check_reuses_its_closure_in_one_read(monkeypatch):
    values, task = await warmed(monkeypatch)
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "off")
    with statements() as original:
        assert await task_hold(task.execution_session_id, values[0]) is None
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "on")
    hits = evidence_cache.stats["task_sources.hit"]
    with statements() as reused:
        assert await task_hold(task.execution_session_id, values[0]) is None
    assert evidence_cache.stats["task_sources.hit"] == hits + 1
    assert len(reused) <= 8 < len(original), (len(reused), len(original))
    # One statement proves the closure: epochs, row versions and part sets.
    assert sum("ASSISTANT_EVIDENCE_EPOCHS" in statement for statement in reused) == 1


@pytest.mark.parametrize("change", ["paused", "canceled", "source", "new_part", "deleted_part",
                                    "membership", "grant_message", "expiry"])
async def test_any_change_to_a_read_fact_ends_reuse(monkeypatch, change):
    values, task = await warmed(monkeypatch)
    owner, workspace = values[0], values[2]
    message_id, part_id = await grant_message(task)
    async with get_db_session() as db:
        if change in {"paused", "canceled"}:
            pass
        elif change == "source":
            part = await db.get(Part, part_id)
            part.data = {**part.data, "text": "Changed original authorization"}
        elif change == "new_part":
            original = await db.get(Part, part_id)
            db.add(Part(id=f"part-{uuid4().hex}", message_id=message_id, session_id=original.session_id,
                        user_id=owner, type="text", data={"type": "text", "text": "Appended later"},
                        created_at=original.created_at + timedelta(seconds=1)))
        elif change == "deleted_part":
            await db.delete(await db.get(Part, part_id))
        elif change == "membership":
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        elif change == "grant_message":
            (await db.get(Message, message_id)).finish = "length"
    if change in {"paused", "canceled"}:
        await hold(task.id, change)
    if change == "expiry":
        class Later(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(days=2)
        monkeypatch.setattr(continuation, "datetime", Later)
    stale, hits = evidence_cache.stats["task_sources.stale"], evidence_cache.stats["task_sources.hit"]
    cached = await task_hold(task.execution_session_id, owner)
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "off")
    assert cached == await task_hold(task.execution_session_id, owner)
    if change not in {"paused", "canceled", "membership"}:
        # Authority and control state are read before the cached graph.
        assert evidence_cache.stats["task_sources.hit"] == hits
        assert evidence_cache.stats["task_sources.stale"] == stale + 1
    if change in {"source", "deleted_part", "membership", "expiry"}:
        assert cached is not None and cached.state == "unavailable"


async def test_volatile_or_unrelated_changes_keep_the_closure(monkeypatch):
    values, task = await warmed(monkeypatch)
    owner, other = values[0], values[1]
    async with get_db_session() as db:
        if postgres():
            # SQLite bumps on any update (its triggers cannot name columns).
            execution = await db.get(Session, task.execution_session_id)
            execution.status, execution.updated_at = "busy", datetime.now(timezone.utc)
            execution.token_usage = {"input": 1}
        other_main = await db.scalar(select(Session).where(Session.user_id == other))
        if other_main is not None:
            other_main.title = "Another owner's change"
        message_id, part_id = await grant_message(task)
        original = await db.get(Part, part_id)
        # A new answer elsewhere in the same session is not part of this closure.
        message = Message(id=f"message-{uuid4().hex}", session_id=original.session_id, user_id=owner,
                          role="assistant", created_at=original.created_at + timedelta(seconds=5))
        db.add(message)
        await db.flush()
        db.add(Part(id=f"part-{uuid4().hex}", message_id=message.id, session_id=original.session_id,
                    user_id=owner, type="text", data={"type": "text", "text": "Streaming"},
                    created_at=message.created_at))
    hits = evidence_cache.stats["task_sources.hit"]
    assert await task_hold(task.execution_session_id, owner) is None
    assert evidence_cache.stats["task_sources.hit"] == hits + 1


async def test_this_transactions_own_write_is_never_hidden_by_reuse(monkeypatch):
    values, task = await warmed(monkeypatch)
    hits = evidence_cache.stats["task_sources.hit"]
    async with get_db_session() as db:
        (await db.get(AssistantTask, task.id)).title = "Renamed before commit"
        await db.flush()
        session = await db.get(Session, task.execution_session_id)
        assert await held_task_locked(db, session) is None
        assert evidence_cache.stats["task_sources.hit"] == hits
        await db.rollback()
    assert await task_hold(task.execution_session_id, values[0]) is None


@pytest.mark.parametrize("read", ["unbounded_parts", "uncovered_table", "unbounded_events", "own_session"])
async def test_a_capture_that_cannot_prove_its_reads_caches_nothing(monkeypatch, read):
    values, task = await automatic_input(monkeypatch)
    evidence_cache.clear()
    from assistant import schedule_runs
    original = schedule_runs.validate_task_schedule_locked

    async def with_extra_read(db, current, **kwargs):
        if read == "unbounded_parts":
            # No key, message or session names a small set of rows.
            await db.scalar(select(Part.id).where(Part.type == "text", Part.user_id == current.user_id).limit(1))
        elif read == "uncovered_table":
            await db.execute(text("SELECT count(*) FROM notifications"))
        elif read == "own_session":
            async with get_db_session() as other:
                await other.get(AssistantTask, current.id)
        elif read == "unbounded_events":
            await db.scalar(select(AgentEvent.id).where(AgentEvent.kind == "part.updated",
                                                        AgentEvent.user_id == current.user_id).limit(1))
        return await original(db, current, **kwargs)

    monkeypatch.setattr(schedule_runs, "validate_task_schedule_locked", with_extra_read)
    for _ in range(3):
        assert await task_hold(task.execution_session_id, values[0]) is None
    assert evidence_cache.stats["task_sources.uncacheable"] == 3
    assert evidence_cache.stats["task_sources.hit"] == 0


async def test_covered_writes_bump_only_their_owner_and_table():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    sessions_key = evidence_schema.key("sessions", owner)
    before = await epochs()
    if postgres():
        # Only PostgreSQL filters volatile columns (SQLite bumps on any update).
        async with get_db_session() as db:
            row = await db.get(Session, main.id)
            row.status, row.updated_at, row.token_usage = "busy", NOW, {"input": 2}
        assert (await epochs()).get(sessions_key) == before.get(sessions_key)
    async with get_db_session() as db:
        (await db.get(Session, main.id)).title = "Renamed"
    changed = await epochs()
    assert changed[sessions_key] != before.get(sessions_key)
    assert changed.get(evidence_schema.key("sessions", other)) == before.get(evidence_schema.key("sessions", other))

    message_id, part_id = f"message-{uuid4().hex}", f"part-{uuid4().hex}"
    async with get_db_session() as db:
        db.add(Message(id=message_id, session_id=main.id, user_id=owner, role="user", created_at=NOW))
        await db.flush()
        db.add(Part(id=part_id, message_id=message_id, session_id=main.id, user_id=owner, type="text",
                    data={"type": "text", "text": "a"}, created_at=NOW))
    inserted = await epochs()
    # Streaming inserts never bump: reads of these rows are bounded by key.
    assert inserted.get(evidence_schema.key("parts", owner)) == changed.get(evidence_schema.key("parts", owner))
    async with get_db_session() as db:
        first = await db.scalar(select(Part.evidence_version).where(Part.id == part_id))
        part = await db.get(Part, part_id)
        part.data = {"type": "text", "text": "ab"}
    async with get_db_session() as db:
        second = await db.scalar(select(Part.evidence_version).where(Part.id == part_id))
        assert second is not None and second != first
        assert "evidence_version" not in part_dict(await db.get(Part, part_id))
        await db.delete(await db.get(Part, part_id))
    assert (await epochs()).get(evidence_schema.key("parts", owner)) != inserted.get(evidence_schema.key("parts", owner))

    # Events are rows too: an insert never bumps, an update changes only its version.
    events_key = evidence_schema.key("agent_events", owner)
    before_events = (await epochs()).get(events_key)
    event_id = f"event-{uuid4().hex}"
    async with get_db_session() as db:
        db.add(AgentEvent(id=event_id, session_id=main.id, user_id=owner, sequence=1, event_key=uuid4().hex,
                          kind="assistant.message.committed", payload={}, created_at=NOW))
    assert (await epochs()).get(events_key) == before_events
    async with get_db_session() as db:
        first = await db.scalar(select(AgentEvent.evidence_version).where(AgentEvent.id == event_id))
        (await db.get(AgentEvent, event_id)).payload = {"changed": True}
    async with get_db_session() as db:
        assert await db.scalar(select(AgentEvent.evidence_version).where(AgentEvent.id == event_id)) != first
    assert (await epochs()).get(events_key) == before_events


async def test_postgres_triggers_match_the_registry():
    if not postgres():
        pytest.skip("The migration installs PostgreSQL coverage")
    async with get_db_session() as db:
        rows = (await db.execute(text(
            "SELECT c.relname, t.tgname, encode(t.tgargs, 'escape') FROM pg_trigger t "
            "JOIN pg_class c ON c.oid = t.tgrelid WHERE t.tgname LIKE 'assistant_evidence_%'"))).all()
    installed = {(table, name): [arg for arg in args.split("\\000") if arg] for table, name, args in rows}
    for table, (kind, owner, volatile) in evidence_schema.COLD.items():
        assert installed[(table, "assistant_evidence_touch")] == [kind, owner, "-", *volatile], table
        assert (table, "assistant_evidence_flush") in installed
    for table, identity in evidence_schema.ROWS.items():
        assert installed[(table, "assistant_evidence_touch_update")] == [
            "u", evidence_schema.row_owner(table), "+", *identity]
        assert (table, "assistant_evidence_version") in installed
    # No insert of a hot row bumps an epoch.
    assert not {name for table, name in installed if table in evidence_schema.ROWS and "insert" in name}
    assert {table for table, _ in installed} == set(evidence_schema.COLD) | set(evidence_schema.ROWS)


async def test_postgres_epoch_bumps_add_no_lock_order_deadlock():
    """Two writers of one owner's rows, in opposite orders, still both commit."""
    if not postgres():
        pytest.skip("Row locks and deferred commit-time bumps need PostgreSQL")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    first_id, second_id = main.id, f"session-{uuid4().hex}"
    async with get_db_session() as db:
        db.add(Session(id=second_id, user_id=owner, workspace_id=workspace, project_id=main.project_id,
                       title="Second", created_at=NOW, updated_at=NOW))
    before = (await epochs()).get(evidence_schema.key("sessions", owner))
    async with get_db_session() as left, get_db_session() as right:
        (await left.get(Session, first_id)).title = "Left first"
        await left.flush()
        (await right.get(Session, second_id)).title = "Right second"
        await right.flush()

        async def left_then_second():
            (await left.get(Session, second_id)).title = "Left second"
            await left.flush()  # Waits on right's row lock, never on an epoch.
            await left.commit()
        waiting = asyncio.create_task(left_then_second())
        await asyncio.sleep(0.3)
        assert not waiting.done()
        await right.commit()
        await asyncio.wait_for(waiting, 10)
    assert (await epochs()).get(evidence_schema.key("sessions", owner)) != before


async def test_a_page_of_answers_is_proven_in_one_read(monkeypatch):
    from assistant.public_history import public_messages
    from session.session import get_messages
    from tests.unit.assistant_source_fixtures import consume_context
    from tests.unit.test_assistant_context_sources import finish, next_turn
    from tests.unit.test_assistant_reads import call_tool, read_turn
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        for index in range(2):
            await consume_context(ctx)
            await finish(ctx, lease, answer, f"PRIVATE_PAGE_ANSWER_{index}")
            ctx, lease, answer = await next_turn(ctx)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        main = await db.get(Session, ctx.session_id)
    messages = await get_messages(ctx.session_id, user_id=ctx.user_id)
    first = await public_messages(main, messages, actor_user_id=ctx.user_id)
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "on")
    with statements() as seen:
        second = await public_messages(main, messages, actor_user_id=ctx.user_id)
    assert [item["source_status"] for item in second] == [item["source_status"] for item in first]
    assert "available" in {item["source_status"] for item in second}
    assert evidence_cache.stats["message_sources.hit"] >= 2
    # One statement proves every answer and result verdict on the page.
    assert sum("ASSISTANT_EVIDENCE_EPOCHS" in statement for statement in seen) == 1


async def test_new_rows_outside_every_bounded_set_keep_the_closure(monkeypatch):
    values, task = await warmed(monkeypatch)
    owner = values[0]
    message_id, part_id = await grant_message(task)
    async with get_db_session() as db:
        original = await db.get(Part, part_id)
        # Another provider call, another answer and another input of the owner.
        db.add(AgentEvent(id=f"event-{uuid4().hex}", session_id=original.session_id, user_id=owner,
                          sequence=10_000, event_key=uuid4().hex, kind="model.requested", payload={},
                          message_id=f"message-{uuid4().hex}", created_at=NOW))
    hits = evidence_cache.stats["task_sources.hit"]
    assert await task_hold(task.execution_session_id, owner) is None
    assert evidence_cache.stats["task_sources.hit"] == hits + 1


async def test_a_new_row_inside_a_bounded_set_ends_reuse(monkeypatch):
    values, task = await warmed(monkeypatch)
    owner = values[0]
    message_id, part_id = await grant_message(task)
    async with get_db_session() as db:
        original = await db.get(Part, part_id)
        # The grant's own message gains a text part: its whole set is read.
        db.add(Part(id=f"part-{uuid4().hex}", message_id=message_id, session_id=original.session_id,
                    user_id=owner, type="text", data={"type": "text", "text": "Later"},
                    created_at=original.created_at + timedelta(seconds=2)))
    stale = evidence_cache.stats["task_sources.stale"]
    await task_hold(task.execution_session_id, owner)
    assert evidence_cache.stats["task_sources.stale"] == stale + 1


async def test_a_provider_projection_reuses_answer_verdicts_in_place_with_identical_output():
    import json
    from agent.loop import _to_llm_messages
    from assistant.projection import project_main_messages
    from session.agent_event_log import load_canonical_model_surface
    from tests.unit.assistant_source_fixtures import consume_context
    from tests.unit.test_assistant_context_sources import finish, next_turn
    from tests.unit.test_assistant_reads import call_tool, read_turn
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        for index in range(2):
            await consume_context(ctx)
            await finish(ctx, lease, answer, f"PRIVATE_PROJECTION_ANSWER_{index}")
            ctx, lease, answer = await next_turn(ctx)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        first = await project_main_messages(list(surface.messages), ctx=ctx)
        hits = evidence_cache.stats["message_sources.hit"]
        second = await project_main_messages(list(surface.messages), ctx=ctx)
        render = lambda projected: json.dumps(  # noqa: E731
            _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True), sort_keys=True)
        assert render(second) == render(first)
        assert "PRIVATE_PROJECTION_ANSWER_1" in render(second)
        # Each earlier answer's verdict joined the step's shared walk in place.
        assert evidence_cache.stats["message_sources.hit"] >= hits + 2
    finally:
        await lease.release(session_status="idle")


async def test_an_unrelated_new_command_keeps_the_tasks_closures(monkeypatch):
    values, task = await warmed(monkeypatch)
    owner, workspace = values[0], values[2]
    async with get_db_session() as db:
        # Another tool command of the same owner and main assistant.
        db.add(AssistantCommand(id=f"command-{uuid4().hex}", actor_user_id=owner, workspace_id=workspace,
                                assistant_session_id=task.assistant_session_id, idempotency_key=uuid4().hex,
                                action="task_create", target_type="task", target_id=None, payload_digest="0" * 64,
                                expected_revision=None, source_ref={}, state="accepted", receipt={},
                                created_at=NOW, updated_at=NOW))
    hits = evidence_cache.stats["task_sources.hit"]
    assert await task_hold(task.execution_session_id, owner) is None
    assert evidence_cache.stats["task_sources.hit"] == hits + 1
