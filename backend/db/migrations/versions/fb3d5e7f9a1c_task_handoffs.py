"""Add durable Task parent/child handoffs.

Revision ID: fb3d5e7f9a1c
Revises: fa2c4e6d8b0a
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "fb3d5e7f9a1c"
down_revision: Union[str, None] = "fa2c4e6d8b0a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "task_handoffs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("parent_session_id", sa.String(length=64), nullable=False),
        sa.Column("parent_message_id", sa.String(length=64), nullable=False),
        sa.Column("parent_part_id", sa.String(length=64), nullable=False),
        sa.Column("parent_run_id", sa.String(length=64), nullable=False),
        sa.Column("parent_generation", sa.Integer(), nullable=False),
        sa.Column("child_session_id", sa.String(length=64), nullable=False),
        sa.Column("child_trigger_message_id", sa.String(length=64), nullable=False),
        sa.Column("child_run_id", sa.String(length=64), nullable=True),
        sa.Column("child_generation", sa.Integer(), nullable=True),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default="accepted",
            nullable=False,
        ),
        sa.Column("task_title", sa.String(length=255), nullable=False),
        sa.Column("subagent_type", sa.String(length=64), nullable=False),
        sa.Column("result_payload", _json_type(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejoined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "state IN ('accepted', 'completed', 'rejoined', 'abandoned')",
            name="ck_task_handoffs_state",
        ),
        sa.CheckConstraint(
            "parent_generation > 0",
            name="ck_task_handoffs_parent_generation",
        ),
        sa.CheckConstraint(
            "child_generation IS NULL OR child_generation > 0",
            name="ck_task_handoffs_child_generation",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_part_id"], ["parts.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_trigger_message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_part_id", name="uq_task_handoffs_parent_part"
        ),
        sa.UniqueConstraint(
            "child_session_id", name="uq_task_handoffs_child_session"
        ),
    )
    op.create_index(
        "ix_task_handoffs_user_state",
        "task_handoffs",
        ["user_id", "state"],
    )
    op.create_index(
        "ix_task_handoffs_parent_state",
        "task_handoffs",
        ["parent_session_id", "state"],
    )
    op.create_index(
        "ix_task_handoffs_child_state",
        "task_handoffs",
        ["child_session_id", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_handoffs_child_state", table_name="task_handoffs")
    op.drop_index("ix_task_handoffs_parent_state", table_name="task_handoffs")
    op.drop_index("ix_task_handoffs_user_state", table_name="task_handoffs")
    op.drop_table("task_handoffs")
