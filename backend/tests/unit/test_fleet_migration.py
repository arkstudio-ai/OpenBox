"""The A3/A4 migration round-trips and protects unassigned pool rows."""
import importlib
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


migration = importlib.import_module(
    "db.migrations.versions.a3f1e5c7d9b2_fleet_pool"
)


def _run(connection, direction):
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


def _schema():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    desktops = sa.Table(
        "cloud_desktops", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=True),
        sa.Column("region_id", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    sa.Index(
        "ix_cloud_desktops_workspace_active", desktops.c.workspace_id,
        unique=True, sqlite_where=desktops.c.is_deleted == sa.false(),
    )
    metadata.create_all(engine)
    return engine, desktops


def test_fleet_migration_upgrade_and_downgrade():
    engine, desktops = _schema()
    with engine.begin() as connection:
        connection.execute(desktops.insert(), {
            "id": "cld-1", "workspace_id": "ws-1", "region_id": "cn-shanghai",
            "status": "running", "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        })
        _run(connection, "upgrade")
        tables = set(sa.inspect(connection).get_table_names())
        assert {"fleet_snapshots", "fleet_alerts", "pool_purchases"} <= tables
        row = connection.execute(
            sa.text("SELECT workspace_id, pool_state FROM cloud_desktops")
        ).one()
        assert row == ("ws-1", "assigned")
        workspace = next(
            column for column in sa.inspect(connection).get_columns("cloud_desktops")
            if column["name"] == "workspace_id"
        )
        assert workspace["nullable"] is True
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(sa.text(
                "INSERT INTO cloud_desktops "
                "(id, workspace_id, region_id, status, pool_state, is_deleted, created_at, updated_at) "
                "VALUES ('bad-state', NULL, 'cn-shanghai', 'running', 'typo', 0, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ))

        _run(connection, "downgrade")
        columns = {column["name"] for column in sa.inspect(connection).get_columns("cloud_desktops")}
        assert "pool_state" not in columns
        assert "fleet_alerts" not in sa.inspect(connection).get_table_names()


def test_fleet_migration_refuses_downgrade_with_live_unassigned_pool_row():
    engine, _desktops = _schema()
    with engine.begin() as connection:
        _run(connection, "upgrade")
        connection.execute(sa.text(
            "INSERT INTO cloud_desktops "
            "(id, workspace_id, region_id, status, pool_state, is_deleted, created_at, updated_at) "
            "VALUES ('pool-1', NULL, 'cn-shanghai', 'running', 'prewarm', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        with pytest.raises(RuntimeError, match="unassigned"):
            _run(connection, "downgrade")
        assert "fleet_alerts" in sa.inspect(connection).get_table_names()
