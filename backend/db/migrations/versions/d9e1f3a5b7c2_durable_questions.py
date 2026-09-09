"""Persist human input and its restart-safe continuation intent.

Revision ID: d9e1f3a5b7c2
Revises: c8e0a2b4d6f1
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d9e1f3a5b7c2"
down_revision = "c8e0a2b4d6f1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "session_executions",
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("run_id", sa.String(64)),
        sa.Column("run_generation", sa.Integer()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("run_origin", sa.String(16)),
        sa.Column("run_progress", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resume_pending", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("resume_error", sa.Text()),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_session_executions_resume", "session_executions", ["resume_pending", "lease_until"])
    op.create_index("ix_session_executions_lease", "session_executions", ["lease_until"])
    op.create_table(
        "question_checkpoints",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.String(64)),
        sa.Column("part_id", sa.String(64)),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("questions", json_type, nullable=False),
        sa.Column("answers", json_type),
        sa.Column("draft", json_type, nullable=False),
        sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("continuation", json_type, nullable=False),
        sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("part_id", name="uq_question_checkpoint_part"),
    )
    op.create_index("ix_question_checkpoints_pending", "question_checkpoints", ["user_id", "status", "created_at"])
    op.create_index("ix_question_checkpoints_turn", "question_checkpoints", ["session_id", "generation", "status"])
    op.create_index("ix_question_checkpoints_expiry", "question_checkpoints", ["status", "expires_at"])


def downgrade() -> None:
    op.drop_table("question_checkpoints")
    op.drop_table("session_executions")
