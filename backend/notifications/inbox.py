"""Message centre: the durable inbox row behind pushes, notices and system events.

Every producer writes through `add_inbox` inside its own transaction. A push
is only a delivery channel for one of these rows, so the phone can mark the
row read when the notification is tapped. Links are allow-listed structures,
never free URLs, except first-party notices whose hosts are configured.
"""
import base64
import os
import re
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import and_, delete, event, func, or_, select, update

from auth.mobile import now, utc
from bus import bus
from bus.events import INBOX_UPDATED
from core.identifier import ascending
from db.models.notification import CATEGORIES, Notification
from db.models.workspace import WorkspaceMember

#: Which inbox tab a producer kind lands in.
CATEGORY_FOR_KIND = {
    "task_completed": "session", "task_failed": "session", "input_required": "session",
    "approval_required": "session", "cron_completed": "session", "cron_failed": "session",
    "publish_done": "system", "publish_failed": "system", "platform_auth_expired": "system",
    "desktop_login_reset": "system", "desktop_login_expired": "system",
    "skill_pending": "system", "skill_listed": "system", "skill_rejected": "system", "skill_delisted": "system",
    "announcement": "notice", "system_test": "system",
}

#: Navigation targets a client may resolve. Anything else opens the inbox.
LINK_KINDS = {"session", "cron", "auth_center", "skills", "admin_skills", "topic", "url", "inbox"}
SESSION_RETENTION = timedelta(days=90)
EXPIRED_RETENTION = timedelta(days=30)
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")


def category_for(kind: str) -> str:
    return CATEGORY_FOR_KIND.get(kind, "system")


def link_for(kind: str, *, workspace_id=None, session_id=None, action_id=None) -> dict | None:
    """Derive the target a business event should open."""
    if session_id and workspace_id:
        return {"kind": "session", "workspaceId": workspace_id, "sessionId": session_id}
    if kind.startswith("cron_") and workspace_id:
        return {"kind": "cron", "workspaceId": workspace_id, **({"jobId": action_id} if action_id else {})}
    if kind in {"platform_auth_expired", "publish_done", "publish_failed", "desktop_login_reset",
                "desktop_login_expired"} and workspace_id:
        link = {"kind": "auth_center", "workspaceId": workspace_id}
        if kind.startswith("publish_") and action_id:
            link["jobId"] = action_id
        return link
    if kind == "skill_pending":
        return {"kind": "admin_skills"}
    if kind.startswith("skill_") and workspace_id:
        return {"kind": "skills", "workspaceId": workspace_id}
    return None


def allowed_link_hosts() -> set[str]:
    """Hosts a first-party `url` link may point at: our own origins plus
    ANNOUNCEMENT_LINK_HOSTS (comma separated)."""
    hosts = {h.strip().lower() for h in os.environ.get("ANNOUNCEMENT_LINK_HOSTS", "").split(",") if h.strip()}
    try:
        from core.config import get_config
        for origin in get_config().cors_origins:
            host = urlsplit(origin).hostname
            if host:
                hosts.add(host.lower())
    except Exception:
        pass
    return hosts


