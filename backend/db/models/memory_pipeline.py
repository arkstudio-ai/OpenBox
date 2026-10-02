"""Durable logical-turn receipts and fenced background extraction.

Only identity, revision and hash boundaries are copied here. Source bodies
remain in the canonical transcript and the memory authority, so an expired
lease cannot retain a separate copy of forgotten input.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class MemoryPipelineEnrollment(Base):
    """Earliest authorized online evidence boundary; never implicit backfill."""
    __tablename__ = "memory_pipeline_enrollments"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    eligible_since: Mapped[datetime] = mapped_column(nullable=False)


class MemoryTurnCompletion(Base):
    __tablename__ = "memory_turn_completions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    branch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    result_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    start_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    end_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_boundaries: Mapped[list] = mapped_column(JSONType, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    acl_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("session_id", "branch_id", "logical_turn_id", "result_message_id", name="uq_memory_completion_boundary"),
        UniqueConstraint("session_id", "branch_id", "ordinal", name="uq_memory_completion_ordinal"),
        CheckConstraint("ordinal > 0 AND start_sequence > 0 AND end_sequence >= start_sequence", name="ck_memory_completion_range"),
        Index("ix_memory_completion_recover", "pipeline_version", "created_at", "id"),
        Index("ix_memory_completion_user", "user_id", "workspace_id", "project_id"),
    )


class MemoryExtractionJob(Base):
    __tablename__ = "memory_extraction_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    completion_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_turn_completions.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    branch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    logical_turn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_memory_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    usage: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("completion_id", "pipeline_version", name="uq_memory_extraction_pipeline"),
        CheckConstraint("state IN ('PENDING', 'RUNNING', 'RETRY', 'SUCCEEDED', 'DEAD', 'CANCELLED')", name="ck_memory_job_state"),
        CheckConstraint("attempts >= 0 AND lease_generation >= 0", name="ck_memory_job_attempts"),
        Index("ix_memory_job_claim", "state", "next_attempt_at", "lease_until", "created_at"),
        Index("ix_memory_job_cursor", "session_id", "branch_id", "pipeline_version", "ordinal"),
        Index("ix_memory_job_user", "user_id", "workspace_id", "project_id"),
    )


class MemoryExtractionCursor(Base):
    __tablename__ = "memory_extraction_cursors"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    branch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pipeline_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    completed_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    completed_sequence: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("completed_ordinal >= 0 AND completed_sequence >= 0", name="ck_memory_cursor_nonnegative"),
    )
