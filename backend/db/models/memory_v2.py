"""Durable memory authority, immutable evidence, delivery and diagnostics.

There are intentionally no cascading session foreign keys: deleting a chat
must explicitly settle evidence, tombstones and indexes in the same policy.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class MemoryRevision(Base):
    __tablename__ = "memory_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), ForeignKey("user_memories.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    value: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(24), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_set_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    valid_from: Mapped[datetime | None] = mapped_column(nullable=True)
    valid_to: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("memory_id", "revision", name="uq_memory_revision"),
        UniqueConstraint("memory_id", "request_id", name="uq_memory_revision_request"),
        Index("ix_memory_revisions_scope", "user_id", "workspace_id", "memory_id"),
    )


class MemorySource(Base):
    __tablename__ = "memory_sources"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PERSONAL'"))
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    part_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)
    acl_epoch: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'ACTIVE'"))
    occurred_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (
        UniqueConstraint("id", "source_revision", name="uq_memory_source_version"),
        Index("ix_memory_sources_scope", "user_id", "workspace_id", "project_id", "status"),
        Index("ix_memory_sources_session", "session_id", "message_id"),
    )


class MemorySourceLink(Base):
    __tablename__ = "memory_source_links"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    memory_id: Mapped[str] = mapped_column(String(64), ForeignKey("user_memories.id"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_sources.id"), nullable=False)
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    relation: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'SUPPORTS'"))
    __table_args__ = (
        UniqueConstraint("memory_id", "revision", "source_id", "source_revision", name="uq_memory_source_link"),
        Index("ix_memory_source_links_source", "source_id", "memory_id"),
    )


class MemoryOutbox(Base):
    __tablename__ = "memory_outbox"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    operation: Mapped[str] = mapped_column(String(24), nullable=False)
    index_generation: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("'memory-v1'"))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PENDING'"))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (
        Index("ix_memory_outbox_claim", "status", "available_at", "priority"),
        Index("ix_memory_outbox_object", "object_kind", "object_id", "revision"),
    )


class MemoryIndexState(Base):
    __tablename__ = "memory_index_state"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index_generation: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    desired_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    indexed_revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    chunk_ids: Mapped[list] = mapped_column(JSONType, default=list)
    chunk_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'PENDING'"))
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    indexed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (UniqueConstraint("object_kind", "object_id", "index_generation", name="uq_memory_index_object"),)


class MemoryTombstone(Base):
    __tablename__ = "memory_tombstones"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fact_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'MEMORY'"))
    purge_status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'PENDING'"))
    deleted_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("object_kind", "object_id", name="uq_memory_tombstone_object"),
        Index("ix_memory_tombstones_suppression", "user_id", "workspace_id", "project_id", "content_hash"),
    )


class MemoryDebugRun(Base):
    __tablename__ = "memory_debug_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    turn_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False, server_default=text("'personal-v1'"))
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_snapshot: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_refs: Mapped[list] = mapped_column(JSONType, default=list)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    expires_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_memory_debug_runs_scope", "user_id", "workspace_id", "created_at"),)


class MemoryRecall(Base):
    """What recall brought for one of the user's messages: the memories found relevant to it (not
    the lasting background every turn carries), so the reply can show what it drew on
    (memory/recalls.py). Ids only, no text: every read authorizes again and shows only memories
    still in use, so a memory forgotten later disappears from here too."""
    __tablename__ = "memory_recalls"
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_ids: Mapped[list] = mapped_column(JSONType, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_memory_recalls_session", "user_id", "session_id", "created_at"),)


class MemoryDebugStep(Base):
    __tablename__ = "memory_debug_steps"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), ForeignKey("memory_debug_runs.id"), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict] = mapped_column(JSONType, default=dict)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_memory_debug_steps_run", "run_id", "created_at"),)
