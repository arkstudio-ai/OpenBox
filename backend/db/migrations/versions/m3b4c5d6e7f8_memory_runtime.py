"""Memory index generation manifests and separately authorized replay previews.

Revision ID: m3b4c5d6e7f8
Revises: m2a3b4c5d6e7
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m3b4c5d6e7f8"
down_revision = "m2a3b4c5d6e7"
branch_labels = None
depends_on = None
JSON = postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade():
    op.create_table("memory_index_generations",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("config", JSON, nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconciled_at", sa.DateTime(timezone=True)))
    op.create_table("memory_replay_previews",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(64)),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("grant", JSON, nullable=False),
        sa.Column("consumed_run_id", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table("memory_replay_previews")
    op.drop_table("memory_index_generations")
