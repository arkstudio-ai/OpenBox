"""The user's daily briefing from their personal assistant (V2 P5).

One row per (user, workspace): whether a briefing is sent every day, at what
local time, and the local date of the last one sent. See assistant/briefing.py.
"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AssistantBriefing(Base):
    __tablename__ = "assistant_briefings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    # "HH:MM" in ``time_zone``.
    local_time: Mapped[str] = mapped_column(String(5), nullable=False)
    time_zone: Mapped[str] = mapped_column(String(64), nullable=False)
    # Local date (YYYY-MM-DD) of the last briefing requested.
    last_sent_on: Mapped[str | None] = mapped_column(String(10), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", name="uq_assistant_briefings_user_workspace"),
        CheckConstraint("revision >= 1", name="ck_assistant_briefings_revision"),
        CheckConstraint("length(local_time) = 5", name="ck_assistant_briefings_local_time"),
    )
