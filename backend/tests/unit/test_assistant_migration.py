"""Old data keeps its audience; downgrade cannot disclose assistant histories."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text

from db.base import _upgrade_desktop_assistant_columns


def legacy_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as connection:
        for name in ("users", "workspaces", "projects"):
            connection.exec_driver_sql(f"CREATE TABLE {name} (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql(
            "CREATE TABLE sessions (id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64), "
            "workspace_id VARCHAR(64), kind VARCHAR(16) DEFAULT 'normal', is_deleted BOOLEAN DEFAULT 0)"
        )
        connection.exec_driver_sql("CREATE TABLE agent_inbox_items (id VARCHAR(64) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO sessions (id,user_id,workspace_id) VALUES ('legacy','u','w')")
        connection.exec_driver_sql("INSERT INTO agent_inbox_items (id) VALUES ('old-input')")
    return engine


def assert_legacy(connection):
    assert connection.execute(text("SELECT visibility, memory_policy FROM sessions WHERE id='legacy'")).one() == (
        "workspace", "standard")
    assert connection.execute(text("SELECT origin, origin_ref FROM agent_inbox_items WHERE id='old-input'")).one() == (
        "unknown", "{}")


def test_alembic_upgrade_preserves_legacy_and_refuses_privacy_losing_downgrade(tmp_path):
    migration = importlib.import_module("db.migrations.versions.pa1b2c3d4e5f_personal_assistant")
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert_legacy(connection)
            assert {"assistant_tasks", "assistant_commands", "assistant_task_submissions", "assistant_task_results",
                    "assistant_read_cursors"} <= set(inspect(connection).get_table_names())
            connection.exec_driver_sql("UPDATE sessions SET visibility='private' WHERE id='legacy'")
            with pytest.raises(RuntimeError, match="Private assistant sessions"):
                migration.downgrade()
            assert "visibility" in {column["name"] for column in inspect(connection).get_columns("sessions")}
    finally:
        engine.dispose()


def test_desktop_bridge_is_idempotent_and_preserves_unknown_authorship(tmp_path):
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection:
            _upgrade_desktop_assistant_columns(connection)
            _upgrade_desktop_assistant_columns(connection)
            assert_legacy(connection)
            assert any(index["name"] == "uq_sessions_active_assistant"
                       for index in inspect(connection).get_indexes("sessions"))
    finally:
        engine.dispose()
