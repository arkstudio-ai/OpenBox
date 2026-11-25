"""Index verified account phone numbers for platform admin lookup."""

from alembic import op
import sqlalchemy as sa

revision = "pt0a6b7c8d9e"
down_revision = "ps9f5a6b7c8d"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("phone_number", sa.String(16), nullable=True))
    op.create_index("ix_users_phone_number", "users", ["phone_number"])


def downgrade():
    op.drop_index("ix_users_phone_number", table_name="users")
    op.drop_column("users", "phone_number")
