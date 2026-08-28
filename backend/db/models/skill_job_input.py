"""Durable inputs that wake a job or feed its next invocation.

User answers, provider callbacks, agent results and operator resumes are
admitted here idempotently; they never mutate the job state machine directly.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillJobInput(Base):
    __tablename__ = "skill_job_inputs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("skill_jobs.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: skill_runtime.types.InputKind value.
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Origin correlation (callback id, chat part id, waiting_user event id).
    source_event_id: Mapped[str] = mapped_column(String(160), nullable=False, server_default=text("''"))
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "idempotency_key", name="uq_skill_job_inputs_idempotency"),
        Index("ix_skill_job_inputs_job", "job_id", "created_at"),
    )
