"""Durable ownership for one agent driver per conversation.

``sessions.status`` is a user-facing read model.  It cannot arbitrate work:
two API workers can both observe ``idle`` and start the same conversation.
This row is the authority for that decision.  ``generation`` is a fencing
token; a worker may only renew, change phase, or release the generation it
acquired.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class AgentDriverState(Base):
    __tablename__ = "agent_driver_states"

    session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    phase: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default="idle",
    )
    trigger_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    abort_requested_at: Mapped[datetime | None] = mapped_column(nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_agent_driver_user_phase", "user_id", "phase"),
        Index("ix_agent_driver_lease", "lease_expires_at"),
    )
