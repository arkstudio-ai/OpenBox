"""Fence desktop installation and assignment with durable attempts."""
from alembic import op
import sqlalchemy as sa

revision = "pa8b9c0d1e2f"
down_revision = "pa7a8b9c0d1e"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cloud_desktops", sa.Column("channel_attempt_id", sa.String(64), nullable=True))
    op.add_column("cloud_desktops", sa.Column("channel_enrollment_grant", sa.String(64), nullable=True))


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT count(*) FROM cloud_desktops WHERE channel_attempt_id IS NOT NULL "
        "OR channel_enrollment_grant IS NOT NULL"
    )):
        raise RuntimeError("Desktop attempt authority must not be discarded by downgrade")
    op.drop_column("cloud_desktops", "channel_enrollment_grant")
    op.drop_column("cloud_desktops", "channel_attempt_id")
