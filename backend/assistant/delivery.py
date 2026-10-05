"""Durable result-outbox convergence, independent of model or execution retry."""
from datetime import datetime, timezone

from sqlalchemy import or_, select

from assistant.policy import AssistantError
from assistant.reporting import mark_report_failed
from assistant.results import deliver_task_result, validate_result_source
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import _lock_fenced, begin_session_write

log = create_logger("assistant.delivery")


async def reconcile_report(result_id: str) -> bool:
    """A settled Inbox is not proof of a successfully committed report.

    Only finalization may set processed. Repairing a crash therefore schedules
    a new read-only attempt, never invents an ACK or re-executes the task.
    Lock order matches delivery/finalization: main Session, then Result.
    """
    async with get_db_session() as db:
        await begin_session_write(db)
        task = await db.scalar(select(AssistantTask).join(TaskResult, TaskResult.task_id == AssistantTask.id)
                               .where(TaskResult.id == result_id))
        if task is None:
            return False
        try:
            main = await _lock_fenced(db, task.assistant_session_id, task.user_id)
        except LookupError:
            result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update())
            if result.delivery_state != "accepted":
                return False
            result.delivery_state, result.last_error_code = "blocked", "assistant_unavailable"
            return True
        result = await db.scalar(select(TaskResult).where(TaskResult.id == result_id).with_for_update())
        if result.delivery_state != "accepted":
            return False
        item = await db.get(AgentInboxItem, result.assistant_inbox_id) if result.assistant_inbox_id else None
        if item is not None and item.state in {"accepted", "claimed"}:
            # Driver and Inbox recovery own live/expired claims. This worker
            # cannot steal a generation merely because its model is slow.
            return False
        now = datetime.now(timezone.utc)
        reason, blocked = "report_interrupted", False
        # Canceling queued input is explicit intent; an aborted runtime can
        # instead be a server shutdown. Active human stops already committed
        # blocked/user_stopped atomically and cannot enter this accepted path.
        if item is not None and item.state == "canceled":
            reason, blocked = "user_stopped", True
        try:
            await validate_result_source(db, result, user_id=task.user_id,
                                         workspace_id=task.workspace_id, main_id=main.id)
        except AssistantError as exc:
            reason, blocked = exc.code, True
        mark_report_failed(result, reason=reason, now=now, blocked=blocked)
        await append_agent_event_locked(db, main, kind="assistant.report.failed", payload={
            "result_id": result.id, "report_attempt": result.report_attempt,
            "inbox_id": result.assistant_inbox_id, "reason": result.last_error_code,
            "state": result.delivery_state,
        }, idempotency_key=f"report-reconciled:{result.id}:{result.report_attempt}")
        return True


async def recover_assistant_results(*, limit: int = 100, result_ids: tuple[str, ...] | None = None) -> int:
    """One bounded SQL scan. Wake is advisory; Inbox recovery closes its gap."""
    limit = max(1, min(100, limit))
    if result_ids is not None and not result_ids:
        return 0
    async with get_db_session() as db:
        query = select(TaskResult.id).outerjoin(
            AgentInboxItem, AgentInboxItem.id == TaskResult.assistant_inbox_id,
        ).where(TaskResult.delivery_state == "accepted", or_(AgentInboxItem.id.is_(None),
            AgentInboxItem.state.in_(("settled", "canceled")),
        ))
        if result_ids is not None:
            query = query.where(TaskResult.id.in_(result_ids[:100]))
        interrupted = list((await db.scalars(query.order_by(TaskResult.created_at, TaskResult.id).limit(limit))).all())
    changed = 0
    for result_id in interrupted:
        try:
            changed += int(await reconcile_report(result_id))
        except Exception:
            log.exception("Report reconciliation deferred result_id=%s", result_id)
    async with get_db_session() as db:
        query = select(TaskResult.id).where(
            TaskResult.delivery_state.in_(("pending", "retry_wait")),
            TaskResult.available_at <= datetime.now(timezone.utc),
        )
        if result_ids is not None:
            query = query.where(TaskResult.id.in_(result_ids[:100]))
        due = list((await db.scalars(query.order_by(TaskResult.available_at, TaskResult.created_at, TaskResult.id).limit(limit))).all())
    from agent.inbox import schedule_inbox_wake
    for result_id in due:
        try:
            receipt = await deliver_task_result(result_id)
            if receipt:
                changed += 1
                schedule_inbox_wake(receipt["session_id"], receipt["user_id"])
        except Exception:
            # A wake failure cannot roll back acceptance. The next Inbox
            # scanner finds it without increasing the report attempt.
            log.exception("Result delivery deferred result_id=%s", result_id)
    return changed
