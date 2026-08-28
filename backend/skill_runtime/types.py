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


# ---------------------------------------------------------------------------
# Handler invocation outcomes (§5.2)
#
# Every invocation is one bounded, resumable step. Waiting outcomes must carry
# a checkpoint sufficient to resume from a different worker; checkpoints never
# contain plaintext secrets, long-lived signed URLs or large payloads.
# ---------------------------------------------------------------------------

from dataclasses import dataclass, field  # noqa: E402
from datetime import datetime  # noqa: E402
from typing import Any, Union  # noqa: E402


@dataclass(frozen=True)
class Succeeded:
    result: dict = field(default_factory=dict)
    #: file_assets ids to link as skill_job_artifacts, in order.
    artifacts: list = field(default_factory=list)


@dataclass(frozen=True)
class WaitExternal:
    checkpoint: dict
    wake_at: datetime
    external_handle: str | None = None
    progress: dict | None = None


@dataclass(frozen=True)
class WaitUser:
    checkpoint: dict
    prompt: str
    input_schema: dict = field(default_factory=dict)
    expires_at: datetime | None = None


@dataclass(frozen=True)
class NeedsAgent:
    checkpoint: dict
    reason: str
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Retry:
    checkpoint: dict
    error_code: str
    retry_at: datetime
    error_message: str = ""


@dataclass(frozen=True)
class Failed:
    error_code: str
    message: str
    retryable: bool = False


@dataclass(frozen=True)
class Cancelled:
    result: dict | None = None


Outcome = Union[Succeeded, WaitExternal, WaitUser, NeedsAgent, Retry, Failed, Cancelled]

#: Attempt-row outcome labels per outcome type.
ATTEMPT_OUTCOMES: dict[type, str] = {
    Succeeded: "succeeded",
    WaitExternal: "wait_external",
    WaitUser: "wait_user",
    NeedsAgent: "needs_agent",
    Retry: "retry",
    Failed: "failed",
    Cancelled: "cancelled",
}


def attempt_outcome_label(outcome: "Outcome") -> str:
    return ATTEMPT_OUTCOMES[type(outcome)]


def outcome_payload_summary(outcome: "Outcome") -> dict[str, Any]:
    """Small, secret-free event payload describing an outcome."""
    if isinstance(outcome, Succeeded):
        return {"artifacts": len(outcome.artifacts)}
    if isinstance(outcome, WaitExternal):
        return {
            "wake_at": outcome.wake_at.isoformat(),
            "external_handle": outcome.external_handle,
        }
    if isinstance(outcome, WaitUser):
        return {
            "prompt": outcome.prompt,
            "input_schema": outcome.input_schema,
            "expires_at": outcome.expires_at.isoformat() if outcome.expires_at else None,
        }
    if isinstance(outcome, NeedsAgent):
        return {"reason": outcome.reason, "payload": outcome.payload}
    if isinstance(outcome, Retry):
        return {"error_code": outcome.error_code, "retry_at": outcome.retry_at.isoformat()}
    if isinstance(outcome, Failed):
        return {"error_code": outcome.error_code, "message": outcome.message}
    if isinstance(outcome, Cancelled):
        return {}
    raise TypeError(f"unknown outcome type: {type(outcome)!r}")
