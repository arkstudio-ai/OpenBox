"""Close a canceled, suspended turn under its existing Session/Driver locks.

A Question has already released its Driver. Cancellation is a control-plane
completion of that original run, never a new lease or another model request.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, func, select, type_coerce

from assistant.policy import AssistantError
from core.identifier import ascending
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.question import QuestionCheckpoint, SessionExecution


@dataclass(frozen=True)
class SuspendedCompletion:
    """Original terminal identity only; it cannot admit execution or tools."""
    session_id: str
    user_id: str
    run_id: str
    generation: int


async def cancel_suspended_locked(db, session):
    """Caller holds the Session and its Task's durable canceled-state fence."""
    driver = await db.scalar(select(AgentDriverState).where(
        AgentDriverState.session_id == session.id).with_for_update())
    if driver is None or driver.user_id != session.user_id or driver.phase != "idle" or driver.run_id:
        return
    execution = await db.scalar(select(SessionExecution).where(
        SessionExecution.session_id == session.id).with_for_update())
    if execution is None or execution.user_id != session.user_id or execution.run_id:
        return
    questions = list((await db.scalars(select(QuestionCheckpoint).where(
        QuestionCheckpoint.session_id == session.id, QuestionCheckpoint.user_id == session.user_id,
        QuestionCheckpoint.generation == execution.generation,
        QuestionCheckpoint.status.in_(("pending", "answered", "rejected")),
    ))).all())
    if not questions or not (execution.resume_pending or any(not row.applied for row in questions)):
        return
    terminal = await db.scalar(select(AgentEvent).where(
        AgentEvent.session_id == session.id, AgentEvent.user_id == session.user_id,
        AgentEvent.generation == driver.generation, AgentEvent.run_id.is_not(None),
        AgentEvent.kind == "turn.finished", AgentEvent.message_id.in_([row.message_id for row in questions]),
        type_coerce(AgentEvent.payload, JSON)["finish"].as_string() == "waiting_input",
    ).order_by(AgentEvent.sequence.desc()).limit(1))
    if terminal is None or not terminal.turn_id:
        raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The suspended task source needs review")
    latest_terminal = await db.scalar(select(AgentEvent.id).where(
        AgentEvent.session_id == session.id, AgentEvent.kind == "turn.finished",
        AgentEvent.turn_id == terminal.turn_id,
    ).order_by(AgentEvent.sequence.desc()).limit(1))
    if latest_terminal != terminal.id:
        raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The original task already advanced")
    waiting = await db.get(Message, terminal.message_id)
    if (waiting is None or waiting.session_id != session.id or waiting.user_id != session.user_id
            or waiting.finish not in {"waiting_input", "tool_calls", "tool-calls"} or waiting.error):
        raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The suspended task source changed")
    parent = await db.scalar(select(Message).where(Message.id == waiting.parent_id,
        Message.session_id == session.id, Message.user_id == session.user_id, Message.role == "user"))
    if parent is None:
        raise AssistantError(409, "ASSISTANT_STOP_TARGET_CHANGED", "The original task input is unavailable")
    original = SuspendedCompletion(session.id, session.user_id, terminal.run_id, driver.generation)
    fence = (session.id, original.run_id, original.generation)
    from question import runtime
    from session.agent_event_log import append_agent_event_locked, append_message_events_locked, ensure_surface_seed_locked
    await ensure_surface_seed_locked(db, session)
    invalidated = await runtime.invalidate_locked(db, execution, "cancelled")
    now = datetime.now(timezone.utc)
    latest = await db.scalar(select(func.max(Message.created_at)).where(Message.session_id == session.id))
    if latest is not None:
        latest = latest.replace(tzinfo=timezone.utc) if latest.tzinfo is None else latest
        now = max(now, latest + timedelta(microseconds=1))
    answer = Message(id=ascending("message"), session_id=session.id, user_id=session.user_id,
        role="assistant", parent_id=parent.id, agent=waiting.agent, model_id=waiting.model_id, created_at=now)
    db.add(answer)
    await db.flush()
    await append_message_events_locked(db, session, answer, operation="created",
        run_fence=fence, logical_turn_id=terminal.turn_id)
    answer.finish = "aborted"
    await db.flush()
    await append_message_events_locked(db, session, answer, operation="updated",
        run_fence=fence, logical_turn_id=terminal.turn_id)
    # A crash can leave the old Inbox claimed after the Question became durable.
    # Settle only that original run; previously settled input is not rewritten.
    claimed = list((await db.scalars(select(AgentInboxItem).where(
        AgentInboxItem.session_id == session.id, AgentInboxItem.user_id == session.user_id,
        AgentInboxItem.state == "claimed", AgentInboxItem.run_id == original.run_id,
        AgentInboxItem.generation == original.generation, AgentInboxItem.turn_id == terminal.turn_id,
    ).with_for_update())).all())
    for item in claimed:
        item.state, item.outcome, item.result_message_id = "settled", "aborted", answer.id
        item.claim_expires_at = None
        item.settled_at = item.updated_at = now
        await append_agent_event_locked(db, session, kind="inbox.settled", payload={
            "item_id": item.id, "state": "settled", "outcome": "aborted", "result_message_id": answer.id,
        }, run_fence=fence, turn_id=item.turn_id, message_id=item.message_id,
            idempotency_key=f"inbox:{item.id}:suspended-cancel:{original.run_id}:{original.generation}")
    from assistant.results import record_execution_result_locked
    await record_execution_result_locked(db, session, lease=original, result_message_id=answer.id,
        inbox_rows=claimed, outcome="aborted", now=now)
    session.status, session.updated_at = "idle", now
    # Result delivery already has a durable periodic outbox. UI hints publish
    # only after the whole cancellation/terminal transaction commits.
    from bus import bus
    runtime._after_commit(db, lambda: runtime.publish_invalidated(invalidated))
    payload = {"userId": session.user_id, "sessionId": session.id,
        "generation": original.generation, "status": "idle"}
    runtime._after_commit(db, lambda: bus.publish("session.status", payload))
