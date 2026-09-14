"""Message centre: one inbox row per thing a user should look at.

Started life as the A5 v2 in-app strip (platform authorization expired,
post published). Now the single durable record behind the message centre:
session events that also go out as a mobile push, system notices such as
skill review results, and first-party announcements fanned out from the
admin console. `user_id` null means everyone in the workspace; `workspace_id`
null means an account-level notice that follows the user across workspaces.
"""
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType

#: Inbox tabs. Session events link to a chat; system rows to a product area;
#: notices to a topic page or an allow-listed URL.
CATEGORIES = ("session", "system", "notice")


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=True
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    category: Mapped[str] = mapped_column(
        String(16), nullable=False, default="system", server_default=text("'system'")
    )
    #: e.g. platform_auth_expired, publish_done, task_completed, announcement
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Allow-listed navigation target; see notifications.inbox.LINK_KINDS.
    link: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: Business idempotency key, the same value as push_messages.event_key.
    source_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    announcement_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("announcements.id"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: The action behind the row (a question, an approval) no longer needs the
    #: user; set together with read_at so stale badges disappear.
    resolved_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_notifications_workspace_created", "workspace_id", "created_at"),
        Index("ix_notifications_user_created", "user_id", "created_at"),
        UniqueConstraint("user_id", "source_key", name="uq_notifications_source"),
    )


class Announcement(Base):
    """A first-party notice authored in the admin console."""

    __tablename__ = "announcements"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: draft | scheduled | published | revoked
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    link: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: {"kind": "all"} | {"kind": "users", "ids": [...]} |
    #: {"kind": "workspace", "id": ...} | {"kind": "role", "role": "admin"}
    audience: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    push: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    publish_at: Mapped[datetime | None] = mapped_column(nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    fanout_at: Mapped[datetime | None] = mapped_column(nullable=True)
    fanout_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default=text("0"))
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class Topic(Base):
    """A topic page: Markdown rendered natively on web and mobile."""

    __tablename__ = "topics"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    cover_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    content_md: Mapped[str] = mapped_column(Text, nullable=False, default="")
    cta_label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    cta_link: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: draft | published
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
