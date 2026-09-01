"""Add fenced Cron delivery outbox.

Revision ID: fd5f7a9c1e3b
Revises: a8c1e4f7b9d2
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "fd5f7a9c1e3b"
down_revision: Union[str, None] = "a8c1e4f7b9d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "cron_delivery_outbox",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_owner", sa.String(length=160), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id", "kind", name="uq_cron_delivery_run_kind"
        ),
    )
    op.create_index(
        "ix_cron_delivery_claim",
        "cron_delivery_outbox",
        ["state", "available_at", "claim_expires_at"],
    )
    op.create_index(
        "ix_cron_delivery_run", "cron_delivery_outbox", ["run_id"]
    )
    op.create_index(
        "ix_cron_delivery_session",
        "cron_delivery_outbox",
        ["session_id", "kind", "state", "available_at"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    pending = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM cron_delivery_outbox "
            "WHERE state <> 'delivered'"
        )
    ).scalar_one()
    if pending:
        raise RuntimeError(
            "cron delivery outbox downgrade refused: pending deliveries exist"
        )
    op.drop_index(
        "ix_cron_delivery_session", table_name="cron_delivery_outbox"
    )
    op.drop_index("ix_cron_delivery_run", table_name="cron_delivery_outbox")
    op.drop_index("ix_cron_delivery_claim", table_name="cron_delivery_outbox")
    op.drop_table("cron_delivery_outbox")