def validate_link(link, *, first_party: bool = False) -> dict | None:
    """Return a normalized link or raise ValueError. Session-origin producers
    may not carry URLs; first-party notices may, on allow-listed hosts."""
    if link is None:
        return None
    if not isinstance(link, dict) or link.get("kind") not in LINK_KINDS:
        raise ValueError("Unsupported link")
    kind = link["kind"]
    out = {"kind": kind}
    if kind == "session":
        if not (isinstance(link.get("workspaceId"), str) and isinstance(link.get("sessionId"), str)):
            raise ValueError("Session link needs workspaceId and sessionId")
        out.update(workspaceId=link["workspaceId"], sessionId=link["sessionId"])
        if link.get("panel") == "desktop":
            out["panel"] = "desktop"
            out["control"] = bool(link.get("control"))
    elif kind in {"cron", "auth_center", "skills"}:
        if not isinstance(link.get("workspaceId"), str):
            raise ValueError(f"{kind} link needs workspaceId")
        out["workspaceId"] = link["workspaceId"]
        if isinstance(link.get("jobId"), str):
            out["jobId"] = link["jobId"]
    elif kind == "topic":
        if not isinstance(link.get("slug"), str) or not _SLUG.match(link["slug"]):
            raise ValueError("Topic link needs a slug")
        out["slug"] = link["slug"]
    elif kind == "url":
        if not first_party:
            raise ValueError("Only first-party notices may link to a URL")
        url = link.get("url")
        parts = urlsplit(url) if isinstance(url, str) else None
        if not parts or parts.scheme != "https" or not parts.hostname or len(url) > 1024:
            raise ValueError("URL links must be https")
        if parts.hostname.lower() not in allowed_link_hosts():
            raise ValueError("URL host is not allow-listed")
        out["url"] = url
    return out


def valid_slug(slug: str) -> bool:
    return bool(_SLUG.match(slug or ""))


def _schedule_badge_refresh(db, user_id: str | None) -> None:
    """Tell the user's open clients to refetch counts once this commits."""
    if not user_id:
        return
    session = db.sync_session
    pending = session.info.setdefault("inbox_updated_users", set())
    if not pending:
        @event.listens_for(session, "after_commit", once=True)
        def _flush(_):
            users = session.info.pop("inbox_updated_users", set())
            for uid in users:
                bus.publish(INBOX_UPDATED, {"userId": uid})
    pending.add(user_id)


async def add_inbox(db, *, user_id: str | None, kind: str, title: str, body: str = "",
                    category: str | None = None, link: dict | None = None,
                    workspace_id: str | None = None, source_key: str | None = None,
                    announcement_id: str | None = None, expires_at=None,
                    first_party: bool = False) -> Notification:
    """Insert one inbox row inside the caller's transaction; idempotent on
    (user_id, source_key). Never commits."""
    category = category or category_for(kind)
    if category not in CATEGORIES:
        raise ValueError("Unsupported inbox category")
    if source_key is not None and (not source_key or len(source_key) > 255):
        raise ValueError("Invalid inbox source key")
    if user_id is None and workspace_id is None:
        raise ValueError("A workspace broadcast needs a workspace")
    if source_key:
        existing = await db.scalar(select(Notification).where(
            Notification.user_id == user_id if user_id else Notification.user_id.is_(None),
            Notification.source_key == source_key,
        ))
        if existing:
            return existing
    row = Notification(
        id=ascending("ntf"), workspace_id=workspace_id, user_id=user_id, category=category,
        kind=kind[:48], title=title.strip()[:255], body=(body or "").strip()[:2000],
        link=validate_link(link, first_party=first_party), source_key=source_key,
        announcement_id=announcement_id, expires_at=expires_at, created_at=now(),
    )
    db.add(row)
    await db.flush()
    _schedule_badge_refresh(db, user_id)
    return row


async def resolve_inbox(db, user_id: str, source_key: str) -> None:
    """The action behind a row is over (answered, cancelled, superseded)."""
    result = await db.execute(update(Notification).where(
        Notification.user_id == user_id, Notification.source_key == source_key,
        Notification.resolved_at.is_(None),
    ).values(resolved_at=now(), read_at=func.coalesce(Notification.read_at, now())))
    if result.rowcount:
        _schedule_badge_refresh(db, user_id)


def _member_workspaces(user_id: str):
    return select(WorkspaceMember.workspace_id).where(
        WorkspaceMember.user_id == user_id, WorkspaceMember.status == "active")


