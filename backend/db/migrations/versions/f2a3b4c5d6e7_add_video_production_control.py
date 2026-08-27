"""Add persistent spoken-video production control plane.

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "video_productions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("brief", sa.Text(), nullable=False),
        sa.Column("mode", sa.String(length=24), server_default=sa.text("'standard'"), nullable=False),
        sa.Column("status", sa.String(length=32), server_default=sa.text("'init'"), nullable=False),
        sa.Column("target_duration_seconds", sa.Integer(), server_default=sa.text("60"), nullable=False),
        sa.Column("ratio", sa.String(length=16), server_default=sa.text("'9:16'"), nullable=False),
        sa.Column("resolution", sa.String(length=16), server_default=sa.text("'720p'"), nullable=False),
        sa.Column("quality_policy", sa.String(length=16), server_default=sa.text("'required'"), nullable=False),
        sa.Column("subtitles", sa.Boolean(), nullable=True),
        sa.Column("channel_name", sa.String(length=100), server_default=sa.text("''"), nullable=False),
        sa.Column("visual_anchor", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("character_asset_id", sa.String(length=64), nullable=True),
        sa.Column("script_text", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("script_hash", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("plan_hash", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("render_asset_id", sa.String(length=64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["character_asset_id"], ["file_assets.id"]),
        sa.ForeignKeyConstraint(["render_asset_id"], ["file_assets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_productions_user_created", "video_productions", ["user_id", "created_at"])
    op.create_index("ix_video_productions_session_updated", "video_productions", ["session_id", "updated_at"])

    op.create_table(
        "video_segments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("production_id", sa.String(length=64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("role", sa.String(length=24), server_default=sa.text("'body'"), nullable=False),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("input_asset_ids", json_type, nullable=False),
        sa.Column("lint_data", json_type, nullable=False),
        sa.Column("status", sa.String(length=24), server_default=sa.text("'planned'"), nullable=False),
        sa.Column("generation_job_id", sa.String(length=64), nullable=True),
        sa.Column("output_asset_id", sa.String(length=64), nullable=True),
        sa.Column("transcript_text", sa.Text(), nullable=True),
        sa.Column("transcript_data", json_type, nullable=False),
        sa.Column("stt_similarity", sa.Float(), nullable=True),
        sa.Column("stt_verdict", sa.String(length=24), nullable=True),
        sa.Column("stt_notes", json_type, nullable=False),
        sa.Column("stt_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_status", sa.String(length=24), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["output_asset_id"], ["file_assets.id"]),
        sa.ForeignKeyConstraint(["production_id"], ["video_productions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("production_id", "ordinal", "revision", name="uq_video_segments_revision"),
    )
    op.create_index("ix_video_segments_production_active", "video_segments", ["production_id", "is_active", "ordinal"])
    op.create_index("ix_video_segments_output_asset", "video_segments", ["output_asset_id"])

    op.create_table(
        "video_approvals",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("production_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("scope_hash", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("max_calls", sa.Integer(), nullable=True),
        sa.Column("used_calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("evidence_message_id", sa.String(length=64), nullable=True),
        sa.Column("evidence_part_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_data", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["production_id"], ["video_productions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_video_approvals_scope", "video_approvals", ["production_id", "kind", "scope_hash", "created_at"])
    op.create_index("ix_video_approvals_user_created", "video_approvals", ["user_id", "created_at"])

    op.add_column("video_jobs", sa.Column("production_id", sa.String(length=64), nullable=True))
    op.add_column("video_jobs", sa.Column("segment_id", sa.String(length=64), nullable=True))
    op.add_column("video_jobs", sa.Column("request_hash", sa.String(length=64), server_default=sa.text("''"), nullable=False))
    op.create_foreign_key("fk_video_jobs_production", "video_jobs", "video_productions", ["production_id"], ["id"])
    op.create_foreign_key("fk_video_jobs_segment", "video_jobs", "video_segments", ["segment_id"], ["id"])
    op.create_index("ix_video_jobs_production", "video_jobs", ["production_id", "created_at"])
    op.create_index("ix_video_jobs_segment", "video_jobs", ["segment_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_video_jobs_segment", table_name="video_jobs")
    op.drop_index("ix_video_jobs_production", table_name="video_jobs")
    op.drop_constraint("fk_video_jobs_segment", "video_jobs", type_="foreignkey")
    op.drop_constraint("fk_video_jobs_production", "video_jobs", type_="foreignkey")
    op.drop_column("video_jobs", "request_hash")
    op.drop_column("video_jobs", "segment_id")
    op.drop_column("video_jobs", "production_id")
    op.drop_index("ix_video_approvals_user_created", table_name="video_approvals")
    op.drop_index("ix_video_approvals_scope", table_name="video_approvals")
    op.drop_table("video_approvals")
    op.drop_index("ix_video_segments_output_asset", table_name="video_segments")
    op.drop_index("ix_video_segments_production_active", table_name="video_segments")
    op.drop_table("video_segments")
    op.drop_index("ix_video_productions_session_updated", table_name="video_productions")
    op.drop_index("ix_video_productions_user_created", table_name="video_productions")
    op.drop_table("video_productions")
