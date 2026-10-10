"""Question resumes and delayed recovery retain their actual User anchors."""
import pytest
from sqlalchemy import select

from agent.driver import reserve_run
from agent.inbox import accept_inbox_item, claim_inbox_boundary
from agent.loop import scan_messages, should_terminate
from core.identifier import ascending
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from session import agent_event_log as event_log
from session.agent_event_log import AgentEventProjectionError, load_canonical_model_surface
from session.session import create_assistant_message, create_user_message, update_message_info
from tests.unit.test_agent_recovery import _seed_recovery_session


async def _waiting_steered_turn():
    user_id, session_id = await _seed_recovery_session("turn-anchor")
    trigger_id = ascending("message")
    lease = await reserve_run(session_id, user_id, trigger_message_id=trigger_id)
    fence = (session_id, lease.run_id, lease.generation)
    try:
        await create_user_message(
            session_id, "Make a video", user_id=user_id,
            message_id=trigger_id, run_fence=fence,
        )
        await lease.set_phase("running")
        first = await create_assistant_message(
            session_id, trigger_id, user_id=user_id, run_fence=fence,
        )
        first.finish = "tool_calls"
        await update_message_info(first, user_id=user_id, run_fence=fence)
        await accept_inbox_item(
            session_id=session_id, user_id=user_id,
            delivery="steer", prompt="Use this character",
        )
        batch = await claim_inbox_boundary(lease, step=2, include_next_turn=False)
        steer = batch.receipts[0]
        assert steer.turn_id == trigger_id
        waiting = await create_assistant_message(
            session_id, steer.message_id, user_id=user_id, run_fence=fence,
        )
        waiting.finish = "waiting_input"
        await update_message_info(waiting, user_id=user_id, run_fence=fence)
    finally:
        await lease.release(session_status="waiting_input")
    return user_id, session_id, trigger_id, steer.message_id


async def _events(session_id):
    async with get_db_session() as db:
        return list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
        ).order_by(AgentEvent.sequence))).scalars())


@pytest.mark.parametrize("read_before_write", [False, True])
async def test_question_resumes_inherit_steered_turn_across_multiple_runs(read_before_write):
    user_id, session_id, turn_id, steer_id = await _waiting_steered_turn()
    for _ in range(2):
        lease = await reserve_run(session_id, user_id, trigger_message_id=steer_id)
        fence = (session_id, lease.run_id, lease.generation)
        try:
            await lease.set_phase("running")
            if read_before_write:
                await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
            assistant = await create_assistant_message(
                session_id, steer_id, user_id=user_id, run_fence=fence,
            )
            assistant.finish = "waiting_input"
            await update_message_info(assistant, user_id=user_id, run_fence=fence)
            for _ in range(2):
                surface = await load_canonical_model_surface(
                    session_id, user_id=user_id, run_fence=fence,
                )
                assert next(m for m in surface.messages if m.id == assistant.id).parent_id == steer_id
            events = await _events(session_id)
            own_events = [e for e in events if e.run_id == lease.run_id]
            assert {e.turn_id for e in own_events} == {turn_id}
            starts = [e for e in own_events if e.kind == "turn.started"]
            assert len(starts) == 1
            assert starts[0].message_id == steer_id
        finally:
            await lease.release(session_status="waiting_input")
        parity = await event_log.verify_agent_event_parity(session_id, user_id=user_id)
        assert parity.ok, parity.model_dump()


