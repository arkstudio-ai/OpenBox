"""Four durable definition tables and the two-table team event journal."""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class DefinitionColumns:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    active_name: Mapped[int | None] = mapped_column(Integer, nullable=True, default=1)
    current_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    draft_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class AgentDefinition(DefinitionColumns, Base):
    __tablename__ = "agent_definitions"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "workspace_id", "name", "active_name", name="uq_agent_definitions_active_name"),
        Index("ix_agent_definitions_owner", "owner_user_id", "workspace_id", "status"),
        CheckConstraint("status IN ('draft', 'active', 'archived')", name="ck_agent_definition_status"),
    )


class TeamDefinition(DefinitionColumns, Base):
    __tablename__ = "team_definitions"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "workspace_id", "name", "active_name", name="uq_team_definitions_active_name"),
        Index("ix_team_definitions_owner", "owner_user_id", "workspace_id", "status"),
        CheckConstraint("status IN ('draft', 'active', 'archived')", name="ck_team_definition_status"),
    )


class VersionColumns:
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec_json: Mapped[dict] = mapped_column(JSONType, nullable=False)
    capability_summary: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    content_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class AgentDefinitionVersion(VersionColumns, Base):
    __tablename__ = "agent_definition_versions"
    definition_id: Mapped[str] = mapped_column(String(64), ForeignKey("agent_definitions.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("definition_id", "version", name="uq_agent_definition_version"),)


class TeamDefinitionVersion(VersionColumns, Base):
    __tablename__ = "team_definition_versions"
    definition_id: Mapped[str] = mapped_column(String(64), ForeignKey("team_definitions.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("definition_id", "version", name="uq_team_definition_version"),)


class TeamRun(Base):
    __tablename__ = "team_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    root_session_id: Mapped[str] = mapped_column(String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    owner_user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    template_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    template_version_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    policy_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    grant_snapshot: Mapped[dict] = mapped_column(JSONType, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    pause_reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    session_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    project_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    state_cache: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    cache_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    start_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    end_snapshot: Mapped[str | None] = mapped_column(String(128), nullable=True)
    final_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_artifact_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)
    __table_args__ = (
        UniqueConstraint("root_session_id", "session_active", name="uq_team_run_active_session"),
        UniqueConstraint("project_id", "project_active", name="uq_team_run_active_project"),
        CheckConstraint("state IN ('provisioning', 'running', 'waiting', 'pausing', 'paused', 'canceling', 'canceled', 'completing', 'completed', 'failed')", name="ck_team_run_state"),
        CheckConstraint("(state IN ('completed', 'canceled', 'failed') AND session_active IS NULL AND project_active IS NULL) OR (state NOT IN ('completed', 'canceled', 'failed') AND session_active IS NOT NULL AND project_active IS NOT NULL AND session_active = 1 AND project_active = 1)", name="ck_team_run_active"),
        Index("ix_team_runs_owner_created", "owner_user_id", "workspace_id", "created_at", "id"),
        Index("ix_team_runs_recovery", "session_active", "updated_at"),
    )


class TeamEvent(Base):
    __tablename__ = "team_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_run_id: Mapped[str] = mapped_column(String(64), ForeignKey("team_runs.id", ondelete="CASCADE"), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_member_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_type: Mapped[str] = mapped_column(String(24), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        UniqueConstraint("team_run_id", "sequence", name="uq_team_event_sequence"),
        UniqueConstraint("team_run_id", "event_key", name="uq_team_event_key"),
        CheckConstraint("sequence > 0", name="ck_team_event_sequence"),
        Index("ix_team_events_member_binding", "entity_id", "kind"),
        Index("ix_team_events_entity", "team_run_id", "entity_type", "entity_id", "sequence"),
    )
