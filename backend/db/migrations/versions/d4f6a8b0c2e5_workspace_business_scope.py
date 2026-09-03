"""Attach workspace scope to tenant-owned business records.

Revision ID: d4f6a8b0c2e5
Revises: c3e5f7a9b1d4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4f6a8b0c2e5"
down_revision: Union[str, None] = "c3e5f7a9b1d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    ("sessions", "user_id", "ix_sessions_workspace_active", ("workspace_id", "is_deleted")),
    ("projects", "user_id", "ix_projects_workspace_active", ("workspace_id", "is_deleted")),
    ("file_assets", "user_id", "ix_file_assets_workspace_active", ("workspace_id", "is_deleted")),
    ("cron_jobs", "user_id", "ix_cron_jobs_workspace_active", ("workspace_id", "is_deleted")),
    ("user_skills", "owner_id", "ix_user_skills_workspace_updated", ("workspace_id", "updated_at")),
    ("user_memories", "user_id", "ix_user_memories_workspace_status", ("workspace_id", "status")),
    ("video_productions", "user_id", "ix_video_productions_workspace_created", ("workspace_id", "created_at")),
)


def upgrade() -> None:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    available_tables = set(inspector.get_table_names())
    for table, owner_column, index_name, index_columns in TABLES:
        if table not in available_tables:
            if connection.dialect.name != "sqlite":
                raise RuntimeError(f"Required business table is missing: {table}")
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if owner_column not in columns or any(
            column not in columns and column != "workspace_id"
            for column in index_columns
        ):
            if connection.dialect.name != "sqlite":
                raise RuntimeError(f"Required columns are missing from {table}")
            continue
        op.add_column(table, sa.Column("workspace_id", sa.String(64), nullable=True))
        connection.execute(
            sa.text(
                f"""UPDATE {table}
                    SET workspace_id = (
                        SELECT users.default_workspace_id
                        FROM users
                        WHERE users.id = {table}.{owner_column}
                    )
                    WHERE workspace_id IS NULL"""
            )
        )
        missing = connection.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE workspace_id IS NULL")
        ).scalar_one()
        if missing:
            raise RuntimeError(
                f"Cannot backfill {table}.workspace_id: {missing} rows have no owning user's default workspace"
            )
        with op.batch_alter_table(table) as batch:
            batch.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
            batch.create_foreign_key(
                f"fk_{table}_workspace", "workspaces", ["workspace_id"], ["id"]
            )
        op.create_index(index_name, table, list(index_columns))
        if table == "projects":
            existing = {item["name"] for item in sa.inspect(connection).get_indexes(table)}
            if "ix_projects_user_slug_active" in existing:
                op.drop_index("ix_projects_user_slug_active", table_name=table)
            op.create_index(
                "ix_projects_user_slug_active",
                table,
                ["workspace_id", "user_id", "slug"],
                unique=True,
                postgresql_where=sa.text("is_deleted = false"),
            )
        elif table == "user_skills":
            constraints = {
                item["name"] for item in sa.inspect(connection).get_unique_constraints(table)
            }
            with op.batch_alter_table(table) as batch:
                if "uq_user_skills_owner_name" in constraints:
                    batch.drop_constraint(
                        "uq_user_skills_owner_name", type_="unique"
                    )
                batch.create_unique_constraint(
                    "uq_user_skills_workspace_owner_name",
                    ["workspace_id", "owner_id", "name"],
                )


def downgrade() -> None:
    connection = op.get_bind()
    available_tables = set(sa.inspect(connection).get_table_names())
    for table, _owner_column, index_name, _index_columns in reversed(TABLES):
        if table not in available_tables:
            continue
        columns = {column["name"] for column in sa.inspect(connection).get_columns(table)}
        if "workspace_id" not in columns:
            continue
        if table == "user_skills":
            constraints = {
                item["name"] for item in sa.inspect(connection).get_unique_constraints(table)
            }
            with op.batch_alter_table(table) as batch:
                if "uq_user_skills_workspace_owner_name" in constraints:
                    batch.drop_constraint(
                        "uq_user_skills_workspace_owner_name", type_="unique"
                    )
                batch.create_unique_constraint(
                    "uq_user_skills_owner_name", ["owner_id", "name"]
                )
        elif table == "projects":
            existing = {item["name"] for item in sa.inspect(connection).get_indexes(table)}
            if "ix_projects_user_slug_active" in existing:
                op.drop_index("ix_projects_user_slug_active", table_name=table)
            op.create_index(
                "ix_projects_user_slug_active",
                table,
                ["user_id", "slug"],
                unique=True,
                postgresql_where=sa.text("is_deleted = false"),
            )
        op.drop_index(index_name, table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(f"fk_{table}_workspace", type_="foreignkey")
            batch.drop_column("workspace_id")