def visible(user_id: str, workspace_id: str | None):
    """Rows a user may see: their own across every workspace they belong to,
    account-level rows, and the current workspace's broadcasts."""
    mine = and_(Notification.user_id == user_id, or_(
        Notification.workspace_id.is_(None), Notification.workspace_id.in_(_member_workspaces(user_id))))
    clauses = [mine]
    if workspace_id:
        clauses.append(and_(Notification.user_id.is_(None), Notification.workspace_id == workspace_id))
    return (or_(*clauses), or_(Notification.expires_at.is_(None), Notification.expires_at > now()))


def encode_cursor(row: Notification) -> str:
    return base64.urlsafe_b64encode(f"{utc(row.created_at).isoformat()}|{row.id}".encode()).decode()


def decode_cursor(cursor: str | None):
    if not cursor:
        return None
    try:
        from datetime import datetime
        stamp, row_id = base64.urlsafe_b64decode(cursor.encode()).decode().split("|", 1)
        return utc(datetime.fromisoformat(stamp)), row_id
    except Exception:
        raise ValueError("Invalid cursor")


async def list_inbox(db, user_id: str, workspace_id: str | None, *, category: str | None = None,
                     unread_only: bool = False, cursor: str | None = None, limit: int = 30):
    stmt = select(Notification).where(*visible(user_id, workspace_id))
    if category:
        stmt = stmt.where(Notification.category == category)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    after = decode_cursor(cursor)
    if after:
        stamp, row_id = after
        stmt = stmt.where(or_(Notification.created_at < stamp,
                              and_(Notification.created_at == stamp, Notification.id < row_id)))
    rows = list((await db.scalars(stmt.order_by(Notification.created_at.desc(), Notification.id.desc())
                                  .limit(limit + 1))).all())
    next_cursor = encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return rows[:limit], next_cursor


async def unread_counts(db, user_id: str, workspace_id: str | None) -> dict:
    rows = (await db.execute(select(Notification.category, func.count()).where(
        *visible(user_id, workspace_id), Notification.read_at.is_(None),
    ).group_by(Notification.category))).all()
    counts = {category: 0 for category in CATEGORIES}
    for category, count in rows:
        counts[category] = int(count)
    return {"total": sum(counts.values()), **counts}


async def mark_read(db, user_id: str, workspace_id: str | None, notification_id: str) -> Notification | None:
    row = await db.scalar(select(Notification).where(
        Notification.id == notification_id, *visible(user_id, workspace_id)))
    if row and row.read_at is None:
        row.read_at = now()
        _schedule_badge_refresh(db, user_id)
    return row


async def mark_all_read(db, user_id: str, workspace_id: str | None, category: str | None = None) -> int:
    stmt = select(Notification.id).where(*visible(user_id, workspace_id), Notification.read_at.is_(None))
    if category:
        stmt = stmt.where(Notification.category == category)
    ids = list((await db.scalars(stmt)).all())
    if not ids:
        return 0
    await db.execute(update(Notification).where(Notification.id.in_(ids)).values(read_at=now()))
    _schedule_badge_refresh(db, user_id)
    return len(ids)


async def sweep(db) -> int:
    """Retention: session rows older than 90 days, expired rows 30 days on."""
    current = now()
    removed = 0
    removed += (await db.execute(delete(Notification).where(
        Notification.category == "session", Notification.created_at < current - SESSION_RETENTION))).rowcount or 0
    removed += (await db.execute(delete(Notification).where(
        Notification.expires_at.is_not(None), Notification.expires_at < current - EXPIRED_RETENTION))).rowcount or 0
    return removed


def public_item(row: Notification) -> dict:
    def iso(value):
        return utc(value).isoformat() if value else None
    return {"id": row.id, "category": row.category, "kind": row.kind, "title": row.title, "body": row.body,
            "link": row.link, "workspaceId": row.workspace_id, "announcementId": row.announcement_id,
            "readAt": iso(row.read_at), "resolvedAt": iso(row.resolved_at), "expiresAt": iso(row.expires_at),
            "createdAt": iso(row.created_at)}
