"""Add the canonical append-only Agent event shadow log.

Revision ID: a8c1e4f7b9d2
Revises: fc4e6d8b0a2c
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a8c1e4f7b9d2"
down_revision: Union[str, None] = "fc4e6d8b0a2c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_key", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("generation", sa.BigInteger(), nullable=True),
        sa.Column("turn_id", sa.String(length=64), nullable=True),
        sa.Column("step_id", sa.String(length=128), nullable=True),
        sa.Column("message_id", sa.String(length=64), nullable=True),
        sa.Column("part_id", sa.String(length=64), nullable=True),
        sa.Column("tool_call_id", sa.String(length=256), nullable=True),
        sa.Column("payload", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_agent_events_positive_sequence",
        ),
        sa.CheckConstraint(
            "(run_id IS NULL AND generation IS NULL) OR "
            "(run_id IS NOT NULL AND generation IS NOT NULL AND generation > 0)",
            name="ck_agent_events_run_generation_pair",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_agent_events_session_sequence",
        ),
        sa.UniqueConstraint(
            "session_id",
            "event_key",
            name="uq_agent_events_session_event_key",
        ),
    )
    op.create_index(
        "ix_agent_events_session_run",
        "agent_events",
        ["session_id", "run_id", "generation"],
    )
    op.create_index(
        "ix_agent_events_session_message",
        "agent_events",
        ["session_id", "message_id"],
    )
    op.create_index(
        "ix_agent_events_session_part",
        "agent_events",
        ["session_id", "part_id"],
    )
    op.create_index(
        "ix_agent_events_user_created",
        "agent_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    # Until this shadow log becomes the sole truth source, downgrade is safe
    # only when no immutable audit history would be discarded silently.
    count = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM agent_events")).scalar_one()
    if count:
        raise RuntimeError(
            "Agent event downgrade refused: export and clear append-only events first"
        )
    op.drop_index("ix_agent_events_user_created", table_name="agent_events")
    op.drop_index("ix_agent_events_session_part", table_name="agent_events")
    op.drop_index("ix_agent_events_session_message", table_name="agent_events")
    op.drop_index("ix_agent_events_session_run", table_name="agent_events")
    op.drop_table("agent_events")
