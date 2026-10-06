"""Monitor observations read current task state, never dispatch authority.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 4.2): a hold check reads only current
permission (membership, ownership, deletion, task controls). It does not walk
the Task's command or continuation sources, so editing them is not retroactive.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from agent.driver import reserve_run
from assistant import continuation
from assistant.commands import accept_task_command
from assistant.scheduling import TaskSchedulingHeld, observe_task_hold, task_hold
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import create_session
from tests.unit.test_assistant_commands import setup_task
from tests.unit.assistant_helpers import call_part, coordinator, ready
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_scheduling import hold


async def granted_task(monkeypatch, key="granted-task"):
    """A task submitted with a retained continuation grant (no report needed)."""
    from tests.unit.assistant_helpers import CONTINUATION_QUOTE
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *args: None)
    values = await setup_task()
    receipt = await accept_task_command(**{**values[-1], "idempotency_key": key, "prompt": CONTINUATION_QUOTE,
        "continuation": {"authorization_quote": CONTINUATION_QUOTE, "max_followups": 1,
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}})
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
    assert task.continuation_policy["grant_command_id"]
    return values, task


async def automatic_input(monkeypatch):
    values, task, result_id = await ready(monkeypatch)
    ctx, lease, _ = await coordinator(values, task, result_id)
    request = {"decision": "continue", "instructions": "Finish the original report."}
    try:
        await call_part(ctx, "tasks.next_step", request)
        await continuation.next_step(ctx, request)
    finally:
        await lease.release(session_status="idle")
    return values, task


async def test_monitor_observation_is_one_bounded_read_without_writes_locks_or_source_walks(monkeypatch):
    values, task = await granted_task(monkeypatch)
    plain = await accept_task_command(**{**values[-1], "idempotency_key": "plain-task"})
    statements = []

    def observed(*args):
        statements.append(args[2].lstrip().upper())

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", observed)
    try:
        assert await task_hold(task.execution_session_id, values[0]) is None
        boundary_queries = len(statements)
        statements.clear()
        assert await observe_task_hold(task.execution_session_id, values[0]) is None
        assert len(statements) == boundary_queries
        assert not any(s.startswith(("UPDATE", "INSERT", "DELETE")) or "FOR UPDATE" in s
                       or "FOR NO KEY UPDATE" in s for s in statements)
        # A task with a continuation grant costs the same as a plain task:
        # its command and grant sources are not read.
        statements.clear()
        assert await observe_task_hold(plain["execution_session_id"], values[0]) is None
        assert len(statements) == boundary_queries
    finally:
        event.remove(engine, "before_cursor_execute", observed)


@pytest.mark.parametrize("change", ["source", "expiry"])
async def test_next_monitor_poll_does_not_revisit_original_continuation_sources(monkeypatch, change):
    values, task = await automatic_input(monkeypatch)
    lease = await reserve_run(task.execution_session_id, values[0])
    await lease.stop_monitor()
    try:
        assert not await lease.abort_was_requested()
        if change == "source":
            async with get_db_session() as db:
                command = await db.get(AssistantCommand, task.continuation_policy["grant_command_id"])
                item = await db.get(AgentInboxItem, command.source_ref["continuation_grant"]["inbox_id"])
                part = await db.scalar(select(Part).where(Part.message_id == item.message_id, Part.type == "text"))
                part.data = {**part.data, "text": "Changed original authorization"}
        else:
            class Later(datetime):
                @classmethod
                def now(cls, tz=None):
                    return datetime.now(tz) + timedelta(days=2)
            monkeypatch.setattr(continuation, "datetime", Later)
        # D1/4.2: the already accepted step keeps running; expiry governs new
        # next steps (continuation.active_grant_locked), not current permission.
        assert not await lease.abort_was_requested()
        assert await observe_task_hold(task.execution_session_id, values[0]) is None
        assert await task_hold(task.execution_session_id, values[0]) is None
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["membership", "paused"])
async def test_next_monitor_poll_stops_on_current_permission_or_control(monkeypatch, change):
    values, task = await granted_task(monkeypatch)
    lease = await reserve_run(task.execution_session_id, values[0])
    await lease.stop_monitor()
    try:
        assert not await lease.abort_was_requested()
        if change == "membership":
            async with get_db_session() as db:
                (await db.get(WorkspaceMember, (values[2], values[0]))).status = "removed"
        else:
            await hold(task.id, "paused")
        assert await lease.abort_was_requested()
        assert not lease._lost  # The original worker can still settle existing facts.
        expected = "unavailable" if change == "membership" else "paused"
        assert (await observe_task_hold(task.execution_session_id, values[0])).state == expected
        assert (await task_hold(task.execution_session_id, values[0])).state == expected
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("state", ["paused", "canceled"])
async def test_new_observations_follow_parent_controls_and_missing_lineage(state):
    owner, _, workspace, main, command = await setup_task()
    receipt = await accept_task_command(**command)
    child = await create_session(user_id=owner, workspace_id=workspace,
                                 parent_id=receipt["execution_session_id"])
    assert await observe_task_hold(child.id, owner) is None
    await hold(receipt["task_id"], state)
    expected = await task_hold(child.id, owner)
    assert expected.state == state
    assert await observe_task_hold(child.id, owner) == expected
    assert await observe_task_hold(main.id, owner) is None
    async with get_db_session() as db:
        (await db.get(Session, receipt["execution_session_id"])).is_deleted = True
    assert (await observe_task_hold(child.id, owner)).state == "unavailable"


async def test_inflight_observation_does_not_block_renewal_or_authorize_later_dispatch(monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent monitor/writer snapshots require PostgreSQL")
    from agent.llm import stream_llm
    from session.agent_event_log import checkpoint_model_request
    from tool.tool import ToolContext

    owner, _, workspace, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    lease = await reserve_run(receipt["execution_session_id"], owner)
    await lease.stop_monitor()
    entered, resume = asyncio.Event(), asyncio.Event()
    from assistant import commands
    original = commands._project

    async def waiting(*args, **kwargs):
        # The first project check after setup belongs to the monitor observation.
        if not entered.is_set():
            entered.set()
            await resume.wait()
        return await original(*args, **kwargs)

    async def forbidden(*args, **kwargs):
        raise AssertionError("Revoked monitor observation must not reach provider billing")

    monkeypatch.setattr(commands, "_project", waiting)
    monkeypatch.setattr("billing.service.UsageMeter.start", forbidden)
    reading = asyncio.create_task(observe_task_hold(lease.session_id, owner))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert await asyncio.wait_for(lease.renew(), 5)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
        # Even while an older allowed snapshot is in flight, actual provider
        # admission must read current authority on an independent connection.
        with pytest.raises(TaskSchedulingHeld):
            await checkpoint_model_request(lease.session_id, user_id=owner,
                run_fence=(lease.session_id, lease.run_id, lease.generation),
                request_id="after-monitor-revocation", model_id="test/model",
                provider_binding_digest="a" * 64, tool_schema_digest="b" * 64,
                prompt_shape_digest="c" * 64, expected_event_sequence=1,
                expected_event_digest="d" * 64)
        ctx = ToolContext(session_id=lease.session_id, user_id=owner, abort=lease.abort)
        with pytest.raises(TaskSchedulingHeld):
            async for _ in stream_llm(None, [], [], {}, "test/model", ctx):
                raise AssertionError("Revoked task must not produce provider output")
        resume.set()
        assert await asyncio.wait_for(reading, 5) is None
        assert await lease.abort_was_requested()
        assert not lease._lost
    finally:
        resume.set()
        await asyncio.gather(reading, return_exceptions=True)
        await lease.release(session_status="idle")


async def test_monitor_reads_explicit_aborts_every_poll_and_replays_holds_at_its_own_pace(monkeypatch):
    from agent import driver
    from db.models.agent_driver import AgentDriverState
    owner, _, _, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    observations, polls = [], []
    original_poll = driver.RunLease.abort_was_requested

    async def observe(session_id, user_id):
        observations.append(session_id)
        return None

    async def poll(self, **kwargs):
        polls.append(kwargs.get("observe_hold", True))
        return await original_poll(self, **kwargs)

    monkeypatch.setattr("assistant.scheduling.observe_task_hold", observe)
    monkeypatch.setattr(driver.RunLease, "abort_was_requested", poll)
    monkeypatch.setattr(driver, "ABORT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(driver, "HOLD_OBSERVE_SECONDS", 0.2)
    lease = await reserve_run(receipt["execution_session_id"], owner)
    try:
        async def observed():
            while not observations:
                await asyncio.sleep(0.01)
        await asyncio.wait_for(observed(), 5)
        await asyncio.sleep(0.25)
        # The hold read is paced; the cheap driver row is read on every poll.
        assert len(polls) >= 4 * len(observations)
        assert polls.count(True) == len(observations)
        # A direct check, unlike a paced monitor poll, always reads the hold.
        await lease.stop_monitor()
        before = len(observations)
        assert not await original_poll(lease)
        assert len(observations) == before + 1
        lease.start_monitor()
        async with get_db_session() as db:
            row = await db.get(AgentDriverState, lease.session_id)
            row.abort_requested_at = row.updated_at = datetime.now(timezone.utc)
        # An explicit control is read by the next cheap poll, not the replay.
        await asyncio.wait_for(lease.abort.wait(), 5)
    finally:
        await lease.release(session_status="idle")
