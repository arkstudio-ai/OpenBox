"""API keys for the public harness API, plus the session columns it exposes.

Revision ID: e7c9a1b3d5f0
Revises: d0a2c4e6f8b1
Create Date: 2026-09-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7c9a1b3d5f0"
down_revision: Union[str, None] = "d0a2c4e6f8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("key_prefix", sa.String(24), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scopes", _json_type(), nullable=False),
        sa.Column("policy", _json_type(), nullable=False),
        sa.Column("rate_limit", sa.String(32), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_api_keys_user_active", "api_keys", ["user_id", "revoked_at"])
    op.create_index("ix_api_keys_workspace", "api_keys", ["workspace_id"])

    op.add_column("sessions", sa.Column("quality", sa.String(16), nullable=True))
    op.add_column("sessions", sa.Column("metadata", _json_type(), nullable=True))
    op.add_column("sessions", sa.Column("api_key_id", sa.String(64), nullable=True))
    op.create_index("ix_sessions_api_key", "sessions", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_sessions_api_key", table_name="sessions")
    with op.batch_alter_table("sessions", reflect_kwargs={"resolve_fks": False}) as batch:
        batch.drop_column("api_key_id")
        batch.drop_column("metadata")
        batch.drop_column("quality")
    op.drop_index("ix_api_keys_workspace", table_name="api_keys")
    op.drop_index("ix_api_keys_user_active", table_name="api_keys")
    op.drop_table("api_keys")
