"""Add the generic skill job runtime tables (rebuild plan §6).

Seven tables: skill_jobs (durable ledger), skill_job_attempts (audit),
skill_job_events (event log + transactional outbox), skill_job_inputs
(durable wakes), skill_job_artifacts (asset join), user_skill_settings
(per-user enable/disable), session_inbox (NeedsAgent continuations).

Revision ID: a2c4e6f8b0d1
Revises: f5c6d7e8f9a0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2c4e6f8b0d1"
down_revision: Union[str, None] = "f5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")

    op.create_table(
        "skill_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("skill_key", sa.String(length=160), nullable=False),
        sa.Column("skill_version", sa.String(length=40), server_default=sa.text("''"), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("runtime_kind", sa.String(length=16), nullable=False),
        sa.Column("queue_name", sa.String(length=40), server_default=sa.text("'default'"), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("phase", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("input_data", json_type, nullable=False),
        sa.Column("checkpoint_data", json_type, nullable=False),
        sa.Column("progress_data", json_type, nullable=False),
        sa.Column("result_data", json_type, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("request_hash", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("desired_state", sa.String(length=8), server_default=sa.text("'run'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handler_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("image_digest", sa.String(length=128), server_default=sa.text("''"), nullable=False),
        sa.Column("last_event_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "skill_key", "operation", "idempotency_key",
            name="uq_skill_jobs_idempotency",
        ),
    )
    op.create_index("ix_skill_jobs_claim", "skill_jobs", ["status", "next_run_at", "queue_name"])
    op.create_index("ix_skill_jobs_user_created", "skill_jobs", ["user_id", "created_at"])
    op.create_index("ix_skill_jobs_session_created", "skill_jobs", ["session_id", "created_at"])
    op.create_index(
        "ix_skill_jobs_running_lease",
        "skill_jobs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
        sqlite_where=sa.text("status = 'running'"),
    )

    op.create_table(
        "skill_job_attempts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=128), nullable=False),
        sa.Column("queue_name", sa.String(length=40), server_default=sa.text("''"), nullable=False),
        sa.Column("runtime_kind", sa.String(length=16), server_default=sa.text("''"), nullable=False),
        sa.Column("lease_token", sa.BigInteger(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=24), server_default=sa.text("''"), nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("usage_data", json_type, nullable=False),
        sa.Column("provider_correlation_id", sa.String(length=160), nullable=True),
        sa.Column("handler_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("image_digest", sa.String(length=128), server_default=sa.text("''"), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["skill_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "attempt_number", name="uq_skill_job_attempts_number"),
    )
    op.create_index("ix_skill_job_attempts_job", "skill_job_attempts", ["job_id", "started_at"])

    op.create_table(
        "skill_job_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["skill_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "seq", name="uq_skill_job_events_seq"),
    )
    op.create_index(
        "ix_skill_job_events_outbox",
        "skill_job_events",
        ["created_at"],
        postgresql_where=sa.text("published_at IS NULL"),
        sqlite_where=sa.text("published_at IS NULL"),
    )

    op.create_table(
        "skill_job_inputs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=24), nullable=False),
        sa.Column("source_event_id", sa.String(length=160), server_default=sa.text("''"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["skill_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "idempotency_key", name="uq_skill_job_inputs_idempotency"),
    )
    op.create_index("ix_skill_job_inputs_job", "skill_job_inputs", ["job_id", "created_at"])

    op.create_table(
        "skill_job_artifacts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=40), server_default=sa.text("'output'"), nullable=False),
        sa.Column("ordinal", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("metadata", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["skill_jobs.id"]),
        sa.ForeignKeyConstraint(["asset_id"], ["file_assets.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "asset_id", "role", name="uq_skill_job_artifacts_role"),
    )
    op.create_index("ix_skill_job_artifacts_job", "skill_job_artifacts", ["job_id", "ordinal"])

    op.create_table(
        "user_skill_settings",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("skill_key", sa.String(length=160), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("settings_data", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("user_id", "skill_key"),
    )

    op.create_table(
        "session_inbox",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), server_default=sa.text("'job_needs_agent'"), nullable=False),
        sa.Column("source_job_id", sa.String(length=64), nullable=False),
        sa.Column("source_event_seq", sa.Integer(), nullable=False),
        sa.Column("payload", json_type, nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_job_id"], ["skill_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_job_id", "source_event_seq", name="uq_session_inbox_source"),
    )
    op.create_index("ix_session_inbox_session", "session_inbox", ["session_id", "status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_session_inbox_session", table_name="session_inbox")
    op.drop_table("session_inbox")
    op.drop_table("user_skill_settings")
    op.drop_index("ix_skill_job_artifacts_job", table_name="skill_job_artifacts")
    op.drop_table("skill_job_artifacts")
    op.drop_index("ix_skill_job_inputs_job", table_name="skill_job_inputs")
    op.drop_table("skill_job_inputs")
    op.drop_index("ix_skill_job_events_outbox", table_name="skill_job_events")
    op.drop_table("skill_job_events")
    op.drop_index("ix_skill_job_attempts_job", table_name="skill_job_attempts")
    op.drop_table("skill_job_attempts")
    op.drop_index("ix_skill_jobs_running_lease", table_name="skill_jobs")
    op.drop_index("ix_skill_jobs_session_created", table_name="skill_jobs")
    op.drop_index("ix_skill_jobs_user_created", table_name="skill_jobs")
    op.drop_index("ix_skill_jobs_claim", table_name="skill_jobs")
    op.drop_table("skill_jobs")
