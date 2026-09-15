"""Index attachment lookup by OSS key.

Revision ID: f8c2a6e0b4d1
Revises: e5c7a9b1d3f4
"""
from alembic import op

revision = "f8c2a6e0b4d1"
down_revision = "e5c7a9b1d3f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_file_assets_oss_key", "file_assets", ["oss_key"])


def downgrade():
    op.drop_index("ix_file_assets_oss_key", table_name="file_assets")
