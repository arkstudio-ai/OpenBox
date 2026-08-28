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
    #: The job was already cancel-requested when this invocation was claimed.
    #: Handlers reaching this point were invoked to UNWIND external state, not
    #: to make progress — check it before starting any new work. (A cancel
    #: that arrives mid-invocation is not reflected here; use
    #: is_cancel_requested() for the live read.)
    cancel_requested: bool = False
    # Input acknowledgement is explicit. Merely handing an input to a handler
    # does not mean the handler understood it; only ids recorded here are
    # consumed by the settlement transaction.
    _consumed_input_ids: set[str] = field(default_factory=set, init=False, repr=False)

    async def progress(self, progress_data: dict | None = None, *, phase: str | None = None) -> None:
        await repo.update_progress(
            self.job_id, self.lease_token, progress_data=progress_data, phase=phase
        )

    async def is_cancel_requested(self) -> bool:
        return await repo.is_cancel_requested(self.job_id)

    async def assert_lease(self) -> None:
        """Extend and verify the lease for reads/cleanup/finalization.

        New billable or irreversible work must use ``may_start_external`` so a
        cancellation that already committed wins the start boundary.
        """
        alive = await repo.heartbeat(
            self.job_id, self.lease_token,
            extend_seconds=self.lease_seconds, attempt_id=self.attempt_id,
        )
        if not alive:
            raise StaleLeaseError(f"job {self.job_id}: lease lost (attempt {self.attempt_number})")

    async def may_start_external(self) -> bool:
        """Ordering gate for a *new* remote/irreversible operation.

        It extends the lease and checks cancellation under the same job-row
        lock. Use ``assert_lease`` for status reads, cancellation, or finalizing
        already-created work; use this method immediately before creating new
        external state.
        """
        return await repo.allow_external_start(
            self.job_id,
            self.lease_token,
            extend_seconds=self.lease_seconds,
            attempt_id=self.attempt_id,
        )

    def consume_input(self, item) -> None:
        """Acknowledge one input after the handler has applied it.

        The id must belong to this invocation's immutable input snapshot. This
        prevents a handler bug from consuming another job's row by id.
        """
        input_id = item if isinstance(item, str) else getattr(item, "id", None)
        available = {getattr(candidate, "id", None) for candidate in self.inputs}
        if not input_id or input_id not in available:
            raise ValueError(f"input {input_id!r} is not part of job {self.job_id} invocation")
        self._consumed_input_ids.add(input_id)

    def consume_inputs(self, items) -> None:
        for item in items:
            self.consume_input(item)

    @property
    def consumed_input_ids(self) -> list[str]:
        """Acknowledged ids in their durable arrival order."""
        return [
            item.id
            for item in self.inputs
            if getattr(item, "id", None) in self._consumed_input_ids
        ]
