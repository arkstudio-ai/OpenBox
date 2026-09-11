"""In-app notifications for the selected workspace."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select

from auth.mobile import now

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.notification import Notification

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


def _visible(user_id: str, workspace_id: str):
    # Compatibility for the authorization-centre strip; the message centre
    # proper lives at /api/inbox and also shows account-level notices.
    return (
        Notification.workspace_id == workspace_id,
        or_(Notification.user_id.is_(None), Notification.user_id == user_id),
        or_(Notification.expires_at.is_(None), Notification.expires_at > now()),
    )


@router.get("")
async def list_notifications(
    unread: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    current_user: dict = Depends(get_current_user),
):
    stmt = select(Notification).where(*_visible(current_user["user_id"], current_user["workspace_id"]))
    if unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    async with get_db_session() as db:
        rows = list((await db.execute(stmt.order_by(Notification.created_at.desc()).limit(limit))).scalars())
        unread_count = (
            await db.execute(
                select(func.count()).select_from(Notification).where(
                    *_visible(current_user["user_id"], current_user["workspace_id"]),
                    Notification.read_at.is_(None),
                )
            )
        ).scalar_one()
    return {"items": [_to_item(r) for r in rows], "unread": int(unread_count or 0)}


@router.post("/{notification_id}/read")
async def mark_read(notification_id: str, current_user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        row = (
            await db.execute(
                select(Notification).where(
                    Notification.id == notification_id,
                    *_visible(current_user["user_id"], current_user["workspace_id"]),
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, detail="Notification not found")
        if row.read_at is None:
            row.read_at = datetime.now(timezone.utc)
            await db.commit()
        return _to_item(row)
