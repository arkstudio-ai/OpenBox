"""Append-only execution facts and rebuildable read models.

There are deliberately no message/part foreign keys: regenerating a chat does
not erase its recorded execution. Session deletion is an explicit lifecycle.
"""
from datetime import datetime
from sqlalchemy import BigInteger, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column
from db.base import Base, JSONType


class SessionTrajectory(Base):
    __tablename__ = "session_trajectories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    next_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    committed_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    projected_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recording_status: Mapped[str] = mapped_column(String(32), nullable=False, default="recording")
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "session_id", name="uq_trajectory_owner_session"),)


class TrajectoryEvent(Base):
    __tablename__ = "trajectory_events"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    call_id: Mapped[str | None] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict] = mapped_column(JSONType, nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("trajectory_id", "seq", name="uq_trajectory_event_seq"),
        Index("ix_trajectory_event_request", "trajectory_id", "request_id", "seq"),
        Index("ix_trajectory_event_call", "trajectory_id", "call_id", "seq"),
        Index("ix_trajectory_event_agent", "trajectory_id", "agent_id", "seq"),
    )


class TrajectoryPayload(Base):
    __tablename__ = "trajectory_payloads"
    payload_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), nullable=False, index=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    storage_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    content: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    encoding: Mapped[str] = mapped_column(String(16), nullable=False, default="binary")
    availability: Mapped[str] = mapped_column(String(24), nullable=False, default="available")
    first_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_asset_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (Index("ix_trajectory_payload_digest", "trajectory_id", "sha256"),
                     Index("ix_trajectory_payload_pending", "storage_status", "availability", "created_at"))


class TrajectoryRecord(Base):
    __tablename__ = "trajectory_records"
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    start_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    end_seq: Mapped[int | None] = mapped_column(BigInteger)
    applied_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    search_text: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (Index("ix_trajectory_record_order", "trajectory_id", "start_seq", "record_id"),
                      Index("ix_trajectory_record_message", "trajectory_id", "message_id"),
                      Index("ix_trajectory_record_filter", "trajectory_id", "kind", "status", "start_seq"))


class TrajectorySessionSummary(Base):
    __tablename__ = "trajectory_session_summaries"
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(nullable=False)
    running_status: Mapped[str] = mapped_column(String(32), nullable=False)
    recording_status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    applied_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statistics: Mapped[dict] = mapped_column(JSONType, nullable=False)
    __table_args__ = (Index("ix_trajectory_summary_activity", "last_activity_at", "session_id"),
                      Index("ix_trajectory_summary_owner", "user_id", "last_activity_at", "session_id"),
                      Index("ix_trajectory_summary_workspace", "workspace_id", "last_activity_at", "session_id"),
                      Index("ix_trajectory_summary_status", "running_status", "recording_status", "last_activity_at"))


class TrajectoryCheckpoint(Base):
    __tablename__ = "trajectory_checkpoints"
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), primary_key=True)
    through_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    projector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(JSONType, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryExport(Base):
    __tablename__ = "trajectory_exports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(String(64), ForeignKey("session_trajectories.id", ondelete="CASCADE"), nullable=False, index=True)
    viewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
