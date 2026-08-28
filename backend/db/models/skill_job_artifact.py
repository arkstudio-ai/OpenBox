"""Join table between jobs and file_assets — the job side never stores bytes."""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillJobArtifact(Base):
    __tablename__ = "skill_job_artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), ForeignKey("skill_jobs.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    asset_id: Mapped[str] = mapped_column(String(64), ForeignKey("file_assets.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'output'"))
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    meta: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("job_id", "asset_id", "role", name="uq_skill_job_artifacts_role"),
        Index("ix_skill_job_artifacts_job", "job_id", "ordinal"),
    )
