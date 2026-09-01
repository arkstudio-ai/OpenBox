"""Add append-only Session Surface change events.

Revision ID: fa2c4e6d8b0a
Revises: e9f1a3c5d7b9
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "fa2c4e6d8b0a"
down_revision: Union[str, None] = "e9f1a3c5d7b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "session_surface_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("anchor_message_id", sa.String(length=64), nullable=False),
        sa.Column("replacement_run_id", sa.String(length=64), nullable=True),
        sa.Column("replacement_generation", sa.BigInteger(), nullable=True),
        sa.Column("hidden_message_ids", _json_type(), nullable=False),
        sa.Column("public_snapshot", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('regenerate', 'dismiss')",
            name="ck_session_surface_events_kind",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_session_surface_events_positive_sequence",
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "sequence",
            name="uq_session_surface_events_session_sequence",
        ),
    )
    op.create_index(
        "ix_session_surface_events_session_created",
        "session_surface_events",
        ["session_id", "created_at"],
    )
    op.create_index(
        "ix_session_surface_events_user_created",
        "session_surface_events",
        ["user_id", "created_at"],
    )


def downgrade() -> None:
    # These rows are the only recovery image after a Surface rewrite.  Refuse
    # to make a downgrade silently destructive; operators can explicitly
    # export/drain the audit stream before retrying.
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM session_surface_events")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "session Surface event downgrade refused: export and clear "
            "append-only events first"
        )
    op.drop_index(
        "ix_session_surface_events_user_created",
        table_name="session_surface_events",
    )
    op.drop_index(
        "ix_session_surface_events_session_created",
        table_name="session_surface_events",
    )
    op.drop_table("session_surface_events")
