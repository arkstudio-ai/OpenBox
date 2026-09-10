"""Track mobile lifecycle independently of socket connectivity.

Revision ID: c3d5e7f9a1b2
Revises: a9c1e3f5b7d2
"""
from alembic import op
import sqlalchemy as sa

revision = "c3d5e7f9a1b2"
down_revision = "a9c1e3f5b7d2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mobile_presence",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("mobile_session_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.execute("""INSERT INTO mobile_presence (user_id, mobile_session_id, state, sequence, reported_at)
        SELECT user_id, session_id, 'detached', 0, updated_at FROM mobile_sessions""")


def downgrade():
    op.drop_table("mobile_presence")
