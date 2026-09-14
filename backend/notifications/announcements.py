"""First-party announcements: audience resolution, fan-out and the janitor.

Publishing turns one announcement into one inbox row per recipient, in
batches, idempotent on `announcement:{id}`. Push is opt-in per announcement
and goes through the same outbox and presence rules as business events.
"""
import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from auth.mobile import mobile_transaction, now, utc
from core.log import create_logger
from db.base import get_db_session
from db.models.notification import Announcement, Notification
from db.models.user import User
from db.models.workspace import WorkspaceMember
from notifications.inbox import add_inbox, sweep
from notifications.store import enqueue_notification

log = create_logger("announcements")

BATCH = 500
AUDIENCE_KINDS = {"all", "users", "workspace", "role"}


def validate_audience(audience) -> dict:
    if not isinstance(audience, dict) or audience.get("kind") not in AUDIENCE_KINDS:
        raise ValueError("Unsupported audience")
    kind = audience["kind"]
    if kind == "users":
        ids = audience.get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 5000 or not all(isinstance(i, str) and i for i in ids):
            raise ValueError("users audience needs 1-5000 ids")
        return {"kind": kind, "ids": sorted(set(ids))}
    if kind == "workspace":
        if not isinstance(audience.get("id"), str) or not audience["id"]:
            raise ValueError("workspace audience needs an id")
        return {"kind": kind, "id": audience["id"]}
    if kind == "role":
        if audience.get("role") not in {"admin", "user"}:
            raise ValueError("role audience needs admin or user")
        return {"kind": kind, "role": audience["role"]}
    return {"kind": "all"}


def _recipients_stmt(audience: dict):
    stmt = select(User.id).where(User.is_active.is_(True), User.is_deleted.is_(False))
    kind = audience["kind"]
    if kind == "users":
        stmt = stmt.where(User.id.in_(audience["ids"]))
    elif kind == "workspace":
        stmt = stmt.where(User.id.in_(select(WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == audience["id"], WorkspaceMember.status == "active")))
    elif kind == "role":
        stmt = stmt.where(User.role == audience["role"])
    return stmt.order_by(User.id)


async def count_recipients(db, audience: dict) -> int:
    from sqlalchemy import func
    return int(await db.scalar(select(func.count()).select_from(_recipients_stmt(audience).subquery())) or 0)


def public_announcement(row: Announcement) -> dict:
    def iso(value):
        return utc(value).isoformat() if value else None
    return {"id": row.id, "status": row.status, "title": row.title, "body": row.body, "link": row.link,
            "audience": row.audience, "push": row.push, "publishAt": iso(row.publish_at),
            "expiresAt": iso(row.expires_at), "publishedAt": iso(row.published_at),
            "fanoutAt": iso(row.fanout_at), "fanoutCount": row.fanout_count,
            "createdBy": row.created_by, "createdAt": iso(row.created_at), "updatedAt": iso(row.updated_at)}


async def deliver_to(db, announcement: Announcement, user_id: str, *, source_key: str | None = None,
                     workspace_id: str | None = None) -> tuple[Notification, bool]:
    """One recipient: inbox row, plus a push if the announcement asks for it.
    Returns the row and whether this call created it."""
    source_key = source_key or f"announcement:{announcement.id}"
    existing = await db.scalar(select(Notification).where(
        Notification.user_id == user_id, Notification.source_key == source_key))
    if existing:
        return existing, False
    row = await add_inbox(db, user_id=user_id, workspace_id=workspace_id, category="notice", kind="announcement",
                          title=announcement.title, body=announcement.body, link=announcement.link,
                          source_key=source_key, announcement_id=announcement.id,
                          expires_at=announcement.expires_at, first_party=True)
    if announcement.push:
        ttl = 86400
        if announcement.expires_at:
            ttl = max(60, min(ttl, int((utc(announcement.expires_at) - now()).total_seconds())))
        await enqueue_notification(db, user_id=user_id, event_key=source_key, kind="notice",
                                   title=announcement.title, body=announcement.body, notification_id=row.id,
                                   ttl_seconds=ttl, guard={"kind": "announcement", "id": announcement.id})
    return row, True


async def fan_out(announcement_id: str) -> int:
    """Deliver a published announcement to everyone in its audience. Safe to
    re-run: recipients that already have their row are skipped."""
    async with get_db_session() as db:
        head = await db.get(Announcement, announcement_id)
        if not head or head.status != "published":
            return 0
        audience = head.audience
    delivered, last_id = 0, ""
    while True:
        async with mobile_transaction() as db:
            announcement = await db.get(Announcement, announcement_id)
            if not announcement or announcement.status != "published":
                return delivered
            ids = list((await db.scalars(_recipients_stmt(audience).where(User.id > last_id).limit(BATCH))).all())
            if not ids:
                announcement.fanout_at, announcement.updated_at = now(), now()
                announcement.fanout_count = await _delivered_count(db, announcement_id)
                return delivered
            workspace_id = audience["id"] if audience["kind"] == "workspace" else None
            for user_id in ids:
                try:
                    _, created = await deliver_to(db, announcement, user_id, workspace_id=workspace_id)
                    delivered += created
                except Exception as error:  # A revoked member must not stall the batch.
                    log.warning("Announcement %s skipped user %s: %s", announcement_id, user_id, type(error).__name__)
            last_id = ids[-1]
            announcement.fanout_count = await _delivered_count(db, announcement_id)
            announcement.updated_at = now()


async def _delivered_count(db, announcement_id: str) -> int:
    from sqlalchemy import func
    return int(await db.scalar(select(func.count()).select_from(Notification).where(
        Notification.announcement_id == announcement_id,
        Notification.source_key == f"announcement:{announcement_id}")) or 0)


async def publish(db, announcement: Announcement) -> None:
    """Flip to published inside the caller's transaction; fan out after commit."""
    announcement.status, announcement.published_at, announcement.updated_at = "published", now(), now()
    await db.flush()


async def revoke(db, announcement: Announcement) -> int:
    """Stop showing the notice: unread rows expire now, pushes get guarded out."""
    announcement.status, announcement.updated_at = "revoked", now()
    result = await db.execute(update(Notification).where(
        Notification.announcement_id == announcement.id, Notification.read_at.is_(None),
    ).values(expires_at=now()))
    return result.rowcount or 0


async def publish_due() -> list[str]:
    """Scheduled announcements whose time has come."""
    published = []
    async with get_db_session() as db:
        rows = list((await db.scalars(select(Announcement).where(
            Announcement.status == "scheduled", Announcement.publish_at <= now()))).all())
        for row in rows:
            await publish(db, row)
            published.append(row.id)
        await db.commit()
    return published


class InboxJanitor:
    """Publishes scheduled announcements every minute and applies retention daily."""

    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self.task = None
        self._last_sweep = None

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self._run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def tick(self):
        for announcement_id in await publish_due():
            await fan_out(announcement_id)
        if self._last_sweep is None or now() - self._last_sweep > timedelta(hours=24):
            async with get_db_session() as db:
                removed = await sweep(db)
                await db.commit()
            self._last_sweep = now()
            if removed:
                log.info("Inbox retention removed %d rows", removed)

    async def _run(self):
        while True:
            try:
                await self.tick()
            except Exception as error:
                log.warning("Inbox janitor deferred: %s", type(error).__name__)
            await asyncio.sleep(self.interval)
