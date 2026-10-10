"""Host-owned declarative profiles, typed records and versioned workflow history."""
from sqlalchemy import Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType
from db.models.wiki_platform import WikiScoped


class WikiProfile(WikiScoped, Base):
    __tablename__ = "wiki_profiles"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONType, default=dict)
    definition_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (UniqueConstraint("domain", "profile_key", name="uq_wiki_profile_key"),)


class WikiTypedRecord(WikiScoped, Base):
    __tablename__ = "wiki_typed_records"
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(80), nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    fields: Mapped[dict] = mapped_column(JSONType, default=dict)
    page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    page_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("profile_id", "entity_type", "slug", name="uq_wiki_typed_record"),
        Index("ix_wiki_typed_record_scope", "user_id", "workspace_id", "project_id"),
    )


class WikiArtifact(WikiScoped, Base):
    __tablename__ = "wiki_artifacts"
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    media_type: Mapped[str] = mapped_column(String(80), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    record_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class WikiWorkflowRun(WikiScoped, Base):
    __tablename__ = "wiki_workflow_runs"
    profile_id: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    definition: Mapped[dict] = mapped_column(JSONType, default=dict)
    workflow_id: Mapped[str] = mapped_column(String(80), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    stage_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    stages: Mapped[dict] = mapped_column(JSONType, default=dict)
    reason: Mapped[str | None] = mapped_column(String(800), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", "request_id", name="uq_wiki_workflow_request"),
        Index("ix_wiki_workflow_scope", "user_id", "workspace_id", "project_id", "status"),
    )


class WikiWorkflowEvent(Base):
    __tablename__ = "wiki_workflow_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    stage_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict)
    actor_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    __table_args__ = (
        UniqueConstraint("run_id", "request_id", name="uq_wiki_workflow_event_request"),
        UniqueConstraint("run_id", "version", name="uq_wiki_workflow_event_version"),
    )
