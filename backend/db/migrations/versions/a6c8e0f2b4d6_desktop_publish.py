"""Desktop (创作者中心) publish route: job details and the per-account auto-publish breaker.

Revision ID: a6c8e0f2b4d6
Revises: f2a4c6e8b0d3
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "a6c8e0f2b4d6"
down_revision = "f2a4c6e8b0d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publish_jobs", sa.Column("details", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))
    op.add_column("platform_accounts", sa.Column("auto_publish_disabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("platform_accounts", sa.Column("auto_publish_disabled_reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("platform_accounts", "auto_publish_disabled_reason")
    op.drop_column("platform_accounts", "auto_publish_disabled_at")
    op.drop_column("publish_jobs", "details")
