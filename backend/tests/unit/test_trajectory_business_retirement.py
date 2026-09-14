"""Retirement of the business-database trajectory tables (SPEC §6.9) and the producer import boundary."""
import ast
import importlib
import os
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

BACKEND = Path(__file__).resolve().parents[2]
#: Not business code: the trajectory package, tests, developer scripts and
#: migrations; the admin read API and its app wiring move into the trajectory
#: worker (w2-service); db/models/trajectory.py is the retired mapping itself.
_SKIPPED_DIRECTORIES = {".venv", "node_modules", "__pycache__", "tests", "trajectory", "scripts", "migrations"}
_SKIPPED_FILES = {"api/admin_trajectories.py", "api/admin_trajectory_ws.py", "main.py", "db/models/trajectory.py"}
_TRACE_STORAGE_MODULES = ("db.models.trajectory", "trajectory.payload", "trajectory.repository",
                          "trajectory.lifecycle", "trajectory.export", "trajectory.store", "trajectory.worker",
                          "trajectory.storage")
#: Removed in-transaction recorder helpers and recording-only asset downloads.
_RETIRED_NAMES = {"read_asset_bytes", "prepare_asset_ids", "retain_request_media_in_tx", "ensure_trajectory_in_tx",
                  "append_events_in_tx", "mark_capture_paused_in_tx", "delete_trajectory_in_tx",
                  "prepare_trajectory_assets", "prepare_trajectory_baseline_assets",
                  "capture_trajectory_baseline_in_tx", "PendingRange"}


def _business_modules():
    for directory, subdirectories, files in os.walk(BACKEND):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _SKIPPED_DIRECTORIES)
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = Path(directory) / name
            relative = path.relative_to(BACKEND).as_posix()
            if relative not in _SKIPPED_FILES:
                yield relative, ast.parse(path.read_text(encoding="utf-8"), filename=relative)


def _is_trace_storage(module: str) -> bool:
    return any(module == forbidden or module.startswith(forbidden + ".") for forbidden in _TRACE_STORAGE_MODULES)


def test_business_code_imports_no_trajectory_storage_and_no_recording_only_downloads():
    violations = []
    scanned = 0
    for relative, tree in _business_modules():
        scanned += 1
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                violations += [f"{relative}: import {alias.name}" for alias in node.names
                               if _is_trace_storage(alias.name)]
            elif isinstance(node, ast.ImportFrom) and node.module:
                if _is_trace_storage(node.module) or any(
                        _is_trace_storage(f"{node.module}.{alias.name}") for alias in node.names):
                    violations.append(f"{relative}: from {node.module} import ...")
                violations += [f"{relative}: imports {alias.name}" for alias in node.names
                               if alias.name in _RETIRED_NAMES]
            elif isinstance(node, ast.Name) and node.id in _RETIRED_NAMES:
                violations.append(f"{relative}: uses {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _RETIRED_NAMES:
                violations.append(f"{relative}: uses .{node.attr}")
    assert scanned > 200  # The walk really covers the business packages.
    assert violations == []


def test_business_metadata_and_readiness_hold_no_trajectory_tables():
    import db.models  # noqa: F401
    from db.base import _READINESS_SCHEMA, Base
    assert not [name for name in Base.metadata.tables if "trajector" in name]
    assert not [name for name in _READINESS_SCHEMA if "trajector" in name]
    # The identity carriers stay in the business schema.
    assert "trace_context" in _READINESS_SCHEMA["session_executions"]
    assert "trace_context" in _READINESS_SCHEMA["cron_runs"]
    for table in ("sessions", "users", "workspaces"):
        assert f"ix_{table}_updated_id" in {index.name for index in Base.metadata.tables[table].indexes}


def _legacy_tables(connection) -> None:
    from db.models.trajectory import LegacyTrajectoryBase, SessionTrajectory
    LegacyTrajectoryBase.metadata.create_all(connection)
    moment = datetime.now(timezone.utc)
    connection.execute(sa.insert(SessionTrajectory.__table__).values(
        id="trj_kept", user_id="u1", session_id="s1", workspace_id="w1", started_at=moment, updated_at=moment,
        next_seq=1, committed_seq=0, projected_seq=0, schema_version=1, recording_status="recording"))


def test_the_retirement_migration_renames_the_tables_with_their_rows_and_back(tmp_path):
    from db.base import LEGACY_TABLE_NAMES
    retirement = importlib.import_module("db.migrations.versions.d3b5f7a9c1e2_retire_session_trajectories")
    assert dict(retirement.TABLES) == LEGACY_TABLE_NAMES
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'business.db'}")
    with engine.begin() as connection:
        _legacy_tables(connection)
        with Operations.context(MigrationContext.configure(connection)):
            retirement.upgrade()
            retirement.upgrade()  # Nothing left to rename the second time.
    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert set(LEGACY_TABLE_NAMES.values()) <= tables and not set(LEGACY_TABLE_NAMES) & tables
        assert connection.exec_driver_sql("SELECT id FROM legacy_trajectory_sessions").scalars().all() == ["trj_kept"]
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            retirement.downgrade()
    with engine.connect() as connection:
        tables = set(sa.inspect(connection).get_table_names())
        assert set(LEGACY_TABLE_NAMES) <= tables and not set(LEGACY_TABLE_NAMES.values()) & tables
        assert connection.exec_driver_sql("SELECT id FROM session_trajectories").scalars().all() == ["trj_kept"]
    engine.dispose()


def test_desktop_databases_retire_the_tables_and_index_the_sync_cursors_repeatably():
    from db.base import LEGACY_TABLE_NAMES, _index_desktop_metadata_sync, _retire_desktop_trajectory_tables
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        _legacy_tables(connection)
        for table in ("sessions", "users", "workspaces"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id VARCHAR PRIMARY KEY, updated_at DATETIME)")
        for _ in range(2):
            _retire_desktop_trajectory_tables(connection)
            _index_desktop_metadata_sync(connection)
        inspector = sa.inspect(connection)
        assert set(LEGACY_TABLE_NAMES.values()) <= set(inspector.get_table_names())
        assert not set(LEGACY_TABLE_NAMES) & set(inspector.get_table_names())
        for table in ("sessions", "users", "workspaces"):
            assert f"ix_{table}_updated_id" in {index["name"] for index in inspector.get_indexes(table)}
        assert connection.exec_driver_sql("SELECT count(*) FROM legacy_trajectory_sessions").scalar_one() == 1
    engine.dispose()


def test_sink_defaults_to_the_spool_and_db_leaves_no_emitter(monkeypatch, tmp_path):
    from trajectory import config
    from trajectory.emitter import get_emitter, reset_emitter_for_tests
    reset_emitter_for_tests()
    monkeypatch.delenv("TRAJECTORY_SINK", raising=False)
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    assert config.sink() == "spool"
    monkeypatch.setenv("TRAJECTORY_SINK", "not-a-sink")
    assert config.sink() == "spool"
    monkeypatch.setenv("TRAJECTORY_SINK", "db")
    assert get_emitter() is None and not (tmp_path / "spool").exists()
    reset_emitter_for_tests()
