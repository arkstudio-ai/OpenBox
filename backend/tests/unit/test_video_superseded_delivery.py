"""A finished clip made for a goal its task has left is still shown, once, and only into a conversation at
rest (docs/VOICE_DISPATCH_PLAN_20261112.md §4.4; light-test 2026-11-09: two paid clips stayed invisible)."""
from sqlalchemy import select, update

from assistant.commands import accept_task_command
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_video_job_recovery import NOW, _fetch, _insert_job
from video import job_recovery


SETTLED = dict(state="settled", run_id="run-1", generation=1, turn_id="turn-1", step_id="step-1",
               claim_token="token-1", claim_owner="test", claimed_at=NOW(), settled_at=NOW())


async def _task_with_finished_clip():
    owner, _, _, _, kwargs = await setup_task()
    created = await accept_task_command(**kwargs)
    session_id = created["execution_session_id"]
    job_id = await _insert_job(age_seconds=600, user_id=owner, session_id=session_id, status="completed",
                               completed_at=NOW(), result_data={"completion_delivery_pending": True})
    return owner, session_id, job_id


async def _messages(session_id):
    async with get_db_session() as db:
        return list((await db.scalars(select(Message).where(Message.session_id == session_id)
                                      .order_by(Message.created_at, Message.id))).all())


async def test_nothing_is_written_while_the_conversation_has_work_queued():
    owner, session_id, job_id = await _task_with_finished_clip()
    assert await job_recovery.deliver_superseded(await _fetch(job_id)) is False  # the opening input is queued
    assert (await _fetch(job_id)).attached_message_id is None and not await _messages(session_id)


async def test_a_conversation_at_rest_gets_the_clip_in_its_own_exchange_once():
    owner, session_id, job_id = await _task_with_finished_clip()
    async with get_db_session() as db:
        await db.execute(update(AgentInboxItem).where(AgentInboxItem.session_id == session_id)
                         .values(**SETTLED, outcome="completed"))
    assert await job_recovery.deliver_superseded(await _fetch(job_id)) is True
    rows = await _messages(session_id)
    assert [row.role for row in rows] == ["user", "assistant"]
    assert (await _fetch(job_id)).attached_message_id == rows[1].id
    assert await job_recovery.deliver_superseded(await _fetch(job_id)) is False  # never a second time
    assert len(await _messages(session_id)) == 2


async def test_a_wake_a_card_closed_unrun_gets_a_fresh_key():
    owner, session_id, job_id = await _task_with_finished_clip()
    job = await _fetch(job_id)
    assert await job_recovery._wake_key(job) == f"vjob:{job_id}"
    async with get_db_session() as db:
        first = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == session_id))
        first.client_id, first.outcome = f"vjob:{job_id}", "not_run"
        for key, value in SETTLED.items():
            setattr(first, key, value)
    assert await job_recovery._wake_key(job) == f"vjob:{job_id}:1"
