"""Canonical append-only Agent history.

Events are the model-context authority. Relational transcript tables are
rebuildable UI/API read models; private replay sidecars remain inside event
payloads and are stripped by the public projector.
"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class AgentEvent(Base):
    __tablename__ = "agent_events"

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
    # SHA-256 of the event occurrence, or of a caller-supplied stable operation
    # identity for explicitly idempotent writes. History is never UPDATEd.
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    step_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    part_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_call_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_agent_events_session_sequence",
        ),
        UniqueConstraint(
            "session_id",
            "event_key",
            name="uq_agent_events_session_event_key",
        ),
        CheckConstraint("sequence > 0", name="ck_agent_events_positive_sequence"),
        CheckConstraint(
            "(run_id IS NULL AND generation IS NULL) OR "
            "(run_id IS NOT NULL AND generation IS NOT NULL AND generation > 0)",
            name="ck_agent_events_run_generation_pair",
        ),
        Index("ix_agent_events_session_run", "session_id", "run_id", "generation"),
        Index("ix_agent_events_session_message", "session_id", "message_id"),
        Index("ix_agent_events_session_part", "session_id", "part_id"),
        Index("ix_agent_events_user_created", "user_id", "created_at"),
    )
