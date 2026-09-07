"""Timeline of what the backend did to a cloud desktop, and how it went.

One row per operation that matters when a desktop misbehaves: a browser
bring-up, a Chrome or relay launch, a runtime check or repair, a channel
verify, a slow or refused desktop lease, and every diagnostic snapshot. Rows
are best-effort and retention-limited; they are an operator's timeline, not a
source of truth for any product state.
"""
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class DesktopEvent(Base):
    __tablename__ = "desktop_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: ECD desktop id when known; the container key otherwise identifies the
    #: sandbox the way the backend addressed it (session id, shared desktop id).
    desktop_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    container_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: browser.ensure | browser.chrome_launch | browser.relay_start |
    #: browser.runtime_check | browser.runtime_repair | browser.diag |
    #: channel.verify | lease.acquire
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    #: ok | fail | timeout | info
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    #: The browser.diag event this one cites, when a snapshot was taken.
    diag_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("ix_desktop_events_ts", "ts"),
        Index("ix_desktop_events_desktop_ts", "desktop_id", "ts"),
        Index("ix_desktop_events_container_ts", "container_key", "ts"),
        Index("ix_desktop_events_session_ts", "session_id", "ts"),
        Index("ix_desktop_events_kind_ts", "kind", "ts"),
    )
