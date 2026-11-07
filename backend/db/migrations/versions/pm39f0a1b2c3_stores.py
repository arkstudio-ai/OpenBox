"""stores: one 门店 per workspace, the Light home's shop name and default topic.

The table of PR #60 (`e2c4a6b8d0f1`, not merged) plus `headline`, carried on
the Light line so the home can read it (docs/LIGHT_MODE_IMPL_PLAN.md §6).

Revision ID: pm39f0a1b2c3
Revises: pl28e9f0a1b2
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pm39f0a1b2c3"
down_revision = "pl28e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "stores",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("headline", sa.String(40), nullable=True),
        sa.Column("category", sa.String(16), nullable=False, server_default="food"),
        sa.Column("main_platforms", JSONType(), nullable=False),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("city", sa.String(64), nullable=True),
        sa.Column("platform_bindings", JSONType(), nullable=False),
        sa.Column("data_sources", JSONType(), nullable=False),
        sa.Column("persona_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("persona_session_id", sa.String(64), nullable=True),
        sa.Column("persona_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", name="uq_stores_workspace"),
    )
    op.create_index("ix_stores_user", "stores", ["user_id"])


def downgrade():
    op.drop_index("ix_stores_user", table_name="stores")
    op.drop_table("stores")
