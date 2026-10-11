"""Partner tariffs (per-workspace ladder pricing) and monthly ladder rebates.

Revision ID: pf39b0c1d2e3
Revises: pe28a9b0c1d2
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pf39b0c1d2e3"
down_revision = "pe28a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "partner_tariffs",
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), primary_key=True),
        sa.Column("tariff", JSONType(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "partner_rebates",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("charged", sa.Numeric(28, 12), nullable=False),
        sa.Column("ladder_credits", sa.Numeric(28, 12), nullable=False),
        sa.Column("rebate", sa.Numeric(28, 12), nullable=False),
        sa.Column("detail", JSONType(), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("ledger_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("workspace_id", "month", name="uq_partner_rebate_month"),
    )


def downgrade():
    op.drop_table("partner_rebates")
    op.drop_table("partner_tariffs")
