"""Add durable, fenced Cron scheduler leases.

Revision ID: e9f1a3c5d7b9
Revises: d8e0f2a4b6c8
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e9f1a3c5d7b9"
down_revision: Union[str, None] = "d8e0f2a4b6c8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch_alter_table keeps the desktop SQLite store and PostgreSQL on the
    # same migration path.
    with op.batch_alter_table("cron_jobs") as batch:
        batch.add_column(
            sa.Column(
                "run_generation",
                sa.BigInteger(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch.add_column(sa.Column("run_token", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("run_owner", sa.String(length=160), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_cron_jobs_lease", "cron_jobs", ["lease_expires_at"])

    with op.batch_alter_table("cron_runs") as batch:
        batch.add_column(sa.Column("claim_token", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("claim_generation", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("claim_owner", sa.String(length=160), nullable=True))
    op.create_index(
        "ix_cron_runs_claim",
        "cron_runs",
        ["job_id", "claim_generation"],
    )


def downgrade() -> None:
    op.drop_index("ix_cron_runs_claim", table_name="cron_runs")
    with op.batch_alter_table("cron_runs") as batch:
        batch.drop_column("claim_owner")
        batch.drop_column("claim_generation")
        batch.drop_column("claim_token")

    op.drop_index("ix_cron_jobs_lease", table_name="cron_jobs")
    with op.batch_alter_table("cron_jobs") as batch:
        batch.drop_column("heartbeat_at")
        batch.drop_column("lease_expires_at")
        batch.drop_column("run_owner")
        batch.drop_column("run_token")
        batch.drop_column("run_generation")
