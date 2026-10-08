"""Physical resource leases and existing effect-ledger admission fences."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "pa4e5f6a7b8c"
down_revision = "pa3d4e5f6a7b"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("resource_control_leases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("resource_type", sa.String(24), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("physical_id", sa.String(160), nullable=False),
        sa.Column("workspace_id", sa.String(64), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("desktop_record_id", sa.String(64), sa.ForeignKey("cloud_desktops.id")),
        sa.Column("owner_kind", sa.String(16), nullable=False),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("admission_state", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("last_observation_ref", postgresql.JSONB().with_variant(sa.Text(), "sqlite")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("epoch > 0", name="ck_resource_control_epoch"),
        sa.CheckConstraint("owner_kind IN ('automation', 'human')", name="ck_resource_control_owner"),
        sa.CheckConstraint("status IN ('active', 'draining', 'hold')", name="ck_resource_control_status"),
        sa.CheckConstraint("admission_state IN ('open', 'closed')", name="ck_resource_control_admission"),
        sa.CheckConstraint("status = 'active' OR admission_state = 'closed'", name="ck_resource_control_hold"),
        sa.CheckConstraint("owner_kind != 'human' OR expires_at IS NOT NULL", name="ck_resource_control_human_expiry"))
    op.create_index("uq_resource_control_physical", "resource_control_leases",
        ["provider", "resource_type", "physical_id"], unique=True)
    op.create_index("ix_resource_control_workspace", "resource_control_leases", ["workspace_id", "status"])
    # Batch also supports the single-user SQLite migration path.
    with op.batch_alter_table("external_effects") as batch:
        batch.add_column(sa.Column("resource_id", sa.String(64)))
        batch.add_column(sa.Column("resource_epoch", sa.Integer()))
        batch.add_column(sa.Column("resource_owner_kind", sa.String(16)))
        batch.add_column(sa.Column("resource_owner_id", sa.String(64)))
        batch.create_foreign_key("fk_external_effect_resource", "resource_control_leases", ["resource_id"], ["id"])
        batch.create_check_constraint("ck_external_effect_resource_fence",
            "(resource_id IS NULL AND resource_epoch IS NULL AND resource_owner_kind IS NULL AND resource_owner_id IS NULL) "
            "OR (resource_id IS NOT NULL AND resource_epoch IS NOT NULL AND resource_epoch > 0 "
            "AND resource_owner_kind IS NOT NULL AND resource_owner_kind IN ('automation', 'human') AND resource_owner_id IS NOT NULL)")
        batch.create_index("ix_external_effect_resource", ["resource_id", "resource_epoch", "state"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM resource_control_leases")):
        raise RuntimeError("Resource control identities and fencing evidence must be retained")
    with op.batch_alter_table("external_effects") as batch:
        batch.drop_index("ix_external_effect_resource")
        batch.drop_constraint("ck_external_effect_resource_fence", type_="check")
        batch.drop_constraint("fk_external_effect_resource", type_="foreignkey")
        for name in ("resource_owner_id", "resource_owner_kind", "resource_epoch", "resource_id"):
            batch.drop_column(name)
    op.drop_table("resource_control_leases")
