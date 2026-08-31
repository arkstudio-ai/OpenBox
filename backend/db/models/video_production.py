"""Persistent control-plane records for one spoken-video production."""
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class VideoProduction(Base):
    __tablename__ = "video_productions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    brief: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'standard'"))
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'init'"))
    target_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("60"))
    ratio: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'9:16'"))
    resolution: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'720p'"))
    quality_policy: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'required'")
    )
    subtitles: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    channel_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default=text("''"))
    visual_anchor: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    character_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("file_assets.id"), nullable=True
    )
    script_text: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    render_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("file_assets.id"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        Index("ix_video_productions_user_created", "user_id", "created_at"),
        Index("ix_video_productions_session_updated", "session_id", "updated_at"),
    )


class VideoSegment(Base):
    __tablename__ = "video_segments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    production_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("video_productions.id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    role: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'body'"))
    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Per-segment model override; NULL = the configured default model.
    model: Mapped[str | None] = mapped_column(String(160), nullable=True)
    input_asset_ids: Mapped[list] = mapped_column(JSONType, default=list)
    lint_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'planned'"))
    generation_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_asset_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("file_assets.id"), nullable=True
    )
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    stt_similarity: Mapped[float | None] = mapped_column(Float, nullable=True)
    stt_verdict: Mapped[str | None] = mapped_column(String(24), nullable=True)
    stt_notes: Mapped[list] = mapped_column(JSONType, default=list)
    stt_checked_at: Mapped[datetime | None] = mapped_column(nullable=True)
    review_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "production_id", "ordinal", "revision", name="uq_video_segments_revision"
        ),
        Index("ix_video_segments_production_active", "production_id", "is_active", "ordinal"),
        Index("ix_video_segments_output_asset", "output_asset_id"),
    )


class VideoApproval(Base):
    __tablename__ = "video_approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    production_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("video_productions.id"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    scope_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    evidence_part_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        Index(
            "ix_video_approvals_scope",
            "production_id",
            "kind",
            "scope_hash",
            "created_at",
        ),
        Index("ix_video_approvals_user_created", "user_id", "created_at"),
    )
