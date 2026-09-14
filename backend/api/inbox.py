"""Message centre for the signed-in user, plus public topic pages."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from db.base import get_db_session
from db.models.notification import Topic
from notifications import inbox

router = APIRouter(prefix="/api/inbox", tags=["inbox"], dependencies=[Depends(get_workspace)])
topics_router = APIRouter(prefix="/api/topics", tags=["topics"])

Category = Literal["session", "system", "notice"]


@router.get("")
async def list_inbox(
    category: Category | None = Query(None),
    unread: bool = Query(False),
    cursor: str | None = Query(None, max_length=256),
    limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
):
    async with get_db_session() as db:
        try:
            rows, next_cursor = await inbox.list_inbox(
                db, current_user["user_id"], current_user.get("workspace_id"),
                category=category, unread_only=unread, cursor=cursor, limit=limit)
        except ValueError:
            raise HTTPException(400, detail={"code": "INBOX_BAD_CURSOR"})
        counts = await inbox.unread_counts(db, current_user["user_id"], current_user.get("workspace_id"))
    return {"items": [inbox.public_item(r) for r in rows], "nextCursor": next_cursor, "unread": counts}


@router.get("/unread")
async def unread(current_user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        return await inbox.unread_counts(db, current_user["user_id"], current_user.get("workspace_id"))


@router.post("/read-all")
async def read_all(category: Category | None = Query(None), current_user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        count = await inbox.mark_all_read(db, current_user["user_id"], current_user.get("workspace_id"), category)
        await db.commit()
    return {"updated": count}


@router.post("/{notification_id}/read")
async def read_one(notification_id: str, current_user: dict = Depends(get_current_user)):
    async with get_db_session() as db:
        row = await inbox.mark_read(db, current_user["user_id"], current_user.get("workspace_id"), notification_id)
        if row is None:
            raise HTTPException(404, detail="Notification not found")
        await db.commit()
        return inbox.public_item(row)


def public_topic(row: Topic) -> dict:
    from auth.mobile import utc
    return {"id": row.id, "slug": row.slug, "title": row.title, "coverUrl": row.cover_url,
            "contentMd": row.content_md, "ctaLabel": row.cta_label, "ctaLink": row.cta_link,
            "publishedAt": utc(row.published_at).isoformat() if row.published_at else None,
            "updatedAt": utc(row.updated_at).isoformat()}


@topics_router.get("/{slug}")
async def get_topic(slug: str):
    """Public: a published topic page. Drafts are 404 to everyone here."""
    if not inbox.valid_slug(slug):
        raise HTTPException(404, detail="Topic not found")
    async with get_db_session() as db:
        row = await db.scalar(select(Topic).where(Topic.slug == slug, Topic.status == "published"))
    if row is None:
        raise HTTPException(404, detail="Topic not found")
    return public_topic(row)
