"""Turn summaries for the lite admin view (docs/SESSION_TRAJECTORY_LITE_PLAN.md §6.1).

``trajectory_records.turn_id`` is added nullable: rows projected before this
migration keep NULL until ``python -m trajectory.ops.turns_backfill`` copies the
id out of their ``summary`` JSON and rebuilds the per-turn rows. New projection
batches fill both from here on.
"""
from alembic import op
import sqlalchemy as sa

from db.base import JSONType

revision = "t0006_turn_summaries"
down_revision = "t0005_recorded_audiences"
branch_labels = None
depends_on = None


def upgrade():
    offline = op.get_context().as_sql
    inspector = None if offline else sa.inspect(op.get_bind())
    if offline or "turn_id" not in {column["name"] for column in inspector.get_columns("trajectory_records")}:
        op.add_column("trajectory_records", sa.Column("turn_id", sa.String(128), nullable=True))
    op.create_index("ix_trajectory_records_turn", "trajectory_records", ["trajectory_id", "turn_id", "start_seq"],
                    if_not_exists=True)
    if offline or not inspector.has_table("trajectory_turn_summaries"):
        op.create_table(
            "trajectory_turn_summaries",
            sa.Column("trajectory_id", sa.String(64), sa.ForeignKey("session_trajectories.id", ondelete="CASCADE"),
                      primary_key=True),
            sa.Column("turn_id", sa.String(128), primary_key=True),
            sa.Column("session_id", sa.String(64), nullable=False),
            sa.Column("user_id", sa.String(64), nullable=False),
            sa.Column("workspace_id", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("duration_ms", sa.BigInteger(), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("status_reason", sa.Text(), nullable=True),
            sa.Column("waiting_kind", sa.String(32), nullable=True),
            sa.Column("user_preview", sa.Text(), nullable=True),
            sa.Column("assistant_preview", sa.Text(), nullable=True),
            sa.Column("user_message_id", sa.String(128), nullable=True),
            sa.Column("assistant_message_id", sa.String(128), nullable=True),
            sa.Column("main_agent_id", sa.String(128), nullable=True),
            sa.Column("has_attachments", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("tool_counts", JSONType(), nullable=False),
            sa.Column("tool_failures", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("agent_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("job_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("artifact_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("request_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("input_tokens", sa.BigInteger(), nullable=True),
            sa.Column("output_tokens", sa.BigInteger(), nullable=True),
            sa.Column("usage_complete", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("first_seq", sa.BigInteger(), nullable=False),
            sa.Column("last_seq", sa.BigInteger(), nullable=False),
            sa.Column("applied_seq", sa.BigInteger(), nullable=False),
        )
    for name, columns in (("ix_trajectory_turn_summaries_owner", ["user_id", "started_at", "turn_id"]),
                          ("ix_trajectory_turn_summaries_trajectory", ["trajectory_id", "started_at"]),
                          ("ix_trajectory_turn_summaries_session", ["session_id", "started_at"]),
                          ("ix_trajectory_turn_summaries_status", ["status", "started_at"])):
        op.create_index(name, "trajectory_turn_summaries", columns, if_not_exists=True)


def downgrade():
    op.drop_table("trajectory_turn_summaries")
    op.drop_index("ix_trajectory_records_turn", table_name="trajectory_records")
    with op.batch_alter_table("trajectory_records") as batch:
        batch.drop_column("turn_id")
