"""Trace database schema (SPEC 6.3). Nothing in ``db.models`` imports this module.

Ids are ``String(64)`` unless noted, JSON columns are ``JSONType`` (JSONB on
PostgreSQL) and timestamps are timezone-aware. The PostgreSQL-only DDL is
attached to the metadata, so ``create_all`` builds the same schema as
migration ``t0001_initial``: the ``pg_trgm`` extension, ``trajectory_events``
range-partitioned by ``recorded_on`` with a default partition (daily
partitions come from ``trajectory.store.partitions``), and the trigram index
on ``trajectory_records.search_doc``. SQLite gets plain tables.
"""
from datetime import date, datetime

from sqlalchemy import (DDL, BigInteger, Boolean, Date, ForeignKey, Index, Integer, PrimaryKeyConstraint,
    String, Text, UniqueConstraint, event, false, text, true)
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Mapped, mapped_column

from trajectory.store.database import TraceBase
from trajectory.store.partitions import DEFAULT_PARTITION, EVENTS_TABLE

#: Autoincrementing id: BIGSERIAL on PostgreSQL, a rowid alias on SQLite.
BIGINT_ID = BigInteger().with_variant(Integer(), "sqlite")


def _defaulted(type_, value):
    """NOT NULL column with the same default for ORM inserts, Core inserts and raw SQL."""
    if isinstance(value, bool):
        server_default = true() if value else false()
    elif isinstance(value, str):
        server_default = text(f"'{value}'")
    else:
        server_default = text(str(value))
    return mapped_column(type_, nullable=False, default=value, server_default=server_default)


def _trajectory_fk():
    return ForeignKey("session_trajectories.id", ondelete="CASCADE")


class PartitionedPrimaryKey(PrimaryKeyConstraint):
    """Primary key that also covers the partition key on PostgreSQL.

    A partitioned PostgreSQL table can only enforce uniqueness that includes
    its partition key, so ``trajectory_events`` is keyed
    ``(trajectory_id, seq, recorded_on)`` there and ``(trajectory_id, seq)`` on
    SQLite. The ORM identity is ``(trajectory_id, seq)`` on both.
    """

    def __init__(self, *columns, partition_columns: tuple[str, ...] = (), **kw):
        super().__init__(*columns, **kw)
        self.partition_columns = tuple(partition_columns)

    def _copy(self, **kw):
        copied = super()._copy(**kw)
        copied.partition_columns = self.partition_columns
        return copied


@compiles(PartitionedPrimaryKey, "postgresql")
def _compile_partitioned_primary_key(constraint, compiler, **kw) -> str:
    preparer = compiler.preparer
    names = [column.name for column in constraint.columns] + list(constraint.partition_columns)
    prefix = f"CONSTRAINT {preparer.format_constraint(constraint)} " if constraint.name is not None else ""
    columns = ", ".join(preparer.quote(name) for name in names)
    return f"{prefix}PRIMARY KEY ({columns}){compiler.define_constraint_deferrability(constraint)}"


class SessionTrajectory(TraceBase):
    __tablename__ = "session_trajectories"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # ingest.session_trajectory_id(session_id)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)  # root session
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(nullable=False)  # max occurred_at ingested
    next_seq: Mapped[int] = _defaulted(BigInteger, 1)
    committed_seq: Mapped[int] = _defaulted(BigInteger, 0)
    projected_seq: Mapped[int] = _defaulted(BigInteger, 0)
    archived_seq: Mapped[int] = _defaulted(BigInteger, 0)
    checkpoint_seq: Mapped[int] = _defaulted(BigInteger, 0)  # through_seq of the latest checkpoint
    schema_version: Mapped[int] = _defaulted(Integer, 2)
    recording_status: Mapped[str] = _defaulted(String(32), "recording")  # recording|gap|paused|deleted|expired
    # Epoch of the last applied resume; producers name periods by millisecond epochs (t0002: BIGINT).
    recording_epoch: Mapped[int] = _defaulted(BigInteger().with_variant(Integer(), "sqlite"), 0)
    event_count: Mapped[int] = _defaulted(BigInteger, 0)
    stored_bytes: Mapped[int] = _defaulted(BigInteger, 0)  # compressed bytes in blobs + segments
    budget_level: Mapped[str] = _defaulted(String(16), "normal")  # normal|degraded|blocked
    budget_reason: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column()
    content_expired_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_session_trajectories_session"),
        UniqueConstraint("user_id", "session_id", name="uq_session_trajectories_owner_session"),
        Index("ix_session_trajectories_last_activity", "last_activity_at"),
    )


