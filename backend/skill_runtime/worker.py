"""Invocation lifecycle: claim → run one bounded handler step → settle.

The worker owns nothing durable. Every fact that matters — claim, heartbeat,
outcome — is a guarded database write; killing this process at any point
leaves the reconciler a consistent story to finish.
"""
from __future__ import annotations

import asyncio
import socket
import uuid
from datetime import datetime, timedelta, timezone

from core.log import create_logger
from skill_runtime import registry, repository as repo
from skill_runtime.context import JobContext
from skill_runtime.repository import ClaimedJob, StaleLeaseError
from skill_runtime.types import (
    Cancelled,
    Failed,
    NeedsAgent,
    Outcome,
    Retry,
    WaitExternal,
    WaitUser,
)

log = create_logger("skill_runtime.worker")

#: Retry backoff for handler crashes/timeouts: 5s, 10s, 20s … capped at 10min.
RETRY_BASE_SECONDS = 5
RETRY_CAP_SECONDS = 600


def _retry_at(attempt_number: int) -> datetime:
    delay = min(RETRY_BASE_SECONDS * (2 ** max(0, attempt_number - 1)), RETRY_CAP_SECONDS)
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


class SkillJobWorker:
    def __init__(
        self,
        *,
        queues: tuple[str, ...] = ("default",),
        worker_id: str | None = None,
        concurrency: int = 4,
        lease_seconds: int = 60,
        per_user_limit: int = 2,
        invocation_timeout: int = 120,
        poll_interval: float = 1.0,
    ):
        self.queues = queues
        self.worker_id = worker_id or f"{socket.gethostname()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = max(1, concurrency)
        self.lease_seconds = lease_seconds
        self.per_user_limit = per_user_limit
        self.invocation_timeout = invocation_timeout
        self.poll_interval = poll_interval
        self._inflight: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._poke = asyncio.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._loop_task is not None:
            return
        self._stop.clear()
        self._loop_task = asyncio.get_event_loop().create_task(self._run())
        log.info(f"Skill worker {self.worker_id} started (queues={','.join(self.queues)})")

    def notify(self) -> None:
        self._poke.set()

    async def stop(self, drain_seconds: float = 30.0) -> None:
        """Stop claiming, then wait for in-flight invocations to settle."""
        self._stop.set()
        self._poke.set()
        if self._loop_task is not None:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._loop_task.cancel()
            self._loop_task = None
        if self._inflight:
            done, pending = await asyncio.wait(self._inflight, timeout=drain_seconds)
            for task in pending:
                task.cancel()
            if pending:
                log.warning(
                    f"Worker stop abandoned {len(pending)} invocation(s); "
                    "their leases will expire for the reconciler"
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as e:
                log.error(f"Worker tick failed: {e}")
            self._poke.clear()
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass

    # -- one tick ----------------------------------------------------------

    async def run_once(self) -> int:
        """Claim up to the free slots and launch invocations. Returns claimed count."""
        free = self.concurrency - len(self._inflight)
        if free <= 0:
            return 0
        claimed = await repo.claim_next(
            queues=self.queues,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
            per_user_limit=self.per_user_limit,
            limit=free,
        )
        for claim in claimed:
            task = asyncio.create_task(self._execute(claim))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
        return len(claimed)

    async def drain(self) -> None:
        """Wait for all in-flight invocations (tests and shutdown)."""
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    # -- invocation --------------------------------------------------------

    async def _execute(self, claim: ClaimedJob) -> None:
        job = claim.job
        if job.desired_state == "cancel" and job.attempt_count <= 1 and not job.checkpoint_data:
            # Never persisted a step: nothing external to unwind. (A crash
            # exactly between an external submit and its first checkpoint is
            # covered by the domain's own reconciliation, e.g. video's sweep.)
            await self._settle(claim, Cancelled())
            return

        builtin = registry.resolve(job.skill_key)
        if builtin is None:
            await self._settle(
                claim,
                Failed(
                    error_code="handler_not_found",
                    message=f"no builtin handler registered for {job.skill_key}",
                ),
            )
            return

        # Whether the handler had a chance to see this cancel: only then may
        # its waiting outcome be overridden below. A cancel that lands
        # mid-invocation must not discard the outcome's checkpoint — the next
        # invocation needs it to unwind external side effects (§7.4).
        cancel_known_at_claim = job.desired_state == "cancel"

        # Inputs only exist for jobs that already ran a step (wait → resume);
        # fresh first attempts skip the query.
        inputs = (
            await repo.unconsumed_inputs(job.id)
            if (job.checkpoint_data or job.attempt_count > 1)
            else []
        )
        ctx = JobContext(
            job_id=job.id,
            user_id=job.user_id,
            session_id=job.session_id,
            project_id=job.project_id,
            skill_key=job.skill_key,
            operation=job.operation,
            attempt_id=claim.attempt_id,
            attempt_number=job.attempt_count,
            lease_token=claim.lease_token,
            lease_seconds=self.lease_seconds,
            inputs=inputs,
        )

        lease_lost = asyncio.Event()
        invocation = asyncio.create_task(
            asyncio.wait_for(
                builtin.handler(ctx, job.operation, job.input_data, job.checkpoint_data),
                timeout=self.invocation_timeout,
            )
        )
        keeper = asyncio.create_task(self._keep_lease(claim, invocation, lease_lost))

        outcome: Outcome
        try:
            outcome = await invocation
        except asyncio.TimeoutError:
            outcome = Retry(
                checkpoint=job.checkpoint_data,
                error_code="invocation_timeout",
                retry_at=_retry_at(job.attempt_count),
                error_message=f"invocation exceeded {self.invocation_timeout}s",
            )
        except asyncio.CancelledError:
            if lease_lost.is_set():
                # A newer claim owns the job now; settling would be a stale write.
                log.warning(f"Invocation for {job.id} abandoned: lease lost")
                return
            raise
        except StaleLeaseError:
            log.warning(f"Invocation for {job.id} hit a stale lease mid-step")
            return
        except Exception as e:
            log.warning(f"Handler for {job.id} raised: {e}")
            outcome = Retry(
                checkpoint=job.checkpoint_data,
                error_code="handler_exception",
                retry_at=_retry_at(job.attempt_count),
                error_message=str(e)[:1000],
            )
        finally:
            keeper.cancel()

        # Cancellation vs a waiting outcome, in two distinct cases:
        # - The handler KNEW about the cancel (flag set at claim) and still
        #   returned an unacknowledged wait: naive handler — override to
        #   Cancelled so the job converges.
        # - The cancel arrived MID-invocation: the handler never saw it, and
        #   discarding its outcome would lose the checkpoint that links any
        #   just-created external side effect (a paid submit). Settle the wait
        #   as returned, then wake immediately so the next invocation runs the
        #   handler's own cancel semantics with the checkpoint in hand.
        wake_for_cancel = False
        if isinstance(outcome, (WaitExternal, WaitUser, NeedsAgent)) and not getattr(
            outcome, "acknowledges_cancel", False
        ):
            try:
                if await repo.is_cancel_requested(job.id):
                    if cancel_known_at_claim:
                        outcome = Cancelled()
                    else:
                        wake_for_cancel = True
            except Exception:
                pass

        await self._settle(claim, outcome, consumed_inputs=inputs, wake_reason=(
            "cancel_pending" if wake_for_cancel else None
        ))

    async def _settle(
        self,
        claim: ClaimedJob,
        outcome: Outcome,
        *,
        consumed_inputs: list | None = None,
        wake_reason: str | None = None,
    ) -> None:
        try:
            await repo.settle_invocation(
                claim.job.id, claim.lease_token, outcome, attempt_id=claim.attempt_id
            )
        except StaleLeaseError:
            log.warning(f"Settlement for {claim.job.id} rejected: stale lease")
            return
        except Exception as e:
            log.error(f"Settlement for {claim.job.id} failed: {e}")
            return
        # A retry re-runs the same step, so its inputs stay pending; every
        # other outcome consumed what the handler saw.
        if consumed_inputs and not isinstance(outcome, Retry):
            await repo.mark_inputs_consumed([i.id for i in consumed_inputs])

        if isinstance(outcome, (WaitExternal, WaitUser, NeedsAgent)):
            try:
                if wake_reason is None and await repo.unconsumed_inputs(claim.job.id):
                    # An input landed while this invocation ran; without a wake
                    # it would sit unconsumed until a second input arrives.
                    wake_reason = "input_pending"
                if wake_reason is not None:
                    await repo.wake_job(claim.job.id, reason=wake_reason)
            except Exception as e:
                log.warning(f"Post-settle wake for {claim.job.id} failed: {e}")

    async def _keep_lease(
        self, claim: ClaimedJob, invocation: asyncio.Task, lease_lost: asyncio.Event
    ) -> None:
        interval = max(5.0, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                alive = await repo.heartbeat(
                    claim.job.id,
                    claim.lease_token,
                    extend_seconds=self.lease_seconds,
                    attempt_id=claim.attempt_id,
                )
            except Exception as e:
                log.warning(f"Heartbeat for {claim.job.id} failed: {e}")
                continue
            if not alive:
                lease_lost.set()
                invocation.cancel()
                return
