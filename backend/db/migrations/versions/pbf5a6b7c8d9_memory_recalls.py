"""Remember which memories recall brought for each of the user's messages.

The reply shows "参考了哪些记忆" from it (memory/recalls.py). One row per
message, memory ids only; reads authorize again and show only memories still
in use. Downgrading drops the table.

Revision ID: pbf5a6b7c8d9
Revises: pbe4f5a6b7c8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pbf5a6b7c8d9"
down_revision = "pbe4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("memory_recalls",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("memory_ids", postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False,
                  server_default=sa.text("'[]'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_memory_recalls_session", "memory_recalls", ["user_id", "session_id", "created_at"])


def downgrade():
    op.drop_index("ix_memory_recalls_session", table_name="memory_recalls")
    op.drop_table("memory_recalls")
