"""Marketing-autopilot template (budget authorisation) on cron jobs.

Revision ID: d2f4a6c8e0b2
Revises: b8d0f2a4c6e8
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d2f4a6c8e0b2"
down_revision = "b8d0f2a4c6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cron_jobs", sa.Column("template", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade() -> None:
    op.drop_column("cron_jobs", "template")
