"""Immutable index-generation contracts and explicit diagnostic replay grants."""
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class MemoryIndexGeneration(Base):
    __tablename__ = "memory_index_generations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    reconciled_at: Mapped[datetime | None] = mapped_column(nullable=True)


class MemoryReplayPreview(Base):
    __tablename__ = "memory_replay_previews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    grant: Mapped[dict] = mapped_column(JSONType, nullable=False)
    consumed_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
