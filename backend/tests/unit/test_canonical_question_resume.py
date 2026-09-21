"""A durable answer resumes the logical turn that received an Inbox steer."""
import pytest
from sqlalchemy import select

from agent.driver import reserve_run
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from models.message import MessageInfo, MessageRole, ToolPartData, ToolStatus
from session import agent_event_log as event_log
from session.session import create_assistant_message, create_user_message, save_part, update_message_info
from tests.unit.test_canonical_model_surface import _seed_session


@pytest.mark.parametrize("legacy_turn_alias", [False, True])
async def test_answer_after_steer_keeps_original_anchor_across_new_lease(monkeypatch, legacy_turn_alias):
    user_id, session_id = await _seed_session()
    first = await reserve_run(session_id, user_id, run_id="before-question")
    fence = (session_id, first.run_id, first.generation)
    original = await create_user_message(session_id, "Start a team", user_id=user_id, run_fence=fence)
    await first.bind_trigger_message(original.id)
    await first.set_phase("running")
    steer = await create_user_message(session_id, "Member is blocked; ask for its scope",
        synthetic=True, user_id=user_id, run_fence=fence)
    question = await create_assistant_message(session_id, steer.id, user_id=user_id, run_fence=fence)
    part = ToolPartData(tool="question", status=ToolStatus.COMPLETED, call_id="answered-scope",
        output="User approved exactly this path", session_id=session_id, message_id=question.id)
    await save_part(part, is_new=True, user_id=user_id, run_fence=fence)
    await update_message_info(MessageInfo(id=question.id, sessionID=session_id,
        role=MessageRole.ASSISTANT, finish="tool_calls"), user_id=user_id, run_fence=fence)
    await first.release(session_status="waiting_input")

    current = await reserve_run(session_id, user_id, run_id="after-answer")
    current_fence = (session_id, current.run_id, current.generation)
    await current.bind_trigger_message(steer.id)
    await current.set_phase("running")
    if legacy_turn_alias:
        real_resolver = event_log._logical_turn_id_locked

        async def old_resolver(db, session, message, **kwargs):
            if kwargs.get("run_fence") == current_fence:
                return steer.id
            return await real_resolver(db, session, message, **kwargs)

        # Reproduce already-persisted history without modifying its events.
        monkeypatch.setattr(event_log, "_logical_turn_id_locked", old_resolver)
    resumed = await create_assistant_message(session_id, steer.id, user_id=user_id, run_fence=current_fence)
    if legacy_turn_alias:
        monkeypatch.setattr(event_log, "_logical_turn_id_locked", real_resolver)
    async with get_db_session() as db:
        event = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == resumed.id,
            AgentEvent.kind == "message.created"))
        assert event.turn_id == (steer.id if legacy_turn_alias else original.id)

    surface = await event_log.load_canonical_model_surface(session_id, user_id=user_id, run_fence=current_fence)
    by_id = {message.id: message for message in surface.messages}
    assert by_id[resumed.id].finish is None  # Never abort the resumed live step.
    assert by_id[resumed.id].parent_id == steer.id
    assert by_id[question.id].finish == "tool_calls"
    assert next(p for p in by_id[question.id].parts if p.type == "tool").output == part.output
    await current.release(session_status="idle")
    # Maintenance and repeated model reads also accept the immutable prefix.
    await event_log.load_canonical_model_surface(session_id, user_id=user_id)
    again = await event_log.load_canonical_model_surface(session_id, user_id=user_id)
    assert next(message for message in again.messages if message.id == resumed.id).finish == "aborted"


async def test_recovery_replies_to_old_queued_users_do_not_orphan_their_turns():
    user_id, session_id = await _seed_session()
    older = []
    for index in range(2):
        lease = await reserve_run(session_id, user_id, run_id=f"queued-{index}")
        await lease.stop_monitor()
        fence = (session_id, lease.run_id, lease.generation)
        user = await create_user_message(session_id, "Team control input", synthetic=True,
            user_id=user_id, run_fence=fence)
        await lease.bind_trigger_message(user.id)
        older.append(user.id)
        await lease.release(session_status="idle")
    live = await reserve_run(session_id, user_id, run_id="resumed-after-controls")
    await live.stop_monitor()
    fence = (session_id, live.run_id, live.generation)
    newest = await create_user_message(session_id, "Resume after answer", synthetic=True,
        user_id=user_id, run_fence=fence)
    await live.bind_trigger_message(newest.id)
    await live.set_phase("running")
    # This appends repair replies after newest in the SQL creation ordering.
    first = await event_log.load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
    repaired = [m for m in first.messages if m.parent_id in older]
    assert len(repaired) == 2 and all(m.finish == "aborted" for m in repaired)
    from agent.loop import scan_messages, should_terminate
    scan = scan_messages(list(first.messages))
    assert scan.last_user.id == newest.id
    assert not should_terminate(scan.last_assistant, scan.last_user)
    reply = await create_assistant_message(session_id, newest.id, user_id=user_id, run_fence=fence)
    second = await event_log.load_canonical_model_surface(session_id, user_id=user_id, run_fence=fence)
    assert next(m for m in second.messages if m.id == reply.id).finish is None
    assert len([m for m in second.messages if m.parent_id in older]) == 2
    await live.release(session_status="idle")
