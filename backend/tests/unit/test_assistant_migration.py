"""Old data keeps its audience; downgrade cannot disclose assistant histories."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

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


def test_event_projection_upgrade_is_additive_and_checkpoints_are_nonnegative(tmp_path):
    initial = importlib.import_module("db.migrations.versions.pa1b2c3d4e5f_personal_assistant")
    migration = importlib.import_module("db.migrations.versions.pa2c3d4e5f6a_assistant_event_projection")
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            initial.upgrade()
            migration.upgrade()
            assert_legacy(connection)
            inspector = inspect(connection)
            assert {column["name"] for column in inspector.get_columns("assistant_event_projections")} == {
                "task_id", "assistant_session_id", "source_sequence", "updated_at"}
            assert {key["referred_table"] for key in inspector.get_foreign_keys("assistant_event_projections")} == {
                "assistant_tasks", "sessions"}
            assert any(item["sqltext"] == "source_sequence >= 0"
                       for item in inspector.get_check_constraints("assistant_event_projections"))
    finally:
        engine.dispose()


def test_request_decision_index_is_additive_and_desktop_repairs_existing_tables(tmp_path):
    initial = importlib.import_module("db.migrations.versions.pa1b2c3d4e5f_personal_assistant")
    migration = importlib.import_module("db.migrations.versions.pa3d4e5f6a7b_assistant_request_decisions")
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            initial.upgrade()
            migration.upgrade()
            assert_legacy(connection)
            index = next(i for i in inspect(connection).get_indexes("assistant_commands")
                         if i["name"] == "uq_assistant_request_decision")
            assert index["unique"] and index["column_names"] == ["target_type", "target_id"]
            migration.downgrade()
            _upgrade_desktop_assistant_columns(connection)
            _upgrade_desktop_assistant_columns(connection)
            assert any(i["name"] == "uq_assistant_request_decision"
                       for i in inspect(connection).get_indexes("assistant_commands"))
    finally:
        engine.dispose()


@pytest.mark.parametrize("desktop_bridge", [False, True])
def test_resource_migration_preserves_effect_evidence_and_enforces_complete_fences(tmp_path, desktop_bridge):
    migration = importlib.import_module("db.migrations.versions.pa4e5f6a7b8c_resource_control")
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql("CREATE TABLE cloud_desktops (id VARCHAR(64) PRIMARY KEY)")
            connection.exec_driver_sql("CREATE TABLE external_effects (id VARCHAR(64) PRIMARY KEY, state VARCHAR(24), request_hash TEXT)")
            connection.exec_driver_sql("CREATE TABLE external_effect_evidence (id VARCHAR(64) PRIMARY KEY, effect_id VARCHAR(64) REFERENCES external_effects(id), evidence TEXT)")
            connection.exec_driver_sql("INSERT INTO external_effects VALUES ('old-effect','outcome_unknown','original-digest')")
            connection.exec_driver_sql("INSERT INTO external_effect_evidence VALUES ('receipt','old-effect','original-evidence')")
            if desktop_bridge:
                # Production single-user init creates new tables before the
                # additive bridge; create_all cannot add columns to old ones.
                from db.models.resource_control import ResourceControlLease
                ResourceControlLease.__table__.create(connection)
                _upgrade_desktop_assistant_columns(connection)
                _upgrade_desktop_assistant_columns(connection)
            else:
                migration.upgrade()
            assert connection.execute(text("SELECT request_hash, resource_id, resource_epoch FROM external_effects")).one() == (
                "original-digest", None, None)
            assert connection.scalar(text("SELECT evidence FROM external_effect_evidence")) == "original-evidence"
            connection.exec_driver_sql("INSERT INTO resource_control_leases "
                "(id,resource_type,provider,physical_id,workspace_id,owner_kind,owner_id,epoch,status,admission_state,created_at,updated_at) "
                "VALUES ('physical','desktop','fixture','region:desktop','w','automation','w',1,'active','open',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)")
            for invalid in (
                "resource_id='physical'",
                "resource_id='physical', resource_epoch=1, resource_owner_id='w'",
                "resource_id='physical', resource_epoch=0, resource_owner_kind='automation', resource_owner_id='w'",
                "resource_id='physical', resource_epoch=1, resource_owner_kind='invalid', resource_owner_id='w'",
            ):
                with pytest.raises(IntegrityError):
                    connection.exec_driver_sql(f"UPDATE external_effects SET {invalid} WHERE id='old-effect'")
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("INSERT INTO external_effects (id,resource_epoch) VALUES ('broken',1)")
            connection.exec_driver_sql("UPDATE external_effects SET resource_id='physical', resource_epoch=1, "
                "resource_owner_kind='automation', resource_owner_id='w' WHERE id='old-effect'")
            assert connection.scalar(text("SELECT state FROM external_effects WHERE id='old-effect'")) == "outcome_unknown"
            if not desktop_bridge:
                with pytest.raises(RuntimeError, match="must be retained"):
                    migration.downgrade()
    finally:
        engine.dispose()


@pytest.mark.parametrize("desktop_bridge", [False, True])
def test_remote_journal_migration_preserves_held_identity_and_refuses_to_drop_pins(tmp_path, desktop_bridge):
    migration = importlib.import_module("db.migrations.versions.pa5f6a7b8c9d_resource_journal")
    engine = legacy_database(tmp_path)
    try:
        with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
            connection.exec_driver_sql("CREATE TABLE resource_control_leases (id TEXT PRIMARY KEY, epoch INTEGER, status TEXT)")
            connection.exec_driver_sql("INSERT INTO resource_control_leases VALUES ('existing-resource',7,'hold')")
            if desktop_bridge:
                _upgrade_desktop_assistant_columns(connection)
                _upgrade_desktop_assistant_columns(connection)
            else:
                migration.upgrade()
            assert connection.execute(text("SELECT id,epoch,status,remote_journal_id,remote_status FROM resource_control_leases")).one() == (
                "existing-resource", 7, "hold", None, None)
            with pytest.raises(IntegrityError):
                connection.exec_driver_sql("UPDATE resource_control_leases SET remote_journal_id='invalid'")
            connection.execute(text("UPDATE resource_control_leases SET remote_journal_id=:pin"), {"pin": "a" * 32})
            if not desktop_bridge:
                with pytest.raises(RuntimeError, match="must be retained"):
                    migration.downgrade()
    finally:
        engine.dispose()
