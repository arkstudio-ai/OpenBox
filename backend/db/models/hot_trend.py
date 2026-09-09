"""Shared, cross-customer cache of hot-list snapshots and resolved media links.

`hot_trends` collects a board once per (source, board, window, category, day)
and every workspace reads the same row: hot lists are public data, and the
plan's rate budget only works if two cron jobs on the same category do not
each drive a desktop browser. Only metadata and links are stored — never the
video files (docs/AUTO_MARKETING_AUTOPILOT_PLAN.md §4).
"""
from datetime import datetime

from sqlalchemy import Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class HotTrendSnapshot(Base):
    __tablename__ = "hot_trend_snapshots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: source:board:<window>h:<category|*>:<YYYY-MM-DD Asia/Shanghai>
    cache_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    board: Mapped[str] = mapped_column(String(32), nullable=False)
    window_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    day: Mapped[str] = mapped_column(String(10), nullable=False)
    #: ok | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items: Mapped[list] = mapped_column(JSONType, default=list)
    #: taxonomy, page title, request body, credits — anything not an item
    extra: Mapped[dict] = mapped_column(JSONType, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_by_workspace: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_by_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_hot_trend_snapshots_source_day", "source", "day", "fetched_at"),
    )


class HotMediaLink(Base):
    """Direct media URL for one hot video, so `video_analyze` can sample it.

    CDN links expire; `expires_at` is our own conservative TTL, not the CDN's.
    """

    __tablename__ = "hot_media_links"

    video_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    media_url: Mapped[str] = mapped_column(Text, nullable=False)
    page_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by_user: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
