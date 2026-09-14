"""Durable human-input checkpoints and fenced, restart-aware execution slots."""
from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SessionExecution(Base):
    __tablename__ = "session_executions"

    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    run_origin: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The task identity survives a suspended question and a worker restart.
    # A new execution lease changes run_id, while the original turn/root stay.
    trace_context: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    run_progress: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    # A transactional outbox: an accepted answer cannot be separated from its
    # scheduling intent by a process exit. Multiple answers coalesce per turn.
    resume_pending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    resume_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index("ix_session_executions_resume", "resume_pending", "lease_until"),
        Index("ix_session_executions_lease", "lease_until"),
    )


class QuestionCheckpoint(Base):
    __tablename__ = "question_checkpoints"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    part_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    questions: Mapped[list] = mapped_column(JSONType, nullable=False)
    answers: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    draft: Mapped[list] = mapped_column(JSONType, nullable=False)
    draft_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    continuation: Mapped[dict] = mapped_column(JSONType, nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("part_id", name="uq_question_checkpoint_part"),
        Index("ix_question_checkpoints_pending", "user_id", "status", "created_at"),
        Index("ix_question_checkpoints_turn", "session_id", "generation", "status"),
        Index("ix_question_checkpoints_expiry", "status", "expires_at"),
    )
