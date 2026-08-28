"""Durable ledger for generic skill script jobs.

PostgreSQL (SQLite in single-user mode) is the single source of truth for a
job's admission, scheduling, waits, recovery and result; Redis/WS only carry
notifications (docs/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md §6.1).
"""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class SkillJob(Base):
    __tablename__ = "skill_jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(64), ForeignKey("users.id"), nullable=False)
    #: Origin/notification target only — never decides the job's lifetime.
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: e.g. "builtin:video-production" or an install snapshot reference.
    skill_key: Mapped[str] = mapped_column(String(160), nullable=False)
    skill_version: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("''"))
    package_sha256: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    operation: Mapped[str] = mapped_column(String(80), nullable=False)
    #: internal | sandbox (skill_runtime.types.RuntimeKind).
    runtime_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    #: Scheduling resource pool, orthogonal to runtime_kind.
    queue_name: Mapped[str] = mapped_column(String(40), nullable=False, server_default=text("'default'"))

    #: skill_runtime.types.JobStatus value.
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    #: Skill-defined phase (declared in the manifest), e.g. "provider_generate".
    phase: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))

    input_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    #: Admission-time output contract. A later manifest deploy must not change
    #: what an already-running handler is allowed to settle as success.
    output_schema: Mapped[dict] = mapped_column(JSONType, default=dict)
    checkpoint_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    progress_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    result_data: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    idempotency_key: Mapped[str] = mapped_column(String(180), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, server_default=text("''"))
    #: run | cancel — cancellation is a desired state, not a task kill.
    desired_state: Mapped[str] = mapped_column(String(8), nullable=False, server_default=text("'run'"))

    #: Monotonic invocation/claim sequence used by the audit trail. Planned
    #: provider polls increment this value but do not consume retry budget.
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: Fault budget only: incremented for Retry outcomes and lost leases, never
    #: for a successful bounded step or a normal waiting_external poll.
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("8"))
    next_run_at: Mapped[datetime | None] = mapped_column(nullable=True)
    #: Hard total deadline derived from the manifest's maxTotalSeconds.
    deadline_at: Mapped[datetime | None] = mapped_column(nullable=True)

    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: Fencing token: incremented on every claim; all progress/terminal writes
    #: carry the claim's token so a stale worker can never settle a new claim.
    lease_token: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    handler_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    image_digest: Mapped[str] = mapped_column(String(128), nullable=False, server_default=text("''"))

    #: Admission-time operation policy snapshot. Running jobs must not silently
    #: inherit changed limits or cancellation semantics from a later deploy.
    invocation_timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("120")
    )
    max_external_wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("86400")
    )
    user_input_timeout_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancel_requires_handler: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    #: Cumulative wall time spent parked in waiting_external. The start marker
    #: is set on park and folded into external_wait_seconds by every wake path.
    external_wait_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    external_wait_started_at: Mapped[datetime | None] = mapped_column(nullable=True)

    #: Event sequence allocator: bumped in the same guarded UPDATE that changes
    #: state, so seq assignment rides the row lock instead of racing MAX(seq).
    last_event_seq: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))

    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "skill_key", "operation", "idempotency_key",
            name="uq_skill_jobs_idempotency",
        ),
        Index("ix_skill_jobs_claim", "status", "next_run_at", "queue_name"),
        Index("ix_skill_jobs_user_created", "user_id", "created_at"),
        Index("ix_skill_jobs_session_created", "session_id", "created_at"),
        Index(
            "ix_skill_jobs_running_lease",
            "lease_expires_at",
            postgresql_where=text("status = 'running'"),
            sqlite_where=text("status = 'running'"),
        ),
    )
