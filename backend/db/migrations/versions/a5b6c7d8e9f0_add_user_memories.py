"""Add user_memories — user-scoped creator memory (ported from bossip).

Revision ID: a5b6c7d8e9f0
Revises: f5c6d7e8f9a0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, None] = "f5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "user_memories",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("value", json_type, nullable=True),
        sa.Column("evidence", json_type, nullable=True),
        sa.Column("confidence", sa.Integer(), server_default=sa.text("50"), nullable=False),
        sa.Column("ttl", sa.DateTime(timezone=True), nullable=True),
        sa.Column("owner", sa.String(length=24), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'CANDIDATE'"), nullable=False),
        sa.Column("promoted_from", sa.String(length=64), nullable=True),
        sa.Column("hit_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_memories_user_scope_status", "user_memories", ["user_id", "scope", "status"]
    )
    op.create_index(
        "ix_user_memories_user_type_status", "user_memories", ["user_id", "type", "status"]
    )
    op.create_index("ix_user_memories_ttl", "user_memories", ["ttl"])


def downgrade() -> None:
    op.drop_index("ix_user_memories_ttl", table_name="user_memories")
    op.drop_index("ix_user_memories_user_type_status", table_name="user_memories")
    op.drop_index("ix_user_memories_user_scope_status", table_name="user_memories")
    op.drop_table("user_memories")
