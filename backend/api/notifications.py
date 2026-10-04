"""In-app notifications for the selected workspace."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.notification import Notification
from notifications import inbox

router = APIRouter(
    prefix="/api/notifications", tags=["notifications"], dependencies=[Depends(get_workspace)]
)


def _iso(when: datetime | None) -> str | None:
    if when is None:
        return None
    aware = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return aware.isoformat()


def _to_item(row: Notification) -> dict:
    return {
        "id": row.id,
        "kind": row.kind,
        "title": row.title,
        "body": row.body,
        "category": row.category,
        "link": row.link,
        "readAt": _iso(row.read_at),
        "createdAt": _iso(row.created_at),
    }


@router.get("")
async def list_notifications(
    unread: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    async with get_db_session() as db:
        from assistant.transactions import begin_snapshot
        await begin_snapshot(db)
        rows, _ = await inbox.list_inbox(db, current_user["user_id"], current_user["workspace_id"],
            unread_only=unread, limit=limit, workspace_only=True)
        counts = await inbox.unread_counts(db, current_user["user_id"], current_user["workspace_id"],
            workspace_only=True)
    return {"items": [_to_item(r) for r in rows], "unread": counts["total"]}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, current_user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        row = await inbox.mark_read(db, current_user["user_id"], current_user["workspace_id"],
            notification_id, workspace_only=True)
        if row is None:
            raise HTTPException(404, detail="Notification not found")
        await db.commit()
        return _to_item(row)
