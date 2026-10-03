"""Persistent personal-assistant tasks, commands, results and user read position.

TaskResult is also the delivery outbox. Driver identities belong to the existing
Inbox and event log; a Submission is an accepted input, not an execution run.
"""
from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base, JSONType


class AssistantTask(Base):
    __tablename__ = "assistant_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    assistant_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    execution_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    desired_state: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    observed_state: Mapped[str] = mapped_column(String(24), nullable=False, default="queued")
    control_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    intent_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    latest_result_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("execution_session_id", name="uq_assistant_task_execution_session"),
        CheckConstraint("control_revision > 0 AND intent_revision > 0", name="ck_assistant_task_revisions"),
        CheckConstraint("desired_state IN ('running', 'paused', 'canceled')", name="ck_assistant_task_desired"),
        Index("ix_assistant_tasks_owner", "user_id", "workspace_id", "updated_at"),
        Index("ix_assistant_tasks_main", "assistant_session_id", "archived_at"),
    )


class AssistantCommand(Base):
    __tablename__ = "assistant_commands"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    assistant_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="accepted")
    receipt: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(nullable=False)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("actor_user_id", "workspace_id", "assistant_session_id", "idempotency_key",
                         name="uq_assistant_command_key"),
        CheckConstraint("length(idempotency_key) BETWEEN 1 AND 64", name="ck_assistant_command_key"),
        CheckConstraint("length(payload_digest) = 64", name="ck_assistant_command_digest"),
        Index("ix_assistant_commands_target", "target_type", "target_id"),
    )


class TaskSubmission(Base):
    __tablename__ = "assistant_task_submissions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("assistant_tasks.id"), nullable=False)
    command_id: Mapped[str] = mapped_column(ForeignKey("assistant_commands.id"), nullable=False)
    inbox_id: Mapped[str] = mapped_column(ForeignKey("agent_inbox_items.id"), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery: Mapped[str] = mapped_column(String(16), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(nullable=True)
    disposition: Mapped[str] = mapped_column(String(24), nullable=False, default="accepted")

    __table_args__ = (
        UniqueConstraint("command_id", name="uq_assistant_submission_command"),
        UniqueConstraint("inbox_id", name="uq_assistant_submission_inbox"),
        Index("ix_assistant_submissions_task", "task_id", "accepted_at"),
    )


class TaskResult(Base):
    __tablename__ = "assistant_task_results"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("assistant_tasks.id"), nullable=False)
    source_event_key: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    settlement_fence: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    consumed_inbox_ids: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    result_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    output_refs: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    observed_intent_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    report_attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    assistant_inbox_id: Mapped[str | None] = mapped_column(ForeignKey("agent_inbox_items.id"), nullable=True)
    processed_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(nullable=False)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        UniqueConstraint("task_id", "source_event_key", name="uq_assistant_result_terminal"),
        CheckConstraint("generation > 0 AND report_attempt > 0 AND retry_count >= 0",
                        name="ck_assistant_result_counters"),
        CheckConstraint("delivery_state IN ('pending', 'accepted', 'retry_wait', 'processed', 'blocked')",
                        name="ck_assistant_result_delivery"),
        Index("ix_assistant_results_delivery", "delivery_state", "available_at"),
        Index("ix_assistant_results_task", "task_id", "created_at"),
    )


class AssistantReadCursor(Base):
    __tablename__ = "assistant_read_cursors"

    assistant_session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    last_seen_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(nullable=False)

    __table_args__ = (
        CheckConstraint("last_seen_sequence >= 0", name="ck_assistant_read_sequence"),
    )
