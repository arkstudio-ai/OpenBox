"""Per-request credit cap on sessions (public API ``max_credits``).

Revision ID: pg40c1d2e3f4
Revises: pf39b0c1d2e3
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pg40c1d2e3f4"
down_revision = "pf39b0c1d2e3"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("credit_cap", JSONType(), nullable=True))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("credit_cap")
