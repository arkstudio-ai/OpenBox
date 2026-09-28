"""Merchant stores (门店档案), one per workspace.

Revision ID: e2c4a6b8d0f1
Revises: d0a2c4e6f8b1
Create Date: 2026-09-23
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e2c4a6b8d0f1"
down_revision: Union[str, None] = "d0a2c4e6f8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "stores",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default="food"),
        sa.Column("main_platforms", _json(), nullable=False),
        sa.Column("address", sa.String(255), nullable=True),
        sa.Column("city", sa.String(64), nullable=True),
        sa.Column("platform_bindings", _json(), nullable=False),
        sa.Column("data_sources", _json(), nullable=False),
        sa.Column("persona_status", sa.String(16), nullable=False, server_default="none"),
        sa.Column("persona_session_id", sa.String(64), nullable=True),
        sa.Column("persona_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", name="uq_stores_workspace"),
    )
    op.create_index("ix_stores_user", "stores", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_stores_user", table_name="stores")
    op.drop_table("stores")
