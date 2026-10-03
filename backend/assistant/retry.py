"""An explicit, idempotent retry only creates a new read-only report attempt."""
from datetime import datetime, timezone

from sqlalchemy import select

from assistant.commands import _authority, command_digest, task_locked
from assistant.policy import AssistantError, lock_actor, main_session_locked
from assistant.results import accept_report_locked, validate_result_source
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, TaskResult
from session.internal_parts import begin_session_write


async def read_command(*, user_id, workspace_id, main_id, command_id):
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id,
            AssistantCommand.actor_user_id == user_id, AssistantCommand.workspace_id == workspace_id,
            AssistantCommand.assistant_session_id == main_id))
        if command is None:
            raise AssistantError(404, "ASSISTANT_COMMAND_UNAVAILABLE", "Command is unavailable")
        if command.target_type == "task":
            await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=command.target_id)
        elif command.target_type == "permission":
            from assistant.permission_requests import event_for, scope_for
            event = await event_for(db, command.target_id, user_id)
            if event is None:
                raise AssistantError(404, "PERMISSION_UNAVAILABLE", "Permission request is unavailable")
            await scope_for(db, event)
        elif command.target_type == "question":
            from assistant.requests import question_task
            from db.models.question import QuestionCheckpoint
            question = await db.get(QuestionCheckpoint, command.target_id)
            if question is None or question.user_id != user_id:
                raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
            await question_task(db, question)
        else:
            result = await db.get(TaskResult, command.target_id)
            if result is None:
                raise AssistantError(404, "ASSISTANT_RESULT_UNAVAILABLE", "Result is unavailable")
            await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        return {"command_id": command.id, "action": command.action, "state": command.state,
                "receipt": dict(command.receipt)}


async def retry_report(*, user_id, workspace_id, main_id, result_id, idempotency_key, expected_report_attempt):
    if not idempotency_key or len(idempotency_key) > 64:
        raise ValueError("A stable command key is required")
    if type(expected_report_attempt) is not int or expected_report_attempt < 1:
        raise ValueError("An expected report attempt is required")
    digest = command_digest({"action": "report_retry", "result_id": result_id,
                              "expected_report_attempt": expected_report_attempt})
    async with get_db_session() as db:
        await begin_session_write(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        main = await main_session_locked(db, user_id, workspace_id, lock=True)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.idempotency_key == idempotency_key).with_for_update())
        if command and command.payload_digest != digest:
            raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
        result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update())
        if result is None:
            raise AssistantError(404, "ASSISTANT_RESULT_UNAVAILABLE", "Result is unavailable")
        task, _ = await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if command:
            return dict(command.receipt)
        now = datetime.now(timezone.utc)
        command = AssistantCommand(id=generate_id(), actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, payload_digest=digest,
            action="report_retry", target_type="result", target_id=result_id, expected_revision=expected_report_attempt,
            source_ref={"actor_user_id": user_id, "entrypoint": "assistant_report_retry"}, state="accepted",
            receipt={}, created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        if result.report_attempt != expected_report_attempt:
            raise AssistantError(409, "ASSISTANT_REPORT_ATTEMPT_CONFLICT", "Report attempt changed; reload the result")
        if result.delivery_state not in {"blocked", "retry_wait"}:
            raise AssistantError(409, "ASSISTANT_REPORT_NOT_RETRYABLE", "This report is not awaiting a retry")
        old = await db.get(AgentInboxItem, result.assistant_inbox_id) if result.assistant_inbox_id else None
        if old and old.state in {"accepted", "claimed"}:
            raise AssistantError(409, "ASSISTANT_REPORT_ACTIVE", "The previous report is still active")
        receipt = await accept_report_locked(db, main, result, task, manual=True)
        command.receipt = {"command_id": command.id, **receipt, "state": "accepted"}
        return dict(command.receipt)
