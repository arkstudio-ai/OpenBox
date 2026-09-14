"""Trace alembic chain (SPEC 6.2, 6.3): one head, migrated SQLite schema equals the models, reversible.

PostgreSQL-only behaviour runs against a real server in tests/integration/test_trace_pg_schema.py.
"""
import io
import re
import warnings
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from trajectory.store.database import TraceBase
import trajectory.store.models  # noqa: F401

BACKEND = Path(__file__).resolve().parents[2]
MIGRATIONS = BACKEND / "trajectory" / "store" / "migrations"
VERSION_TABLE = "trajectory_alembic_version"

PRIMARY_KEYS = {
    "session_trajectories": ["id"],
    "trajectory_events": ["trajectory_id", "seq"],
    "trajectory_event_keys": ["event_id"],
    "trajectory_segments": ["trajectory_id", "from_seq"],
    "trajectory_payloads": ["payload_id"],
    "trajectory_records": ["trajectory_id", "record_id"],
    "trajectory_record_events": ["trajectory_id", "record_id", "seq"],
    "trajectory_session_summaries": ["trajectory_id"],
    "trajectory_checkpoints": ["trajectory_id", "through_seq"],
    "trajectory_exports": ["id"],
    "trajectory_meta_sessions": ["id"],
    "trajectory_meta_users": ["id"],
    "trajectory_meta_workspaces": ["id"],
    "trajectory_meta_assets": ["id"],
    "trajectory_ingest_producers": ["producer_id"],
    "trajectory_ingest_files": ["producer_id", "file_name"],
    "trajectory_gc_queue": ["id"],
    "trajectory_worker_state": ["key"],
    "trajectory_audit_outbox": ["id"],
}
INDEXES = {
    "session_trajectories": {("last_activity_at",)},
    "trajectory_events": {("trajectory_id", "seq"), ("trajectory_id", "request_id", "seq"),
                          ("trajectory_id", "call_id", "seq")},
    "trajectory_event_keys": {("recorded_at",)},
    "trajectory_segments": {("trajectory_id", "to_seq")},
    "trajectory_payloads": {("trajectory_id", "sha256"), ("source_asset_id",)},
    "trajectory_records": {("trajectory_id", "start_seq", "record_id"), ("trajectory_id", "message_id"),
                           ("trajectory_id", "kind", "status", "start_seq"), ("trajectory_id", "agent_id")},
    "trajectory_session_summaries": {("last_activity_at", "session_id"), ("user_id", "last_activity_at", "session_id"),
                                     ("workspace_id", "last_activity_at", "session_id"),
                                     ("running_status", "recording_status", "last_activity_at")},
    "trajectory_exports": {("trajectory_id",)},
    "trajectory_meta_sessions": {("updated_at", "id"), ("user_id", "updated_at"), ("workspace_id", "updated_at"),
                                 ("parent_id",)},
    "trajectory_meta_assets": {("session_id",)},
    "trajectory_gc_queue": {("next_attempt_at",)},
}
UNIQUES = {
    "session_trajectories": {("session_id",), ("user_id", "session_id")},
    "trajectory_payloads": {("trajectory_id", "dedupe_key")},
}
CASCADE_CHILDREN = {"trajectory_events", "trajectory_segments", "trajectory_payloads", "trajectory_records",
                    "trajectory_session_summaries", "trajectory_checkpoints", "trajectory_exports"}


def _config(**kwargs) -> Config:
    config = Config(str(BACKEND / "alembic_trajectory.ini"), **kwargs)
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


def _upgrade(path: Path, monkeypatch) -> Config:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", f"sqlite+aiosqlite:///{path}")
    config = _config()
    command.upgrade(config, "head")
    return config


def _schema(path: Path) -> dict:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        inspector = sa.inspect(engine)
        schema = {}
        for table in inspector.get_table_names():
            if table == VERSION_TABLE:
                continue
            schema[table] = {
                "columns": {column["name"]: (str(column["type"]), column["nullable"], column["default"])
                            for column in inspector.get_columns(table)},
                "pk": inspector.get_pk_constraint(table)["constrained_columns"],
                "fks": sorted((tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]),
                               fk["options"].get("ondelete")) for fk in inspector.get_foreign_keys(table)),
                "uniques": sorted((unique["name"], tuple(unique["column_names"]))
                                  for unique in inspector.get_unique_constraints(table)),
                "indexes": sorted((index["name"], tuple(index["column_names"]), bool(index["unique"]))
                                  for index in inspector.get_indexes(table)),
            }
        return schema
    finally:
        engine.dispose()


