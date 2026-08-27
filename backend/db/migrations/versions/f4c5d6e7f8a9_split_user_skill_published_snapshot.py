"""Split mutable personal drafts from immutable published skill snapshots.

Revision ID: f4c5d6e7f8a9
Revises: f3b4c5d6e7f8
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4c5d6e7f8a9"
down_revision: Union[str, None] = "f3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.add_column(
        "user_skills",
        sa.Column("published_name", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_install_dir", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_description", sa.Text(), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_icon", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "user_skills", sa.Column("published_version", sa.Integer(), nullable=True)
    )
    op.add_column(
        "user_skills",
        sa.Column("published_archive_data", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_archive_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_archive_size", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "user_skills",
        sa.Column("published_metadata_data", json_type, nullable=True),
    )

    # Preserve every package that was public under the original one-snapshot
    # schema.  Column-to-column UPDATE values compile on both PostgreSQL and
    # SQLite, which also keeps migration tests portable.
    skills = sa.table(
        "user_skills",
        sa.column("status", sa.String()),
        sa.column("name", sa.String()),
        sa.column("install_dir", sa.String()),
        sa.column("description", sa.Text()),
        sa.column("icon", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("archive_data", sa.LargeBinary()),
        sa.column("archive_sha256", sa.String()),
        sa.column("archive_size", sa.BigInteger()),
        sa.column("metadata_data", json_type),
        sa.column("published_name", sa.String()),
        sa.column("published_install_dir", sa.String()),
        sa.column("published_description", sa.Text()),
        sa.column("published_icon", sa.String()),
        sa.column("published_version", sa.Integer()),
        sa.column("published_archive_data", sa.LargeBinary()),
        sa.column("published_archive_sha256", sa.String()),
        sa.column("published_archive_size", sa.BigInteger()),
        sa.column("published_metadata_data", json_type),
    )
    op.execute(
        skills.update()
        .where(skills.c.status == "published")
        .values(
            published_name=skills.c.name,
            published_install_dir=skills.c.install_dir,
            published_description=skills.c.description,
            published_icon=skills.c.icon,
            published_version=skills.c.version,
            published_archive_data=skills.c.archive_data,
            published_archive_sha256=skills.c.archive_sha256,
            published_archive_size=skills.c.archive_size,
            published_metadata_data=skills.c.metadata_data,
        )
    )


def downgrade() -> None:
    op.drop_column("user_skills", "published_metadata_data")
    op.drop_column("user_skills", "published_archive_size")
    op.drop_column("user_skills", "published_archive_sha256")
    op.drop_column("user_skills", "published_archive_data")
    op.drop_column("user_skills", "published_version")
    op.drop_column("user_skills", "published_icon")
    op.drop_column("user_skills", "published_description")
    op.drop_column("user_skills", "published_install_dir")
    op.drop_column("user_skills", "published_name")
