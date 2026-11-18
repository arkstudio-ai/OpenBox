"""Persist the guided production form on its Agent session."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pq7d3e4f5a6b"
down_revision = "pp6c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column(
        "light_production", postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=True,
    ))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("light_production")
