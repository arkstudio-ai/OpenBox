"""Durable orchestration records for provider and sandbox video jobs."""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class VideoJob(Base):
    __tablename__ = "video_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # segment | stt | render
    production_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("video_productions.id"), nullable=True
    )
    segment_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("video_segments.id"), nullable=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    # Cross-user content key: hash of everything that shapes the generated
    # output (prompt, model, params, input digests) and nothing that doesn't
    # (user, session, time). NULL = dedupe not applicable for this job.
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    sandbox_job_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    result_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    output_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("file_assets.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    attached_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("user_id", "kind", "idempotency_key", name="uq_video_jobs_idempotency"),
        Index("ix_video_jobs_user_created", "user_id", "created_at"),
        Index("ix_video_jobs_status_updated", "status", "updated_at"),
        Index("ix_video_jobs_provider_task", "provider_task_id"),
        Index("ix_video_jobs_prompt_hash", "prompt_hash", "status", "completed_at"),
        Index("ix_video_jobs_production", "production_id", "created_at"),
        Index("ix_video_jobs_segment", "segment_id", "created_at"),
    )
