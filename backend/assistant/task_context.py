"""The assistant's watch list: current task facts, read once per ordinary turn.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 3.2): one join reads every watched
task with its latest result summary, and one grouped read counts pending
questions. Earlier snapshots are history, never re-validated or replayed as
current state.
"""
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.orm import aliased

from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from memory.redaction import redact_credentials

MAX_CONTEXT_TASKS = 12
SUMMARY_PREVIEW = 600


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else value


async def task_context(db, main) -> dict:
    latest = aliased(TaskResult)
    rows = (await db.execute(select(AssistantTask, Project.name, Session.status, latest)
        .join(Session, Session.id == AssistantTask.execution_session_id)
        .join(Project, Project.id == AssistantTask.project_id)
        .outerjoin(latest, latest.id == AssistantTask.latest_result_id)
        .where(AssistantTask.assistant_session_id == main.id, AssistantTask.user_id == main.user_id,
               AssistantTask.workspace_id == main.workspace_id, AssistantTask.archived_at.is_(None),
               Session.user_id == main.user_id, Session.workspace_id == main.workspace_id,
               Session.is_deleted.is_(False), Project.user_id == main.user_id,
               Project.workspace_id == main.workspace_id, Project.is_deleted.is_(False))
        .order_by(case((AssistantTask.observed_state == "waiting_input", 0),
                       (AssistantTask.observed_state.in_(("queued", "running")), 1), else_=2),
                  AssistantTask.updated_at.desc(), AssistantTask.id.desc())
        .limit(MAX_CONTEXT_TASKS + 1))).all()
    selected = rows[:MAX_CONTEXT_TASKS]
    pending = {}
    if selected:
        pending = dict((await db.execute(select(QuestionCheckpoint.session_id, func.count())
            .join(SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id)
            .where(QuestionCheckpoint.session_id.in_([row[0].execution_session_id for row in selected]),
                   QuestionCheckpoint.user_id == main.user_id, QuestionCheckpoint.status == "pending",
                   QuestionCheckpoint.generation == SessionExecution.generation)
            .group_by(QuestionCheckpoint.session_id))).all())
    items = []
    for task, project_name, session_status, result in selected:
        item = {"task_id": task.id, "title": redact_credentials(task.title),
                "project": {"id": task.project_id, "name": redact_credentials(project_name)},
                "session_id": task.execution_session_id, "session_status": session_status,
                "desired_state": task.desired_state, "observed_state": task.observed_state,
                "revision": task.control_revision, "updated_at": _iso(task.updated_at),
                "pending_questions": pending.get(task.execution_session_id, 0)}
        if result is not None:
            summary = result.summary or ""
            item["latest_result"] = {"result_id": result.id, "outcome": result.outcome,
                "delivery_state": result.delivery_state, "created_at": _iso(result.created_at),
                "summary": summary[:SUMMARY_PREVIEW] + ("…" if len(summary) > SUMMARY_PREVIEW else "")}
        items.append(item)
    return {"items": items, "has_more": len(rows) > MAX_CONTEXT_TASKS,
            "read_more": "Use tasks.list for the full list; tasks.get, results.read or history.read for details.",
            "untrusted_data": True, "grants_authority": False}
