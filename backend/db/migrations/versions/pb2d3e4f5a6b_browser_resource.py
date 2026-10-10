"""Pin independent browser profiles to private runtimes and resource leases."""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "pb2d3e4f5a6b"
down_revision = "pb1c2d3e4f5a"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("browser_resource_bindings",
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("resource_control_leases.id"), primary_key=True),
        sa.Column("private_runtime_id", sa.String(64), sa.ForeignKey("private_runtimes.id"), nullable=False, unique=True),
        sa.Column("runtime_revision", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("assistant_session_id", sa.String(64), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("identity", JSONType(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("actor_user_id", "workspace_id", name="uq_browser_resource_actor"))
    op.create_table("browser_resource_sessions",
        sa.Column("resource_id", sa.String(64), sa.ForeignKey("browser_resource_bindings.resource_id"), primary_key=True),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM browser_resource_bindings")):
        raise RuntimeError("Retained browser control identities must not be discarded by downgrade")
    op.drop_table("browser_resource_sessions")
    op.drop_table("browser_resource_bindings")
