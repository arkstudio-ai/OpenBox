"""Read-only assistant views; these never create Sessions or wake execution."""
import json
from datetime import datetime

from sqlalchemy import func, select

from assistant.commands import _authority, _project, task_locked
from assistant.transactions import read_session
from db.models.assistant import AssistantTask, TaskResult
from db.models.assistant import TaskSubmission
from db.models.agent_inbox import AgentInboxItem
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.project import Project
from db.models.session import Session


def _page(rows, limit, render):
    items = []
    for row in rows[:limit]:
        item = render(row)
        for field in ("title", "name"):
            if isinstance(item.get(field), str) and len(item[field]) > 1024:
                item[field] = item[field][:1024] + "…"
                item["label_truncated"] = True
        if items and len(json.dumps({"items": [*items, item]}, ensure_ascii=False, default=str).encode()) > 40000:
            break
        items.append(item)
    return {"items": items, "next_cursor": rows[len(items) - 1].id if items and len(rows) > len(items) else None,
            "untrusted_data": True}


def task_view(task) -> dict:
    return {key: getattr(task, key) for key in ("id", "title", "project_id", "execution_session_id",
        "desired_state", "observed_state", "control_revision", "intent_revision", "updated_at")}


def result_view(result) -> dict | None:
    if result is None:
        return None
    value = {"result_id": result.id, **{key: getattr(result, key) for key in (
        "run_id", "generation", "result_message_id", "outcome", "delivery_state", "report_attempt",
        "assistant_inbox_id", "last_error_code", "processed_message_id", "observed_intent_revision", "created_at")}}
    return {key: item.isoformat() if isinstance(item, datetime) else item for key, item in value.items()}


async def list_projects(*, user_id, workspace_id, main_id, limit=50, cursor=None, db=None) -> dict:
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = list((await db.scalars(select(Project).where(Project.user_id == user_id,
            Project.workspace_id == workspace_id, Project.is_deleted.is_(False), Project.id > (cursor or ""))
            .order_by(Project.id).limit(limit + 1))).all())
        return _page(rows, limit, lambda row: {"id": row.id, "name": row.name})


async def list_sessions(*, user_id, workspace_id, main_id, project_id=None, status=None,
                        limit=50, cursor=None, db=None) -> dict:
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if project_id:
            await _project(db, project_id, user_id, workspace_id)
        query = select(Session).join(Project, Project.id == Session.project_id).where(
            Session.user_id == user_id, Session.workspace_id == workspace_id,
            Session.kind == "normal", Session.is_deleted.is_(False), Session.id > (cursor or ""),
            Project.user_id == user_id, Project.workspace_id == workspace_id, Project.is_deleted.is_(False))
        if project_id:
            query = query.where(Session.project_id == project_id)
        if status:
            query = query.where(Session.status == status)
        rows = list((await db.scalars(query.order_by(Session.id).limit(limit + 1))).all())
        return _page(rows, limit, lambda row: {key: getattr(row, key) for key in
                                             ("id", "title", "status", "kind", "project_id", "updated_at")})


async def list_tasks(*, user_id, workspace_id, main_id, status=None, limit=50, cursor=None, db=None) -> dict:
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        query = select(AssistantTask).join(Session, Session.id == AssistantTask.execution_session_id).join(
            Project, Project.id == AssistantTask.project_id).where(
            AssistantTask.user_id == user_id, AssistantTask.workspace_id == workspace_id,
            AssistantTask.assistant_session_id == main_id, AssistantTask.id > (cursor or ""),
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
            Session.visibility == "private", Session.memory_policy == "assistant_isolated", Session.kind == "normal",
            Project.user_id == user_id, Project.workspace_id == workspace_id, Project.is_deleted.is_(False))
        if status:
            query = query.where(AssistantTask.observed_state == status)
        rows = list((await db.scalars(query.order_by(AssistantTask.id).limit(limit + 1))).all())
        return _page(rows, limit, task_view)


async def get_task(*, user_id, workspace_id, main_id, task_id, db=None) -> dict:
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        task, execution = await task_locked(db, user_id=user_id, workspace_id=workspace_id,
                                            main_id=main_id, task_id=task_id)
        latest = await db.get(TaskResult, task.latest_result_id) if task.latest_result_id else None
        driver = await db.get(AgentDriverState, execution.id)
        submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == task.id)
                                     .order_by(TaskSubmission.accepted_at.desc(), TaskSubmission.id.desc()).limit(1))
        item = await db.get(AgentInboxItem, submission.inbox_id) if submission else None
        result = result_view(latest)
        if result is not None:
            result["processed_sequence"] = await db.scalar(select(func.min(AgentEvent.sequence)).where(
                AgentEvent.session_id == main_id, AgentEvent.user_id == user_id,
                AgentEvent.message_id == latest.processed_message_id, AgentEvent.kind == "turn.finished",
            )) if latest.processed_message_id else None
        # Increment 1 links waiting_input back to the original execution page;
        # it does not claim that a missing main-page card means no pending work.
        return {"task": task_view(task), "latest_result": result,
                "execution_session": {"id": execution.id, "status": execution.status},
                "run_binding": {"run_id": driver.run_id, "generation": driver.generation,
                                "phase": driver.phase} if driver else None,
                "latest_submission": {"submission_id": submission.id, "command_id": submission.command_id,
                    "inbox_id": submission.inbox_id, "disposition": submission.disposition,
                    "accepted_at": submission.accepted_at, "applied_at": submission.applied_at,
                    "run_id": item.run_id if item else None, "generation": item.generation if item else None,
                } if submission else None,
                "pending_requests_location": "execution_session"}
