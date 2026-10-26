"""Owner-bound work navigation for voice, including conversations never watched by the assistant.

This is a business SQL snapshot, not a memory search. Reading a conversation
does not link it to the assistant, create a task or wake execution.
"""
import json
from datetime import datetime, timezone
from hashlib import sha256

from sqlalchemy import and_, case, func, or_, select

from assistant.commands import _authority
from assistant.history import _decode_cursor, _encode_cursor
from assistant.reads import execution_settings
from assistant.transactions import read_session
from assistant.work_context import conversation_background
from db.models.assistant import AssistantTask, TaskResult
from db.models.file_asset import FileAsset
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from db.models.video_job import VideoJob
from memory.redaction import redact_credentials


def _iso(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


async def overview(scope, arguments, *, limit=12):
    query = str(arguments.get("query") or "").strip()
    media = arguments.get("media_kind")
    identity = "voice-work:" + sha256(json.dumps(
        [scope.user_id, scope.workspace_id, scope.main_session_id, query, media],
        ensure_ascii=False).encode()).hexdigest()
    cursor = _decode_cursor(arguments["cursor"], identity) if arguments.get("cursor") else None
    async with read_session() as db:
        await _authority(db, user_id=scope.user_id, workspace_id=scope.workspace_id, main_id=scope.main_session_id)
        # STT jobs are not a new video. Verify the output asset separately.
        ranked = select(VideoJob.id, VideoJob.session_id, func.row_number().over(
            partition_by=VideoJob.session_id,
            order_by=(VideoJob.created_at.desc(), VideoJob.id.desc())).label("rank")).where(
                VideoJob.user_id == scope.user_id, VideoJob.kind.in_(("segment", "render"))).subquery()
        activity = case((VideoJob.updated_at > Session.updated_at, VideoJob.updated_at), else_=Session.updated_at)
        statement = select(Session, Project.name, AssistantTask, TaskResult, VideoJob, FileAsset,
                           activity.label("activity_at")).select_from(Session).join(Project, Project.id == Session.project_id).outerjoin(
            AssistantTask, and_(AssistantTask.execution_session_id == Session.id,
                AssistantTask.assistant_session_id == scope.main_session_id,
                AssistantTask.user_id == scope.user_id, AssistantTask.workspace_id == scope.workspace_id,
                AssistantTask.archived_at.is_(None))).outerjoin(
            TaskResult, and_(TaskResult.id == AssistantTask.latest_result_id, TaskResult.task_id == AssistantTask.id)).outerjoin(
            ranked, and_(ranked.c.session_id == Session.id, ranked.c.rank == 1)).outerjoin(
            VideoJob, and_(VideoJob.id == ranked.c.id, VideoJob.user_id == scope.user_id)).outerjoin(
            FileAsset, and_(FileAsset.id == VideoJob.output_asset_id, FileAsset.user_id == scope.user_id,
                FileAsset.workspace_id == scope.workspace_id, FileAsset.session_id == Session.id,
                FileAsset.is_deleted.is_(False))).where(
            Session.user_id == scope.user_id, Session.workspace_id == scope.workspace_id,
            Session.kind == "normal", Session.parent_id.is_(None), Session.is_deleted.is_(False),
            Project.user_id == scope.user_id, Project.workspace_id == scope.workspace_id, Project.is_deleted.is_(False))
        if media == "video":
            statement = statement.where(VideoJob.id.is_not(None))
        candidates = statement
        relaxed = bool(cursor and cursor.get("relaxed"))
        if query and not relaxed:
            pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            statement = statement.where(or_(Session.title.ilike(pattern, escape="\\"),
                                            AssistantTask.title.ilike(pattern, escape="\\")))
        if cursor:
            moment = datetime.fromisoformat(cursor["at"])
            statement = statement.where(or_(activity < moment, and_(activity == moment, Session.id > cursor["id"])))
        rows = (await db.execute(statement.order_by(activity.desc(), Session.id).limit(limit + 1))).all()
        if not rows and query and not cursor:
            # A spoken description is rarely the exact stored title. Widen
            # navigation within the same authority, then let the model resolve
            # meaning from real candidates; never parse Chinese intent here.
            relaxed = True
            rows = (await db.execute(candidates.order_by(activity.desc(), Session.id).limit(limit + 1))).all()
        selected = rows[:limit]
        from assistant.execution_errors import result_execution_errors
        errors = await result_execution_errors(db, [row[3] for row in selected], user_id=scope.user_id)
        context = await conversation_background(db, [row[0].id for row in selected], user_id=scope.user_id)
        pending = {}
        for question in (await db.scalars(select(QuestionCheckpoint).join(SessionExecution,
                SessionExecution.session_id == QuestionCheckpoint.session_id).where(
                QuestionCheckpoint.session_id.in_([row[0].id for row in selected]),
                QuestionCheckpoint.user_id == scope.user_id, QuestionCheckpoint.status == "pending",
                QuestionCheckpoint.generation == SessionExecution.generation,
                QuestionCheckpoint.detached.is_(False),
                or_(QuestionCheckpoint.expires_at.is_(None), QuestionCheckpoint.expires_at > func.now()))
                .order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()))).all():
            pending.setdefault(question.session_id, question)
        items = []
        for session, project, task, result, job, asset, observed_activity in selected:
            available = bool(job and job.status == "completed" and asset and asset.status == "ready")
            state = task.observed_state if task else (job.status if job else session.status)
            if not task and job and job.status == "completed" and not available:
                state = "output_unavailable"
            item = {"session_id": session.id, "task_id": task.id if task else None,
                "title": redact_credentials((task.title if task else None) or session.title or project or "")[:200],
                "project": redact_credentials(project or "")[:100], "watched": task is not None,
                "state": state, "state_source": "assistant_task" if task else "video_job" if job else "conversation",
                "conversation_state": session.status, "created_at": _iso(session.created_at),
                "current_settings": execution_settings(session),
                "updated_at": _iso(observed_activity), "url": f"/app/s/{session.id}"}
            if question := pending.get(session.id):
                # A fresh pending decision is more useful than a stale task
                # summary. It is not an unfinished calculation or a result.
                item["state"] = "waiting_input"
                item["waiting_for_user"] = True
                item["pending_request"] = {"request_id": question.id,
                    "questions": [{"header": redact_credentials(str(q.get("header") or ""))[:80],
                                   "question": redact_credentials(str(q.get("question") or ""))[:300]}
                                  for q in question.questions[:2]],
                    "next": "Read cards_pending with this request_id before explaining options or answering."}
            if result:
                item["latest"] = redact_credentials(result.summary or "")[:300]
                item["latest_at"] = _iso(result.created_at)
                if error := errors.get(result.id):
                    item["execution_error"] = error
            if job:
                item["video"] = {"job_id": job.id, "status": job.status, "kind": job.kind,
                    "created_at": _iso(job.created_at), "completed_at": _iso(job.completed_at),
                    "output_available": available, "asset_id": asset.id if available else None,
                    "description": redact_credentials(job.prompt or "")[:200]}
            previews = context.get(session.id, [])[-2:]
            if previews:
                item["recent_messages"] = [{**entry, "text": entry["text"][:200],
                    "truncated": entry["truncated"] or len(entry["text"]) > 200} for entry in previews]
            items.append(item)
        next_cursor = (_encode_cursor({"scope": identity, "at": _iso(selected[-1][-1]), "id": selected[-1][0].id,
                                       "relaxed": relaxed})
                       if len(rows) > limit else None)
    return {"items": items, "next_cursor": next_cursor, "observed_at": _iso(datetime.now(timezone.utc)),
            "coverage": "own_project_conversations_and_watched_tasks", "title_filter_only": bool(query and not relaxed),
            "query_relaxed": relaxed, "needs_assistant": not items, "untrusted_data": True, "grants_authority": False}
