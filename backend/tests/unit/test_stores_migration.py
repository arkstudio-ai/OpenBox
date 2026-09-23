"""Alembic round trip for the `stores` table on SQLite."""
import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

stores = importlib.import_module("db.migrations.versions.e2c4a6b8d0f1_stores")


def _engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("users", metadata, sa.Column("id", sa.String(64), primary_key=True))
    sa.Table("workspaces", metadata, sa.Column("id", sa.String(64), primary_key=True))
    metadata.create_all(engine)
    return engine


def test_stores_upgrade_and_downgrade_round_trip():
    engine = _engine()
    with engine.begin() as conn:
        context = MigrationContext.configure(conn)
        with Operations.context(context):
            stores.upgrade()
        columns = {c["name"] for c in sa.inspect(conn).get_columns("stores")}
        assert {"id", "workspace_id", "user_id", "name", "category", "main_platforms", "platform_bindings",
                "data_sources", "persona_status", "persona_session_id", "persona_started_at"} <= columns
        uniques = {u["name"] for u in sa.inspect(conn).get_unique_constraints("stores")}
        assert "uq_stores_workspace" in uniques
        conn.execute(sa.text("INSERT INTO workspaces (id) VALUES ('ws1')"))
        conn.execute(sa.text("INSERT INTO users (id) VALUES ('u1')"))
        conn.execute(sa.text(
            "INSERT INTO stores (id, workspace_id, user_id, name, main_platforms, platform_bindings, data_sources, "
            "created_at, updated_at) VALUES ('s1', 'ws1', 'u1', '店', '[]', '{}', '{}', '2026-09-23', '2026-09-23')"
        ))
        row = conn.execute(sa.text("SELECT category, persona_status FROM stores")).one()
        assert tuple(row) == ("food", "none")
        with Operations.context(context):
            stores.downgrade()
        assert "stores" not in sa.inspect(conn).get_table_names()


def test_stores_migration_chain_head():
    assert stores.down_revision == "d0a2c4e6f8b1"
    assert stores.revision == "e2c4a6b8d0f1"
