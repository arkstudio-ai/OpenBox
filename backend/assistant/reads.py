"""Read-only assistant views; these never create Sessions or wake execution."""
import json
from datetime import datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased

from assistant.commands import _authority, _project, task_locked
from assistant.transactions import read_session
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult
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
    value = {key: getattr(task, key) for key in ("id", "title", "project_id", "execution_session_id",
        "desired_state", "observed_state", "control_revision", "intent_revision", "updated_at", "archived_at")}
    # Where the user opens the work: a link the assistant can show instead of an ID.
    value["url"] = f"/app/s/{task.execution_session_id}"
    if task.continuation_policy:
        from assistant.continuation import public_policy
        value["continuation"] = public_policy(task)
    return value


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


async def list_sessions(*, user_id, workspace_id, main_id, project_id=None, status=None, query=None,
                        watched=None, limit=50, cursor=None, include_link=False, db=None) -> dict:
    """The user's top-level conversations, newest first (V2: any visibility).

    Pages follow next_cursor (the last id of a page).

    A watched conversation also carries its Task and latest result summary.
    """
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if project_id:
            await _project(db, project_id, user_id, workspace_id)
        statement = select(*Session.__table__.columns, Project.name.label("project_name"),
                           AssistantTask.id.label("task_id"), AssistantTask.archived_at.label("task_archived_at"),
                           TaskResult.summary.label("latest_summary")).select_from(Session).join(
            Project, Project.id == Session.project_id).outerjoin(AssistantTask, and_(
                AssistantTask.execution_session_id == Session.id, AssistantTask.assistant_session_id == main_id,
                AssistantTask.user_id == user_id)).outerjoin(
            TaskResult, TaskResult.id == AssistantTask.latest_result_id).where(
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.kind == "normal",
            Session.parent_id.is_(None), Session.is_deleted.is_(False),
            Project.user_id == user_id, Project.workspace_id == workspace_id, Project.is_deleted.is_(False))
        if cursor:
            # Session ids are descending (core.identifier.descending): a newer
            # conversation sorts first, so ascending ids are newest first.
            statement = statement.where(Session.id > cursor)
        if project_id:
            statement = statement.where(Session.project_id == project_id)
        if status:
            statement = statement.where(Session.status == status)
        if query:
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            statement = statement.where(Session.title.ilike(pattern, escape="\\"))
        if watched is True:
            statement = statement.where(AssistantTask.id.is_not(None), AssistantTask.archived_at.is_(None))
        elif watched is False:
            statement = statement.where(or_(AssistantTask.id.is_(None), AssistantTask.archived_at.is_not(None)))
        rows = list((await db.execute(statement.order_by(Session.id).limit(limit + 1))).all())
        details = {}
        if include_link:
            from assistant.linking import candidate
            for row in rows[:limit]:
                details[row.id] = {"link": await candidate(db, row)}

        def render(row):
            item = {key: getattr(row, key) for key in ("id", "title", "status", "kind", "project_id",
                                                        "visibility", "updated_at")}
            summary = row.latest_summary or ""
            item.update(project_name=(row.project_name or "")[:1024], url=f"/app/s/{row.id}",
                        watched=row.task_id is not None and row.task_archived_at is None,
                        task_id=row.task_id,
                        latest_summary=summary[:300] + ("…" if len(summary) > 300 else "") if summary else None,
                        **details.get(row.id, {}))
            return item
        return _page(rows, limit, render)


async def list_tasks(*, user_id, workspace_id, main_id, status=None, limit=50, cursor=None, db=None) -> dict:
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        query = select(AssistantTask).join(Session, Session.id == AssistantTask.execution_session_id).join(
            Project, Project.id == AssistantTask.project_id).where(
            AssistantTask.user_id == user_id, AssistantTask.workspace_id == workspace_id,
            AssistantTask.assistant_session_id == main_id, AssistantTask.id > (cursor or ""),
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
            Session.kind == "normal",
            Project.user_id == user_id, Project.workspace_id == workspace_id, Project.is_deleted.is_(False))
        if status:
            query = query.where(AssistantTask.observed_state == status)
        rows = list((await db.scalars(query.order_by(AssistantTask.id).limit(limit + 1))).all())
        return _page(rows, limit, task_view)


async def get_task(*, user_id, workspace_id, main_id, task_id, db=None) -> dict:
    return (await get_tasks(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                            task_ids=[task_id], db=db))[0]


