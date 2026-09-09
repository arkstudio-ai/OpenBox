"""Shared hot-list snapshots and resolved media links for `hot_trends`.

Revision ID: f2a4c6e8b0d3
Revises: e1f3a5b7c9d2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f2a4c6e8b0d3"
down_revision = "e1f3a5b7c9d2"
branch_labels = None
depends_on = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "hot_trend_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("cache_key", sa.String(200), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("board", sa.String(32), nullable=False),
        sa.Column("window_hours", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("day", sa.String(10), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("items", _json(), nullable=False),
        sa.Column("extra", _json(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("fetched_by_workspace", sa.String(64), nullable=True),
        sa.Column("fetched_by_user", sa.String(64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("cache_key", name="uq_hot_trend_snapshots_cache_key"),
    )
    op.create_index("ix_hot_trend_snapshots_source_day", "hot_trend_snapshots", ["source", "day", "fetched_at"])
    op.create_table(
        "hot_media_links",
        sa.Column("video_id", sa.String(32), primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("media_url", sa.Text(), nullable=False),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("resolved_by_user", sa.String(64), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("hot_media_links")
    op.drop_index("ix_hot_trend_snapshots_source_day", table_name="hot_trend_snapshots")
    op.drop_table("hot_trend_snapshots")
