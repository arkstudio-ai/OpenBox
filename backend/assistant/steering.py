"""Bind unconsumed task steering to one exact, still-live execution.

Accepted steering is durable, but only its target generation may claim it.
If that generation stops first, preserve a not-applied receipt instead of
silently converting the instruction into a new run or a followup.
"""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant.policy import AssistantError
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskSubmission


class ExpectedRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str = Field(min_length=1, max_length=64)
    generation: int = Field(ge=1, strict=True)


async def current_steer_target(db, session):
    from agent.driver import _database_now
    return await db.scalar(select(AgentDriverState).where(
        AgentDriverState.session_id == session.id, AgentDriverState.user_id == session.user_id,
        AgentDriverState.phase.in_(("reserved", "running")),
        AgentDriverState.trigger_message_id.is_not(None),
        AgentDriverState.abort_requested_at.is_(None),
        AgentDriverState.lease_expires_at > _database_now(db),
    ).with_for_update())


async def require_steer_target_locked(db, session, expected):
    state = await current_steer_target(db, session)
    if state is None or {"run_id": state.run_id, "generation": state.generation} != expected:
        raise AssistantError(409, "ASSISTANT_RUN_CONFLICT",
                             "The target run has stopped or changed; reload the task before choosing new input")


async def expire_task_steers_locked(db, session, *, ending_run=None):
    """Caller holds Session; this also covers recovery before wake/claim.

    A release passes its ending identity before clearing the Driver. Otherwise
    only a currently live, non-aborting target can retain accepted steering.
    No messages or original inputs are deleted and no replacement is queued.
    """
    if session.kind == "assistant":
        return
    # Any watched conversation (V2: not only isolated task sessions).
    task = await db.scalar(select(AssistantTask).where(
        AssistantTask.execution_session_id == session.id, AssistantTask.user_id == session.user_id,
    ).with_for_update())
    if task is None:
        return
    state = None if ending_run else await current_steer_target(db, session)
    live = {"run_id": state.run_id, "generation": state.generation} if state else None
    rows = (await db.execute(select(AgentInboxItem, TaskSubmission).join(
        TaskSubmission, TaskSubmission.inbox_id == AgentInboxItem.id).where(
        TaskSubmission.task_id == task.id, AgentInboxItem.session_id == session.id,
        AgentInboxItem.user_id == session.user_id, AgentInboxItem.state == "accepted",
        AgentInboxItem.delivery == "steer",
    ).order_by(AgentInboxItem.created_at, AgentInboxItem.id).with_for_update())).all()
    from session.agent_event_log import append_agent_event_locked
    now = datetime.now(timezone.utc)
    for item, submission in rows:
        expected = (item.origin_ref or {}).get("expected_run")
        if (ending_run and expected != ending_run) or (not ending_run and expected and expected == live):
            continue
        item.state, item.outcome = "canceled", "not_applied"
        item.error = {"code": "ASSISTANT_STEER_NOT_APPLIED", "message": "The target run stopped before claiming this modification"}
        item.canceled_at = item.updated_at = now
        submission.disposition = "not_applied"
        if task.desired_state == "running":
            task.observed_state = "input_not_applied"
        task.control_revision += 1
        task.updated_at = now
        await append_agent_event_locked(db, session, kind="inbox.canceled", payload={
            "item_id": item.id, "state": "canceled", "reason": item.error["message"],
            "code": item.error["code"], "expected_run": expected,
        }, idempotency_key=f"inbox:{item.id}:canceled")
