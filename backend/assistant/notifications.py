"""One private message-centre item per original execution result.

Only fixed status text leaves the execution transaction. Opening, counting
and sending a notification all revalidate the original result's authority;
report attempts and subsequent runs never replace its identity.
"""
from assistant.policy import AssistantError
from db.models.assistant import AssistantTask, TaskResult


def result_source_key(task, result):
    return f"assistant-result:{task.id}:{result.source_event_key}"


async def valid_target(db, *, user_id, workspace_id, link, source_key=None):
    if not isinstance(link, dict) or link.get("kind") != "assistant_task" or not workspace_id:
        return False
    if link.get("workspaceId") != workspace_id:
        return False
    result = await db.get(TaskResult, link.get("resultId") or "")
    task = await db.get(AssistantTask, link.get("taskId") or "")
    if (result is None or task is None or result.task_id != task.id
            or task.user_id != user_id or task.workspace_id != workspace_id
            or task.assistant_session_id != link.get("sessionId")
            or source_key is not None and source_key != result_source_key(task, result)):
        return False
    from assistant.results import validate_result_source
    try:
        await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id,
            main_id=task.assistant_session_id)
    except AssistantError:
        return False
    return True


async def result_finished(db, task, result):
    link = {"kind": "assistant_task", "workspaceId": task.workspace_id,
        "sessionId": task.assistant_session_id, "taskId": task.id, "resultId": result.id}
    if not await valid_target(db, user_id=task.user_id, workspace_id=task.workspace_id, link=link):
        return
    kind = ("assistant_result_ready" if result.outcome == "succeeded" else
        "assistant_result_stopped" if result.outcome in {"aborted", "canceled", "cancelled"} else
        "assistant_result_failed")
    from notifications.events import emit
    await emit(db, user_id=task.user_id, workspace_id=task.workspace_id,
        session_id=task.assistant_session_id, kind=kind, event_key=result_source_key(task, result),
        link=link, guard={"kind": "assistant_result", "link": link})


async def read_target(*, user_id, workspace_id, main_id, result_id):
    from sqlalchemy import func, select
    from assistant.reads import get_task, result_view
    from assistant.results import validate_result_source
    from assistant.transactions import begin_snapshot
    from db.base import get_db_session
    from db.models.agent_event import AgentEvent
    async with get_db_session() as db:
        await begin_snapshot(db)
        result = await db.get(TaskResult, result_id)
        if result is None:
            raise AssistantError(404, "ASSISTANT_RESULT_UNAVAILABLE", "Result is unavailable")
        task, _ = await validate_result_source(db, result, user_id=user_id,
            workspace_id=workspace_id, main_id=main_id)
        view = await get_task(db=db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=task.id)
        selected = result_view(result)
        selected["processed_sequence"] = await db.scalar(select(func.min(AgentEvent.sequence)).where(
            AgentEvent.session_id == main_id, AgentEvent.user_id == user_id,
            AgentEvent.message_id == result.processed_message_id, AgentEvent.kind == "turn.finished",
        )) if result.processed_message_id else None
        return {"assistant_session_id": main_id, "task": view, "result": selected}
