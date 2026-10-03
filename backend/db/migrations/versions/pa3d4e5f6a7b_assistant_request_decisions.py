"""One durable decision per assistant-linked human request."""
from alembic import op
import sqlalchemy as sa

revision = "pa3d4e5f6a7b"
down_revision = "pa2c3d4e5f6a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("uq_assistant_request_decision", "assistant_commands",
        ["target_type", "target_id"], unique=True,
        postgresql_where=sa.text("action = 'request_reply'"),
        sqlite_where=sa.text("action = 'request_reply'"))


def downgrade():
    # Removing the arbiter would permit competing effective decisions.
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM assistant_commands WHERE action = 'request_reply'")):
        raise RuntimeError("Assistant request decisions must retain their uniqueness boundary")
    op.drop_index("uq_assistant_request_decision", table_name="assistant_commands")
