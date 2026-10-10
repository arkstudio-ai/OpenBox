"""Pin resource journals without replacing existing resource or effect evidence."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pa5f6a7b8c9d"
down_revision = "pa4e5f6a7b8c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("resource_control_leases") as batch:
        batch.add_column(sa.Column("remote_journal_id", sa.String(32)))
        batch.add_column(sa.Column("remote_status", postgresql.JSONB().with_variant(sa.Text(), "sqlite")))
        batch.create_check_constraint("ck_resource_control_journal",
            "remote_journal_id IS NULL OR length(remote_journal_id) = 32")


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM resource_control_leases WHERE remote_journal_id IS NOT NULL")):
        raise RuntimeError("Pinned resource journal identities must be retained")
    with op.batch_alter_table("resource_control_leases") as batch:
        batch.drop_constraint("ck_resource_control_journal", type_="check")
        batch.drop_column("remote_status")
        batch.drop_column("remote_journal_id")
