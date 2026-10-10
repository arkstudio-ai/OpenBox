"""Keep private runtime authority separate from legacy workspace resources."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pb1c2d3e4f5a"
down_revision = "pa8b9c0d1e2f"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("private_runtimes",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("actor_user_id", sa.String(64), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("isolation_mode", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("provision_phase", sa.String(32), nullable=False),
        sa.Column("claim_token", sa.String(64)),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True)),
        sa.Column("container_name", sa.String(128), nullable=False, unique=True),
        sa.Column("container_id", sa.String(64), unique=True),
        sa.Column("workspace_volume", sa.String(128), unique=True),
        sa.Column("data_volume", sa.String(128), nullable=False, unique=True),
        sa.Column("volume_identities", postgresql.JSONB().with_variant(sa.Text(), "sqlite"), nullable=False),
        sa.Column("image", sa.String(256), nullable=False),
        sa.Column("image_id", sa.String(128)),
        sa.Column("host_port", sa.Integer()),
        sa.Column("route_key", sa.String(96), nullable=False, unique=True),
        sa.Column("api_key_ciphertext", sa.Text(), nullable=False),
        sa.Column("api_key_hash", sa.String(64), nullable=False),
        sa.Column("physical_digest", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "actor_user_id", "kind", name="uq_private_runtime_actor_kind"),
        sa.CheckConstraint("kind IN ('sandbox','browser_profile')", name="ck_private_runtime_kind"),
        sa.CheckConstraint("(kind = 'sandbox' AND isolation_mode = 'process_uid') OR (kind = 'browser_profile' AND isolation_mode IN ('chromium_sandbox','container_uid'))", name="ck_private_runtime_isolation"),
        sa.CheckConstraint("(kind = 'sandbox' AND workspace_volume IS NOT NULL) OR (kind = 'browser_profile' AND workspace_volume IS NULL)", name="ck_private_runtime_volumes"),
        sa.CheckConstraint("status IN ('reserved','provisioning','ready','blocked')", name="ck_private_runtime_status"),
        sa.CheckConstraint("revision > 0", name="ck_private_runtime_revision"),
        sa.CheckConstraint("host_port IS NULL OR (host_port > 0 AND host_port < 65536)", name="ck_private_runtime_port"),
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM private_runtimes")):
        raise RuntimeError("Private runtime bindings must be retained; downgrade cannot discard isolation authority")
    op.drop_table("private_runtimes")
