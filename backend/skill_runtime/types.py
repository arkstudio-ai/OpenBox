"""Skill job status machine and shared type vocabulary.

docs/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md §7 owns the semantics. Every module
in the runtime imports statuses and transition rules from here; nothing else
may invent a status string.
"""
from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_EXTERNAL = "waiting_external"
    WAITING_USER = "waiting_user"
    WAITING_AGENT = "waiting_agent"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})

#: Waiting states hold no worker lease; a wake (callback, user input, due
#: reconciliation) moves them back to QUEUED.
WAITING_STATUSES = frozenset(
    {JobStatus.WAITING_EXTERNAL, JobStatus.WAITING_USER, JobStatus.WAITING_AGENT}
)

#: States a worker claim may take (together with next_run_at <= now).
CLAIMABLE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.RETRY_SCHEDULED})

_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.WAITING_EXTERNAL,
            JobStatus.WAITING_USER,
            JobStatus.WAITING_AGENT,
            JobStatus.RETRY_SCHEDULED,
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.WAITING_EXTERNAL: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.WAITING_USER: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.WAITING_AGENT: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    # §7.2 claims retry_scheduled directly (status IN (queued, retry_scheduled)
    # → running); the queued edge is the early wake (input arrives before the
    # retry timer), and cancel may win any unclaimed state.
    JobStatus.RETRY_SCHEDULED: frozenset(
        {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED}
    ),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    def __init__(self, src: JobStatus, dst: JobStatus):
        self.src = src
        self.dst = dst
        super().__init__(f"illegal skill job transition: {src.value} -> {dst.value}")


def can_transition(src: JobStatus, dst: JobStatus) -> bool:
    return dst in _TRANSITIONS[src]


def assert_transition(src: JobStatus, dst: JobStatus) -> None:
    if not can_transition(src, dst):
        raise InvalidTransition(src, dst)


def allowed_targets(src: JobStatus) -> frozenset[JobStatus]:
    return _TRANSITIONS[src]


class RuntimeKind(str, Enum):
    """Where handler code executes. Remote nodes are not a runtime — they are
    an adapter capability an internal handler delegates to (§4.4)."""

    INTERNAL = "internal"
    SANDBOX = "sandbox"


class DesiredState(str, Enum):
    RUN = "run"
    CANCEL = "cancel"


class JobEventType(str, Enum):
    CREATED = "job.created"
    CLAIMED = "job.claimed"
    PROGRESSED = "job.progressed"
    WAITING_EXTERNAL = "job.waiting_external"
    WAITING_USER = "job.waiting_user"
    NEEDS_AGENT = "job.needs_agent"
    RETRY_SCHEDULED = "job.retry_scheduled"
    CANCEL_REQUESTED = "job.cancel_requested"
    SUCCEEDED = "job.succeeded"
    FAILED = "job.failed"
    CANCELLED = "job.cancelled"


#: Event emitted when a job enters a given status. PROGRESSED/CLAIMED and
#: CANCEL_REQUESTED are not status entries and are emitted explicitly.
STATUS_EVENTS: dict[JobStatus, JobEventType] = {
    JobStatus.QUEUED: JobEventType.CREATED,
    JobStatus.WAITING_EXTERNAL: JobEventType.WAITING_EXTERNAL,
    JobStatus.WAITING_USER: JobEventType.WAITING_USER,
    JobStatus.WAITING_AGENT: JobEventType.NEEDS_AGENT,
    JobStatus.RETRY_SCHEDULED: JobEventType.RETRY_SCHEDULED,
    JobStatus.SUCCEEDED: JobEventType.SUCCEEDED,
    JobStatus.FAILED: JobEventType.FAILED,
    JobStatus.CANCELLED: JobEventType.CANCELLED,
}


class InputKind(str, Enum):
    """skill_job_inputs.kind — what woke the job or fed its next invocation."""

    USER_ANSWER = "user_answer"
    PROVIDER_CALLBACK = "provider_callback"
    AGENT_RESULT = "agent_result"
    OPERATOR_RESUME = "operator_resume"
