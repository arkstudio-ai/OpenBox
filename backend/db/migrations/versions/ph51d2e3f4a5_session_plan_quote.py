"""Whole-plan estimate on sessions (registered before the price card, enforced on confirmation).

Revision ID: ph51d2e3f4a5
Revises: pg40c1d2e3f4
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "ph51d2e3f4a5"
down_revision = "pg40c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("plan_quote", JSONType(), nullable=True))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("plan_quote")
