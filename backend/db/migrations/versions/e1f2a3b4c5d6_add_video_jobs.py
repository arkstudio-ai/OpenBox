"""Add durable provider/sandbox video orchestration jobs.

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "video_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("model", sa.String(length=160), nullable=True),
        sa.Column("provider_task_id", sa.String(length=160), nullable=True),
        sa.Column("sandbox_job_id", sa.String(length=96), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("request_data", json_type, nullable=False),
        sa.Column("result_data", json_type, nullable=False),
        sa.Column("output_asset_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("attached_message_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["output_asset_id"], ["file_assets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "kind", "idempotency_key", name="uq_video_jobs_idempotency"),
    )
    op.create_index("ix_video_jobs_user_created", "video_jobs", ["user_id", "created_at"])
    op.create_index("ix_video_jobs_status_updated", "video_jobs", ["status", "updated_at"])
    op.create_index("ix_video_jobs_provider_task", "video_jobs", ["provider_task_id"])


def downgrade() -> None:
    op.drop_index("ix_video_jobs_provider_task", table_name="video_jobs")
    op.drop_index("ix_video_jobs_status_updated", table_name="video_jobs")
    op.drop_index("ix_video_jobs_user_created", table_name="video_jobs")
    op.drop_table("video_jobs")
