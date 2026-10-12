"""Bounded background for the model to associate work by meaning, not title words.

These excerpts are navigation data, never user authorization. The model can
read the referenced history and page the directory with the existing tools.
"""
from sqlalchemy import JSON, and_, func, or_, select, type_coerce

from db.models.message import Message
from db.models.part import Part
from db.models.project_brief import ProjectBrief
from memory.redaction import redact_credentials


def excerpt(text, limit):
    text = redact_credentials(str(text or ""))
    return {"text": text[:limit], "truncated": len(text) > limit}


async def project_background(db, rows, *, user_id, workspace_id, include_brief=True):
    ids = [row.id for row in rows]
    briefs = {row.project_id: row.content for row in (await db.scalars(select(ProjectBrief).where(
        ProjectBrief.project_id.in_(ids), ProjectBrief.user_id == user_id,
        ProjectBrief.workspace_id == workspace_id))).all()} if ids else {}
    return {row.id: {"description": excerpt(row.description, 400),
                    **({"brief": excerpt(briefs.get(row.id), 600)} if include_brief
                       else {"brief_available": bool(briefs.get(row.id))}),
                    "is_default": row.slug == "default"}
            for row in rows}


async def conversation_background(db, ids, *, user_id, include_assistant=True):
    if not ids:
        return {}
    # Rank in SQL, so a conversation with years of history does not load it all.
    # Only ordinary visible text is considered; tool output and hidden summaries
    # must not become apparent user requests in a candidate preview.
    data = type_coerce(Part.data, JSON)
    ranked = select(Part.session_id, Part.message_id, Message.role, Part.data,
        func.row_number().over(partition_by=(Part.session_id, Message.role),
            order_by=(Message.created_at.desc(), Message.id.desc(), Part.id.desc())).label("rank"),
        func.row_number().over(partition_by=(Part.session_id, Message.role),
            order_by=(Message.created_at, Message.id, Part.id)).label("first_rank"),
        Message.created_at).join(Message, Message.id == Part.message_id).where(
            Part.session_id.in_(ids), Part.user_id == user_id, Message.user_id == user_id,
            Message.session_id == Part.session_id, Part.type == "text",
            Message.role.in_(("user", "assistant") if include_assistant else ("user",)), Message.summary.is_not(True),
            data["ignored"].as_boolean().is_not(True),
            data["synthetic"].as_boolean().is_not(True)).subquery()
    rows = (await db.execute(select(ranked).where(or_(ranked.c.rank == 1,
        and_(ranked.c.role == "user", ranked.c.first_rank == 1)))
        .order_by(ranked.c.created_at, ranked.c.message_id))).all()
    result = {}
    for row in rows:
        text = excerpt((row.data or {}).get("text"), 500)
        if text["text"]:
            result.setdefault(row.session_id, []).append({"message_id": row.message_id,
                "role": row.role, **text})
    return result


async def directory(db, main):
    """An initial page is supplied before every ordinary assistant decision.

    A cursor means the inventory is incomplete, not that no matching work exists.
    Report and coordination turns never receive this cross-project background.
    """
    from assistant.reads import list_projects, list_sessions
    from assistant.business_context import _safe
    scope = {"user_id": main.user_id, "workspace_id": main.workspace_id, "main_id": main.id,
             "db": db, "include_context": True}
    # Automatic navigation must not replay project memory or raw result bodies
    # across turns. The model reads those explicitly when a candidate warrants it.
    return _safe({"projects": await list_projects(**scope, limit=8, include_brief=False),
                  "conversations": await list_sessions(**scope, limit=6, include_assistant=False), "untrusted_data": True})