def _table_names(path: Path) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return set(sa.inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_single_head_and_unique_revision_ids():
    ids: dict[str, list[str]] = {}
    for path in (MIGRATIONS / "versions").glob("*.py"):
        m = re.search(r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([0-9A-Za-z_]+)["\']', path.read_text(encoding="utf-8"), re.M)
        assert m, f"{path.name} has no revision"
        ids.setdefault(m.group(1), []).append(path.name)
    dupes = {k: v for k, v in ids.items() if len(v) > 1}
    assert not dupes, f"duplicate revision ids: {dupes}"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # alembic warns on duplicates; make it fatal here
        script = ScriptDirectory.from_config(_config())
        heads = script.get_heads()
    assert len(heads) == 1, f"expected one trace alembic head, found {heads}"
    assert script.get_bases() == ["t0001_initial"]
    business = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    assert not {revision.revision for revision in business.walk_revisions()} & set(ids)


def test_sqlite_upgrade_head_matches_models(tmp_path, monkeypatch):
    _upgrade(tmp_path / "migrated.db", monkeypatch)
    modeled = sa.create_engine(f"sqlite:///{tmp_path / 'modeled.db'}")
    TraceBase.metadata.create_all(modeled)
    modeled.dispose()

    migrated = _schema(tmp_path / "migrated.db")
    assert set(migrated) == set(PRIMARY_KEYS) == set(TraceBase.metadata.tables)
    assert migrated == _schema(tmp_path / "modeled.db")
    assert "alembic_version" not in _table_names(tmp_path / "migrated.db")


def test_sqlite_schema_has_spec_keys_indexes_and_cascades(tmp_path, monkeypatch):
    path = tmp_path / "trace.db"
    _upgrade(path, monkeypatch)
    schema = _schema(path)
    for table, shape in schema.items():
        assert shape["pk"] == PRIMARY_KEYS[table], table
        assert {columns for _, columns, unique in shape["indexes"] if not unique} == INDEXES.get(table, set()), table
        assert {columns for _, columns in shape["uniques"]} == UNIQUES.get(table, set()), table
    assert {table for table, shape in schema.items() if shape["fks"]} == CASCADE_CHILDREN
    for table in CASCADE_CHILDREN:
        assert schema[table]["fks"] == [(("trajectory_id",), "session_trajectories", ("id",), "CASCADE")], table
    assert schema["trajectory_records"]["columns"]["search_doc"][:2] == ("TEXT", False)
    assert schema["trajectory_events"]["columns"]["recorded_on"][:2] == ("DATE", False)


def test_migrated_defaults_and_autoincrement_ids(tmp_path, monkeypatch):
    path = tmp_path / "trace.db"
    _upgrade(path, monkeypatch)
    at = datetime.now(timezone.utc).isoformat()
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO session_trajectories (id, user_id, session_id, workspace_id, started_at, updated_at, "
            "last_activity_at) VALUES ('trj_a', 'user_a', 'session_a', 'ws_a', ?, ?, ?)", (at, at, at))
        row = connection.exec_driver_sql(
            "SELECT next_seq, committed_seq, projected_seq, archived_seq, checkpoint_seq, schema_version, "
            "recording_status, recording_epoch, event_count, stored_bytes, budget_level FROM session_trajectories").one()
        assert tuple(row) == (1, 0, 0, 0, 0, 2, "recording", 0, 0, 0, "normal")
        for _ in range(2):
            connection.exec_driver_sql(
                "INSERT INTO trajectory_gc_queue (kind, storage_key, reason, next_attempt_at, created_at) "
                "VALUES ('key', 'trajectories/trj_a/', 'deleted', ?, ?)", (at, at))
        assert connection.exec_driver_sql("SELECT id, attempts FROM trajectory_gc_queue ORDER BY id").all() == [(1, 0), (2, 0)]
        connection.exec_driver_sql("INSERT INTO trajectory_ingest_producers (producer_id, last_seen_at) VALUES ('p', ?)", (at,))
        assert tuple(connection.exec_driver_sql(
            "SELECT last_n, goodbye, abandoned FROM trajectory_ingest_producers").one()) == (0, 0, 0)
    engine.dispose()


def test_sqlite_downgrade_base_leaves_no_tables_and_upgrades_again(tmp_path, monkeypatch):
    path = tmp_path / "trace.db"
    config = _upgrade(path, monkeypatch)
    command.downgrade(config, "base")
    assert _table_names(path) == {VERSION_TABLE}
    engine = sa.create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql(f"SELECT count(*) FROM {VERSION_TABLE}").scalar_one() == 0
    engine.dispose()
    command.upgrade(config, "head")
    assert _table_names(path) == set(PRIMARY_KEYS) | {VERSION_TABLE}


def test_autogenerate_finds_no_drift_on_sqlite(tmp_path, monkeypatch):
    # `alembic check` runs env.py in autogenerate mode. A migrated SQLite database must match the models, so a
    # future autogenerated revision cannot pick up the PostgreSQL-only trigram index as a plain SQLite index.
    config = _upgrade(tmp_path / "trace.db", monkeypatch)
    command.check(config)


def test_offline_sql_carries_postgresql_only_ddl(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", "postgresql+asyncpg://trace:secret@localhost:5432/openbox_trace")
    buffer = io.StringIO()
    command.upgrade(_config(output_buffer=buffer), "head", sql=True)
    postgresql = buffer.getvalue()
    for fragment in ("CREATE EXTENSION IF NOT EXISTS pg_trgm", "PRIMARY KEY (trajectory_id, seq, recorded_on)",
                     "PARTITION BY RANGE (recorded_on)",
                     "CREATE TABLE trajectory_events_default PARTITION OF trajectory_events DEFAULT",
                     "ON trajectory_records USING gin (search_doc gin_trgm_ops)", f"CREATE TABLE {VERSION_TABLE}",
                     f"INSERT INTO {VERSION_TABLE} (version_num) VALUES ('t0001_initial')"):
        assert fragment in postgresql, fragment

    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", "sqlite+aiosqlite:///offline-trace.db")
    buffer = io.StringIO()
    command.upgrade(_config(output_buffer=buffer), "head", sql=True)
    sqlite = buffer.getvalue()
    assert "PRIMARY KEY (trajectory_id, seq)," in sqlite
    for fragment in ("PARTITION", "pg_trgm", "gin_trgm_ops", "EXTENSION"):
        assert fragment not in sqlite, fragment
