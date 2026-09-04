"""Make cloud desktop ownership workspace-scoped.

Revision ID: e6a8c0d2f4b7
Revises: d4f6a8b0c2e5
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e6a8c0d2f4b7"
down_revision: Union[str, None] = "d4f6a8b0c2e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("cloud_desktops", sa.Column("workspace_id", sa.String(64), nullable=True))
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE cloud_desktops
            SET workspace_id = (
                SELECT users.default_workspace_id
                FROM users
                WHERE users.id = cloud_desktops.user_id
            )
            """
        )
    )
    missing = connection.execute(
        sa.text("SELECT COUNT(*) FROM cloud_desktops WHERE workspace_id IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"cannot migrate {missing} cloud desktop row(s) without a default workspace"
        )
    op.drop_index("ix_cloud_desktops_user_active", table_name="cloud_desktops")
    with op.batch_alter_table("cloud_desktops") as batch:
        batch.alter_column("workspace_id", existing_type=sa.String(64), nullable=False)
        batch.alter_column("user_id", existing_type=sa.String(64), nullable=True)
        batch.create_foreign_key(
            "fk_cloud_desktops_workspace_id", "workspaces", ["workspace_id"], ["id"]
        )
    op.create_index(
        "ix_cloud_desktops_workspace_active",
        "cloud_desktops",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
        sqlite_where=sa.text("is_deleted = 0"),
    )


def downgrade() -> None:
    op.drop_index("ix_cloud_desktops_workspace_active", table_name="cloud_desktops")
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE cloud_desktops
            SET user_id = (
                SELECT workspaces.owner_user_id
                FROM workspaces
                WHERE workspaces.id = cloud_desktops.workspace_id
            )
            WHERE user_id IS NULL
            """
        )
    )
    missing = connection.execute(
        sa.text("SELECT COUNT(*) FROM cloud_desktops WHERE user_id IS NULL")
    ).scalar_one()
    if missing:
        raise RuntimeError(
            f"cannot downgrade {missing} cloud desktop row(s) without a workspace owner"
        )
    with op.batch_alter_table("cloud_desktops") as batch:
        batch.drop_constraint("fk_cloud_desktops_workspace_id", type_="foreignkey")
        batch.alter_column("user_id", existing_type=sa.String(64), nullable=False)
        batch.drop_column("workspace_id")
    op.create_index(
        "ix_cloud_desktops_user_active",
        "cloud_desktops",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
        sqlite_where=sa.text("is_deleted = 0"),
    )
