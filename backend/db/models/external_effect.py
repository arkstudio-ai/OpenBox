"""Durable ledger for external effects that cross a process boundary.

The mutable ``ExternalEffect`` row is the fenced projection used by workers.
``ExternalEffectEvidence`` is append-only audit evidence: no provider secret,
raw request body, signed URL, or authorization material belongs in either
table.
"""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class ExternalEffect(Base):
    __tablename__ = "external_effects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # OpenBox currently uses User.id as its tenant boundary.  Naming the field
    # tenant_id makes that boundary explicit in an otherwise generic ledger.
    tenant_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id"), nullable=False
    )
    project_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("projects.id"), nullable=True
    )
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id"), nullable=False
    )
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_generation: Mapped[int] = mapped_column(Integer, nullable=False)

    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    safe_context: Mapped[dict] = mapped_column(JSONType, default=dict)

    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'prepared'")
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    reconcile_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    # This generation fences effect workers.  It is deliberately independent
    # of ``run_generation``: a recovery worker may reconcile an old provider
    # receipt, but it can never dispatch that old Agent generation again.
    claim_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    claim_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    provider_handle: Mapped[str | None] = mapped_column(String(256), nullable=True)
    provider_receipt: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    projection: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    last_error: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    reconcile_after: Mapped[datetime | None] = mapped_column(nullable=True)

    prepared_at: Mapped[datetime] = mapped_column(nullable=False)
    submitting_at: Mapped[datetime | None] = mapped_column(nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "adapter",
            "idempotency_key",
            name="uq_external_effect_idempotency",
        ),
        CheckConstraint(
            "state IN ('prepared', 'submitting', 'accepted', 'succeeded', "
            "'failed', 'outcome_unknown', 'manual_review')",
            name="ck_external_effect_state",
        ),
        CheckConstraint(
            "claim_kind IS NULL OR claim_kind IN ('dispatch', 'reconcile')",
            name="ck_external_effect_claim_kind",
        ),
        CheckConstraint(
            "length(request_hash) = 64",
            name="ck_external_effect_request_hash",
        ),
        CheckConstraint(
            "run_generation > 0 AND claim_generation >= 0 "
            "AND attempt_count >= 0 AND reconcile_count >= 0",
            name="ck_external_effect_generations",
        ),
        CheckConstraint(
            "(claim_token IS NULL AND claim_owner IS NULL AND claim_kind IS NULL "
            "AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claim_owner IS NOT NULL "
            "AND claim_kind IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_external_effect_claim_shape",
        ),
        Index(
            "ix_external_effect_recovery",
            "state",
            "reconcile_after",
            "claim_expires_at",
            "updated_at",
        ),
        Index(
            "ix_external_effect_run",
            "session_id",
            "run_id",
            "run_generation",
        ),
        Index(
            "ix_external_effect_tenant_created",
            "tenant_id",
            "created_at",
        ),
    )


class ExternalEffectEvidence(Base):
    """One immutable, bounded and public-safe phase observation."""

    __tablename__ = "external_effect_evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    effect_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("external_effects.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    claim_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "effect_id", "sequence", name="uq_external_effect_evidence_sequence"
        ),
        CheckConstraint(
            "sequence > 0 AND claim_generation >= 0",
            name="ck_external_effect_evidence_sequence",
        ),
        Index(
            "ix_external_effect_evidence_effect",
            "effect_id",
            "sequence",
        ),
    )
