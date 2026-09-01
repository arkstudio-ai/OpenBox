"""Cron jobs and runs ORM models."""
from datetime import datetime

from sqlalchemy import (
    String,
    Boolean,
    Integer,
    BigInteger,
    Index,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class CronJob(Base):
    """Scheduled task definition."""
    __tablename__ = "cron_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The owning project: a task acts on its files and logs into its cron/.
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Optional notify target — set when created from a chat; results are
    # injected there. NULL for tasks created from the management page.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    # Retry config
    delete_after_run: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    max_retries: Mapped[int] = mapped_column(Integer, server_default=text("3"))

    # Scheduler state
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    running_at: Mapped[datetime | None] = mapped_column(nullable=True)
    # Durable scheduler ownership. ``running_at`` remains the public/product
    # marker; these fields decide which backend replica is allowed to execute
    # and, critically, which result is allowed to clear/update the job.
    run_generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("0")
    )
    run_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(nullable=True)
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
        # Session's jobs
        Index(
            "ix_cron_jobs_session",
            "session_id",
            postgresql_where=text("NOT is_deleted"),
        ),
        # Project's jobs
        Index("ix_cron_jobs_project", "project_id"),
        # Startup recovery and competing schedulers only inspect expired leases.
        Index("ix_cron_jobs_lease", "lease_expires_at"),
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
    # Claim identity that created this run. Recovery uses it to mark only the
    # expired run, never a newer run for the same CronJob.
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_generation: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)

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
        # Pending injection query
        Index(
            "ix_cron_runs_pending",
            "session_id", "injected",
            postgresql_where=text("status = 'ok' AND injected = false"),
        ),
        # Job history (newest first)
        Index("ix_cron_runs_job", "job_id", "started_at"),
        Index("ix_cron_runs_claim", "job_id", "claim_generation"),
        # Cleanup query
        Index(
            "ix_cron_runs_cleanup",
            "started_at",
            postgresql_where=text("temp_session_id IS NOT NULL"),
        ),
    )


class CronDeliveryOutbox(Base):
    """Durable, claimed delivery work created by a fenced Cron settlement.

    ``id`` is the externally visible delivery receipt.  It is stable for one
    run/kind pair, so retries after an ambiguous crash can be deduplicated by
    the local message/runlog sinks and by webhook receivers.
    """

    __tablename__ = "cron_delivery_outbox"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)

    # pending -> processing -> delivered.  An expired processing lease is
    # claimable again; failed attempts return to pending with backoff.
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_owner: Mapped[str | None] = mapped_column(String(160), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("run_id", "kind", name="uq_cron_delivery_run_kind"),
        Index(
            "ix_cron_delivery_claim",
            "state",
            "available_at",
            "claim_expires_at",
        ),
        Index("ix_cron_delivery_run", "run_id"),
        Index(
            "ix_cron_delivery_session",
            "session_id",
            "kind",
            "state",
            "available_at",
        ),
    )
