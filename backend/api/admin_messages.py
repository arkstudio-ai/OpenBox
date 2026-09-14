"""Platform-admin console for first-party announcements and topic pages."""
import asyncio
from datetime import datetime
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from api.inbox import public_topic
from audit import record
from auth.mobile import now, utc
from core.identifier import ascending
from db.base import get_db_session
from db.models.notification import Announcement, Topic
from notifications import announcements, inbox
from notifications.testing import live_admin

router = APIRouter(prefix="/api/admin/messages", tags=["admin-messages"])


class AnnouncementIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=500)
    link: dict | None = None
    audience: dict = Field(default_factory=lambda: {"kind": "all"})
    push: bool = False
    publishAt: datetime | None = None
    expiresAt: datetime | None = None


class TopicIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    slug: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=120)
    coverUrl: str | None = Field(default=None, max_length=1024)
    contentMd: str = Field(default="", max_length=200_000)
    ctaLabel: str | None = Field(default=None, max_length=40)
    ctaLink: dict | None = None


def _bad(message: str):
    return HTTPException(422, detail={"code": "MESSAGE_INVALID", "message": message})


def _apply_announcement(row: Announcement, body: AnnouncementIn) -> None:
    try:
        row.link = inbox.validate_link(body.link, first_party=True)
        row.audience = announcements.validate_audience(body.audience)
    except ValueError as error:
        raise _bad(str(error))
    if body.expiresAt and body.publishAt and utc(body.expiresAt) <= utc(body.publishAt):
        raise _bad("expiresAt must be after publishAt")
    if body.expiresAt and utc(body.expiresAt) <= now():
        raise _bad("expiresAt is in the past")
    row.title, row.body, row.push = body.title, body.body, body.push
    row.publish_at = utc(body.publishAt) if body.publishAt else None
    row.expires_at = utc(body.expiresAt) if body.expiresAt else None
    row.updated_at = now()


async def _announcement(db, announcement_id: str) -> Announcement:
    row = await db.get(Announcement, announcement_id)
    if row is None:
        raise HTTPException(404, detail="Announcement not found")
    return row


async def _view(db, row: Announcement) -> dict:
    return {**announcements.public_announcement(row),
            "recipientCount": await announcements.count_recipients(db, row.audience)}


# ── Announcements ──────────────────────────────────────────────────────────
@router.get("/announcements")
async def list_announcements(status: Literal["draft", "scheduled", "published", "revoked"] | None = Query(None),
                             limit: int = Query(50, ge=1, le=200), user=Depends(live_admin)):
    stmt = select(Announcement)
    if status:
        stmt = stmt.where(Announcement.status == status)
    async with get_db_session() as db:
        rows = list((await db.scalars(stmt.order_by(Announcement.created_at.desc()).limit(limit))).all())
        return {"items": [announcements.public_announcement(r) for r in rows]}


@router.post("/announcements", status_code=201)
async def create_announcement(body: AnnouncementIn, request: Request, user=Depends(live_admin)):
    row = Announcement(id=ascending("ann"), status="draft", created_by=user["user_id"],
                       created_at=now(), updated_at=now(), audience={"kind": "all"})
    _apply_announcement(row, body)
    async with get_db_session() as db:
        db.add(row)
        await db.commit()
        view = await _view(db, row)
    await record(user["user_id"], None, "admin.announcement_create", "announcement", row.id, None, request)
    return view


@router.get("/announcements/{announcement_id}")
async def get_announcement(announcement_id: str, user=Depends(live_admin)):
    async with get_db_session() as db:
        return await _view(db, await _announcement(db, announcement_id))


