"""Trace models: separate metadata, SPEC 6.3 column types and widths, dialect-specific DDL."""
import re
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

from db.base import Base, JSONType
from trajectory.store import models
from trajectory.store.database import TraceBase

BACKEND = Path(__file__).resolve().parents[2]


def test_trace_metadata_is_separate_from_business_metadata():
    assert TraceBase.metadata is not Base.metadata
    for name, table in TraceBase.metadata.tables.items():
        assert Base.metadata.tables.get(name) is not table
    importers = [path for path in (BACKEND / "db").rglob("*.py")
                 if re.search(r"^\s*(from|import)\s+trajectory\.store", path.read_text(encoding="utf-8"), re.M)]
    assert importers == []
    store = BACKEND / "trajectory" / "store"
    business_imports = [path for path in store.rglob("*.py")
                        if re.search(r"^\s*(from|import)\s+db\.(models|migrations)", path.read_text(encoding="utf-8"), re.M)]
    assert business_imports == []


def test_json_and_timezone_aware_columns():
    assert TraceBase.type_annotation_map[dict] is JSONType
    assert TraceBase.type_annotation_map[datetime].timezone is True
    json_columns = set()
    for table in TraceBase.metadata.tables.values():
        for column in table.columns:
            if isinstance(column.type, sa.DateTime):
                assert column.type.timezone, f"{table.name}.{column.name}"
            if isinstance(column.type, JSONType):
                json_columns.add(f"{table.name}.{column.name}")
    assert json_columns == {
        "trajectory_events.context", "trajectory_events.data", "trajectory_events.hints",
        "trajectory_records.data", "trajectory_records.summary", "trajectory_session_summaries.statistics",
        "trajectory_checkpoints.state", "trajectory_worker_state.value", "trajectory_audit_outbox.payload",
    }


def test_column_widths_follow_spec_and_business_replicas():
    tables = TraceBase.metadata.tables
    widths = {
        ("session_trajectories", "id"): 64, ("session_trajectories", "budget_level"): 16,
        ("trajectory_events", "event_id"): 128, ("trajectory_events", "type"): 64,
        ("trajectory_events", "request_id"): 128, ("trajectory_event_keys", "event_id"): 128,
        ("trajectory_payloads", "media_type"): 128, ("trajectory_payloads", "availability"): 24,
        ("trajectory_records", "record_id"): 256, ("trajectory_record_events", "record_id"): 256,
        ("trajectory_meta_users", "email"): 255, ("trajectory_meta_workspaces", "name"): 128,
        ("trajectory_ingest_producers", "producer_id"): 128, ("trajectory_ingest_files", "file_name"): 64,
    }
    for (table, column), length in widths.items():
        assert tables[table].c[column].type.length == length, (table, column)
    for table, column in (("trajectory_segments", "storage_key"), ("trajectory_records", "search_doc"),
                          ("trajectory_meta_sessions", "title"), ("trajectory_meta_assets", "oss_key")):
        assert isinstance(tables[table].c[column].type, sa.Text), (table, column)


def test_events_primary_key_includes_partition_key_only_on_postgresql():
    table = models.TrajectoryEvent.__table__
    pg = str(CreateTable(table).compile(dialect=postgresql.dialect()))
    lite = str(CreateTable(table).compile(dialect=sqlite.dialect()))
    assert "PRIMARY KEY (trajectory_id, seq, recorded_on)" in pg
    assert "PARTITION BY RANGE (recorded_on)" in pg
    assert "PRIMARY KEY (trajectory_id, seq)," in lite and "PARTITION" not in lite
    assert [column.name for column in sa.inspect(models.TrajectoryEvent).primary_key] == ["trajectory_id", "seq"]
    copy = sa.MetaData()
    models.SessionTrajectory.__table__.to_metadata(copy)
    copied = table.to_metadata(copy)
    assert "PRIMARY KEY (trajectory_id, seq, recorded_on)" in str(CreateTable(copied).compile(dialect=postgresql.dialect()))


def test_postgresql_only_objects_are_skipped_on_sqlite():
    index = next(index for index in models.TrajectoryRecord.__table__.indexes
                 if index.name == "ix_trajectory_records_search_trgm")
    assert "USING gin (search_doc gin_trgm_ops)" in str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "BIGSERIAL" in str(CreateTable(models.TrajectoryGcQueue.__table__).compile(dialect=postgresql.dialect()))

    engine = sa.create_engine("sqlite://")
    TraceBase.metadata.create_all(engine)
    inspector = sa.inspect(engine)
    assert set(inspector.get_table_names()) == set(TraceBase.metadata.tables)
    names = {index["name"] for index in inspector.get_indexes("trajectory_records")}
    assert "ix_trajectory_records_order" in names and "ix_trajectory_records_search_trgm" not in names
    at = datetime.now(timezone.utc)
    queue = models.TrajectoryGcQueue.__table__
    with engine.begin() as connection:
        for key in ("a", "b"):
            connection.execute(queue.insert().values(kind="key", storage_key=key, reason="deleted",
                                                     next_attempt_at=at, created_at=at))
        assert connection.execute(sa.select(queue.c.id, queue.c.attempts).order_by(queue.c.id)).all() == [(1, 0), (2, 0)]
    engine.dispose()
