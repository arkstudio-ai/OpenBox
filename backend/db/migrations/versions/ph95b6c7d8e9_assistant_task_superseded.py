"""Record which newer task replaced an archived one, so task chains fold in the list."""
from alembic import op
import sqlalchemy as sa

revision = "ph95b6c7d8e9"
down_revision = "ph84a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("assistant_tasks", sa.Column("superseded_by_task_id", sa.String(64), nullable=True))


def downgrade():
    op.drop_column("assistant_tasks", "superseded_by_task_id")