@router.put("/announcements/{announcement_id}")
async def update_announcement(announcement_id: str, body: AnnouncementIn, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _announcement(db, announcement_id)
        if row.status not in {"draft", "scheduled"}:
            raise HTTPException(409, detail={"code": "MESSAGE_ALREADY_PUBLISHED"})
        _apply_announcement(row, body)
        if row.status == "scheduled" and not row.publish_at:
            row.status = "draft"
        await db.commit()
        view = await _view(db, row)
    await record(user["user_id"], None, "admin.announcement_update", "announcement", row.id, None, request)
    return view


@router.post("/announcements/{announcement_id}/publish")
async def publish_announcement(announcement_id: str, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _announcement(db, announcement_id)
        if row.status not in {"draft", "scheduled"}:
            raise HTTPException(409, detail={"code": "MESSAGE_ALREADY_PUBLISHED"})
        if row.publish_at and utc(row.publish_at) > now():
            row.status, row.updated_at = "scheduled", now()
        else:
            await announcements.publish(db, row)
        await db.commit()
        status = row.status
        view = await _view(db, row)
    if status == "published":
        asyncio.create_task(announcements.fan_out(announcement_id))
    await record(user["user_id"], None, "admin.announcement_publish", "announcement", announcement_id,
                 {"status": status}, request)
    return view


@router.post("/announcements/{announcement_id}/revoke")
async def revoke_announcement(announcement_id: str, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _announcement(db, announcement_id)
        if row.status not in {"scheduled", "published"}:
            raise HTTPException(409, detail={"code": "MESSAGE_NOT_PUBLISHED"})
        hidden = await announcements.revoke(db, row)
        await db.commit()
        view = await _view(db, row)
    await record(user["user_id"], None, "admin.announcement_revoke", "announcement", announcement_id,
                 {"hidden": hidden}, request)
    return {**view, "hidden": hidden}


@router.post("/announcements/{announcement_id}/preview", status_code=202)
async def preview_announcement(announcement_id: str, request: Request, user=Depends(live_admin)):
    """Deliver this announcement to the requesting admin only, as a recipient
    would receive it (inbox row, and a push when the announcement asks)."""
    from auth.mobile import mobile_transaction
    async with mobile_transaction() as db:
        row = await _announcement(db, announcement_id)
        preview = Announcement(id=row.id, status="published", title=f"预览 · {row.title}"[:120], body=row.body,
                               link=row.link, audience=row.audience, push=row.push, expires_at=row.expires_at)
        item, _ = await announcements.deliver_to(db, preview, user["user_id"],
                                                 source_key=f"announcement:{row.id}:preview:{uuid4().hex}")
        result = inbox.public_item(item)
    await record(user["user_id"], None, "admin.announcement_preview", "announcement", announcement_id, None, request)
    return result


# ── Topics ─────────────────────────────────────────────────────────────────
def _apply_topic(row: Topic, body: TopicIn) -> None:
    if not inbox.valid_slug(body.slug):
        raise _bad("slug must be 2-64 lowercase letters, digits or dashes")
    if body.coverUrl and not body.coverUrl.startswith("https://"):
        raise _bad("coverUrl must be https")
    try:
        row.cta_link = inbox.validate_link(body.ctaLink, first_party=True)
    except ValueError as error:
        raise _bad(str(error))
    if bool(body.ctaLabel) != bool(row.cta_link):
        raise _bad("ctaLabel and ctaLink go together")
    row.slug, row.title, row.cover_url = body.slug, body.title, body.coverUrl or None
    row.content_md, row.cta_label = body.contentMd, body.ctaLabel or None
    row.updated_at = now()


async def _topic(db, topic_id: str) -> Topic:
    row = await db.get(Topic, topic_id)
    if row is None:
        raise HTTPException(404, detail="Topic not found")
    return row


def _topic_view(row: Topic) -> dict:
    return {**public_topic(row), "status": row.status, "createdBy": row.created_by,
            "createdAt": utc(row.created_at).isoformat()}


async def _slug_free(db, slug: str, topic_id: str | None) -> None:
    other = await db.scalar(select(Topic).where(Topic.slug == slug))
    if other and other.id != topic_id:
        raise HTTPException(409, detail={"code": "TOPIC_SLUG_TAKEN"})


@router.get("/topics")
async def list_topics(limit: int = Query(100, ge=1, le=500), user=Depends(live_admin)):
    async with get_db_session() as db:
        rows = list((await db.scalars(select(Topic).order_by(Topic.updated_at.desc()).limit(limit))).all())
        return {"items": [_topic_view(r) for r in rows]}


@router.post("/topics", status_code=201)
async def create_topic(body: TopicIn, request: Request, user=Depends(live_admin)):
    row = Topic(id=ascending("tpc"), status="draft", created_by=user["user_id"], created_at=now(), updated_at=now())
    _apply_topic(row, body)
    async with get_db_session() as db:
        await _slug_free(db, row.slug, None)
        db.add(row)
        await db.commit()
    await record(user["user_id"], None, "admin.topic_create", "topic", row.id, {"slug": row.slug}, request)
    return _topic_view(row)


@router.get("/topics/{topic_id}")
async def get_topic(topic_id: str, user=Depends(live_admin)):
    async with get_db_session() as db:
        return _topic_view(await _topic(db, topic_id))


@router.put("/topics/{topic_id}")
async def update_topic(topic_id: str, body: TopicIn, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _topic(db, topic_id)
        if row.status == "published" and body.slug != row.slug:
            raise HTTPException(409, detail={"code": "TOPIC_SLUG_LOCKED"})
        await _slug_free(db, body.slug, row.id)
        _apply_topic(row, body)
        await db.commit()
        view = _topic_view(row)
    await record(user["user_id"], None, "admin.topic_update", "topic", topic_id, None, request)
    return view


@router.post("/topics/{topic_id}/publish")
async def publish_topic(topic_id: str, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _topic(db, topic_id)
        if row.status != "published":
            row.status, row.published_at, row.updated_at = "published", now(), now()
        await db.commit()
        view = _topic_view(row)
    await record(user["user_id"], None, "admin.topic_publish", "topic", topic_id, None, request)
    return view


@router.post("/topics/{topic_id}/unpublish")
async def unpublish_topic(topic_id: str, request: Request, user=Depends(live_admin)):
    async with get_db_session() as db:
        row = await _topic(db, topic_id)
        row.status, row.updated_at = "draft", now()
        await db.commit()
        view = _topic_view(row)
    await record(user["user_id"], None, "admin.topic_unpublish", "topic", topic_id, None, request)
    return view
