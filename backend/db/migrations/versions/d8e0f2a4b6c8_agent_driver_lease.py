"""Add durable per-session agent driver ownership.

Revision ID: d8e0f2a4b6c8
Revises: c7d9e1f3a5b7
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8e0f2a4b6c8"
down_revision: Union[str, None] = "c7d9e1f3a5b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "agent_driver_states",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("owner_id", sa.String(length=160), nullable=True),
        sa.Column("phase", sa.String(length=24), server_default="idle", nullable=False),
        sa.Column("trigger_message_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("abort_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_agent_driver_user_phase",
        "agent_driver_states",
        ["user_id", "phase"],
    )
    op.create_index(
        "ix_agent_driver_lease",
        "agent_driver_states",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_driver_lease", table_name="agent_driver_states")
    op.drop_index("ix_agent_driver_user_phase", table_name="agent_driver_states")
    op.drop_table("agent_driver_states")
