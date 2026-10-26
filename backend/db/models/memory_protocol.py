"""Memory change clocks, committed write receipts and private experience versions.

These rows coordinate existing authorities; they do not contain another copy of
chat transcripts or replace UserMemory, documents, tasks or UserSkill.
"""
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class MemoryChangeClock(Base):
    __tablename__ = "memory_change_clocks"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class MemoryWriteReceipt(Base):
    __tablename__ = "memory_write_receipts"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    result: Mapped[dict] = mapped_column(JSONType, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    occurred_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class MemoryExperience(Base):
    __tablename__ = "memory_experiences"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operation_id: Mapped[str] = mapped_column(String(160), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="candidate")
    content: Mapped[dict] = mapped_column(JSONType, nullable=False)
    source_refs: Mapped[list] = mapped_column(JSONType, nullable=False)
    validation: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    skill_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    skill_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("user_id", "operation_id", name="uq_memory_experience_operation"),
        CheckConstraint("state IN ('candidate', 'validated', 'enabled', 'retired')", name="ck_memory_experience_state"),
        Index("ix_memory_experience_scope", "user_id", "workspace_id", "state"),
    )
