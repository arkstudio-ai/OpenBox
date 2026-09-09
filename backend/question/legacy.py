"""Close pre-checkpoint ask cards on the first durable-runtime startup.

Old in-memory coroutines cannot be reconstructed or assumed approved. Deploy
this protocol change with old workers drained; a mixed old/new worker rollout
cannot safely route answers to both implementations.
"""
from sqlalchemy import JSON, func, select, type_coerce

from bus import bus
from db.base import get_db_session
from db.models.part import Part
from db.models.question import SessionExecution
from db.models.session import Session
from question import runtime


async def reconcile_legacy_questions() -> int:
    data = type_coerce(Part.data, JSON)
    tool = func.coalesce(Part.canonical_tool_id, data["tool"].as_string())
    filters = (
        Part.type == "tool", data["status"].as_string().in_(("pending", "running")),
        ((tool.in_(("question", "plan_enter"))) |
         ((tool == "creator_context") & (data["input"]["action"].as_string() == "propose_memory"))),
    )
    async with get_db_session() as db:
        candidates = (await db.execute(select(Part.session_id, Part.user_id)
            .join(Session, Session.id == Part.session_id)
            .outerjoin(SessionExecution, SessionExecution.session_id == Part.session_id)
            .where(*filters, Session.is_deleted == False,  # noqa: E712
                   SessionExecution.session_id.is_(None)).distinct())).all()
    closed = 0
    for session_id, user_id in candidates:
        try:
            async with runtime.transaction(session_id, user_id) as (db, session, execution):
                if execution.run_id or execution.generation:
                    continue  # New work acquired the session after the scan.
                rows = (await db.scalars(select(Part).where(
                    *filters, Part.session_id == session_id, Part.user_id == user_id,
                ))).all()
                for part in rows:
                    questions = (part.data.get("input") or {}).get("questions") or []
                    part.data = {**part.data, "status": "error", "title": "Question expired",
                        "error": "This question belonged to an interrupted legacy run. No approval was granted; send a message to continue.",
                        "metadata": {**(part.data.get("metadata") or {}), "question_status": "expired",
                            "questions": [q["question"] for q in questions if isinstance(q, dict) and "question" in q]}}
                if rows:
                    session.status = "error"
                    execution.resume_error = "Legacy question interrupted; fresh confirmation is required."
            for part in rows:
                bus.publish("part.updated", {"userId": user_id, "sessionId": session_id,
                    "messageId": part.message_id, "part": part.data})
            if rows:
                closed += len(rows)
                runtime.publish_status(session_id, user_id, "error")
        except LookupError:
            continue
    return closed
