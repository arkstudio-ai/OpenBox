"""Add ECD fleet snapshots, alerts, purchases, and pool state.

Revision ID: a3f1e5c7d9b2
Revises: c3e5a7b9d1f4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a3f1e5c7d9b2"
down_revision: Union[str, None] = "c3e5a7b9d1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_cloud_desktops_workspace_active", table_name="cloud_desktops")
    with op.batch_alter_table("cloud_desktops") as batch:
        batch.alter_column(
            "workspace_id", existing_type=sa.String(64), nullable=True
        )
        batch.add_column(
            sa.Column(
                "pool_state",
                sa.String(16),
                nullable=False,
                server_default="assigned",
            )
        )
        batch.add_column(sa.Column("pool", sa.String(16), nullable=True))
        batch.add_column(sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("spec", sa.String(48), nullable=True))
        batch.add_column(sa.Column("golden_image_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("last_snapshot_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.create_check_constraint(
            "ck_cloud_desktops_pool_state",
            "pool_state IN ('reserve', 'prewarm', 'assigned', 'released', "
            "'recycling', 'retired', 'assigning')",
        )
    op.create_index(
        "ix_cloud_desktops_workspace_active",
        "cloud_desktops",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false AND workspace_id IS NOT NULL"),
        sqlite_where=sa.text("is_deleted = 0 AND workspace_id IS NOT NULL"),
    )

    op.create_table(
        "fleet_snapshots",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("taken_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_fleet_snapshots_taken", "fleet_snapshots", ["taken_at"])
    op.create_index(
        "ix_fleet_snapshots_source_taken", "fleet_snapshots", ["source", "taken_at"]
    )

    op.create_table(
        "fleet_alerts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("rule", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(96), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acked_by", sa.String(64), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("muted_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "uq_fleet_alerts_open_rule_resource",
        "fleet_alerts",
        ["rule", "resource_id"],
        unique=True,
        postgresql_where=sa.text("resolved_at IS NULL"),
        sqlite_where=sa.text("resolved_at IS NULL"),
    )
    op.create_index(
        "ix_fleet_alerts_state_seen", "fleet_alerts", ["resolved_at", "last_seen_at"]
    )

    op.create_table(
        "pool_purchases",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("desktop_id", sa.String(96), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.create_index("ix_pool_purchases_created", "pool_purchases", ["created_at"])


def downgrade() -> None:
    connection = op.get_bind()
    unowned = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM cloud_desktops "
            "WHERE workspace_id IS NULL AND is_deleted = false"
        )
    ).scalar_one()
    if unowned:
        raise RuntimeError(
            f"cannot downgrade while {unowned} active pool desktop(s) are unassigned"
        )
    op.drop_index("ix_pool_purchases_created", table_name="pool_purchases")
    op.drop_table("pool_purchases")
    op.drop_index("ix_fleet_alerts_state_seen", table_name="fleet_alerts")
    op.drop_index("uq_fleet_alerts_open_rule_resource", table_name="fleet_alerts")
    op.drop_table("fleet_alerts")
    op.drop_index("ix_fleet_snapshots_source_taken", table_name="fleet_snapshots")
    op.drop_index("ix_fleet_snapshots_taken", table_name="fleet_snapshots")
    op.drop_table("fleet_snapshots")

    op.drop_index("ix_cloud_desktops_workspace_active", table_name="cloud_desktops")
    with op.batch_alter_table("cloud_desktops") as batch:
        batch.drop_constraint("ck_cloud_desktops_pool_state", type_="check")
        batch.drop_column("last_snapshot_at")
        batch.drop_column("golden_image_id")
        batch.drop_column("spec")
        batch.drop_column("released_at")
        batch.drop_column("assigned_at")
        batch.drop_column("pool")
        batch.drop_column("pool_state")
        batch.alter_column(
            "workspace_id", existing_type=sa.String(64), nullable=False
        )
    op.create_index(
        "ix_cloud_desktops_workspace_active",
        "cloud_desktops",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("is_deleted = false"),
        sqlite_where=sa.text("is_deleted = 0"),
    )
