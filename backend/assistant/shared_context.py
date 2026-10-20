"""Fresh, bounded working facts shared by the assistant's input surfaces.

SQL is authoritative. This is short-term navigation, not long-term memory or
permission; asset IDs remain scoped to their original owner and workspace.
"""
import json

from sqlalchemy import func, select

from assets.service import resource_query
from db.models.file_asset import FileAsset
from memory.redaction import redact_credentials


async def recent_decisions(db, user_id, workspace_id, *, limit=12):
    """Actual card answers, including manual clicks, with their original task identity."""
    from db.models.question import QuestionCheckpoint
    from db.models.session import Session
    rows = (await db.execute(select(QuestionCheckpoint, Session.title).join(
        Session, Session.id == QuestionCheckpoint.session_id).where(
        QuestionCheckpoint.user_id == user_id, Session.user_id == user_id,
        Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
        QuestionCheckpoint.status.in_(("answered", "rejected")))
        .order_by(QuestionCheckpoint.updated_at.desc(), QuestionCheckpoint.id.desc()).limit(limit))).all()
    items, remaining = [], 12000
    for row, title in rows:
        setup = (row.continuation or {}).get("assistant_task_setup") or {}
        original = setup.get("input") or {}
        selected = (row.answers or [[]])[0]
        item = {"request_id": row.id, "session_id": row.session_id,
            "conversation": redact_credentials(title or "个人助理"), "state": row.status,
            "questions": [{"header": q.get("header"), "question": redact_credentials(q.get("question") or "")[:400]}
                          for q in (row.questions or [])[:6]],
            "answers": [[redact_credentials(str(answer))[:400] for answer in group[:10]]
                        for group in (row.answers or [])[:6]],
            "task_title": redact_credentials(str(original.get("title") or ""))[:200],
            "selected_settings": (setup.get("choices") or {}).get(selected[0]) if len(selected) == 1 else None,
            "answered_at": row.updated_at.isoformat(), "execution_confirmed": False}
        size = len(json.dumps(item, ensure_ascii=False))
        if size > remaining:
            item = {key: item[key] for key in ("request_id", "session_id", "state", "answered_at", "execution_confirmed")}
            item["details_omitted"] = True
            size = len(json.dumps(item, ensure_ascii=False))
        if size > remaining:
            break
        items.append(item)
        remaining -= size
    return items


async def recent_assets(db, user_id, workspace_id, *, limit=12):
    rows = (await db.scalars(resource_query(user_id, workspace_id, owned_only=True).order_by(
        func.coalesce(FileAsset.description_updated_at, FileAsset.created_at).desc(),
        FileAsset.id.desc()).limit(limit))).all()
    return [{"asset_id": row.id, "name": redact_credentials(row.name), "mime": row.mime,
             "description": redact_credentials(row.description or "")[:600],
             "session_id": row.session_id, "created_at": row.created_at.isoformat()}
            for row in rows]
