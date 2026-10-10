"""Retain bounded original-task continuation authority without a second queue."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pa7a8b9c0d1e"
down_revision = "pa6f7a8b9c0d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assistant_tasks", sa.Column("continuation_policy",
        sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text(
            "SELECT count(*) FROM assistant_tasks WHERE continuation_policy IS NOT NULL "
            "AND CAST(continuation_policy AS TEXT) <> 'null'")):
        raise RuntimeError("Original-task continuation authority must be retained")
    op.drop_column("assistant_tasks", "continuation_policy")
