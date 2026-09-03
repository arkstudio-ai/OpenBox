"""Add workspace tenancy, invitations, audit scope, and internal task state.

Revision ID: c3e5f7a9b1d4
Revises: b2d4f6a8c0e2
"""
from datetime import datetime, timezone
from typing import Sequence, Union
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision: str = "c3e5f7a9b1d4"
down_revision: Union[str, None] = "b2d4f6a8c0e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _workspace_id() -> str:
    return f"ws_{uuid4().hex}"


def upgrade() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("owner_user_id", sa.String(64), nullable=False),
        sa.Column("plan_id", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(16), server_default=sa.text("'personal'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_deleted", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workspaces_owner_active", "workspaces", ["owner_user_id", "is_deleted"]
    )
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("user_id", sa.String(64), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("invited_by", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("workspace_id", "user_id"),
    )
    op.create_index(
        "ix_workspace_members_user_status",
        "workspace_members",
        ["user_id", "status"],
    )
    op.create_table(
        "workspace_invitations",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("workspace_id", sa.String(64), nullable=False),
        sa.Column("target", sa.String(255), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_by", sa.String(64), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_workspace_invitations_token_hash"),
    )
    op.create_index(
        "ix_workspace_invitations_workspace",
        "workspace_invitations",
        ["workspace_id", "created_at"],
    )
    op.create_index(
        "ix_workspace_invitations_target",
        "workspace_invitations",
        ["target", "accepted_at"],
    )
    op.create_table(
        "internal_task_state",
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("running_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_status", sa.String(16), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )

    connection = op.get_bind()
    inspector = sa.inspect(connection)
    tables = set(inspector.get_table_names())
    user_columns = {column["name"] for column in inspector.get_columns("users")}
    op.add_column("users", sa.Column("default_workspace_id", sa.String(64), nullable=True))
    has_audit_logs = "audit_logs" in tables
    if has_audit_logs:
        op.add_column("audit_logs", sa.Column("workspace_id", sa.String(64), nullable=True))
        op.create_index(
            "ix_audit_logs_workspace_created",
            "audit_logs",
            ["workspace_id", "created_at"],
        )

    name_expression = "username" if "username" in user_columns else "id"
    users = list(
        connection.execute(
            sa.text(f"SELECT id, {name_expression} AS username FROM users ORDER BY id")
        ).mappings()
    )
    now = datetime.now(timezone.utc)
    workspace_rows = []
    member_rows = []
    user_rows = []
    for user in users:
        workspace_id = _workspace_id()
        workspace_rows.append(
            {
                "id": workspace_id,
                "name": user["username"] or "Personal",
                "owner_user_id": user["id"],
                "created_at": now,
                "updated_at": now,
            }
        )
        member_rows.append(
            {
                "workspace_id": workspace_id,
                "user_id": user["id"],
                "role": "owner",
                "status": "active",
                "created_at": now,
                "updated_at": now,
            }
        )
        user_rows.append({"id": user["id"], "workspace_id": workspace_id})

    if workspace_rows:
        connection.execute(
            sa.text(
                """INSERT INTO workspaces
                   (id, name, owner_user_id, kind, created_at, updated_at, is_deleted)
                   VALUES (:id, :name, :owner_user_id, 'personal', :created_at, :updated_at, false)"""
            ),
            workspace_rows,
        )
        connection.execute(
            sa.text(
                """INSERT INTO workspace_members
                   (workspace_id, user_id, role, status, created_at, updated_at)
                   VALUES (:workspace_id, :user_id, :role, :status, :created_at, :updated_at)"""
            ),
            member_rows,
        )
        connection.execute(
            sa.text(
                "UPDATE users SET default_workspace_id = :workspace_id WHERE id = :id"
            ),
            user_rows,
        )

    with op.batch_alter_table("users") as batch:
        batch.create_foreign_key(
            "fk_users_default_workspace",
            "workspaces",
            ["default_workspace_id"],
            ["id"],
        )
    if has_audit_logs:
        with op.batch_alter_table("audit_logs") as batch:
            batch.create_foreign_key(
                "fk_audit_logs_workspace", "workspaces", ["workspace_id"], ["id"]
            )


def downgrade() -> None:
    connection = op.get_bind()
    if "audit_logs" in sa.inspect(connection).get_table_names():
        with op.batch_alter_table("audit_logs") as batch:
            batch.drop_constraint("fk_audit_logs_workspace", type_="foreignkey")
        op.drop_index("ix_audit_logs_workspace_created", table_name="audit_logs")
        op.drop_column("audit_logs", "workspace_id")
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("fk_users_default_workspace", type_="foreignkey")
    op.drop_column("users", "default_workspace_id")
    op.drop_table("internal_task_state")
    op.drop_index("ix_workspace_invitations_target", table_name="workspace_invitations")
    op.drop_index("ix_workspace_invitations_workspace", table_name="workspace_invitations")
    op.drop_table("workspace_invitations")
    op.drop_index("ix_workspace_members_user_status", table_name="workspace_members")
    op.drop_table("workspace_members")
    op.drop_index("ix_workspaces_owner_active", table_name="workspaces")
    op.drop_table("workspaces")