class TrajectoryEvent(TraceBase):
    """Hot, not yet archived events; archived ranges live in object-storage segments."""
    __tablename__ = EVENTS_TABLE
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_on: Mapped[date] = mapped_column(Date, nullable=False)  # partition key: UTC day of recorded_at
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(128))
    call_id: Mapped[str | None] = mapped_column(String(128))
    agent_id: Mapped[str | None] = mapped_column(String(128))
    context: Mapped[dict] = mapped_column(nullable=False)  # non-null identity fields
    data: Mapped[dict] = mapped_column(nullable=False)  # as recorded; externalized values replaced by references
    hints: Mapped[dict | None] = mapped_column()  # worker-only {"preview": {field: text}}
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        PartitionedPrimaryKey("trajectory_id", "seq", partition_columns=("recorded_on",)),
        # The only secondary index: every hot row is written and, after archival, deleted again with its index
        # entries. t0003 dropped (trajectory_id, seq), a prefix of the primary key, and (trajectory_id, call_id,
        # seq), which no reader used.
        Index("ix_trajectory_events_request", "trajectory_id", "request_id", "seq"),
        {"postgresql_partition_by": "RANGE (recorded_on)"},
    )


class TrajectoryEventKey(TraceBase):
    """Idempotency registry; outlives the archived event row."""
    __tablename__ = "trajectory_event_keys"
    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_trajectory_event_keys_recorded_at", "recorded_at"),)


class TrajectorySegment(TraceBase):
    __tablename__ = "trajectory_segments"
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), primary_key=True)
    from_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    to_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    stored_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)  # of the uncompressed JSONL
    compression: Mapped[str] = _defaulted(String(16), "zstd")
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_trajectory_segments_to_seq", "trajectory_id", "to_seq"),)


class TrajectoryPayload(TraceBase):
    """Per-trajectory content references; ``payload_id`` is the identity used by ``$payload``/``$media``."""
    __tablename__ = "trajectory_payloads"
    payload_id: Mapped[str] = mapped_column(String(64), primary_key=True)  # pld_ + uuid hex
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))  # null for asset references without a known hash
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)  # uncompressed
    stored_bytes: Mapped[int] = _defaulted(BigInteger, 0)  # 0 for asset references
    media_type: Mapped[str] = mapped_column(String(128), nullable=False)
    encoding: Mapped[str] = _defaulted(String(16), "identity")  # identity|zstd
    storage_kind: Mapped[str] = mapped_column(String(16), nullable=False)  # blob|asset
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)  # blob key, or the asset oss_key
    source_asset_id: Mapped[str | None] = mapped_column(String(64))
    availability: Mapped[str] = _defaulted(String(24), "available")  # available|deleted|expired
    first_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)  # visibility lower bound
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column()
    __table_args__ = (
        UniqueConstraint("trajectory_id", "dedupe_key", name="uq_trajectory_payloads_dedupe"),
        Index("ix_trajectory_payloads_digest", "trajectory_id", "sha256"),
        Index("ix_trajectory_payloads_source_asset", "source_asset_id"),
    )


class TrajectoryRecord(TraceBase):
    __tablename__ = "trajectory_records"
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(String(128))
    message_id: Mapped[str | None] = mapped_column(String(128))
    start_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_seq: Mapped[int | None] = mapped_column(BigInteger)
    applied_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    projector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    data: Mapped[dict] = mapped_column(nullable=False)  # large values stored as $ref
    summary: Mapped[dict] = mapped_column(nullable=False)  # the record without data and blocks
    search_doc: Mapped[str] = mapped_column(Text, nullable=False)  # at most 4000 characters
    __table_args__ = (
        Index("ix_trajectory_records_order", "trajectory_id", "start_seq", "record_id"),
        Index("ix_trajectory_records_message", "trajectory_id", "message_id"),
        Index("ix_trajectory_records_filter", "trajectory_id", "kind", "status", "start_seq"),
        Index("ix_trajectory_records_agent", "trajectory_id", "agent_id"),
        Index("ix_trajectory_records_search_trgm", "search_doc", postgresql_using="gin",
              postgresql_ops={"search_doc": "gin_trgm_ops"}).ddl_if(dialect="postgresql"),
    )


class TrajectoryRecordEvent(TraceBase):
    """One row per (event, target record) pair written by the projector, implicit targets included."""
    __tablename__ = "trajectory_record_events"
    trajectory_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_id: Mapped[str] = mapped_column(String(256), primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)


class TrajectorySessionSummary(TraceBase):
    __tablename__ = "trajectory_session_summaries"
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_activity_at: Mapped[datetime] = mapped_column(nullable=False)
    running_status: Mapped[str] = mapped_column(String(32), nullable=False)
    recording_status: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str | None] = mapped_column(String(128))
    applied_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statistics: Mapped[dict] = mapped_column(nullable=False)
    __table_args__ = (
        Index("ix_trajectory_session_summaries_activity", "last_activity_at", "session_id"),
        Index("ix_trajectory_session_summaries_owner", "user_id", "last_activity_at", "session_id"),
        Index("ix_trajectory_session_summaries_workspace", "workspace_id", "last_activity_at", "session_id"),
        Index("ix_trajectory_session_summaries_status", "running_status", "recording_status", "last_activity_at"),
    )


