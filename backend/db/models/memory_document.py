"""User-uploaded knowledge documents, durable ingestion and immutable revisions."""
from datetime import datetime

from sqlalchemy import Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType
from db.models.wiki_platform import WikiScoped


class MemoryDocument(WikiScoped, Base):
    __tablename__ = "memory_documents"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    source_ids: Mapped[list] = mapped_column(JSONType, default=list)
    page_ids: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("domain", "file_hash", name="uq_memory_document_file"),
        Index("ix_memory_document_claim", "status", "available_at"),
        Index("ix_memory_document_scope", "user_id", "workspace_id", "project_id"),
    )


class MemoryDocumentRevision(Base):
    __tablename__ = "memory_document_revisions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sections: Mapped[list] = mapped_column(JSONType, default=list)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONType, default=dict)
    origin: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (UniqueConstraint("document_id", "revision", name="uq_memory_document_revision"),)
