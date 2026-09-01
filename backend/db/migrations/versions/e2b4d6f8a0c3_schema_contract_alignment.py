"""Align legacy relational nullability with the current ORM contract.

Revision ID: e2b4d6f8a0c3
Revises: d0a2c4e6f8b1
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e2b4d6f8a0c3"
down_revision: Union[str, None] = "d0a2c4e6f8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    """Permit the repository's deliberately minimal SQLite upgrade fixtures."""
    bind = op.get_bind()
    return bool(sa.inspect(bind).has_table(name))


def _json_type():
    return postgresql.JSONB().with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    # Containers now represent one user-level WUYING desktop rather than a
    # project-owned Docker pod, so a legacy relational row may be projectless.
    if _has_table("containers"):
        with op.batch_alter_table("containers") as batch:
            batch.alter_column(
                "project_id",
                existing_type=sa.String(length=64),
                nullable=True,
            )

    # These payloads are value objects in the ORM. Backfill historical NULLs
    # before enforcing the non-null invariant used by all current writers.
    if _has_table("image_gen_cache"):
        op.execute(
            sa.text(
                "UPDATE image_gen_cache SET request_data = '{}' "
                "WHERE request_data IS NULL"
            )
        )
        with op.batch_alter_table("image_gen_cache") as batch:
            batch.alter_column(
                "request_data",
                existing_type=_json_type(),
                nullable=False,
            )

    if _has_table("user_memories"):
        op.execute(
            sa.text("UPDATE user_memories SET value = '{}' WHERE value IS NULL")
        )
        op.execute(
            sa.text(
                "UPDATE user_memories SET evidence = '{}' "
                "WHERE evidence IS NULL"
            )
        )
        with op.batch_alter_table("user_memories") as batch:
            batch.alter_column(
                "value",
                existing_type=_json_type(),
                nullable=False,
            )
            batch.alter_column(
                "evidence",
                existing_type=_json_type(),
                nullable=False,
            )


def downgrade() -> None:
    if _has_table("user_memories"):
        with op.batch_alter_table("user_memories") as batch:
            batch.alter_column(
                "evidence",
                existing_type=_json_type(),
                nullable=True,
            )
            batch.alter_column(
                "value",
                existing_type=_json_type(),
                nullable=True,
            )

    if _has_table("image_gen_cache"):
        with op.batch_alter_table("image_gen_cache") as batch:
            batch.alter_column(
                "request_data",
                existing_type=_json_type(),
                nullable=True,
            )

    if _has_table("containers"):
        bind = op.get_bind()
        projectless = int(
            bind.execute(
                sa.text(
                    "SELECT COUNT(*) FROM containers WHERE project_id IS NULL"
                )
            ).scalar_one()
        )
        if projectless:
            raise RuntimeError(
                "schema contract downgrade refused: projectless containers exist"
            )
        with op.batch_alter_table("containers") as batch:
            batch.alter_column(
                "project_id",
                existing_type=sa.String(length=64),
                nullable=False,
            )
