"""Per-job monotonic event log doubling as the transactional outbox.

Status updates and their event rows commit in one transaction; the outbox
publisher stamps published_at after the Redis/WS notification goes out, so a
lost notification is repaired by replay instead of trusting the frontend.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillJobEvent(Base):
    __tablename__ = "skill_job_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("skill_jobs.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Monotonic per job; clients subscribe with (job_id, after_seq).
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    #: skill_runtime.types.JobEventType value.
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("job_id", "seq", name="uq_skill_job_events_seq"),
        Index(
            "ix_skill_job_events_outbox",
            "created_at",
            postgresql_where=text("published_at IS NULL"),
            sqlite_where=text("published_at IS NULL"),
        ),
    )