class TrajectoryCheckpoint(TraceBase):
    __tablename__ = "trajectory_checkpoints"
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), primary_key=True)
    through_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    projector_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[dict] = mapped_column(nullable=False)  # record_pages hold payload references
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryExport(TraceBase):
    __tablename__ = "trajectory_exports"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trajectory_id: Mapped[str] = mapped_column(String(64), _trajectory_fk(), nullable=False)
    viewer_id: Mapped[str] = mapped_column(String(64), nullable=False)
    through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column()
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    __table_args__ = (Index("ix_trajectory_exports_trajectory", "trajectory_id"),)


# Metadata replicas, fed by *.meta controls (last writer wins by updated_at).
# Only identity and ordering columns are required: a sparse control must not
# fail an ingest batch. Text widths are at least the business column widths.

class TrajectoryMetaSession(TraceBase):
    __tablename__ = "trajectory_meta_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(64))
    parent_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(128))
    agent: Mapped[str | None] = mapped_column(String(64))
    is_deleted: Mapped[bool] = _defaulted(Boolean, False)
    deleted_at: Mapped[datetime | None] = mapped_column()
    created_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    synced_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        Index("ix_trajectory_meta_sessions_updated", "updated_at", "id"),
        Index("ix_trajectory_meta_sessions_owner", "user_id", "updated_at"),
        Index("ix_trajectory_meta_sessions_workspace", "workspace_id", "updated_at"),
        Index("ix_trajectory_meta_sessions_parent", "parent_id"),
    )


class TrajectoryMetaUser(TraceBase):
    __tablename__ = "trajectory_meta_users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))  # users.email width
    role: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = _defaulted(Boolean, True)
    is_deleted: Mapped[bool] = _defaulted(Boolean, False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    synced_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryMetaWorkspace(TraceBase):
    __tablename__ = "trajectory_meta_workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128))  # workspaces.name width
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    synced_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryMetaAsset(TraceBase):
    __tablename__ = "trajectory_meta_assets"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str | None] = mapped_column(String(64))
    session_id: Mapped[str | None] = mapped_column(String(64))
    oss_key: Mapped[str | None] = mapped_column(Text)
    mime: Mapped[str | None] = mapped_column(String(128))
    size: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[str | None] = mapped_column(String(32))
    is_deleted: Mapped[bool] = _defaulted(Boolean, False)
    deleted_at: Mapped[datetime | None] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    synced_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (Index("ix_trajectory_meta_assets_session", "session_id"),)


# Worker bookkeeping.

class TrajectoryIngestProducer(TraceBase):
    __tablename__ = "trajectory_ingest_producers"
    producer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    hostname: Mapped[str | None] = mapped_column(String(64))
    pid: Mapped[int | None] = mapped_column(Integer)
    boot_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[datetime | None] = mapped_column()
    last_n: Mapped[int] = _defaulted(BigInteger, 0)
    last_seen_at: Mapped[datetime] = mapped_column(nullable=False)
    goodbye: Mapped[bool] = _defaulted(Boolean, False)
    abandoned: Mapped[bool] = _defaulted(Boolean, False)


class TrajectoryIngestFile(TraceBase):
    __tablename__ = "trajectory_ingest_files"
    producer_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    file_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    bytes_consumed: Mapped[int] = _defaulted(BigInteger, 0)
    lines_consumed: Mapped[int] = _defaulted(BigInteger, 0)
    done: Mapped[bool] = _defaulted(Boolean, False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryGcQueue(TraceBase):
    __tablename__ = "trajectory_gc_queue"
    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # key|prefix
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = _defaulted(Integer, 0)
    next_attempt_at: Mapped[datetime] = mapped_column(nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    __table_args__ = (
        Index("ix_trajectory_gc_queue_next_attempt", "next_attempt_at"),
        # Ingest probes the queue by key for every batch with uploads, and so does the orphan sweep (t0003).
        Index("ix_trajectory_gc_queue_storage_key", "storage_key"),
    )


class TrajectoryWorkerState(TraceBase):
    __tablename__ = "trajectory_worker_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)


class TrajectoryAuditOutbox(TraceBase):
    __tablename__ = "trajectory_audit_outbox"
    id: Mapped[int] = mapped_column(BIGINT_ID, primary_key=True, autoincrement=True)
    payload: Mapped[dict] = mapped_column(nullable=False)
    attempts: Mapped[int] = _defaulted(Integer, 0)
    next_attempt_at: Mapped[datetime] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(nullable=False)


event.listen(TraceBase.metadata, "before_create",
             DDL("CREATE EXTENSION IF NOT EXISTS pg_trgm").execute_if(dialect="postgresql"))
event.listen(TrajectoryEvent.__table__, "after_create",
             DDL(f"CREATE TABLE {DEFAULT_PARTITION} PARTITION OF {EVENTS_TABLE} DEFAULT").execute_if(dialect="postgresql"))
