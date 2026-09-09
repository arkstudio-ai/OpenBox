"""One attempt to post a resource-centre file to a platform.

For Douyin the backend never uploads anything: it signs a share schema, the
person scans it, and the Douyin app fetches the video itself. The job row is
the handle the webhook uses to report back (matched on share_id) and the
thing the page polls.
"""
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Filled in when the webhook tells us which bound account published it.
    platform_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_asset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    hashtags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    #: Douyin share_id: the correlation key for the create_video webhook.
    share_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: pending | published | failed | expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    item_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    from_open_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Route-specific record. Desktop (创作者中心) route: mode, visibility,
    #: declaration, hot_word, scheduled_at, elapsed_ms, evidence asset, risk signal.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_publish_jobs_share", "share_id"),
        Index("ix_publish_jobs_workspace_created", "workspace_id", "created_at"),
    )
