"""Persist the guided production form on its Agent session."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pr8e4f5a6b7c"
down_revision = "pq7d3e4f5a6b"
branch_labels = None
depends_on = None


def upgrade():
    if not op.get_context().as_sql:
        inspector = sa.inspect(op.get_bind())
        # Local preview databases used the taxonomy revision ID for the form.
        # Reconcile that schema without rewriting the stamped revision or its data.
        if not inspector.has_table("light_taxonomy"):
            from db.migrations.versions.pq7d3e4f5a6b_light_taxonomy import upgrade as add_taxonomy

            add_taxonomy()
        if any(column["name"] == "light_production" for column in inspector.get_columns("sessions")):
            return
    op.add_column("sessions", sa.Column(
        "light_production", postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=True,
    ))


def downgrade():
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("light_production")
