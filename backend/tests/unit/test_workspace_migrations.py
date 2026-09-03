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
