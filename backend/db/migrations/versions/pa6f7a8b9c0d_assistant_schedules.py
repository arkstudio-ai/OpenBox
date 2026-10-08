"""Durable private schedule commands and distinct CronRun/Task identities."""
from alembic import op
import sqlalchemy as sa

revision = "pa6f7a8b9c0d"
down_revision = "pa5f6a7b8c9d"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("cron_jobs") as batch:
        batch.add_column(sa.Column("assistant_session_id", sa.String(64)))
        batch.add_column(sa.Column("assistant_command_id", sa.String(64)))
        batch.add_column(sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
        batch.create_foreign_key("fk_cron_assistant_session", "sessions", ["assistant_session_id"], ["id"])
        batch.create_foreign_key("fk_cron_assistant_command", "assistant_commands", ["assistant_command_id"], ["id"])
        batch.create_check_constraint("ck_cron_revision", "revision > 0")
        batch.create_check_constraint("ck_cron_assistant_binding", "(assistant_session_id IS NULL) = (assistant_command_id IS NULL)")
    with op.batch_alter_table("cron_runs") as batch:
        for field, table in (("assistant_task_id", "assistant_tasks"), ("assistant_submission_id", "assistant_task_submissions"),
                             ("assistant_configuration_id", "assistant_commands"), ("assistant_result_id", "assistant_task_results")):
            batch.add_column(sa.Column(field, sa.String(64)))
            batch.create_foreign_key("fk_cron_run_" + field, table, [field], ["id"])
        batch.add_column(sa.Column("assistant_slot", sa.String(64)))
        batch.create_unique_constraint("uq_cron_assistant_task", ["assistant_task_id"])
        batch.create_unique_constraint("uq_cron_assistant_submission", ["assistant_submission_id"])
        batch.create_index("uq_cron_assistant_slot", ["job_id", "assistant_slot"], unique=True)
        batch.create_index("uq_cron_assistant_active", ["job_id"], unique=True,
            postgresql_where=sa.text("assistant_task_id IS NOT NULL AND ended_at IS NULL"),
            sqlite_where=sa.text("assistant_task_id IS NOT NULL AND ended_at IS NULL"))
        batch.create_check_constraint("ck_cron_run_assistant_binding",
            "(assistant_task_id IS NULL AND assistant_submission_id IS NULL AND assistant_configuration_id IS NULL AND assistant_slot IS NULL) OR (assistant_task_id IS NOT NULL AND assistant_submission_id IS NOT NULL AND assistant_configuration_id IS NOT NULL AND assistant_slot IS NOT NULL)")


def downgrade():
    if (op.get_bind().scalar(sa.text("SELECT count(*) FROM cron_jobs WHERE assistant_session_id IS NOT NULL"))
            or op.get_bind().scalar(sa.text("SELECT count(*) FROM cron_runs WHERE assistant_task_id IS NOT NULL"))):
        raise RuntimeError("Private schedule authority and execution receipts must be retained")
    with op.batch_alter_table("cron_runs") as batch:
        batch.drop_constraint("ck_cron_run_assistant_binding", type_="check")
        for name in ("uq_cron_assistant_slot", "uq_cron_assistant_active"):
            batch.drop_index(name)
        for name in ("uq_cron_assistant_task", "uq_cron_assistant_submission"):
            batch.drop_constraint(name, type_="unique")
        for field in ("assistant_task_id", "assistant_submission_id", "assistant_configuration_id", "assistant_result_id"):
            batch.drop_constraint("fk_cron_run_" + field, type_="foreignkey")
            batch.drop_column(field)
        batch.drop_column("assistant_slot")
    with op.batch_alter_table("cron_jobs") as batch:
        batch.drop_constraint("ck_cron_assistant_binding", type_="check")
        batch.drop_constraint("ck_cron_revision", type_="check")
        batch.drop_constraint("fk_cron_assistant_session", type_="foreignkey")
        batch.drop_constraint("fk_cron_assistant_command", type_="foreignkey")
        for field in ("assistant_session_id", "assistant_command_id", "revision"):
            batch.drop_column(field)
