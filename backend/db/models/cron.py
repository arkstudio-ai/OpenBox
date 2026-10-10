"""Cron jobs and runs ORM models."""
from datetime import datetime

from sqlalchemy import String, Boolean, CheckConstraint, ForeignKey, Integer, Index, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class CronJob(Base):
    """Scheduled task definition."""
    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("workspaces.id"), nullable=False
    )
    # The owning project: a task acts on its files and logs into its cron/.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional notify target — set when created from a chat; results are
    # injected there. NULL for tasks created from the management page.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Private assistant schedules use the durable Task/Inbox runner. They are
    # never handled by the legacy summary/injection/temp-session executor.
    assistant_session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True)
    assistant_command_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_commands.id"), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    # Basic info
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    # Schedule config (stored as JSON for flexibility)
    schedule: Mapped[dict] = mapped_column(JSONType, nullable=False)

    # Task config
    task_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    agent: Mapped[str] = mapped_column(String(64), server_default="build")
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, server_default=text("1800"))

    # Delivery config
    delivery: Mapped[dict] = mapped_column(JSONType, server_default="{}")

    # Marketing-autopilot budget authorisation (autopilot/template.py); NULL for plain tasks.
    template: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # Retry config
    delete_after_run: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_retries: Mapped[int] = mapped_column(Integer, server_default=text("3"))

    # Scheduler state
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    running_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    consecutive_errors: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    # Summary cache
    summary_cache: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_cache_msg_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Stats
    total_runs: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_successes: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    # Metadata
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("revision > 0", name="ck_cron_revision"),
        CheckConstraint("(assistant_session_id IS NULL) = (assistant_command_id IS NULL)", name="ck_cron_assistant_binding"),
        # Timer query: find due jobs efficiently
        Index(
            "ix_cron_jobs_timer",
            "enabled", "next_run_at",
            postgresql_where=text("NOT is_deleted AND enabled = true"),
        ),
        # User's jobs
        Index(
            "ix_cron_jobs_user",
            "user_id",
            postgresql_where=text("NOT is_deleted"),
        ),
        Index("ix_cron_jobs_workspace_active", "workspace_id", "is_deleted"),
        # Session's jobs
        Index(
            "ix_cron_jobs_session",
            "session_id",
            postgresql_where=text("NOT is_deleted"),
        ),
        # Project's jobs
        Index("ix_cron_jobs_project", "project_id"),
    )


class CronRun(Base):
    """Execution history for cron jobs."""
    __tablename__ = "cron_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    temp_session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    assistant_task_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_tasks.id"), nullable=True)
    assistant_submission_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_task_submissions.id"), nullable=True)
    assistant_configuration_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_commands.id"), nullable=True)
    assistant_result_id: Mapped[str | None] = mapped_column(ForeignKey("assistant_task_results.id"), nullable=True)
    assistant_slot: Mapped[str | None] = mapped_column(String(64), nullable=True)

    trace_context: Mapped[dict | None] = mapped_column(JSONType, nullable=True)

    # Execution state
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Content
    task_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Injection state
    injected: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    injected_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Token stats
    input_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    total_tokens: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    duration_ms: Mapped[int] = mapped_column(Integer, server_default=text("0"))

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("assistant_task_id", name="uq_cron_assistant_task"),
        UniqueConstraint("assistant_submission_id", name="uq_cron_assistant_submission"),
        Index("uq_cron_assistant_slot", "job_id", "assistant_slot", unique=True),
        Index("uq_cron_assistant_active", "job_id", unique=True,
              postgresql_where=text("assistant_task_id IS NOT NULL AND ended_at IS NULL"),
              sqlite_where=text("assistant_task_id IS NOT NULL AND ended_at IS NULL")),
        CheckConstraint("(assistant_task_id IS NULL AND assistant_submission_id IS NULL AND assistant_configuration_id IS NULL AND assistant_slot IS NULL) OR (assistant_task_id IS NOT NULL AND assistant_submission_id IS NOT NULL AND assistant_configuration_id IS NOT NULL AND assistant_slot IS NOT NULL)", name="ck_cron_run_assistant_binding"),
        # Pending injection query
        Index(
            "ix_cron_runs_pending",
            "session_id", "injected",
            postgresql_where=text("status = 'ok' AND injected = false"),
        ),
        # Job history (newest first)
        Index("ix_cron_runs_job", "job_id", "started_at"),
        # Cleanup query
        Index(
            "ix_cron_runs_cleanup",
            "started_at",
            postgresql_where=text("temp_session_id IS NOT NULL"),
        ),
    )
