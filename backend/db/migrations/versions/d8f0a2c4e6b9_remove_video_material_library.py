"""Remove OpenBox-owned provider material groups and real-person identities.

Revision ID: d8f0a2c4e6b9
Revises: c7d9e1f3a5b7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8f0a2c4e6b9"
down_revision: Union[str, None] = "c7d9e1f3a5b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("video_productions") as batch:
        batch.drop_column("character_identity_id")
        batch.drop_column("character_reference_type")

    op.drop_index("ix_video_material_assets_source", table_name="video_material_assets")
    op.drop_index(
        "ix_video_material_assets_user_updated", table_name="video_material_assets"
    )
    op.drop_table("video_material_assets")
    op.drop_index("ix_video_material_groups_status", table_name="video_material_groups")
    op.drop_index(
        "ix_video_material_groups_user_updated", table_name="video_material_groups"
    )
    op.drop_table("video_material_groups")


def downgrade() -> None:
    op.create_table(
        "video_material_groups",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column(
            "project_name",
            sa.String(length=128),
            server_default=sa.text("'default'"),
            nullable=False,
        ),
        sa.Column("group_type", sa.String(length=24), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("provider_group_id", sa.String(length=160), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider_token", sa.Text(), nullable=True),
        sa.Column("authorization_url", sa.Text(), nullable=True),
        sa.Column("qr_code", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            "group_type",
            "label",
            name="uq_video_material_groups_user_label",
        ),
        sa.UniqueConstraint(
            "provider", "provider_group_id", name="uq_video_material_groups_provider_id"
        ),
    )
    op.create_index(
        "ix_video_material_groups_user_updated",
        "video_material_groups",
        ["user_id", "updated_at"],
    )
    op.create_index(
        "ix_video_material_groups_status",
        "video_material_groups",
        ["status", "updated_at"],
    )

    op.create_table(
        "video_material_assets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("source_asset_id", sa.String(length=64), nullable=False),
        sa.Column("provider_asset_id", sa.String(length=160), nullable=True),
        sa.Column("asset_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["group_id"], ["video_material_groups.id"]),
        sa.ForeignKeyConstraint(["source_asset_id"], ["file_assets.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id", "source_asset_id", name="uq_video_material_asset_source"
        ),
        sa.UniqueConstraint(
            "provider_asset_id", name="uq_video_material_assets_provider_id"
        ),
    )
    op.create_index(
        "ix_video_material_assets_user_updated",
        "video_material_assets",
        ["user_id", "updated_at"],
    )
    op.create_index(
        "ix_video_material_assets_source",
        "video_material_assets",
        ["source_asset_id"],
    )

    with op.batch_alter_table("video_productions") as batch:
        batch.add_column(
            sa.Column(
                "character_reference_type",
                sa.String(length=24),
                server_default=sa.text("'virtual'"),
                nullable=False,
            )
        )
        batch.add_column(
            sa.Column("character_identity_id", sa.String(length=64), nullable=True)
        )
        batch.create_foreign_key(
            "fk_video_productions_character_identity",
            "video_material_groups",
            ["character_identity_id"],
            ["id"],
        )
