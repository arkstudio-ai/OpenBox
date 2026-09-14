"""In-app notifications (A5 v2): things that happened while nobody was looking.

First producer is the platform-token refresher ("your Douyin authorization
expired, re-authorize"). `user_id` null means everyone in the workspace.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: e.g. platform_auth_expired, publish_done
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    read_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_notifications_workspace_created", "workspace_id", "created_at"),
    )
