"""Create the trajectory trace database.

Revision ID: t0001_initial
Revises:
Create Date: 2026-09-14

PostgreSQL also gets the pg_trgm extension, RANGE partitioning of
trajectory_events by recorded_on with a default partition (daily partitions
are maintained by trajectory.store.partitions) and a trigram GIN index on
trajectory_records.search_doc. SQLite gets plain tables and btree indexes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

from db.base import JSONType

# revision identifiers
revision: str = "t0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BIGINT_ID = sa.BigInteger().with_variant(sa.Integer(), "sqlite")

TABLES = (
    "session_trajectories", "trajectory_events", "trajectory_event_keys", "trajectory_segments",
    "trajectory_payloads", "trajectory_records", "trajectory_record_events", "trajectory_session_summaries",
    "trajectory_checkpoints", "trajectory_exports", "trajectory_meta_sessions", "trajectory_meta_users",
    "trajectory_meta_workspaces", "trajectory_meta_assets", "trajectory_ingest_producers",
    "trajectory_ingest_files", "trajectory_gc_queue", "trajectory_worker_state", "trajectory_audit_outbox",
)


def _string(name: str, length: int, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.String(length), nullable=nullable)


def _timestamp(name: str, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _defaulted(name: str, type_, value) -> sa.Column:
    if isinstance(value, bool):
        server_default = sa.true() if value else sa.false()
    elif isinstance(value, str):
        server_default = sa.text(f"'{value}'")
    else:
        server_default = sa.text(str(value))
    return sa.Column(name, type_, nullable=False, server_default=server_default)


def _trajectory_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["trajectory_id"], ["session_trajectories.id"], ondelete="CASCADE")


def upgrade() -> None:
    postgresql = op.get_context().dialect.name == "postgresql"
    if postgresql:
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "session_trajectories",
        _string("id", 64),
        _string("user_id", 64),
        _string("session_id", 64),
        _string("workspace_id", 64),
        _timestamp("started_at"),
        _timestamp("updated_at"),
        _timestamp("last_activity_at"),
        _defaulted("next_seq", sa.BigInteger(), 1),
        _defaulted("committed_seq", sa.BigInteger(), 0),
        _defaulted("projected_seq", sa.BigInteger(), 0),
        _defaulted("archived_seq", sa.BigInteger(), 0),
        _defaulted("checkpoint_seq", sa.BigInteger(), 0),
        _defaulted("schema_version", sa.Integer(), 2),
        _defaulted("recording_status", sa.String(32), "recording"),
        _defaulted("recording_epoch", sa.Integer(), 0),
        _defaulted("event_count", sa.BigInteger(), 0),
        _defaulted("stored_bytes", sa.BigInteger(), 0),
        _defaulted("budget_level", sa.String(16), "normal"),
        _string("budget_reason", 64, nullable=True),
        _timestamp("deleted_at", nullable=True),
        _timestamp("content_expired_at", nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_session_trajectories_session"),
        sa.UniqueConstraint("user_id", "session_id", name="uq_session_trajectories_owner_session"),
    )
    op.create_index("ix_session_trajectories_last_activity", "session_trajectories", ["last_activity_at"])

    # PostgreSQL can only enforce a primary key that includes the partition key.
    events_key = ("trajectory_id", "seq", "recorded_on") if postgresql else ("trajectory_id", "seq")
    op.create_table(
        "trajectory_events",
        _string("trajectory_id", 64),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("recorded_on", sa.Date(), nullable=False),
        _string("event_id", 128),
        _string("type", 64),
        sa.Column("version", sa.Integer(), nullable=False),
        _string("user_id", 64),
        _string("session_id", 64),
        _string("source_session_id", 64),
        _string("request_id", 128, nullable=True),
        _string("call_id", 128, nullable=True),
        _string("agent_id", 128, nullable=True),
        sa.Column("context", JSONType(), nullable=False),
        sa.Column("data", JSONType(), nullable=False),
        sa.Column("hints", JSONType(), nullable=True),
        _string("content_hash", 64),
        _timestamp("occurred_at"),
        _timestamp("recorded_at"),
        sa.PrimaryKeyConstraint(*events_key),
        _trajectory_fk(),
        postgresql_partition_by="RANGE (recorded_on)",
    )
    if postgresql:
        op.execute("CREATE TABLE trajectory_events_default PARTITION OF trajectory_events DEFAULT")
    op.create_index("ix_trajectory_events_seq", "trajectory_events", ["trajectory_id", "seq"])
    op.create_index("ix_trajectory_events_request", "trajectory_events", ["trajectory_id", "request_id", "seq"])
    op.create_index("ix_trajectory_events_call", "trajectory_events", ["trajectory_id", "call_id", "seq"])

    op.create_table(
        "trajectory_event_keys",
        _string("event_id", 128),
        _string("trajectory_id", 64),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        _string("content_hash", 64),
        _timestamp("recorded_at"),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index("ix_trajectory_event_keys_recorded_at", "trajectory_event_keys", ["recorded_at"])

    op.create_table(
        "trajectory_segments",
        _string("trajectory_id", 64),
        sa.Column("from_seq", sa.BigInteger(), nullable=False),
        sa.Column("to_seq", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("raw_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stored_bytes", sa.BigInteger(), nullable=False),
        _string("sha256", 64),
        _defaulted("compression", sa.String(16), "zstd"),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("trajectory_id", "from_seq"),
        _trajectory_fk(),
    )
    op.create_index("ix_trajectory_segments_to_seq", "trajectory_segments", ["trajectory_id", "to_seq"])

    op.create_table(
        "trajectory_payloads",
        _string("payload_id", 64),
        _string("trajectory_id", 64),
        _string("dedupe_key", 64),
        _string("sha256", 64, nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        _defaulted("stored_bytes", sa.BigInteger(), 0),
        _string("media_type", 128),
        _defaulted("encoding", sa.String(16), "identity"),
        _string("storage_kind", 16),
        sa.Column("storage_key", sa.Text(), nullable=False),
        _string("source_asset_id", 64, nullable=True),
        _defaulted("availability", sa.String(24), "available"),
        sa.Column("first_seq", sa.BigInteger(), nullable=False),
        _timestamp("created_at"),
        _timestamp("deleted_at", nullable=True),
        sa.PrimaryKeyConstraint("payload_id"),
        _trajectory_fk(),
        sa.UniqueConstraint("trajectory_id", "dedupe_key", name="uq_trajectory_payloads_dedupe"),
    )
    op.create_index("ix_trajectory_payloads_digest", "trajectory_payloads", ["trajectory_id", "sha256"])
    op.create_index("ix_trajectory_payloads_source_asset", "trajectory_payloads", ["source_asset_id"])

    op.create_table(
        "trajectory_records",
        _string("trajectory_id", 64),
        _string("record_id", 256),
        _string("kind", 32),
        _string("status", 32),
        _string("agent_id", 128, nullable=True),
        _string("message_id", 128, nullable=True),
        sa.Column("start_seq", sa.BigInteger(), nullable=False),
        sa.Column("end_seq", sa.BigInteger(), nullable=True),
        sa.Column("applied_seq", sa.BigInteger(), nullable=False),
        sa.Column("projector_version", sa.Integer(), nullable=False),
        sa.Column("data", JSONType(), nullable=False),
        sa.Column("summary", JSONType(), nullable=False),
        sa.Column("search_doc", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("trajectory_id", "record_id"),
        _trajectory_fk(),
    )
    op.create_index("ix_trajectory_records_order", "trajectory_records", ["trajectory_id", "start_seq", "record_id"])
    op.create_index("ix_trajectory_records_message", "trajectory_records", ["trajectory_id", "message_id"])
    op.create_index("ix_trajectory_records_filter", "trajectory_records", ["trajectory_id", "kind", "status", "start_seq"])
    op.create_index("ix_trajectory_records_agent", "trajectory_records", ["trajectory_id", "agent_id"])
    if postgresql:
        op.create_index("ix_trajectory_records_search_trgm", "trajectory_records", ["search_doc"],
                        postgresql_using="gin", postgresql_ops={"search_doc": "gin_trgm_ops"})

    op.create_table(
        "trajectory_record_events",
        _string("trajectory_id", 64),
        _string("record_id", 256),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("trajectory_id", "record_id", "seq"),
    )

    op.create_table(
        "trajectory_session_summaries",
        _string("trajectory_id", 64),
        _string("user_id", 64),
        _string("session_id", 64),
        _string("workspace_id", 64),
        _timestamp("last_activity_at"),
        _string("running_status", 32),
        _string("recording_status", 32),
        _string("model", 128, nullable=True),
        sa.Column("applied_seq", sa.BigInteger(), nullable=False),
        sa.Column("statistics", JSONType(), nullable=False),
        sa.PrimaryKeyConstraint("trajectory_id"),
        _trajectory_fk(),
    )
    op.create_index("ix_trajectory_session_summaries_activity", "trajectory_session_summaries",
                    ["last_activity_at", "session_id"])
    op.create_index("ix_trajectory_session_summaries_owner", "trajectory_session_summaries",
                    ["user_id", "last_activity_at", "session_id"])
    op.create_index("ix_trajectory_session_summaries_workspace", "trajectory_session_summaries",
                    ["workspace_id", "last_activity_at", "session_id"])
    op.create_index("ix_trajectory_session_summaries_status", "trajectory_session_summaries",
                    ["running_status", "recording_status", "last_activity_at"])

    op.create_table(
        "trajectory_checkpoints",
        _string("trajectory_id", 64),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        sa.Column("projector_version", sa.Integer(), nullable=False),
        sa.Column("state", JSONType(), nullable=False),
        _string("digest", 64),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("trajectory_id", "through_seq"),
        _trajectory_fk(),
    )

    op.create_table(
        "trajectory_exports",
        _string("id", 64),
        _string("trajectory_id", 64),
        _string("viewer_id", 64),
        sa.Column("through_seq", sa.BigInteger(), nullable=False),
        _string("status", 24),
        sa.Column("storage_key", sa.Text(), nullable=True),
        _string("sha256", 64, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _string("lease_owner", 64, nullable=True),
        _timestamp("lease_until", nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        _trajectory_fk(),
    )
    op.create_index("ix_trajectory_exports_trajectory", "trajectory_exports", ["trajectory_id"])

    op.create_table(
        "trajectory_meta_sessions",
        _string("id", 64),
        _string("user_id", 64),
        _string("workspace_id", 64, nullable=True),
        _string("project_id", 64, nullable=True),
        _string("parent_id", 64, nullable=True),
        _string("kind", 32, nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        _string("status", 32, nullable=True),
        _string("model", 128, nullable=True),
        _string("agent", 64, nullable=True),
        _defaulted("is_deleted", sa.Boolean(), False),
        _timestamp("deleted_at", nullable=True),
        _timestamp("created_at", nullable=True),
        _timestamp("updated_at"),
        _timestamp("synced_at"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trajectory_meta_sessions_updated", "trajectory_meta_sessions", ["updated_at", "id"])
    op.create_index("ix_trajectory_meta_sessions_owner", "trajectory_meta_sessions", ["user_id", "updated_at"])
    op.create_index("ix_trajectory_meta_sessions_workspace", "trajectory_meta_sessions", ["workspace_id", "updated_at"])
    op.create_index("ix_trajectory_meta_sessions_parent", "trajectory_meta_sessions", ["parent_id"])

    op.create_table(
        "trajectory_meta_users",
        _string("id", 64),
        _string("username", 64, nullable=True),
        _string("email", 255, nullable=True),
        _string("role", 32, nullable=True),
        _defaulted("is_active", sa.Boolean(), True),
        _defaulted("is_deleted", sa.Boolean(), False),
        _timestamp("updated_at"),
        _timestamp("synced_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "trajectory_meta_workspaces",
        _string("id", 64),
        _string("name", 128, nullable=True),
        _timestamp("updated_at"),
        _timestamp("synced_at"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "trajectory_meta_assets",
        _string("id", 64),
        _string("user_id", 64),
        _string("workspace_id", 64, nullable=True),
        _string("session_id", 64, nullable=True),
        sa.Column("oss_key", sa.Text(), nullable=True),
        _string("mime", 128, nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        _string("status", 32, nullable=True),
        _defaulted("is_deleted", sa.Boolean(), False),
        _timestamp("deleted_at", nullable=True),
        _timestamp("updated_at"),
        _timestamp("synced_at"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trajectory_meta_assets_session", "trajectory_meta_assets", ["session_id"])

    op.create_table(
        "trajectory_ingest_producers",
        _string("producer_id", 128),
        _string("hostname", 64, nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        _string("boot_id", 64, nullable=True),
        _string("role", 32, nullable=True),
        _timestamp("started_at", nullable=True),
        _defaulted("last_n", sa.BigInteger(), 0),
        _timestamp("last_seen_at"),
        _defaulted("goodbye", sa.Boolean(), False),
        _defaulted("abandoned", sa.Boolean(), False),
        sa.PrimaryKeyConstraint("producer_id"),
    )

    op.create_table(
        "trajectory_ingest_files",
        _string("producer_id", 128),
        _string("file_name", 64),
        _defaulted("bytes_consumed", sa.BigInteger(), 0),
        _defaulted("lines_consumed", sa.BigInteger(), 0),
        _defaulted("done", sa.Boolean(), False),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("producer_id", "file_name"),
    )

    op.create_table(
        "trajectory_gc_queue",
        sa.Column("id", BIGINT_ID, nullable=False, autoincrement=True),
        _string("kind", 16),
        sa.Column("storage_key", sa.Text(), nullable=False),
        _string("reason", 64),
        _defaulted("attempts", sa.Integer(), 0),
        _timestamp("next_attempt_at"),
        sa.Column("last_error", sa.Text(), nullable=True),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_trajectory_gc_queue_next_attempt", "trajectory_gc_queue", ["next_attempt_at"])

    op.create_table(
        "trajectory_worker_state",
        _string("key", 64),
        sa.Column("value", JSONType(), nullable=False),
        _timestamp("updated_at"),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "trajectory_audit_outbox",
        sa.Column("id", BIGINT_ID, nullable=False, autoincrement=True),
        sa.Column("payload", JSONType(), nullable=False),
        _defaulted("attempts", sa.Integer(), 0),
        _timestamp("next_attempt_at"),
        _timestamp("created_at"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    # Dropping trajectory_events also drops every partition on PostgreSQL.
    # pg_trgm stays installed: extensions are database-wide and may be shared.
    for table in reversed(TABLES):
        op.drop_table(table)
