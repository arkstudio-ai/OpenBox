"""Per-claim execution record: audit and debugging trail, never user-facing state."""
from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillJobAttempt(Base):
    __tablename__ = "skill_job_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("skill_jobs.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)

    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("''"))
    runtime_kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("''"))
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False)

    started_at: Mapped[datetime] = mapped_column(nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: Invocation outcome: succeeded / wait_external / wait_user / needs_agent /
    #: retry / failed / cancelled / lost (lease expired without settling).
    outcome: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("''"))
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    provider_correlation_id: Mapped[str | None] = mapped_column(String(160), nullable=True)

    handler_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    image_digest: Mapped[str] = mapped_column(String(128), nullable=False, server_default=text("''"))

    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_skill_job_attempts_number"),
        Index("ix_skill_job_attempts_job", "job_id", "started_at"),
    )
