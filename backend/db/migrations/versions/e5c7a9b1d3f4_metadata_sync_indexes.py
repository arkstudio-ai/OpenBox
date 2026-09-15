"""Index the trajectory metadata sync cursors.

trajectory.meta_sync pages sessions, users and workspaces changed since an
(updated_at, id) cursor every 30 seconds; without these indexes each page
would scan and sort the whole table.

Revision ID: e5c7a9b1d3f4
Revises: d3b5f7a9c1e2
"""
from alembic import op

revision = "e5c7a9b1d3f4"
down_revision = "d3b5f7a9c1e2"
branch_labels = None
depends_on = None

INDEXES = (
    ("ix_sessions_updated_id", "sessions"),
    ("ix_users_updated_id", "users"),
    ("ix_workspaces_updated_id", "workspaces"),
)


def upgrade():
    for name, table in INDEXES:
        op.create_index(name, table, ["updated_at", "id"], unique=False)


def downgrade():
    for name, table in reversed(INDEXES):
        op.drop_index(name, table_name=table)
