"""Host-owned immutable candidates, published derivatives and fenced jobs."""
from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class MemoryWikiPage(Base):
    __tablename__ = "memory_wiki_pages"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    target_identity: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PERSONAL'"))
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    memory_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    paragraphs: Mapped[list] = mapped_column(JSONType, default=list)
    acl_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    invalidation_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (Index("ix_memory_wiki_pages_scope", "user_id", "workspace_id", "project_id", "status"),)


class MemoryWikiCandidate(Base):
    __tablename__ = "memory_wiki_candidates"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PERSONAL'"))
    target_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    target_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    candidate_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    draft: Mapped[dict] = mapped_column(JSONType, default=dict)
    source_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    memory_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    acl_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_target_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_target_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default=text("'PENDING'"))
    reason_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(nullable=True)
    approved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    __table_args__ = (Index("ix_memory_wiki_candidates_scope", "user_id", "workspace_id", "project_id", "status"),)


class MemoryWikiDependency(Base):
    __tablename__ = "memory_wiki_dependencies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    object_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    object_id: Mapped[str] = mapped_column(String(64), nullable=False)
    object_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("page_id", "page_revision", "object_kind", "object_id", name="uq_memory_wiki_dependency"),
        Index("ix_memory_wiki_dependencies_reverse", "object_kind", "object_id", "page_id"),
    )


class MemoryWikiJob(Base):
    __tablename__ = "memory_wiki_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_identity: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    spec: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    candidate_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", "request_id", name="uq_memory_wiki_job_request"),
        Index("ix_memory_wiki_jobs_claim", "status", "available_at"),
        Index("ix_memory_wiki_jobs_input", "target_identity", "input_hash"),
    )