async def test_existing_unanchored_resume_is_readable_without_rewriting_events(monkeypatch):
    user_id, session_id, turn_id, steer_id = await _waiting_steered_turn()
    lease = await reserve_run(session_id, user_id, trigger_message_id=steer_id)
    fence = (session_id, lease.run_id, lease.generation)

    async def old_turn_fallback(db, session, message, **kwargs):
        return message.id if message.role == "user" else message.parent_id

    try:
        # Reproduce the immutable production history written by the old code.
        with monkeypatch.context() as patch:
            patch.setattr(event_log, "_logical_turn_id_locked", old_turn_fallback)
            assistant = await create_assistant_message(
                session_id, steer_id, user_id=user_id, run_fence=fence,
            )
            assistant.finish = "aborted"
            await update_message_info(assistant, user_id=user_id, run_fence=fence)
    finally:
        await lease.release(session_status="error")
    before = await _events(session_id)
    assert {e.turn_id for e in before if e.message_id == assistant.id} == {steer_id}
    assert steer_id != turn_id
    for _ in range(2):
        surface = await load_canonical_model_surface(session_id, user_id=user_id)
        assert {m.id for m in surface.messages} >= {turn_id, steer_id, assistant.id}
        assert next(m for m in surface.messages if m.id == assistant.id).finish == "aborted"
    after = await _events(session_id)
    assert [(e.id, e.event_key, e.payload) for e in after] == [
        (e.id, e.event_key, e.payload) for e in before
    ]


async def test_delayed_recovery_reply_does_not_break_or_terminate_new_turn():
    user_id, session_id = await _seed_recovery_session("delayed-anchor")
    old_id = ascending("message")
    old = await reserve_run(session_id, user_id, trigger_message_id=old_id)
    try:
        await create_user_message(
            session_id, "Older unanswered input", user_id=user_id, message_id=old_id,
            run_fence=(session_id, old.run_id, old.generation),
        )
    finally:
        await old.release(session_status="idle")
    current_id = ascending("message")
    lease = await reserve_run(session_id, user_id, trigger_message_id=current_id)
    fence = (session_id, lease.run_id, lease.generation)
    try:
        await create_user_message(
            session_id, "New input", user_id=user_id,
            message_id=current_id, run_fence=fence,
        )
        await lease.set_phase("running")
        first = await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
        second = await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
        assert [m.id for m in first.messages] == [m.id for m in second.messages]
        assert [m.role for m in second.messages] == ["user", "user", "assistant"]
        assert second.messages[-1].parent_id == old_id
        assert second.messages[-1].finish == "aborted"
        scan = scan_messages(list(second.messages))
        assert scan.last_user.id == current_id
        assert not should_terminate(scan.last_assistant, scan.last_user)
        async with get_db_session() as db:
            replies = list((await db.execute(select(Message).where(
                Message.session_id == session_id, Message.role == "assistant",
            ))).scalars())
            assert len(replies) == 1  # Repeated reads never synthesize duplicate closures.
        reply = await create_assistant_message(
            session_id, current_id, user_id=user_id, run_fence=fence,
        )
        reply.finish = "stop"
        await update_message_info(reply, user_id=user_id, run_fence=fence)
        surface = await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
        scan = scan_messages(list(surface.messages))
        assert should_terminate(scan.last_assistant, scan.last_user)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("parent_kind", ["other_turn", "absent"])
async def test_explicit_turn_still_rejects_an_unrelated_or_absent_parent(parent_kind):
    user_id, session_id, _turn_id, steer_id = await _waiting_steered_turn()
    current_id = ascending("message")
    lease = await reserve_run(session_id, user_id, trigger_message_id=current_id)
    fence = (session_id, lease.run_id, lease.generation)
    try:
        await create_user_message(
            session_id, "Unrelated new turn", user_id=user_id,
            message_id=current_id, run_fence=fence,
        )
        await create_assistant_message(
            session_id, steer_id if parent_kind == "other_turn" else "missing-user",
            user_id=user_id, run_fence=fence,
        )
        with pytest.raises(AgentEventProjectionError, match="Assistant parent crosses its logical turn"):
            await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
    finally:
        await lease.release(session_status="error")


async def test_unstarted_resume_cannot_invent_a_missing_user_anchor():
    user_id, session_id, _turn_id, steer_id = await _waiting_steered_turn()
    lease = await reserve_run(session_id, user_id, trigger_message_id=steer_id)
    fence = (session_id, lease.run_id, lease.generation)
    try:
        await create_assistant_message(
            session_id, "absent-user", user_id=user_id, run_fence=fence,
        )
        with pytest.raises(AgentEventProjectionError, match="assistant tail has no User turn anchor"):
            await load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
    finally:
        await lease.release(session_status="error")
