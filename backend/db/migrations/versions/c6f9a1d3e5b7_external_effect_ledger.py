"""Add the durable external-effect ledger.

Revision ID: c6f9a1d3e5b7
Revises: b5e8f1a4c7d0
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c6f9a1d3e5b7"
down_revision: Union[str, None] = "b5e8f1a4c7d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "external_effects",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("run_generation", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("safe_context", _json_type(), nullable=False),
        sa.Column("state", sa.String(length=24), server_default="prepared", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("reconcile_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claim_kind", sa.String(length=16), nullable=True),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_owner", sa.String(length=160), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_handle", sa.String(length=256), nullable=True),
        sa.Column("provider_receipt", _json_type(), nullable=True),
        sa.Column("projection", _json_type(), nullable=True),
        sa.Column("last_error", _json_type(), nullable=True),
        sa.Column("reconcile_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitting_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('prepared', 'submitting', 'accepted', 'succeeded', "
            "'failed', 'outcome_unknown', 'manual_review')",
            name="ck_external_effect_state",
        ),
        sa.CheckConstraint(
            "claim_kind IS NULL OR claim_kind IN ('dispatch', 'reconcile')",
            name="ck_external_effect_claim_kind",
        ),
        sa.CheckConstraint(
            "length(request_hash) = 64",
            name="ck_external_effect_request_hash",
        ),
        sa.CheckConstraint(
            "run_generation > 0 AND claim_generation >= 0 "
            "AND attempt_count >= 0 AND reconcile_count >= 0",
            name="ck_external_effect_generations",
        ),
        sa.CheckConstraint(
            "(claim_token IS NULL AND claim_owner IS NULL AND claim_kind IS NULL "
            "AND claim_expires_at IS NULL) OR "
            "(claim_token IS NOT NULL AND claim_owner IS NOT NULL "
            "AND claim_kind IS NOT NULL AND claim_expires_at IS NOT NULL)",
            name="ck_external_effect_claim_shape",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "adapter",
            "idempotency_key",
            name="uq_external_effect_idempotency",
        ),
    )
    op.create_index(
        "ix_external_effect_recovery",
        "external_effects",
        ["state", "reconcile_after", "claim_expires_at", "updated_at"],
    )
    op.create_index(
        "ix_external_effect_run",
        "external_effects",
        ["session_id", "run_id", "run_generation"],
    )
    op.create_index(
        "ix_external_effect_tenant_created",
        "external_effects",
        ["tenant_id", "created_at"],
    )

    op.create_table(
        "external_effect_evidence",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("effect_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("claim_generation", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(length=32), nullable=False),
        sa.Column("evidence", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence > 0 AND claim_generation >= 0",
            name="ck_external_effect_evidence_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["effect_id"], ["external_effects.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "effect_id", "sequence", name="uq_external_effect_evidence_sequence"
        ),
    )
    op.create_index(
        "ix_external_effect_evidence_effect",
        "external_effect_evidence",
        ["effect_id", "sequence"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    effect_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM external_effects")
    ).scalar_one()
    evidence_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM external_effect_evidence")
    ).scalar_one()
    if effect_count or evidence_count:
        raise RuntimeError(
            "external-effect ledger downgrade refused: durable effects still exist"
        )
    op.drop_index(
        "ix_external_effect_evidence_effect",
        table_name="external_effect_evidence",
    )
    op.drop_table("external_effect_evidence")
    op.drop_index(
        "ix_external_effect_tenant_created", table_name="external_effects"
    )
    op.drop_index("ix_external_effect_run", table_name="external_effects")
    op.drop_index("ix_external_effect_recovery", table_name="external_effects")
    op.drop_table("external_effects")
