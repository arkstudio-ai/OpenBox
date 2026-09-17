"""Delegated work stops with its run: subagents and in-run cron callbacks.

Revocation comes from another worker (the database only), so the lease checks
and the parent's heartbeat must notice without an in-process signal.
"""
import asyncio
import time
from uuid import uuid4

import pytest
from sqlalchemy import select

import db.base as database
from db.models.cron import CronRun
from db.models.message import Message
from db.models.part import Part
from db.models.question import SessionExecution
from models.message import ToolPartData, ToolStatus
from question import runtime
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import (  # noqa: F401
    acting_as,
    add_session,
    emitted,
    facts,
    loop_harness,
    recording,
    supersede_elsewhere,
)
from tool.tool import ToolContext


def endless_provider(monkeypatch, processor) -> asyncio.Event:
    """A provider that starts streaming and never answers; only an abort ends it."""
    started = asyncio.Event()

    async def stream(**kwargs):
        started.set()
        await asyncio.Event().wait()
        yield {}
    monkeypatch.setattr(processor, "stream_llm", stream)
    return started


async def test_parent_revoked_by_another_worker_stops_its_subagent_within_the_bound(
        state, loop_harness, monkeypatch):
    from core.config import get_config, ProviderConfig
    config = get_config().model_copy(deep=True)
    config.provider["openai"] = ProviderConfig(api_key="test-key", base_url="https://provider.invalid/v1")
    monkeypatch.setattr("core.config.get_config", lambda: config)
    from session.session import create_assistant_message, create_user_message, save_part, update_part_data
    from tool import task
    monkeypatch.setattr(runtime, "LEASE_SECONDS", 0.6)
    child_streaming = endless_provider(monkeypatch, loop_harness.processor)
    prompt = await create_user_message("s1", "Delegate the research", user_id="u1")
    from agent import driver
    from agent.subagent_authority import compose_subagent_authority
    from permission.permission import Rule
    lease = await driver.reserve_run("s1", "u1", trigger_message_id=prompt.id)
    parent = await runtime.start_run("s1", "u1", driver_lease=lease)
    lease_token = driver.bind_current_lease(lease)
    parent_abort = asyncio.Event()
    with acting_as(parent):
        assistant = await create_assistant_message("s1", prompt.id, user_id="u1")
        card = ToolPartData(tool="task", status=ToolStatus.RUNNING, input={}, call_id="delegate",
                            session_id="s1", message_id=assistant.id)
        await save_part(card, is_new=True, user_id="u1")
        ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1", message_id=assistant.id,
                          part_id=card.id, abort=parent_abort, run_id=lease.run_id,
                          run_generation=lease.generation)
        ctx._subagent_authority_snapshot = compose_subagent_authority(
            tool_ids=("task",), permission_rules=[Rule(permission="*", pattern="*", action="allow")],
            guard_rules=(),
        ).to_json()
        heartbeat = asyncio.create_task(runtime.heartbeat(parent, parent_abort))
        delegation = asyncio.create_task(task.execute(
            task.TaskArgs(description="Research", prompt="Look it up", subagent_type="explore"), ctx))
    try:
        waiter = asyncio.create_task(child_streaming.wait())
        done, _ = await asyncio.wait({waiter, delegation}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
        if delegation in done:
            await delegation
        assert waiter in done, "child provider did not start"
        child_id = (await read(Part, card.id)).data["metadata"]["child_session_id"]
        assert (await read(SessionExecution, child_id)).run_id is not None
        await supersede_elsewhere("s1")
        revoked_at = time.monotonic()
        await asyncio.wait_for(asyncio.shield(delegation), timeout=15)
        # Bound: one parent heartbeat (a third of the lease) plus the child's unwinding.
        assert time.monotonic() - revoked_at < 5
    finally:
        heartbeat.cancel()
        delegation.cancel()
        await asyncio.gather(heartbeat, delegation, return_exceptions=True)
        driver.reset_current_lease(lease_token)
        await lease.release()
    assert parent_abort.is_set() and runtime.is_revoked(parent.run_id)
    assert (await read(SessionExecution, child_id)).run_id is None

    # The superseded parent can no longer write its own tool card.
    late = {**(await read(Part, card.id)).data, "title": "late child pointer"}
    with acting_as(parent), pytest.raises(runtime.RunRevoked):
        await update_part_data(card.id, late, user_id="u1")
    assert (await read(Part, card.id)).data.get("title") != "late child pointer"


async def test_parent_superseded_elsewhere_spawns_no_subagent(state, monkeypatch):
    from tool import task
    spawned = []

    async def run_child(ctx, child_id):
        spawned.append(child_id)
    monkeypatch.setattr(task, "_run_child", run_child)
    parent = await runtime.start_run("s1", "u1")
    await supersede_elsewhere("s1")
    ctx = ToolContext(session_id="s1", user_id="u1", workspace_id="w1")
    with acting_as(parent), pytest.raises(runtime.RunRevoked):
        await task.execute(task.TaskArgs(description="Research", prompt="Look it up",
                                         subagent_type="explore"), ctx)
    assert spawned == []


async def test_cron_timeout_interrupts_the_run_and_releases_its_lease(
        state, recording, loop_harness, emitted, monkeypatch):
    from session.session import create_user_message
    streaming = endless_provider(monkeypatch, loop_harness.processor)
    await add_session("cron-run", kind="cron", parent_id="s1")
    await create_user_message("cron-run", "Scheduled task: check the dashboard", synthetic=True, user_id="u1")
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(loop_harness.loop.run_loop("cron-run", user_id="u1"), timeout=1.5)
    assert streaming.is_set()
    execution = await read(SessionExecution, "cron-run")
    assert execution.run_id is None and execution.lease_until is None
    if recording:
        terminal = [fact["data"] for fact in facts(emitted, "run.finished", "run.interrupted")
                    if fact.get("source_session_id") == "cron-run"]
        assert [(data["status"], data["reason"]) for data in terminal] == [("cancelled", "interrupted")]


async def _cron_result(summary: str) -> str:
    run_id = uuid4().hex
    async with database.get_db_session() as db:
        db.add(CronRun(id=run_id, job_id=uuid4().hex, user_id="u1", session_id="s1", status="ok",
                       task_prompt="Check the dashboard", summary_text=summary, injected=False,
                       started_at=runtime.now()))
    return run_id


async def _injected_rows(run_id: str) -> list:
    """The injected user message, its reply and their parts."""
    async with database.get_db_session() as db:
        user = await db.scalar(select(Message).where(Message.client_message_id == f"cron:{run_id}"))
        if user is None:
            return []
        replies = (await db.scalars(select(Message).where(Message.parent_id == user.id))).all()
        message_ids = [user.id, *(reply.id for reply in replies)]
        parts = (await db.scalars(select(Part).where(Part.message_id.in_(message_ids)))).all()
    return [user, *replies, *parts]


async def test_revoked_run_flush_consumes_no_cron_result(state, recording):
    from cron.injector import flush_pending_cron_results
    run_id = await _cron_result("The dashboard is green")
    ticket = await runtime.start_run("s1", "u1")
    await supersede_elsewhere("s1")
    with acting_as(ticket):
        with pytest.raises(runtime.RunRevoked):
            await runtime.assert_current("flush")
        with pytest.raises(runtime.RunRevoked):
            await flush_pending_cron_results("s1", "u1")
    assert not (await read(CronRun, run_id)).injected
    assert await _injected_rows(run_id) == []


async def test_cron_injection_commits_messages_and_consumption_together_with_recording_off(state, monkeypatch):
    import session.session as sessions
    from cron import injector
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "false")
    run_id = await _cron_result("The dashboard is green")
    job = {"id": "job", "session_id": "s1", "user_id": "u1", "name": "Dashboard", "task_prompt": "Check it"}
    real = sessions.record_projection_in_tx
    projections = []

    async def failing_midway(db, session_id, user_id, event_type, data, **kwargs):
        projections.append(event_type)
        if len(projections) == 3:
            raise RuntimeError("injected failure after the first message")
        return await real(db, session_id, user_id, event_type, data, **kwargs)
    monkeypatch.setattr(sessions, "record_projection_in_tx", failing_midway)
    assert await injector.try_inject_result(run_id, job, "The dashboard is green") is False
    assert await _injected_rows(run_id) == []
    assert not (await read(CronRun, run_id)).injected

    monkeypatch.setattr(sessions, "record_projection_in_tx", real)
    assert await injector.try_inject_result(run_id, job, "The dashboard is green") is True
    assert len(await _injected_rows(run_id)) == 4
    assert (await read(CronRun, run_id)).injected
    # A repeated delivery consumes nothing twice.
    assert await injector.try_inject_result(run_id, job, "The dashboard is green") is True
    assert len(await _injected_rows(run_id)) == 4
