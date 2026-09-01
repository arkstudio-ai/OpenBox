"""Add durable personal-Skill lifecycle fencing.

Revision ID: fc4e6d8b0a2c
Revises: fb3d5e7f9a1c
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fc4e6d8b0a2c"
down_revision: Union[str, None] = "fb3d5e7f9a1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_skills",
        sa.Column(
            "lifecycle_state",
            sa.String(length=16),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column(
        "user_skills",
        sa.Column(
            "lifecycle_generation",
            sa.Integer(),
            server_default="1",
            nullable=False,
        ),
    )
    op.create_index(
        "ix_user_skills_owner_lifecycle",
        "user_skills",
        ["owner_id", "lifecycle_state", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_skills_owner_lifecycle", table_name="user_skills")
    op.drop_column("user_skills", "lifecycle_generation")
    op.drop_column("user_skills", "lifecycle_state")
