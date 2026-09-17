"""Durable parent/child delivery state for the Task tool.

The child Session transcript is the source of the subagent answer, while this
row is the delivery contract that survives the parent coroutine or worker.  A
parent ToolPart and child Session may participate in at most one handoff.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class TaskHandoff(Base):
    __tablename__ = "task_handoffs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_part_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("parts.id", ondelete="CASCADE"),
        nullable=False,
    )
    parent_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_generation: Mapped[int] = mapped_column(Integer, nullable=False)

    child_session_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_trigger_message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    child_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    child_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)

    state: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        server_default="accepted",
    )
    task_title: Mapped[str] = mapped_column(String(255), nullable=False)
    subagent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Only the bounded, directly replayable ToolPart projection is stored.
    # The child transcript remains the source for the complete answer.
    result_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    rejoined_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint(
            "state IN ('accepted', 'completed', 'rejoined', 'abandoned')",
            name="ck_task_handoffs_state",
        ),
        CheckConstraint(
            "parent_generation > 0",
            name="ck_task_handoffs_parent_generation",
        ),
        CheckConstraint(
            "child_generation IS NULL OR child_generation > 0",
            name="ck_task_handoffs_child_generation",
        ),
        UniqueConstraint("parent_part_id", name="uq_task_handoffs_parent_part"),
        UniqueConstraint("child_session_id", name="uq_task_handoffs_child_session"),
        Index("ix_task_handoffs_user_state", "user_id", "state"),
        Index("ix_task_handoffs_parent_state", "parent_session_id", "state"),
        Index("ix_task_handoffs_child_state", "child_session_id", "state"),
    )
