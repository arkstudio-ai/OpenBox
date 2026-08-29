"""Media-gen routing + dedupe: video prompt hash, per-segment model, image cache.

Revision ID: a6c8e0f2b4d6
Revises: a5b6c7d8e9f0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a6c8e0f2b4d6"
down_revision: Union[str, None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.add_column("video_jobs", sa.Column("prompt_hash", sa.String(length=64), nullable=True))
    op.create_index(
        "ix_video_jobs_prompt_hash", "video_jobs", ["prompt_hash", "status", "completed_at"]
    )
    op.add_column("video_segments", sa.Column("model", sa.String(length=160), nullable=True))
    op.create_table(
        "image_gen_cache",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("op", sa.String(length=16), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=False),
        sa.Column("request_data", json_type, nullable=True),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["file_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_image_gen_cache_fingerprint", "image_gen_cache", ["fingerprint", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_image_gen_cache_fingerprint", table_name="image_gen_cache")
    op.drop_table("image_gen_cache")
    op.drop_column("video_segments", "model")
    op.drop_index("ix_video_jobs_prompt_hash", table_name="video_jobs")
    op.drop_column("video_jobs", "prompt_hash")
