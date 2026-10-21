"""Restart-safe automatic image descriptions; existing descriptions stay untouched."""
from alembic import op
import sqlalchemy as sa

revision = "ph73f4a5b6c7"
down_revision = "ph62e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("file_assets", sa.Column("description_state", sa.String(16), nullable=False, server_default="none"))
    op.add_column("file_assets", sa.Column("description_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("file_assets", sa.Column("description_lease_until", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_file_assets_description_work", "file_assets", ["description_state", "description_lease_until"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM file_assets WHERE description_state IN ('pending', 'processing')")):
        raise RuntimeError("Finish pending image descriptions before downgrade")
    op.drop_index("ix_file_assets_description_work", table_name="file_assets")
    op.drop_column("file_assets", "description_lease_until")
    op.drop_column("file_assets", "description_attempts")
    op.drop_column("file_assets", "description_state")
