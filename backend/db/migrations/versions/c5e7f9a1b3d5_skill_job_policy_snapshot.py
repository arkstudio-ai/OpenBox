"""Snapshot skill-job policy, retry budgets, and continuation claim fencing.

Revision ID: c5e7f9a1b3d5
Revises: b4d6e8f0a2c3
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c5e7f9a1b3d5"
down_revision: Union[str, None] = "b4d6e8f0a2c3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.add_column(
        "skill_jobs",
        sa.Column(
            "output_schema",
            json_type,
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.add_column(
        "skill_jobs",
        sa.Column("retry_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "skill_jobs",
        sa.Column(
            "invocation_timeout_seconds",
            sa.Integer(),
            server_default=sa.text("120"),
            nullable=False,
        ),
    )
    op.add_column(
        "skill_jobs",
        sa.Column(
            "max_external_wait_seconds",
            sa.Integer(),
            server_default=sa.text("86400"),
            nullable=False,
        ),
    )
    op.add_column(
        "skill_jobs",
        sa.Column("user_input_timeout_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "skill_jobs",
        sa.Column(
            "cancel_requires_handler",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )
    op.add_column(
        "skill_jobs",
        sa.Column(
            "external_wait_seconds",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "skill_jobs",
        sa.Column("external_wait_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "session_inbox",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_session_inbox_claim_recovery",
        "session_inbox",
        ["status", "consumed_at"],
    )
    # Narrow the previous prefix-only receipt index to actual platform receipt
    # rows. A user-authored message must never be able to occupy this durable
    # idempotency slot before the terminal event is delivered.
    op.drop_index("uq_messages_receipt_marker", table_name="messages")
    op.create_index(
        "uq_messages_receipt_marker",
        "messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text(
            "client_message_id LIKE 'sjr:%' "
            "AND role = 'assistant' AND finish = 'skill_job_receipt'"
        ),
        sqlite_where=sa.text(
            "client_message_id LIKE 'sjr:%' "
            "AND role = 'assistant' AND finish = 'skill_job_receipt'"
        ),
    )
    # ``sji:`` was not reserved by the public message API before this release.
    # No legitimate continuation marker can predate the runtime/claim columns
    # added above, so clear historical client-chosen values before installing
    # the unique platform namespace. Otherwise two old messages with the same
    # arbitrary value could make an otherwise additive migration fail.
    op.execute(
        sa.text(
            "UPDATE messages SET client_message_id = NULL "
            "WHERE client_message_id LIKE 'sji:%'"
        )
    )
    op.create_index(
        "uq_messages_inbox_marker",
        "messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id LIKE 'sji:%'"),
        sqlite_where=sa.text("client_message_id LIKE 'sji:%'"),
    )

    # Preserve the semantics of already-admitted v2 video jobs. Other legacy
    # rows retain conservative defaults and all new jobs receive exact values
    # from their manifest at admission.
    op.execute(
        sa.text(
            """
            UPDATE skill_jobs
               SET cancel_requires_handler = true,
                   max_external_wait_seconds = 7200,
                   invocation_timeout_seconds = CASE
                       WHEN operation = 'segment.transcribe' THEN 600
                       ELSE 120
                   END
             WHERE skill_key = 'builtin:video-production'
               AND operation IN ('segment.generate', 'segment.transcribe', 'production.render')
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_messages_inbox_marker", table_name="messages")
    op.drop_index("uq_messages_receipt_marker", table_name="messages")
    op.create_index(
        "uq_messages_receipt_marker",
        "messages",
        ["session_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id LIKE 'sjr:%'"),
        sqlite_where=sa.text("client_message_id LIKE 'sjr:%'"),
    )
    op.drop_index("ix_session_inbox_claim_recovery", table_name="session_inbox")
    op.drop_column("session_inbox", "claim_token")
    op.drop_column("skill_jobs", "external_wait_started_at")
    op.drop_column("skill_jobs", "external_wait_seconds")
    op.drop_column("skill_jobs", "cancel_requires_handler")
    op.drop_column("skill_jobs", "user_input_timeout_seconds")
    op.drop_column("skill_jobs", "max_external_wait_seconds")
    op.drop_column("skill_jobs", "invocation_timeout_seconds")
    op.drop_column("skill_jobs", "retry_count")
    op.drop_column("skill_jobs", "output_schema")
