"""Alembic-path coverage for B1 workspace backfills and round trips."""
import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


core = importlib.import_module(
    "db.migrations.versions.c3e5f7a9b1d4_workspace_core"
)
business = importlib.import_module(
    "db.migrations.versions.d4f6a8b0c2e5_workspace_business_scope"
)
desktop_workspace = importlib.import_module(
    "db.migrations.versions.e6a8c0d2f4b7_workspace_cloud_desktops"
)
prepaid = importlib.import_module(
    "db.migrations.versions.f7b9d1e3a5c8_prepaid_desktop_metadata"
)


def _legacy_schema() -> tuple[sa.Engine, sa.MetaData]:
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table(
        "users", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False),
    )
    sa.Table(
        "audit_logs", metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    for table, owner_column, _index, index_columns in business.TABLES:
        columns = [
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column(owner_column, sa.String(64), nullable=False),
        ]
        if table == "projects":
            columns.append(sa.Column("slug", sa.String(128)))
        elif table == "user_skills":
            columns.extend(
                [
                    sa.Column("name", sa.String(64)),
                    sa.UniqueConstraint(
                        "owner_id", "name", name="uq_user_skills_owner_name"
                    ),
                ]
            )
        for name in index_columns:
            if name == "workspace_id":
                continue
            column_type = (
                sa.Boolean()
                if name == "is_deleted"
                else sa.DateTime()
                if name.endswith("_at")
                else sa.String(32)
            )
            columns.append(sa.Column(name, column_type))
        row = sa.Table(table, metadata, *columns)
        if table == "projects":
            sa.Index(
                "ix_projects_user_slug_active",
                row.c.user_id,
                row.c.slug,
                unique=True,
            )
    metadata.create_all(engine)
    return engine, metadata


def _run(connection, migration, direction: str) -> None:
    migration.op = Operations(MigrationContext.configure(connection))
    getattr(migration, direction)()


def test_workspace_migrations_backfill_non_null_and_round_trip():
    engine, metadata = _legacy_schema()
    with engine.begin() as connection:
        connection.execute(
            metadata.tables["users"].insert(),
            [{"id": "u1", "username": "alice"}, {"id": "u2", "username": "bob"}],
        )
        for table, owner_column, _index, _columns in business.TABLES:
            connection.execute(
                metadata.tables[table].insert(),
                {"id": f"{table}_1", owner_column: "u1"},
            )

        _run(connection, core, "upgrade")
        _run(connection, business, "upgrade")

        users = connection.execute(
            sa.text("SELECT id, default_workspace_id FROM users ORDER BY id")
        ).mappings().all()
        assert all(row["default_workspace_id"] for row in users)
        for user in users:
            workspace = connection.execute(
                sa.text("SELECT owner_user_id FROM workspaces WHERE id = :id"),
                {"id": user["default_workspace_id"]},
            ).scalar_one()
            assert workspace == user["id"]
            member = connection.execute(
                sa.text(
                    "SELECT role, status FROM workspace_members "
                    "WHERE workspace_id = :workspace_id AND user_id = :user_id"
                ),
                {"workspace_id": user["default_workspace_id"], "user_id": user["id"]},
            ).one()
            assert member == ("owner", "active")

        u1_workspace = users[0]["default_workspace_id"]
        for table, _owner, index_name, _columns in business.TABLES:
            assert connection.execute(
                sa.text(f"SELECT workspace_id FROM {table}")
            ).scalar_one() == u1_workspace
            workspace_column = next(
                column for column in sa.inspect(connection).get_columns(table)
                if column["name"] == "workspace_id"
            )
            assert workspace_column["nullable"] is False
            assert index_name in {item["name"] for item in sa.inspect(connection).get_indexes(table)}

        _run(connection, business, "downgrade")
        _run(connection, core, "downgrade")
        _run(connection, core, "upgrade")
        _run(connection, business, "upgrade")

        assert connection.execute(
            sa.text("SELECT count(*) FROM users WHERE default_workspace_id IS NULL")
        ).scalar_one() == 0
        for table, _owner, _index, _columns in business.TABLES:
            assert connection.execute(
                sa.text(f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")
            ).scalar_one() == 0


def test_cloud_desktop_workspace_and_prepaid_migrations_round_trip():
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    users = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("default_workspace_id", sa.String(64), nullable=False),
    )
    sa.Table(
        "workspaces",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
    )
    desktops = sa.Table(
        "cloud_desktops",
        metadata,
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    sa.Index(
        "ix_cloud_desktops_user_active",
        desktops.c.user_id,
        unique=True,
        sqlite_where=desktops.c.is_deleted == sa.false(),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO workspaces(id, owner_user_id) VALUES ('ws-1', 'u1')")
        )
        connection.execute(users.insert(), {"id": "u1", "default_workspace_id": "ws-1"})
        connection.execute(desktops.insert(), {"id": "cld-1", "user_id": "u1"})

        _run(connection, desktop_workspace, "upgrade")
        _run(connection, prepaid, "upgrade")
        row = connection.execute(
            sa.text(
                "SELECT workspace_id, charge_type, expires_at FROM cloud_desktops"
            )
        ).one()
        assert row == ("ws-1", None, None)
        columns = {c["name"]: c for c in sa.inspect(connection).get_columns("cloud_desktops")}
        assert columns["workspace_id"]["nullable"] is False
        assert columns["user_id"]["nullable"] is True

        # Rows adopted from ECD tags need not have a triggering user.  A
        # downgrade recovers the workspace owner instead of failing NOT NULL.
        connection.execute(
            sa.text("UPDATE cloud_desktops SET user_id = NULL WHERE id = 'cld-1'")
        )

        _run(connection, prepaid, "downgrade")
        _run(connection, desktop_workspace, "downgrade")
        assert connection.execute(
            sa.text("SELECT user_id FROM cloud_desktops")
        ).scalar_one() == "u1"
