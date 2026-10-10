"""Add durable completed-turn receipts, extraction jobs and cursors.

Revision ID: m2a3b4c5d6e7
Revises: m1a2b3c4d5e6
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "m2a3b4c5d6e7"
down_revision = "m1a2b3c4d5e6"
branch_labels = None
depends_on = None


def _json():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def _text(name, length=64, nullable=False):
    return sa.Column(name, sa.String(length), nullable=nullable)


def upgrade():
    op.create_table(
        "memory_pipeline_enrollments", _text("user_id"), _text("workspace_id"), _text("pipeline_version"),
        sa.Column("eligible_since", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", "workspace_id", "pipeline_version"),
    )
    op.create_table(
        "memory_turn_completions",
        _text("id"), _text("user_id"), _text("workspace_id"), _text("project_id", nullable=True),
        _text("session_id"), _text("branch_id"), _text("logical_turn_id"), _text("run_id"),
        sa.Column("run_generation", sa.Integer(), nullable=False), _text("result_message_id"),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_sequence", sa.Integer(), nullable=False),
        sa.Column("end_sequence", sa.Integer(), nullable=False),
        sa.Column("source_boundaries", _json(), nullable=False),
        _text("input_hash"), _text("acl_hash"), _text("pipeline_version"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "branch_id", "logical_turn_id", "result_message_id", name="uq_memory_completion_boundary"),
        sa.UniqueConstraint("session_id", "branch_id", "ordinal", name="uq_memory_completion_ordinal"),
        sa.CheckConstraint("ordinal > 0 AND start_sequence > 0 AND end_sequence >= start_sequence", name="ck_memory_completion_range"),
    )
    op.create_index("ix_memory_completion_recover", "memory_turn_completions", ["pipeline_version", "created_at", "id"])
    op.create_index("ix_memory_completion_user", "memory_turn_completions", ["user_id", "workspace_id", "project_id"])
    op.create_table(
        "memory_extraction_jobs",
        _text("id"), _text("completion_id"), _text("idempotency_key"),
        _text("user_id"), _text("workspace_id"), _text("project_id", nullable=True),
        _text("session_id"), _text("branch_id"), _text("logical_turn_id"),
        sa.Column("ordinal", sa.Integer(), nullable=False), _text("input_hash"), _text("pipeline_version"),
        sa.Column("state", sa.String(16), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        _text("lease_owner", 160, nullable=True),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        _text("last_error", 128, nullable=True),
        sa.Column("result_memory_ids", _json(), nullable=False), sa.Column("usage", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"), sa.ForeignKeyConstraint(["completion_id"], ["memory_turn_completions.id"]),
        sa.UniqueConstraint("idempotency_key", name="uq_memory_extraction_idempotency"),
        sa.UniqueConstraint("completion_id", "pipeline_version", name="uq_memory_extraction_pipeline"),
        sa.CheckConstraint("state IN ('PENDING', 'RUNNING', 'RETRY', 'SUCCEEDED', 'DEAD', 'CANCELLED')", name="ck_memory_job_state"),
        sa.CheckConstraint("attempts >= 0 AND lease_generation >= 0", name="ck_memory_job_attempts"),
    )
    op.create_index("ix_memory_job_claim", "memory_extraction_jobs", ["state", "next_attempt_at", "lease_until", "created_at"])
    op.create_index("ix_memory_job_cursor", "memory_extraction_jobs", ["session_id", "branch_id", "pipeline_version", "ordinal"])
    op.create_index("ix_memory_job_user", "memory_extraction_jobs", ["user_id", "workspace_id", "project_id"])
    op.create_table(
        "memory_extraction_cursors",
        _text("session_id"), _text("branch_id"), _text("pipeline_version"),
        sa.Column("completed_ordinal", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("completed_sequence", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "branch_id", "pipeline_version"),
        sa.CheckConstraint("completed_ordinal >= 0 AND completed_sequence >= 0", name="ck_memory_cursor_nonnegative"),
    )


def downgrade():
    # App rollback keeps successful boundaries and dead letters recoverable.
    # A schema downgrade may remove these tables only when they hold no work.
    for table in ("memory_turn_completions", "memory_extraction_jobs"):
        if op.get_bind().execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar_one():
            raise RuntimeError("Memory pipeline downgrade refused: durable work must be preserved")
    op.drop_table("memory_extraction_cursors")
    op.drop_index("ix_memory_job_user", table_name="memory_extraction_jobs")
    op.drop_index("ix_memory_job_cursor", table_name="memory_extraction_jobs")
    op.drop_index("ix_memory_job_claim", table_name="memory_extraction_jobs")
    op.drop_table("memory_extraction_jobs")
    op.drop_index("ix_memory_completion_user", table_name="memory_turn_completions")
    op.drop_index("ix_memory_completion_recover", table_name="memory_turn_completions")
    op.drop_table("memory_turn_completions")
    op.drop_table("memory_pipeline_enrollments")
