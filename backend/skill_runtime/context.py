"""JobContext: the only surface a handler gets.

Identity, lease and inputs come from server records; a handler never receives
raw connections, user ids from payloads, or global secrets (§5.2). Capability
accessors (secrets/assets/providers/remote) arrive with the manifest phase —
until then the context carries identity, progress, cancellation and the lease
assertion that must precede any billable external call.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from skill_runtime import repository as repo
from skill_runtime.repository import StaleLeaseError


@dataclass
class JobContext:
    job_id: str
    user_id: str
    session_id: str | None
    project_id: str | None
    skill_key: str
    operation: str
    attempt_id: str
    attempt_number: int
    lease_token: int
    lease_seconds: int = 60
    #: Unconsumed inputs admitted since the last invocation (user answers,
    #: provider callbacks, agent results), oldest first.
    inputs: list = field(default_factory=list)

    async def progress(self, progress_data: dict | None = None, *, phase: str | None = None) -> None:
        await repo.update_progress(
            self.job_id, self.lease_token, progress_data=progress_data, phase=phase
        )

    async def is_cancel_requested(self) -> bool:
        return await repo.is_cancel_requested(self.job_id)

    async def assert_lease(self) -> None:
        """Extend and verify the lease. Call immediately before any billable or
        irreversible external call — fencing only protects database writes."""
        alive = await repo.heartbeat(
            self.job_id, self.lease_token,
            extend_seconds=self.lease_seconds, attempt_id=self.attempt_id,
        )
        if not alive:
            raise StaleLeaseError(f"job {self.job_id}: lease lost (attempt {self.attempt_number})")
