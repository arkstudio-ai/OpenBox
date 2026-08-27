"""Add personal skill packages and published store snapshots.

Revision ID: f3b4c5d6e7f8
Revises: f2a3b4c5d6e7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f3b4c5d6e7f8"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    json_type = postgresql.JSONB().with_variant(sa.JSON(), "sqlite")
    op.create_table(
        "user_skills",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("install_dir", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("icon", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), server_default=sa.text("'unpublished'"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("archive_data", sa.LargeBinary(), nullable=False),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("archive_size", sa.BigInteger(), nullable=False),
        sa.Column("metadata_data", json_type, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "name", name="uq_user_skills_owner_name"),
    )
    op.create_index("ix_user_skills_owner_updated", "user_skills", ["owner_id", "updated_at"])
    op.create_index("ix_user_skills_status_published", "user_skills", ["status", "published_at"])
    op.create_table(
        "skill_installs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("user_skill_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("install_dir", sa.String(length=64), nullable=False),
        sa.Column("installed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["user_skill_id"], ["user_skills.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "install_dir", name="uq_skill_installs_user_dir"),
    )
    op.create_index("ix_skill_installs_user", "skill_installs", ["user_id", "installed_at"])


def downgrade() -> None:
    op.drop_index("ix_skill_installs_user", table_name="skill_installs")
    op.drop_table("skill_installs")
    op.drop_index("ix_user_skills_status_published", table_name="user_skills")
    op.drop_index("ix_user_skills_owner_updated", table_name="user_skills")
    op.drop_table("user_skills")
