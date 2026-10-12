"""The api_keys migration runs on a pre-existing schema and reverses cleanly."""
import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = "db.migrations.versions.e7c9a1b3d5f0_api_keys_and_session_metadata"


def _legacy_schema(connection):
    from db.models.user import User
    from db.models.workspace import Workspace

    User.__table__.create(connection)
    Workspace.__table__.create(connection)
    connection.exec_driver_sql(
        "CREATE TABLE sessions (id VARCHAR PRIMARY KEY, user_id VARCHAR, status VARCHAR, "
        "tool_exposure_state TEXT NOT NULL DEFAULT '{}', variant VARCHAR)"
    )
    connection.exec_driver_sql("INSERT INTO sessions (id, user_id, status) VALUES ('s1', 'u1', 'idle')")


def test_upgrade_adds_table_and_columns_then_downgrade_removes_them():
    engine = sa.create_engine("sqlite:///:memory:")
    migration = importlib.import_module(MIGRATION)
    with engine.begin() as connection:
        _legacy_schema(connection)
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = sa.inspect(connection)
        assert "api_keys" in inspector.get_table_names()
        key_columns = {c["name"] for c in inspector.get_columns("api_keys")}
        from db.models.api_key import ApiKey
        assert key_columns == set(ApiKey.__table__.columns.keys())
        session_columns = {c["name"] for c in inspector.get_columns("sessions")}
        assert {"quality", "metadata", "api_key_id"} <= session_columns
        assert {i["name"] for i in inspector.get_indexes("sessions")} >= {"ix_sessions_api_key"}
        row = connection.execute(sa.text("SELECT quality, metadata, api_key_id FROM sessions")).one()
        assert row == (None, None, None)

        from db.base import _missing_readiness_schema
        missing = _missing_readiness_schema(connection)
        assert not any(item.startswith(("api_keys", "sessions.")) for item in missing)

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        inspector = sa.inspect(connection)
        assert "api_keys" not in inspector.get_table_names()
        assert {"quality", "metadata", "api_key_id"}.isdisjoint(
            {c["name"] for c in inspector.get_columns("sessions")}
        )
