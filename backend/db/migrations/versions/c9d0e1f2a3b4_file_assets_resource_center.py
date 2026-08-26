"""Turn file_assets into the resource centre's index.

Adds the project the file is filed under, who produced it (person vs agent),
a transient marker for the desktop screenshots that should stay out of the
listing, and soft-delete columns so removing a resource does not orphan the
file parts hanging off old messages.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("file_assets", sa.Column("project_id", sa.String(64), nullable=True))
    op.add_column(
        "file_assets",
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("'user'")),
    )
    op.add_column(
        "file_assets",
        sa.Column("transient", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "file_assets",
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("file_assets", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_file_assets_user_project", "file_assets", ["user_id", "project_id", "created_at"]
    )

    # Backfill: an asset pinned to a session belongs to that session's project.
    op.execute(
        "UPDATE file_assets fa SET project_id = s.project_id "
        "FROM sessions s WHERE fa.session_id = s.id AND fa.project_id IS NULL"
    )
    # Existing rows predate the source column. A file part hanging off an
    # assistant message is something the agent produced; the transient flag on
    # that part marks the desktop screenshots the listing should skip.
    op.execute(
        "UPDATE file_assets fa SET source = 'agent' "
        "FROM parts p JOIN messages m ON m.id = p.message_id "
        "WHERE p.type = 'file' AND p.data->>'asset_id' = fa.id AND m.role = 'assistant'"
    )
    op.execute(
        "UPDATE file_assets fa SET transient = true "
        "FROM parts p "
        "WHERE p.type = 'file' AND p.data->>'asset_id' = fa.id "
        "AND (p.data->>'transient') = 'true'"
    )


def downgrade() -> None:
    op.drop_index("ix_file_assets_user_project", table_name="file_assets")
    for column in ("deleted_at", "is_deleted", "transient", "source", "project_id"):
        op.drop_column("file_assets", column)
