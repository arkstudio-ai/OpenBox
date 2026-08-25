"""Scope cron jobs to projects; session becomes an optional notify target.

A scheduled task acts on a project's files and logs into the project's cron/
directory — the project is its natural owner. The session link survives only
as "where to post results" (set when the job was created from a chat), so
deleting a conversation no longer kills its schedules.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-08-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cron_jobs", sa.Column("project_id", sa.String(64), nullable=True))
    op.execute(
        """
        UPDATE cron_jobs SET project_id = (
            SELECT sessions.project_id FROM sessions
            WHERE sessions.id = cron_jobs.session_id
        )
        """
    )
    op.alter_column("cron_jobs", "session_id", nullable=True)
    op.create_index("ix_cron_jobs_project", "cron_jobs", ["project_id"])

    op.add_column("cron_runs", sa.Column("project_id", sa.String(64), nullable=True))
    op.execute(
        """
        UPDATE cron_runs SET project_id = (
            SELECT cron_jobs.project_id FROM cron_jobs
            WHERE cron_jobs.id = cron_runs.job_id
        )
        """
    )
    op.alter_column("cron_runs", "session_id", nullable=True)


def downgrade() -> None:
    op.alter_column("cron_runs", "session_id", nullable=False)
    op.drop_column("cron_runs", "project_id")
    op.drop_index("ix_cron_jobs_project", table_name="cron_jobs")
    op.alter_column("cron_jobs", "session_id", nullable=False)
    op.drop_column("cron_jobs", "project_id")
