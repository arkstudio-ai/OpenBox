"""Persist private tool exposure state and provider transcript parts.

Revision ID: c7d9e1f3a5b7
Revises: b6d8f0a2c4e6
Create Date: 2026-08-30

The migration is deliberately expand/backfill/contract in one revision.  The
runtime writer remains feature-gated by ``tool_exposure.mode``; operators can
deploy the reader/schema everywhere before enabling writes.
"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c7d9e1f3a5b7"
down_revision: Union[str, None] = "b6d8f0a2c4e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json_type():
    return postgresql.JSONB().with_variant(sa.Text(), "sqlite")


def upgrade() -> None:
    # Expand first so a rolling old reader keeps working while old rows are
    # backfilled.  batch_alter_table makes the contract step valid on the
    # desktop SQLite database as well as PostgreSQL.
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(
            sa.Column(
                "tool_exposure_state",
                _json_type(),
                server_default=sa.text("'{}'"),
                nullable=True,
            )
        )
    op.execute(
        sa.text(
            "UPDATE sessions SET tool_exposure_state = '{}' "
            "WHERE tool_exposure_state IS NULL"
        )
    )
    with op.batch_alter_table("sessions") as batch:
        batch.alter_column(
            "tool_exposure_state",
            existing_type=_json_type(),
            existing_server_default=sa.text("'{}'"),
            server_default=sa.text("'{}'"),
            nullable=False,
        )

    with op.batch_alter_table("parts") as batch:
        batch.add_column(sa.Column("stream_seq", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("canonical_tool_id", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("wire_tool_name", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("provider_binding_digest", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("provider_dialect", sa.String(length=64), nullable=True))
    op.create_index("ix_parts_message_stream", "parts", ["message_id", "stream_seq"])
    op.create_index(
        "ix_parts_canonical_tool",
        "parts",
        ["session_id", "canonical_tool_id"],
    )

    op.create_table(
        "internal_parts",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("capability_key_digest", sa.String(length=64), nullable=False),
        sa.Column("response_chain_id", sa.String(length=128), nullable=False),
        sa.Column("stream_seq", sa.Integer(), nullable=False),
        sa.Column("origin_seq", sa.BigInteger(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=64), nullable=True),
        sa.Column("data", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "dedupe_key",
            name="uq_internal_parts_session_dedupe",
        ),
        sa.UniqueConstraint(
            "session_id",
            "origin_seq",
            name="uq_internal_parts_session_origin",
        ),
    )
    op.create_index(
        "ix_internal_parts_message_stream",
        "internal_parts",
        ["message_id", "stream_seq"],
    )
    op.create_index(
        "ix_internal_parts_session_kind_origin",
        "internal_parts",
        ["session_id", "kind", "origin_seq"],
    )
    op.create_index(
        "ix_internal_parts_replay_binding",
        "internal_parts",
        ["session_id", "capability_key_digest", "response_chain_id", "stream_seq"],
    )
    op.create_index("ix_internal_parts_user", "internal_parts", ["user_id"])


def downgrade() -> None:
    """Refuse to discard the only copy of private replay/reveal evidence.

    Operators must first disable writers, drain native responses, project
    closed chains into provider-neutral history, and explicitly clear both the
    internal table and state JSON.  A stricter preflight is safer than a lossy
    automatic conversion inside a schema migration.
    """
    bind = op.get_bind()
    internal_count = bind.execute(sa.text("SELECT COUNT(*) FROM internal_parts")).scalar_one()
    tool_identity_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM parts WHERE stream_seq IS NOT NULL "
            "OR canonical_tool_id IS NOT NULL OR wire_tool_name IS NOT NULL "
            "OR provider_binding_digest IS NOT NULL OR provider_dialect IS NOT NULL"
        )
    ).scalar_one()
    dirty_state_count = 0
    for (raw_state,) in bind.execute(
        sa.text("SELECT tool_exposure_state FROM sessions")
    ):
        state = raw_state
        if isinstance(state, str):
            try:
                state = json.loads(state)
            except (TypeError, ValueError):
                dirty_state_count += 1
                continue
        if state in (None, {}):
            continue
        if not isinstance(state, dict):
            dirty_state_count += 1
            continue
        if state.get("agents") or state.get("provider_fallback"):
            dirty_state_count += 1
    if internal_count or dirty_state_count or tool_identity_count:
        raise RuntimeError(
            "tool exposure downgrade refused: disable writers and clear/project "
            "internal_parts, ToolPart identities, and session exposure state first"
        )

    op.drop_index("ix_internal_parts_user", table_name="internal_parts")
    op.drop_index("ix_internal_parts_replay_binding", table_name="internal_parts")
    op.drop_index("ix_internal_parts_session_kind_origin", table_name="internal_parts")
    op.drop_index("ix_internal_parts_message_stream", table_name="internal_parts")
    op.drop_table("internal_parts")
    op.drop_index("ix_parts_canonical_tool", table_name="parts")
    op.drop_index("ix_parts_message_stream", table_name="parts")
    with op.batch_alter_table("parts") as batch:
        batch.drop_column("provider_dialect")
        batch.drop_column("provider_binding_digest")
        batch.drop_column("wire_tool_name")
        batch.drop_column("canonical_tool_id")
        batch.drop_column("stream_seq")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("tool_exposure_state")
