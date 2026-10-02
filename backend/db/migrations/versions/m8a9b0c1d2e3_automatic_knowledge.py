"""Distinguish consumer maintenance from explicitly managed internal runs."""
from alembic import op
import sqlalchemy as sa

revision = "m8a9b0c1d2e3"
down_revision = "m7f8a9b0c1d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("wiki_maintenance_policies", sa.Column("automatic", sa.Boolean(), nullable=False,
                                                       server_default=sa.false()))


def downgrade():
    if op.get_bind().execute(sa.text("SELECT 1 FROM wiki_maintenance_policies WHERE automatic = true AND enabled = true LIMIT 1")).first():
        raise RuntimeError("Disable automatic maintenance before removing its policy marker")
    op.drop_column("wiki_maintenance_policies", "automatic")