async def get_tasks(*, user_id, workspace_id, main_id, task_ids, db=None) -> list[dict]:
    """Task views in a constant number of batched reads (V2: snapshot and cards)."""
    async with read_session(db) as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if not task_ids:
            return []
        from assistant.commands import read_task_scopes
        scoped = await read_task_scopes(db, user_id=user_id, workspace_id=workspace_id,
                                        main_id=main_id, task_ids=task_ids)
        tasks = [task for task, _ in scoped]
        ids = [task.id for task in tasks]
        latest = {row.id: row for row in (await db.scalars(select(TaskResult).where(
            TaskResult.id.in_([task.latest_result_id for task in tasks if task.latest_result_id])))).all()}
        drivers = {row.session_id: row for row in (await db.scalars(select(AgentDriverState).where(
            AgentDriverState.session_id.in_([execution.id for _, execution in scoped])))).all()}
        ranked = select(TaskSubmission, func.row_number().over(partition_by=TaskSubmission.task_id,
            order_by=(TaskSubmission.accepted_at.desc(), TaskSubmission.id.desc())).label("rank")).where(
            TaskSubmission.task_id.in_(ids)).subquery()
        newest = aliased(TaskSubmission, ranked)
        submissions = {row.task_id: row for row in (await db.scalars(select(newest).where(ranked.c.rank == 1))).all()}
        items = {row.id: row for row in (await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.id.in_([row.inbox_id for row in submissions.values()])))).all()}
        processed_ids = [row.processed_message_id for row in latest.values() if row.processed_message_id]
        processed = dict((await db.execute(select(AgentEvent.message_id, func.min(AgentEvent.sequence)).where(
            AgentEvent.session_id == main_id, AgentEvent.user_id == user_id,
            AgentEvent.message_id.in_(processed_ids), AgentEvent.kind == "turn.finished",
        ).group_by(AgentEvent.message_id))).all()) if processed_ids else {}
        ranked_controls = select(AssistantCommand, func.row_number().over(partition_by=AssistantCommand.target_id,
            order_by=(AssistantCommand.created_at.desc(), AssistantCommand.id.desc())).label("rank")).where(
            AssistantCommand.target_id.in_(ids),
            AssistantCommand.action.in_(("task_pause", "task_resume", "task_cancel"))).subquery()
        newest_control = aliased(AssistantCommand, ranked_controls)
        controls = {row.target_id: row for row in (await db.scalars(
            select(newest_control).where(ranked_controls.c.rank == 1))).all()}
        values = []
        for task, execution in scoped:
            result_row = latest.get(task.latest_result_id)
            result = result_view(result_row)
            if result is not None:
                result["processed_sequence"] = (processed.get(result_row.processed_message_id)
                                                if result_row.processed_message_id else None)
            submission = submissions.get(task.id)
            item = items.get(submission.inbox_id) if submission else None
            submission_view = {"submission_id": submission.id, "command_id": submission.command_id,
                "inbox_id": submission.inbox_id, "disposition": submission.disposition,
                "accepted_at": submission.accepted_at, "applied_at": submission.applied_at,
                "run_id": item.run_id if item else None, "generation": item.generation if item else None,
            } if submission else None
            if submission and (submission.delivery == "steer" or item and item.state == "canceled"):
                submission_view.update(delivery=submission.delivery,
                    expected_run=(item.origin_ref or {}).get("expected_run") if item else None,
                    state=item.state if item else None, error=item.error if item else None)
            driver = drivers.get(execution.id)
            value = {"task": task_view(task), "latest_result": result,
                     "execution_session": {"id": execution.id, "status": execution.status},
                     "run_binding": {"run_id": driver.run_id, "generation": driver.generation,
                                     "phase": driver.phase} if driver else None,
                     "latest_submission": submission_view,
                     "pending_requests_location": "execution_session"}
            control = controls.get(task.id)
            if control is not None:
                value["latest_control"] = {"command_id": control.id, "action": control.action,
                                           "state": control.state, "receipt": dict(control.receipt)}
                if control.state == "blocked":
                    event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == execution.id,
                        AgentEvent.kind == "assistant.control.blocked").order_by(AgentEvent.sequence.desc()).limit(1))
                    if event is not None and event.payload.get("command_id") == control.id:
                        value["latest_control"]["error_code"] = event.payload.get("code")
            values.append(value)
        return values


async def watch_list(*, user_id, workspace_id) -> dict:
    """Watched conversations for the sidebar (assistant.task_context, 4 reads)."""
    from assistant.policy import main_session_locked, require_membership
    from assistant.task_context import task_context
    async with read_session() as db:
        await require_membership(db, user_id, workspace_id)
        main = await main_session_locked(db, user_id, workspace_id)
        if main is None:
            return {"items": [], "has_more": False}
        value = await task_context(db, main)
        return {"items": value["items"], "has_more": value["has_more"]}
