"""Let a pipeline stage wake its session when it finishes.

Ordinary completion writes a receipt and stops, which is right for a one-shot
job but stalls a multi-stage pipeline at every step. This snapshots the
operation's declaration onto the job, so a manifest edit cannot change what an
in-flight job will do.

Revision ID: e3a5c7d9f1b2
Revises: d2f4a6b8c0e1
Create Date: 2026-08-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a5c7d9f1b2"
down_revision: Union[str, None] = "d2f4a6b8c0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "skill_jobs",
        sa.Column(
            "continue_agent_on_success",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("skill_jobs", "continue_agent_on_success")
