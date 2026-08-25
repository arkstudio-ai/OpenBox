"""Add sessions.kind to distinguish cron run sessions from normal ones.

Cron temp sessions used to be recognizable only by their title prefix and a
reverse lookup through cron_runs.temp_session_id — too fragile for the places
that must treat them differently (quota, retention, panels). parent_id can't
serve: task-tool subagent sessions use it too.

Revision ID: f1a2b3c4d5e6
Revises: e5b7c9d1f3a6
Create Date: 2026-08-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e5b7c9d1f3a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "sessions",
        sa.Column("kind", sa.String(16), nullable=False, server_default="normal"),
    )
    # Backfill: every session ever used as a cron run transcript is a cron
    # session. Runs already reaped leave no row here — nothing to mark.
    op.execute(
        """
        UPDATE sessions SET kind = 'cron'
        WHERE id IN (
            SELECT DISTINCT temp_session_id FROM cron_runs
            WHERE temp_session_id IS NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.drop_column("sessions", "kind")
