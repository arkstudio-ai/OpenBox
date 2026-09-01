"""Add durable continuable subagent protocol.

Revision ID: fe6a8c0e2b4d
Revises: fd5f7a9c1e3b
Create Date: 2026-08-31
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "fe6a8c0e2b4d"
down_revision: Union[str, None] = "fd5f7a9c1e3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "subagent_descriptors",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("parent_session_id", sa.String(length=64), nullable=False),
        sa.Column("child_session_id", sa.String(length=64), nullable=False),
        sa.Column("root_session_id", sa.String(length=64), nullable=False),
        sa.Column("parent_descriptor_id", sa.String(length=64), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("subagent_type", sa.String(length=64), nullable=False),
        sa.Column("lifecycle", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("active_activation_id", sa.String(length=64), nullable=True),
        sa.Column("interrupt_requested_generation", sa.Integer(), nullable=True),
        sa.Column("interrupt_applied_generation", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("depth >= 1", name="ck_subagent_descriptors_depth"),
        sa.CheckConstraint("generation >= 1", name="ck_subagent_descriptors_generation"),
        sa.CheckConstraint(
            "lifecycle IN ('one_shot', 'continuable')",
            name="ck_subagent_descriptors_lifecycle",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'settled', 'interrupted', 'error')",
            name="ck_subagent_descriptors_state",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["root_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_descriptor_id"], ["subagent_descriptors.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "child_session_id", name="uq_subagent_descriptors_child_session"
        ),
    )
    op.create_index(
        "ix_subagent_descriptors_parent_state",
        "subagent_descriptors",
        ["parent_session_id", "user_id", "state"],
    )
    op.create_index(
        "ix_subagent_descriptors_project_state",
        "subagent_descriptors",
        ["project_id", "user_id", "state"],
    )

    op.create_table(
        "subagent_activations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("descriptor_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("parent_session_id", sa.String(length=64), nullable=False),
        sa.Column("parent_message_id", sa.String(length=64), nullable=False),
        sa.Column("parent_part_id", sa.String(length=64), nullable=False),
        sa.Column("parent_run_id", sa.String(length=64), nullable=False),
        sa.Column("parent_generation", sa.Integer(), nullable=False),
        sa.Column("descriptor_generation", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("child_session_id", sa.String(length=64), nullable=False),
        sa.Column("child_trigger_message_id", sa.String(length=64), nullable=False),
        sa.Column("child_run_id", sa.String(length=64), nullable=True),
        sa.Column("child_generation", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=16), server_default="accepted", nullable=False),
        sa.Column("claim_token", sa.String(length=64), nullable=True),
        sa.Column("claim_owner", sa.String(length=160), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_title", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('spawn', 'follow_up')", name="ck_subagent_activations_kind"
        ),
        sa.CheckConstraint(
            "state IN ('accepted', 'claimed', 'bound', 'completed', 'abandoned')",
            name="ck_subagent_activations_state",
        ),
        sa.CheckConstraint(
            "parent_generation > 0", name="ck_subagent_activations_parent_generation"
        ),
        sa.CheckConstraint(
            "descriptor_generation > 0",
            name="ck_subagent_activations_descriptor_generation",
        ),
        sa.CheckConstraint(
            "child_generation IS NULL OR child_generation > 0",
            name="ck_subagent_activations_child_generation",
        ),
        sa.ForeignKeyConstraint(
            ["descriptor_id"], ["subagent_descriptors.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["parent_part_id"], ["parts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["child_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["child_trigger_message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "parent_part_id", name="uq_subagent_activations_parent_part"
        ),
        sa.UniqueConstraint(
            "descriptor_id", "descriptor_generation",
            name="uq_subagent_activations_descriptor_generation",
        ),
        sa.UniqueConstraint(
            "child_trigger_message_id", name="uq_subagent_activations_trigger"
        ),
    )
    op.create_index(
        "ix_subagent_activations_claim",
        "subagent_activations",
        ["state", "claim_expires_at", "created_at"],
    )
    op.create_index(
        "ix_subagent_activations_child_state",
        "subagent_activations",
        ["child_session_id", "state"],
    )
    op.create_index(
        "ix_subagent_activations_parent_state",
        "subagent_activations",
        ["parent_session_id", "state"],
    )

    op.create_table(
        "subagent_outbox",
        sa.Column("activation_id", sa.String(length=64), nullable=False),
        sa.Column("descriptor_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("parent_session_id", sa.String(length=64), nullable=False),
        sa.Column("parent_message_id", sa.String(length=64), nullable=False),
        sa.Column("parent_part_id", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="waiting", nullable=False),
        sa.Column("outcome", sa.String(length=24), server_default="waiting", nullable=False),
        sa.Column("result_payload", _json_type(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('waiting', 'ready', 'delivered')",
            name="ck_subagent_outbox_state",
        ),
        sa.CheckConstraint(
            "outcome IN ('waiting', 'succeeded', 'interrupted', "
            "'outcome_unknown', 'error')",
            name="ck_subagent_outbox_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["activation_id"], ["subagent_activations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["descriptor_id"], ["subagent_descriptors.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["parent_session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"], ["messages.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["parent_part_id"], ["parts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("activation_id"),
    )
    op.create_index(
        "ix_subagent_outbox_parent_state",
        "subagent_outbox",
        ["parent_session_id", "state"],
    )
    op.create_index(
        "ix_subagent_outbox_descriptor",
        "subagent_outbox",
        ["descriptor_id", "state"],
    )


def downgrade() -> None:
    count = op.get_bind().execute(
        sa.text("SELECT COUNT(*) FROM subagent_descriptors")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "subagent continuation downgrade refused: descriptors still exist"
        )
    op.drop_index("ix_subagent_outbox_descriptor", table_name="subagent_outbox")
    op.drop_index("ix_subagent_outbox_parent_state", table_name="subagent_outbox")
    op.drop_table("subagent_outbox")
    op.drop_index(
        "ix_subagent_activations_parent_state", table_name="subagent_activations"
    )
    op.drop_index(
        "ix_subagent_activations_child_state", table_name="subagent_activations"
    )
    op.drop_index("ix_subagent_activations_claim", table_name="subagent_activations")
    op.drop_table("subagent_activations")
    op.drop_index(
        "ix_subagent_descriptors_project_state", table_name="subagent_descriptors"
    )
    op.drop_index(
        "ix_subagent_descriptors_parent_state", table_name="subagent_descriptors"
    )
    op.drop_table("subagent_descriptors")
