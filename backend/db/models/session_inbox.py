"""Durable continuations that wake an agent session (NeedsAgent, §8.3).

Consumption is idempotent per (source_job_id, source_event_seq); a deleted or
archived session expires its pending items instead of leaving jobs dangling.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SessionInbox(Base):
    __tablename__ = "session_inbox"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'job_needs_agent'"))
    source_job_id: Mapped[str] = mapped_column(String(64), ForeignKey("skill_jobs.id"), nullable=False)
    source_event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    #: pending / processing / consumed / expired
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'pending'"))
    #: Fencing token for one dispatcher claim. A recovered stale process may
    #: still be alive, but cannot heartbeat or settle a newer claim.
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("source_job_id", "source_event_seq", name="uq_session_inbox_source"),
        Index("ix_session_inbox_session", "session_id", "status", "created_at"),
        Index("ix_session_inbox_claim_recovery", "status", "consumed_at"),
    )
