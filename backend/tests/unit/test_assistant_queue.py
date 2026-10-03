"""Persistent main turn fairness through real Inbox/Driver claim transactions."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from assistant.inputs import accept_turn
from assistant.service import ensure_main_session
from db.base import close_engine, get_db_session, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.internal_parts import _lock_fenced, begin_session_write
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def setup():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    return owner, workspace, main


async def enqueue(owner, workspace, main, mode, number):
    if mode == "ordinary":
        return (await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
            client_id=f"human-{number}", text=f"Human question {number}"))["inbox_id"]
    async with get_db_session() as db:
        await begin_session_write(db)
        locked = await _lock_fenced(db, main.id, owner)
        receipt = await inbox.accept_inbox_item_locked(db, locked, delivery="followup",
            prompt=f"Queued result {number}", client_id=f"report-{number}", origin="task_result",
            origin_ref={"result_id": f"result-{number}", "report_attempt": 1, "execution_mode": "report_only"})
        return receipt.id


async def take(owner, main):
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert len(batch.receipts) == 1
        assert not (await inbox.claim_inbox_boundary(lease, step=2, include_next_turn=False)).receipts
        receipt = batch.receipts[0]
        await inbox.settle_claimed_inbox_items(lease, outcome="aborted", result_message_id=None)
        return receipt.origin, receipt.id
    finally:
        await lease.release(session_status="idle")


async def test_result_backlog_cannot_starve_new_human_turns():
    owner, workspace, main = await setup()
    reports = [await enqueue(owner, workspace, main, "report_only", i) for i in range(5)]
    people = [await enqueue(owner, workspace, main, "ordinary", i) for i in range(3)]
    observed = [await take(owner, main) for _ in range(5)]
    assert [mode for mode, _ in observed] == ["human", "human", "task_result", "human", "task_result"]
    assert [key for mode, key in observed if mode == "human"] == people
    assert [key for mode, key in observed if mode == "task_result"] == reports[:2]


async def test_continuous_human_input_cannot_starve_results():
    owner, workspace, main = await setup()
    report = await enqueue(owner, workspace, main, "report_only", 1)
    origins = []
    for index in range(3):
        await enqueue(owner, workspace, main, "ordinary", index)
        origins.append(await take(owner, main))
    assert origins[-1] == ("task_result", report)
    async with get_db_session() as db:
        history = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.queue.claimed").order_by(AgentEvent.sequence))).all())
        assert [e.payload["mode"] for e in history] == ["ordinary", "ordinary", "report_only"]


async def test_overdue_report_uses_next_free_boundary_without_waiting_for_full_burst():
    owner, workspace, main = await setup()
    await enqueue(owner, workspace, main, "ordinary", 0)
    await take(owner, main)
    report = await enqueue(owner, workspace, main, "report_only", 1)
    await enqueue(owner, workspace, main, "ordinary", 1)
    async with get_db_session() as db:
        (await db.get(AgentInboxItem, report)).created_at = datetime.now(timezone.utc) - timedelta(seconds=31)
    assert await take(owner, main) == ("task_result", report)


async def test_failed_claim_rolls_back_scheduling_history_and_input():
    owner, workspace, main = await setup()
    item = await enqueue(owner, workspace, main, "ordinary", 0)
    lease = await reserve_run(main.id, owner)
    def fail(phase):
        if phase == "materialized":
            raise RuntimeError("claim rollback")
    try:
        with pytest.raises(RuntimeError, match="claim rollback"):
            await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True, fault=fail)
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, item)).state == "accepted"
            assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == main.id,
                AgentEvent.kind == "assistant.queue.claimed"))
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert [r.id for r in batch.receipts] == [item]
        await inbox.settle_claimed_inbox_items(lease, outcome="aborted", result_message_id=None)
    finally:
        await lease.release()


async def test_new_engine_keeps_the_committed_turn_budget():
    owner, workspace, main = await setup()
    report = await enqueue(owner, workspace, main, "report_only", 0)
    for index in range(3):
        await enqueue(owner, workspace, main, "ordinary", index)
    assert (await take(owner, main))[0] == "human"
    assert (await take(owner, main))[0] == "human"
    async with get_db_session() as db:
        url = db.get_bind().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await take(owner, main) == ("task_result", report)
