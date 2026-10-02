"""Scoped Wiki concepts, incremental extraction and durable organization runs.

The SQL rows carry authority and versions. Model output and index hits cannot
create publication authority; compiled pages still use the existing review CAS.
"""
from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class WikiScoped:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, server_default=text("'PERSONAL'"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class WikiConcept(WikiScoped, Base):
    __tablename__ = "wiki_concepts"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_key: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(String(80), nullable=False)
    page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    merged_into: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Explicit user edits survive future extraction; evidence bindings do not.
    user_edited: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    extra_metadata: Mapped[dict] = mapped_column(JSONType, default=dict)
    __table_args__ = (
        UniqueConstraint("domain", "canonical_key", name="uq_wiki_concept_canonical"),
        Index("ix_wiki_concept_scope", "user_id", "workspace_id", "project_id", "status"),
    )


class WikiConceptBinding(Base):
    __tablename__ = "wiki_concept_bindings"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    concept_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    extraction_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    __table_args__ = (
        UniqueConstraint("concept_id", "memory_id", name="uq_wiki_concept_binding"),
        Index("ix_wiki_binding_memory", "memory_id", "status"),
    )


class WikiConceptExtraction(WikiScoped, Base):
    __tablename__ = "wiki_concept_extractions"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    cache_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    memory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    memory_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    source_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    memory_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    acl_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    concepts: Mapped[list] = mapped_column(JSONType, default=list)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    usage: Mapped[dict] = mapped_column(JSONType, default=dict)
    __table_args__ = (Index("ix_wiki_extraction_memory", "memory_id", "status"),)


class WikiOrganizationRun(WikiScoped, Base):
    __tablename__ = "wiki_organization_runs"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    spec: Mapped[dict] = mapped_column(JSONType, default=dict)
    result: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    cursor: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(nullable=True)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    automation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        UniqueConstraint("user_id", "workspace_id", "request_id", name="uq_wiki_organization_request"),
        Index("ix_wiki_organization_claim", "status", "available_at"),
        Index("ix_wiki_organization_domain", "domain", "created_at"),
    )


class WikiMaintenancePolicy(WikiScoped, Base):
    __tablename__ = "wiki_maintenance_policies"
    domain: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    automatic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    compile_pages: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    call_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(nullable=False)
    next_check_at: Mapped[datetime] = mapped_column(nullable=False)
    last_input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(80), nullable=True)


class WikiRelation(WikiScoped, Base):
    __tablename__ = "wiki_relations"
    domain: Mapped[str] = mapped_column(String(64), nullable=False)
    identity: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    relation_type: Mapped[str] = mapped_column(String(80), nullable=False)
    from_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    from_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    to_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attributes: Mapped[dict] = mapped_column(JSONType, default=dict)
    evidence: Mapped[list] = mapped_column(JSONType, default=list)
    memory_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    source_manifest: Mapped[list] = mapped_column(JSONType, default=list)
    acl_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    profile_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    __table_args__ = (Index("ix_wiki_relation_scope", "user_id", "workspace_id", "project_id", "status"),)
