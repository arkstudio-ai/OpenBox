"""Append-only audit records for destructive Session Surface projections.

The live ``messages``/``parts`` tables remain the model-visible Surface.  A
regenerate or dismiss operation may remove rows from that projection, but it
must first preserve the complete public branch here in the same transaction.
"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SessionSurfaceEvent(Base):
    __tablename__ = "session_surface_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    anchor_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    replacement_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    replacement_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Both JSON values are deliberately required.  ``hidden_message_ids`` is
    # convenient for exact provenance queries, while ``public_snapshot`` is a
    # self-contained recovery image of those messages and their public parts.
    hidden_message_ids: Mapped[list] = mapped_column(JSONType, nullable=False)
    public_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_session_surface_events_session_sequence",
        ),
        CheckConstraint("sequence > 0", name="ck_session_surface_events_positive_sequence"),
        CheckConstraint(
            "kind IN ('regenerate', 'dismiss')",
            name="ck_session_surface_events_kind",
        ),
        Index("ix_session_surface_events_session_created", "session_id", "created_at"),
        Index("ix_session_surface_events_user_created", "user_id", "created_at"),
    )
