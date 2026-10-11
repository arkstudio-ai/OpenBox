"""Operator pricing rules over rates.json, and the cost side of every usage event.

Revision ID: pd17c8d9e0f1
Revises: pc06b7c8d9e0
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pd17c8d9e0f1"
down_revision = "pc06b7c8d9e0"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pricing_rules",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("key", sa.String(160), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("sale", JSONType(), nullable=True),
        sa.Column("cost", JSONType(), nullable=True),
        sa.Column("valid_from", sa.DateTime(), nullable=True),
        sa.Column("valid_until", sa.DateTime(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=False),
        sa.Column("audit_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("superseded_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("key", "revision", name="uq_pricing_rules_key_revision"),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_pricing_rules_status"),
    )
    op.create_index("ix_pricing_rules_key_current", "pricing_rules", ["key", "superseded_at"])
    with op.batch_alter_table("usage_events") as batch:
        batch.add_column(sa.Column("cost_credits", sa.Numeric(28, 12), nullable=True))
        batch.create_check_constraint("ck_usage_cost_nonnegative", "cost_credits IS NULL OR cost_credits >= 0")
    op.create_index("ix_usage_model_created", "usage_events", ["model_id", "created_at"])


def downgrade():
    op.drop_index("ix_usage_model_created", table_name="usage_events")
    with op.batch_alter_table("usage_events") as batch:
        batch.drop_constraint("ck_usage_cost_nonnegative", type_="check")
        batch.drop_column("cost_credits")
    op.drop_index("ix_pricing_rules_key_current", table_name="pricing_rules")
    op.drop_table("pricing_rules")
