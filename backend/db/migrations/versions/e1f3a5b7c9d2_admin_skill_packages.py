"""Editable store packages and durable deletion tombstones.

Revision ID: e1f3a5b7c9d2
Revises: d9e1f3a5b7c2
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e1f3a5b7c9d2"
down_revision = "d9e1f3a5b7c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Names can be 64 characters plus the kind prefix; desktop ids up to 96.
    # SQLite requires table recreation for type changes; batch mode preserves
    # the same ALTER COLUMN operation on PostgreSQL.
    with op.batch_alter_table("audit_logs") as batch:
        batch.alter_column(
            "resource_id", existing_type=sa.String(64), type_=sa.String(128)
        )
    op.create_table(
        "skill_catalog_packages",
        sa.Column("catalog_id", sa.String(96), primary_key=True),
        sa.Column(
            "definition",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("archive_data", sa.LargeBinary(), nullable=True),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("changed_by", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("skill_catalog_packages")
    # Do not shrink audit identifiers: historical events must stay intact.
