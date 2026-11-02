"""Which Light entry a guided session was started from (light/mode.py).

Revision ID: pi95b6c7d8e9
Revises: ph84a5b6c7d8
"""
from alembic import op
import sqlalchemy as sa

revision = "pi95b6c7d8e9"
down_revision = "ph84a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("light_entry", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("light_entry")
