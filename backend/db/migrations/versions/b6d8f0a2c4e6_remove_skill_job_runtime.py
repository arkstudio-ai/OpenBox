"""Remove the retired skill-job orchestration tables.

Revision ID: b6d8f0a2c4e6
Revises: e3a5c7d9f1b2
Create Date: 2026-08-30

Before the artifact join table is removed, output asset descriptors are copied
into historical receipt parts that predate the embedded ``artifacts`` field.
The continuation-only ``sji:`` uniqueness index is retired with its inbox;
the historical ``sjr:`` receipt namespace remains intact.
The downgrade restores the complete schema that existed immediately before
this revision, including the policy snapshot and continuation columns added
after the original seven-table migration. Other table data is intentionally
not recoverable through Alembic; operators must use the pre-upgrade snapshot.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b6d8f0a2c4e6"
down_revision: Union[str, None] = "e3a5c7d9f1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    _backfill_receipt_artifacts()
    op.drop_index("uq_messages_inbox_marker", table_name="messages")
    op.drop_table("session_inbox")
    op.drop_table("skill_job_artifacts")
    op.drop_table("skill_job_inputs")
    op.drop_table("skill_job_events")
    op.drop_table("skill_job_attempts")
    op.drop_table("skill_jobs")
    op.drop_table("user_skill_settings")


def _backfill_receipt_artifacts() -> None:
    """Preserve output links that only exist in the retired join table.

    Early receipt rows were written before the runtime embedded artifact
    descriptors in ``parts.data``. Loading the small retirement dataset into
    Python keeps the transform identical on PostgreSQL JSONB and SQLite JSON,
    while the user-id joins prevent one tenant's asset metadata from crossing
    into another tenant's transcript.
    """
    bind = op.get_bind()
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    parts = sa.table(
        "parts",
        sa.column("id", sa.String(length=64)),
        sa.column("user_id", sa.String(length=64)),
        sa.column("type", sa.String(length=32)),
        sa.column("data", json_type),
    )
    artifacts = sa.table(
        "skill_job_artifacts",
        sa.column("id", sa.String(length=64)),
        sa.column("job_id", sa.String(length=64)),
        sa.column("user_id", sa.String(length=64)),
        sa.column("asset_id", sa.String(length=64)),
        sa.column("role", sa.String(length=40)),
        sa.column("ordinal", sa.Integer()),
    )
    assets = sa.table(
        "file_assets",
        sa.column("id", sa.String(length=64)),
        sa.column("user_id", sa.String(length=64)),
        sa.column("name", sa.String(length=255)),
        sa.column("mime", sa.String(length=128)),
    )

    outputs_by_job: dict[tuple[str, str], list[tuple[int, str, dict]]] = {}
    output_rows = bind.execute(
        sa.select(
            artifacts.c.id.label("artifact_id"),
            artifacts.c.job_id,
            artifacts.c.user_id,
            artifacts.c.ordinal,
            assets.c.id.label("asset_id"),
            assets.c.name,
            assets.c.mime,
        )
        .select_from(
            artifacts.join(
                assets,
                sa.and_(
                    assets.c.id == artifacts.c.asset_id,
                    assets.c.user_id == artifacts.c.user_id,
                ),
            )
        )
        .where(artifacts.c.role == "output")
    ).mappings()
    for row in output_rows:
        descriptor = {
            "assetId": row["asset_id"],
            "name": row["name"],
            "mime": row["mime"],
        }
        outputs_by_job.setdefault((row["user_id"], row["job_id"]), []).append(
            (row["ordinal"], row["artifact_id"], descriptor)
        )
    for ordered_outputs in outputs_by_job.values():
        ordered_outputs.sort(key=lambda item: (item[0], item[1]))

    receipt_rows = bind.execute(
        sa.select(parts.c.id, parts.c.user_id, parts.c.data).where(
            parts.c.type == "skill_job"
        )
    ).mappings()
    for row in receipt_rows:
        data = row["data"]
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (TypeError, ValueError):
                continue
        if not isinstance(data, dict):
            continue
        if "artifacts" in data and data.get("artifacts") not in (None, []):
            continue
        job_id = data.get("jobId")
        if not isinstance(job_id, str) or not job_id:
            continue
        matched_outputs = outputs_by_job.get((row["user_id"], job_id), [])
        if not matched_outputs:
            continue
        updated = dict(data)
        updated["artifacts"] = [descriptor for _, _, descriptor in matched_outputs]
        bind.execute(
            parts.update().where(parts.c.id == row["id"]).values(data=updated)
        )


def downgrade() -> None:
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
        sa.Column("output_schema", json_type, server_default=sa.text("'{}'"), nullable=False),
        sa.Column("checkpoint_data", json_type, nullable=False),
        sa.Column("progress_data", json_type, nullable=False),
        sa.Column("result_data", json_type, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=180), nullable=False),
        sa.Column("request_hash", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.Column("desired_state", sa.String(length=8), server_default=sa.text("'run'"), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("8"), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_token", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("handler_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("image_digest", sa.String(length=128), server_default=sa.text("''"), nullable=False),
        sa.Column("invocation_timeout_seconds", sa.Integer(), server_default=sa.text("120"), nullable=False),
        sa.Column("max_external_wait_seconds", sa.Integer(), server_default=sa.text("86400"), nullable=False),
        sa.Column("user_input_timeout_seconds", sa.Integer(), nullable=True),
        sa.Column("cancel_requires_handler", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("continue_agent_on_success", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("external_wait_seconds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("external_wait_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_event_seq", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "skill_key",
            "operation",
            "idempotency_key",
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
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["source_job_id"], ["skill_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_job_id", "source_event_seq", name="uq_session_inbox_source"),
    )
    op.create_index("ix_session_inbox_session", "session_inbox", ["session_id", "status", "created_at"])
    op.create_index("ix_session_inbox_claim_recovery", "session_inbox", ["status", "consumed_at"])
    op.create_index(
        "uq_messages_inbox_marker",
        "messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id LIKE 'sji:%'"),
        sqlite_where=sa.text("client_message_id LIKE 'sji:%'"),
    )
