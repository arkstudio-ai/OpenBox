"""Durable continuable-subagent descriptor, activation inbox, and outbox."""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SubagentDescriptor(Base):
    __tablename__ = "subagent_descriptors"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    parent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    child_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    root_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    parent_descriptor_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("subagent_descriptors.id", ondelete="CASCADE"),
        nullable=True,
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False)
    subagent_type: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    # Versioned, monotonically narrowing intersection of every delegator's
    # tool and permission boundary. It is private kernel state, never a
    # Session API field, and is revalidated on each cold child run/follow-up.
    authority_snapshot: Mapped[dict] = mapped_column(
        JSONType, nullable=False, server_default="{}",
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="active",
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    active_activation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    interrupt_requested_generation: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    interrupt_applied_generation: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint("depth >= 1", name="ck_subagent_descriptors_depth"),
        CheckConstraint("generation >= 1", name="ck_subagent_descriptors_generation"),
        CheckConstraint(
            "lifecycle IN ('one_shot', 'continuable')",
            name="ck_subagent_descriptors_lifecycle",
        ),
        CheckConstraint(
            "state IN ('active', 'settled', 'interrupted', 'error')",
            name="ck_subagent_descriptors_state",
        ),
        UniqueConstraint(
            "child_session_id", name="uq_subagent_descriptors_child_session",
        ),
        Index(
            "ix_subagent_descriptors_parent_state",
            "parent_session_id", "user_id", "state",
        ),
        Index(
            "ix_subagent_descriptors_project_state",
            "project_id", "user_id", "state",
        ),
    )


class SubagentActivation(Base):
    __tablename__ = "subagent_activations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    descriptor_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("subagent_descriptors.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    parent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    parent_message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    parent_part_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("parts.id", ondelete="CASCADE"), nullable=False,
    )
    parent_run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    descriptor_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    child_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    child_trigger_message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    child_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    child_generation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="accepted",
    )
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    task_title: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('spawn', 'follow_up')", name="ck_subagent_activations_kind",
        ),
        CheckConstraint(
            "state IN ('accepted', 'claimed', 'bound', 'completed', 'abandoned')",
            name="ck_subagent_activations_state",
        ),
        CheckConstraint(
            "parent_generation > 0", name="ck_subagent_activations_parent_generation",
        ),
        CheckConstraint(
            "descriptor_generation > 0",
            name="ck_subagent_activations_descriptor_generation",
        ),
        CheckConstraint(
            "child_generation IS NULL OR child_generation > 0",
            name="ck_subagent_activations_child_generation",
        ),
        UniqueConstraint(
            "parent_part_id", name="uq_subagent_activations_parent_part",
        ),
        UniqueConstraint(
            "descriptor_id", "descriptor_generation",
            name="uq_subagent_activations_descriptor_generation",
        ),
        UniqueConstraint(
            "child_trigger_message_id", name="uq_subagent_activations_trigger",
        ),
        Index(
            "ix_subagent_activations_claim",
            "state", "claim_expires_at", "created_at",
        ),
        Index(
            "ix_subagent_activations_child_state",
            "child_session_id", "state",
        ),
        Index(
            "ix_subagent_activations_parent_state",
            "parent_session_id", "state",
        ),
    )


class SubagentOutbox(Base):
    __tablename__ = "subagent_outbox"

    activation_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("subagent_activations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    descriptor_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("subagent_descriptors.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False,
    )
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False,
    )
    parent_session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False,
    )
    parent_message_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False,
    )
    parent_part_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("parts.id", ondelete="CASCADE"), nullable=False,
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="waiting",
    )
    outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="waiting",
    )
    result_payload: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    ready_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        CheckConstraint(
            "state IN ('waiting', 'ready', 'delivered')",
            name="ck_subagent_outbox_state",
        ),
        CheckConstraint(
            "outcome IN ('waiting', 'succeeded', 'interrupted', "
            "'outcome_unknown', 'error')",
            name="ck_subagent_outbox_outcome",
        ),
        Index(
            "ix_subagent_outbox_parent_state", "parent_session_id", "state",
        ),
        Index("ix_subagent_outbox_descriptor", "descriptor_id", "state"),
    )
