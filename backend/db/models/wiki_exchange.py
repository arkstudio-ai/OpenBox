"""Untrusted exchange bundles and separately reviewed document authority."""
from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType
from db.models.wiki_platform import WikiScoped


class WikiExchangeBundle(WikiScoped, Base):
    __tablename__ = "wiki_exchange_bundles"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest: Mapped[dict] = mapped_column(JSONType, default=dict)
    attachments: Mapped[dict] = mapped_column(JSONType, default=dict)
    warnings: Mapped[list] = mapped_column(JSONType, default=list)
    __table_args__ = (UniqueConstraint("domain", "content_hash", name="uq_wiki_exchange_bundle"),)


class WikiExchangeDocument(WikiScoped, Base):
    __tablename__ = "wiki_exchange_documents"
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    frontmatter: Mapped[dict] = mapped_column(JSONType, default=dict)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_ids: Mapped[list] = mapped_column(JSONType, default=list)
    __table_args__ = (
        UniqueConstraint("bundle_id", "path", name="uq_wiki_exchange_document"),
        Index("ix_wiki_exchange_document_scope", "user_id", "workspace_id", "project_id", "status"),
    )
