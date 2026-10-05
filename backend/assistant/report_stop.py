"""Complete an explicitly stopped report in the same transaction as revocation."""
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select

from core.identifier import ascending
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.message import Message
from db.models.part import Part
from models.message import StepFinishPart


async def stop_report_locked(db, main, *, expected_run_id: str) -> bool:
    """Caller holds Session and revokes this run before committing.

    Only a persisted stop request for the same Driver can close its report.
    This is control-plane settlement: it cannot run tools, reopen an attempt,
    or turn partial provider output into a successful report.
    """
    if main.kind != "assistant":
        return False
    driver = await db.scalar(select(AgentDriverState).where(
        AgentDriverState.session_id == main.id, AgentDriverState.user_id == main.user_id,
    ).with_for_update())
    if (driver is None or driver.run_id != expected_run_id or driver.phase == "idle"
            or driver.abort_requested_at is None):
        return False
    claimed = await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
        AgentInboxItem.run_id == driver.run_id, AgentInboxItem.generation == driver.generation,
        AgentInboxItem.origin == "task_result", AgentInboxItem.state == "claimed",
    ).limit(1))
    if claimed is None:
        return False
    from assistant.reporting import bound_report_locked, mark_report_failed
    from session.agent_event_log import append_agent_event_locked, append_message_events_locked, append_part_event_locked
    binding = await bound_report_locked(db, main, run_id=driver.run_id,
                                        generation=driver.generation, verify_sources=False)
    if binding is None:
        return False
    item, result = binding.inbox, binding.result
    fence = (main.id, driver.run_id, driver.generation)
    now = datetime.now(timezone.utc)
    message_ids = select(AgentEvent.message_id).where(
        AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
        AgentEvent.run_id == driver.run_id, AgentEvent.generation == driver.generation,
        AgentEvent.turn_id == item.turn_id, AgentEvent.kind == "message.created",
    )
    messages = list((await db.scalars(select(Message).where(
        Message.id.in_(message_ids), Message.session_id == main.id,
        Message.user_id == main.user_id, Message.role == "assistant",
    ).order_by(Message.created_at, Message.id))).all())
    if not messages:
        # A stop can arrive after claim but before the first provider step.
        answer = Message(id=ascending("message"), session_id=main.id, user_id=main.user_id,
            role="assistant", parent_id=item.message_id, agent="assistant", model_id=main.model,
            created_at=now)
        db.add(answer)
        await db.flush()
        await append_message_events_locked(db, main, answer, operation="created",
            run_fence=fence, logical_turn_id=item.turn_id)
        messages.append(answer)
    for message in messages:
        parts = list((await db.scalars(select(Part).where(Part.message_id == message.id,
            Part.session_id == main.id, Part.user_id == main.user_id,
        ).order_by(Part.created_at, Part.id))).all())
        finishes = Counter(part.data.get("step") for part in parts if part.type == "step-finish")
        for part in parts:
            if part.type == "tool" and part.data.get("status") in {"pending", "running"}:
                status = part.data["status"]
                part.data = {**part.data, "status": "error", "error": "Report stopped by the user.",
                    "metadata": {**(part.data.get("metadata") or {}),
                        "execution_outcome": "unknown" if status == "running" else "not_started"}}
                await append_part_event_locked(db, main, part, message, operation="updated", run_fence=fence)
            if part.type != "step-start":
                continue
            step = part.data.get("step")
            if finishes[step]:
                finishes[step] -= 1
                continue
            finish = StepFinishPart(id=ascending("part"), step=int(step or 0),
                session_id=main.id, message_id=message.id, duration=0, snapshot=part.data.get("snapshot"))
            row = Part(id=finish.id, message_id=message.id, session_id=main.id,
                user_id=main.user_id, type="step-finish", data=finish.model_dump(), created_at=now)
            db.add(row)
            await db.flush()
            await append_part_event_locked(db, main, row, message, operation="created", run_fence=fence)
        if message is messages[-1] or message.finish is None:
            message.finish, message.error = "aborted", None
            await append_message_events_locked(db, main, message, operation="updated",
                run_fence=fence, logical_turn_id=item.turn_id)
    answer = messages[-1]
    # No evidence read is necessary to record a stop. The original result and
    # all its source bytes remain untouched and still require fresh read ACLs.
    mark_report_failed(result, reason="user_stopped", now=now, blocked=True)
    item.state, item.outcome, item.result_message_id = "settled", "aborted", answer.id
    item.error, item.claim_expires_at = None, None
    item.settled_at = item.updated_at = now
    await append_agent_event_locked(db, main, kind="assistant.report.failed", payload={
        "result_id": result.id, "report_attempt": result.report_attempt, "inbox_id": item.id,
        "reason": result.last_error_code, "state": result.delivery_state,
    }, run_fence=fence, message_id=answer.id,
        idempotency_key=f"report-failed:{result.id}:{result.report_attempt}")
    await append_agent_event_locked(db, main, kind="inbox.settled", payload={
        "item_id": item.id, "state": item.state, "outcome": item.outcome,
        "result_message_id": answer.id, "error": None,
    }, run_fence=fence, turn_id=item.turn_id, step_id=item.step_id, message_id=item.message_id,
        idempotency_key=f"inbox:{item.id}:settled:{driver.run_id}:{driver.generation}")
    return True
