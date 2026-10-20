"""Committed task submissions survive failure of the coordinating reply.

Only SQL receipts of this exact run/generation count. Model text, tool status
and another turn's submissions cannot prove that an operation was accepted.
"""
from sqlalchemy import JSON, select, type_coerce

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskSubmission
from db.models.session import Session


async def for_run(session_id, user_id, run_id, generation):
    from assistant.blocking import blocked_views
    from assistant.policy import require_membership

    if not run_id or generation is None:
        return []
    async with get_db_session() as db:
        main = await db.get(Session, session_id)
        if main is None or main.user_id != user_id or main.is_deleted or main.kind != "assistant":
            return []
        await require_membership(db, user_id, main.workspace_id)
        source = type_coerce(AssistantCommand.source_ref, JSON)
        rows = (await db.execute(select(AssistantTask, TaskSubmission, AgentInboxItem, Session)
            .select_from(AssistantTask)
            .join(TaskSubmission, TaskSubmission.task_id == AssistantTask.id)
            .join(AssistantCommand, AssistantCommand.id == TaskSubmission.command_id)
            .join(AgentInboxItem, AgentInboxItem.id == TaskSubmission.inbox_id)
            .join(Session, Session.id == AssistantTask.execution_session_id)
            .where(AssistantCommand.assistant_session_id == session_id,
                   AssistantCommand.actor_user_id == user_id,
                   AssistantCommand.workspace_id == main.workspace_id,
                   AssistantCommand.target_type == "task",
                   AssistantCommand.target_id == AssistantTask.id,
                   source["run_id"].as_string() == run_id,
                   source["generation"].as_integer() == generation,
                   AssistantTask.assistant_session_id == session_id,
                   AssistantTask.user_id == user_id, AssistantTask.workspace_id == main.workspace_id,
                   AgentInboxItem.user_id == user_id,
                   AgentInboxItem.session_id == Session.id,
                   Session.user_id == user_id, Session.workspace_id == main.workspace_id,
                   Session.is_deleted.is_(False))
            .order_by(TaskSubmission.accepted_at, TaskSubmission.id))).all()
        blocked = await blocked_views(db, [session for _, _, _, session in rows])
        return [{"task_id": task.id, "title": task.title, "submission_id": submission.id,
                 "session_id": session.id, "link": f"/app/s/{session.id}",
                 "state": item.state, "outcome": item.outcome,
                 "disposition": submission.disposition,
                 "blocked_by": blocked.get(session.id)}
                for task, submission, item, session in rows]


async def for_inbox(session_id, user_id, inbox_id):
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, inbox_id) if inbox_id else None
        if item is None or item.session_id != session_id or item.user_id != user_id:
            return []
        run_id, generation = item.run_id, item.generation
    return await for_run(session_id, user_id, run_id, generation)


def describe(receipts, lang="zh"):
    """State exactly what happened to each submitted instruction, not the whole video."""
    lines = []
    for row in receipts:
        name = row["title"]
        if row["state"] == "accepted":
            state = ("已接收，正在等待原任务中的确认，尚未开始执行这条指令"
                     if row.get("blocked_by") else "已接收并排队，尚未开始执行这条指令")
            english = "accepted, waiting for a decision in the task" if row.get("blocked_by") else "accepted and queued"
        elif row["state"] == "claimed":
            state, english = "已接收，正在处理这条指令", "accepted and being processed"
        elif row["state"] == "canceled":
            state, english = "已取消", "cancelled"
        elif row["outcome"] == "succeeded":
            state, english = "本轮处理已结束，最终成品请查看任务记录", "this processing turn ended; check the task for its output"
        elif row["outcome"] == "error":
            state, english = "执行这条指令时出现错误，请查看原任务", "this instruction encountered an execution error; check the task"
        else:
            state, english = "已接收过，当前执行结果需要在原任务核对", "was accepted; check the task for its execution outcome"
        lines.append(f"「{name}」：{state}。" if lang != "en" else f"{name}: {english}.")
    return " ".join(lines)
