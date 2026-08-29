"""Invocation lifecycle: claim → run one bounded handler step → settle.

The worker owns nothing durable. Every fact that matters — claim, heartbeat,
outcome — is a guarded database write; killing this process at any point
leaves the reconciler a consistent story to finish.
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from core.log import create_logger
from skill_runtime import registry, repository as repo
from skill_runtime.context import JobContext
from skill_runtime.repository import ClaimedJob, StaleLeaseError
from skill_runtime.types import (
    Cancelled,
    Failed,
    error_is_retryable,
    InputKind,
    NeedsAgent,
    public_error_text,
    Outcome,
    Retry,
    Succeeded,
    WaitExternal,
    WaitUser,
    operator_reconciliation_wait,
)

log = create_logger("skill_runtime.worker")

#: Retry backoff for handler crashes/timeouts: 5s, 10s, 20s … capped at 10min.
RETRY_BASE_SECONDS = 5
RETRY_CAP_SECONDS = 600
MAX_STATE_BYTES = 256 * 1024
MAX_RESULT_BYTES = 512 * 1024


def _retry_at(attempt_number: int) -> datetime:
    delay = min(RETRY_BASE_SECONDS * (2 ** max(0, attempt_number - 1)), RETRY_CAP_SECONDS)
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


def _is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _json_contract_error(value, label: str, max_bytes: int) -> str | None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        return f"{label} must be JSON-serializable"
    if len(encoded) > max_bytes:
        return f"{label} exceeds the {max_bytes}-byte limit"
    return None


def _outcome_contract_error(
    outcome: object,
    output_schema: dict | None = None,
) -> str | None:
    """Validate the runtime shape of a handler result before DB settlement.

    Dataclass annotations do not enforce values at runtime. This boundary also
    serves future sandbox handlers, whose output is untrusted: malformed JSON
    must consume the ordinary handler fault budget, not repeatedly explode the
    settlement transaction and wait for lease recovery.
    """
    if isinstance(outcome, Succeeded):
        if not isinstance(outcome.result, dict):
            return "Succeeded.result must be an object"
        json_error = _json_contract_error(
            outcome.result, "Succeeded.result", MAX_RESULT_BYTES
        )
        if json_error:
            return json_error
        if not isinstance(outcome.artifacts, list) or any(
            not isinstance(asset_id, str) or not asset_id or len(asset_id) > 64
            for asset_id in outcome.artifacts
        ):
            return "Succeeded.artifacts must contain 1-to-64-character asset ids"
        if len(outcome.artifacts) > 100:
            return "Succeeded.artifacts exceeds the 100-item limit"
        if output_schema:
            if not isinstance(output_schema, dict):
                return "admitted output schema is not an object"
            from skill_runtime.manifest import ManifestError, validate_schema_value

            try:
                validate_schema_value(output_schema, outcome.result, label="result")
            except ManifestError as exc:
                return f"Succeeded.result violates output schema: {exc}"
        return None
    if isinstance(outcome, WaitExternal):
        if not isinstance(outcome.checkpoint, dict):
            return "WaitExternal.checkpoint must be an object"
        json_error = _json_contract_error(
            outcome.checkpoint, "WaitExternal.checkpoint", MAX_STATE_BYTES
        )
        if json_error:
            return json_error
        if not isinstance(outcome.wake_at, datetime):
            return "WaitExternal.wake_at must be a datetime"
        if not _is_aware(outcome.wake_at):
            return "WaitExternal.wake_at must include a timezone"
        if outcome.external_handle is not None and not isinstance(
            outcome.external_handle, str
        ):
            return "WaitExternal.external_handle must be a string"
        if outcome.external_handle is not None and len(outcome.external_handle) > 512:
            return "WaitExternal.external_handle exceeds 512 characters"
        if outcome.progress is not None and not isinstance(outcome.progress, dict):
            return "WaitExternal.progress must be an object"
        if outcome.progress is not None:
            json_error = _json_contract_error(
                outcome.progress, "WaitExternal.progress", 64 * 1024
            )
            if json_error:
                return json_error
        if not isinstance(outcome.acknowledges_cancel, bool):
            return "WaitExternal.acknowledges_cancel must be a boolean"
        return None
    if isinstance(outcome, WaitUser):
        if not isinstance(outcome.checkpoint, dict):
            return "WaitUser.checkpoint must be an object"
        json_error = _json_contract_error(
            outcome.checkpoint, "WaitUser.checkpoint", MAX_STATE_BYTES
        )
        if json_error:
            return json_error
        if not isinstance(outcome.prompt, str) or len(outcome.prompt) > 16_000:
            return "WaitUser.prompt must be a string of at most 16000 characters"
        if not isinstance(outcome.input_schema, dict):
            return "WaitUser.input_schema must be an object"
        json_error = _json_contract_error(
            outcome.input_schema, "WaitUser.input_schema", 64 * 1024
        )
        if json_error:
            return json_error
        from skill_runtime.manifest import ManifestError, validate_schema_definition

        try:
            validate_schema_definition(
                outcome.input_schema,
                label="WaitUser.input_schema",
            )
        except ManifestError as exc:
            return f"WaitUser.input_schema is invalid: {exc}"
        if outcome.expires_at is not None and not isinstance(outcome.expires_at, datetime):
            return "WaitUser.expires_at must be a datetime"
        if outcome.expires_at is not None and not _is_aware(outcome.expires_at):
            return "WaitUser.expires_at must include a timezone"
        if not isinstance(outcome.acknowledges_cancel, bool):
            return "WaitUser.acknowledges_cancel must be a boolean"
        return None
    if isinstance(outcome, NeedsAgent):
        if not isinstance(outcome.checkpoint, dict):
            return "NeedsAgent.checkpoint must be an object"
        json_error = _json_contract_error(
            outcome.checkpoint, "NeedsAgent.checkpoint", MAX_STATE_BYTES
        )
        if json_error:
            return json_error
        if (
            not isinstance(outcome.reason, str)
            or not outcome.reason
            or len(outcome.reason) > 1_000
        ):
            return "NeedsAgent.reason must contain 1 to 1000 characters"
        if not isinstance(outcome.payload, dict):
            return "NeedsAgent.payload must be an object"
        json_error = _json_contract_error(
            outcome.payload, "NeedsAgent.payload", 64 * 1024
        )
        if json_error:
            return json_error
        return None
    if isinstance(outcome, Retry):
        if not isinstance(outcome.checkpoint, dict):
            return "Retry.checkpoint must be an object"
        json_error = _json_contract_error(
            outcome.checkpoint, "Retry.checkpoint", MAX_STATE_BYTES
        )
        if json_error:
            return json_error
        if (
            not isinstance(outcome.error_code, str)
            or not outcome.error_code
            or len(outcome.error_code) > 64
        ):
            return "Retry.error_code must contain 1 to 64 characters"
        if not isinstance(outcome.retry_at, datetime):
            return "Retry.retry_at must be a datetime"
        if not _is_aware(outcome.retry_at):
            return "Retry.retry_at must include a timezone"
        if not isinstance(outcome.error_message, str) or len(outcome.error_message) > 4_000:
            return "Retry.error_message must be a string of at most 4000 characters"
        if not isinstance(outcome.consume_budget, bool):
            return "Retry.consume_budget must be a boolean"
        return None
    if isinstance(outcome, Failed):
        if (
            not isinstance(outcome.error_code, str)
            or not outcome.error_code
            or len(outcome.error_code) > 64
        ):
            return "Failed.error_code must contain 1 to 64 characters"
        if not isinstance(outcome.message, str) or len(outcome.message) > 4_000:
            return "Failed.message must be a string of at most 4000 characters"
        if not isinstance(outcome.retryable, bool):
            return "Failed.retryable must be a boolean"
        return None
    if isinstance(outcome, Cancelled):
        if outcome.result is not None and not isinstance(outcome.result, dict):
            return "Cancelled.result must be an object"
        if outcome.result is not None:
            json_error = _json_contract_error(
                outcome.result, "Cancelled.result", MAX_RESULT_BYTES
            )
            if json_error:
                return json_error
        return None
    return f"unsupported outcome {type(outcome).__name__}"


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
            _, pending = await asyncio.wait(self._inflight, timeout=drain_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
                log.warning(
                    f"Worker stop abandoned {len(pending)} invocation(s); "
                    "their leases will expire for the reconciler"
                )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:
                log.error(f"Worker tick failed: {type(exc).__name__}")
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
            queue_limit=self.concurrency,
            per_user_limit=self.per_user_limit,
            limit=free,
        )
        for claim in claimed:
            task = asyncio.create_task(self._execute(claim))
            self._inflight.add(task)
            task.add_done_callback(self._invocation_done)
        return len(claimed)

    def _invocation_done(self, task: asyncio.Task) -> None:
        self._inflight.discard(task)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None:
            # Failures before the handler/settlement try blocks (for example a
            # transient DB error while converting an expired wait to cancel)
            # must not disappear in an unobserved background Task. The lease
            # remains durable and the Reconciler will reclaim it.
            log.error(
                "Unhandled skill invocation failure: "
                f"{type(error).__name__}"
            )

    async def drain(self) -> None:
        """Wait for all in-flight invocations (tests and shutdown)."""
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    # -- invocation --------------------------------------------------------

    async def _execute(self, claim: ClaimedJob) -> None:
        job = claim.job

        # A due wake folds the just-finished park into this admission-time
        # budget. Turn an exhausted external wait into a durable cancel request
        # before invoking the handler, so external state gets unwound rather
        # than ending as an orphaned local failure.
        external_wait_expired = (
            job.max_external_wait_seconds > 0
            and job.external_wait_seconds >= job.max_external_wait_seconds
        )
        if external_wait_expired and job.desired_state != "cancel":
            await repo.request_cancel(
                job.id,
                job.user_id,
                reason="external_wait_timeout",
            )
            job.desired_state = "cancel"

        if job.desired_state == "cancel":
            # Whether the handler must run is declared, not guessed: an
            # operation that owns no external state is settled here, so a
            # cancel can never turn into one more attempt at the work the
            # user just stopped. Operations holding provider tasks declare
            # cancelRequiresHandler and get invoked to unwind them.
            if not job.cancel_requires_handler:
                await self._settle(claim, Cancelled(), cancel_known_at_claim=True)
                return

        builtin = registry.resolve(job.skill_key, job.handler_version)
        if builtin is None:
            await self._settle(
                claim,
                Retry(
                    checkpoint=job.checkpoint_data,
                    error_code="handler_version_unavailable",
                    error_message=(
                        f"no compatible handler for {job.skill_key} v{job.handler_version} "
                        "in this worker image"
                    ),
                    retry_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                    consume_budget=False,
                ),
            )
            return

        # Whether the handler had a chance to see this cancel: only then may
        # its waiting outcome be overridden below. A cancel that lands
        # mid-invocation must not discard the outcome's checkpoint — the next
        # invocation needs it to unwind external side effects (§7.4).
        cancel_known_at_claim = job.desired_state == "cancel"

        # This immutable snapshot distinguishes inputs the handler deliberately
        # left unconsumed from inputs that race in after execution starts.
        inputs = await repo.unconsumed_inputs(job.id)
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
            cancel_requested=cancel_known_at_claim,
        )
        input_schema = (job.progress_data or {}).get("input_schema") or {}
        if input_schema.get("x-openbox-review") == "external-reconciliation":
            # This operator action belongs to the generic runtime rather than a
            # domain handler. Consume it atomically with whatever outcome the
            # newly authorized reconciliation attempt produces.
            for item in inputs:
                if (
                    item.kind == InputKind.OPERATOR_RESUME.value
                    and (item.payload or {}).get("retry_reconciliation") is True
                ):
                    ctx.consume_input(item)

        # The operation's admission-time policy snapshot wins over the worker
        # default. This keeps a rolling deploy or later Manifest edit from
        # changing the execution contract of an already accepted job.
        invocation_timeout = job.invocation_timeout_seconds or self.invocation_timeout

        lease_lost = asyncio.Event()
        invocation = asyncio.create_task(
            asyncio.wait_for(
                builtin.handler(ctx, job.operation, job.input_data, job.checkpoint_data),
                timeout=invocation_timeout,
            )
        )
        keeper = asyncio.create_task(self._keep_lease(claim, invocation, lease_lost))

        outcome: Outcome
        try:
            outcome = await invocation
        except asyncio.TimeoutError:
            outcome = Retry(
                checkpoint=(
                    job.checkpoint_data if isinstance(job.checkpoint_data, dict) else {}
                ),
                error_code="invocation_timeout",
                retry_at=_retry_at(job.retry_count + 1),
                error_message=f"invocation exceeded {invocation_timeout}s",
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
            # Exception text from providers can contain signed URLs, response
            # bodies or credentials. Keep the durable/user-visible failure to a
            # class-level diagnostic and correlate detailed server logs by job.
            # The traceback only exists if it is actually emitted — without
            # exc_info there is nothing to correlate and a failing handler is
            # undiagnosable in production.
            log.warning(f"Handler for {job.id} raised {type(e).__name__}", exc_info=True)
            # A handler may declare its message safe to show and its fault
            # permanent (see HandlerError). Anything that has not said so keeps
            # the conservative treatment: class name only, ordinary retries.
            detail = public_error_text(e)
            message = (
                f"{type(e).__name__}: {detail}" if detail
                else f"handler raised {type(e).__name__}"
            )
            if error_is_retryable(e):
                outcome = Retry(
                    checkpoint=job.checkpoint_data,
                    error_code="handler_exception",
                    retry_at=_retry_at(job.retry_count + 1),
                    error_message=message,
                )
            else:
                # Waiting cannot fix a misconfiguration; spending the budget on
                # one only delays telling anyone by twenty minutes.
                outcome = Failed(error_code="handler_permanent", message=message)
        finally:
            keeper.cancel()
            with suppress(asyncio.CancelledError):
                await keeper

        contract_error = _outcome_contract_error(outcome, job.output_schema or {})
        if contract_error is not None:
            # A malformed plugin/handler result is a handler fault, not an
            # abandoned lease that should masquerade as `worker_lost` minutes
            # later. Persist the real cause through the ordinary retry budget.
            outcome = Retry(
                checkpoint=job.checkpoint_data,
                error_code="invalid_handler_outcome",
                error_message=contract_error,
                retry_at=_retry_at(job.retry_count + 1),
            )
        elif isinstance(outcome, Failed) and outcome.retryable:
            # ``Failed(retryable=True)`` is part of the public Outcome
            # contract. Normalize it here so it shares the exact same fault
            # budget, backoff and cancellation arbitration as an explicit
            # Retry instead of being silently persisted as a terminal failure.
            outcome = Retry(
                checkpoint=(
                    job.checkpoint_data
                    if isinstance(job.checkpoint_data, dict)
                    else {}
                ),
                error_code=outcome.error_code,
                error_message=outcome.message,
                retry_at=_retry_at(job.retry_count + 1),
            )

        outcome, exhausted_now = self._bound_external_wait(outcome, job)
        if exhausted_now and job.desired_state != "cancel":
            await repo.request_cancel(
                job.id,
                job.user_id,
                reason="external_wait_timeout",
            )
        consumed_input_ids = ctx.consumed_input_ids
        await self._settle(
            claim,
            outcome,
            consumed_input_ids=consumed_input_ids,
            observed_input_ids=[item.id for item in inputs],
            cancel_known_at_claim=cancel_known_at_claim,
        )

    @staticmethod
    def _bound_external_wait(outcome: Outcome, job) -> tuple[Outcome, bool]:
        """Clamp a park to the *remaining cumulative* external-wait budget."""
        if not isinstance(outcome, WaitExternal):
            return outcome, False
        remaining = max(
            0,
            int(job.max_external_wait_seconds or 0) - int(job.external_wait_seconds or 0),
        )
        deadline = job.deadline_at
        if deadline is not None and deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        if (
            job.desired_state == "cancel"
            and (
                (deadline is not None and datetime.now(timezone.utc) >= deadline)
                or (deadline is None and remaining <= 0)
            )
        ):
            return (
                operator_reconciliation_wait(
                    outcome.checkpoint,
                    detail="总时限内未能确认外部任务终态或取消回执",
                ),
                False,
            )
        # Once cancellation is in progress, remote fact reconciliation is a
        # safety operation rather than ordinary external waiting. Give it a
        # low-frequency poll until the total job deadline, where the
        # Reconciler turns an unconfirmed cancel into explicit operator review.
        if remaining <= 0 and job.desired_state == "cancel":
            remaining = 60
        latest = datetime.now(timezone.utc) + timedelta(seconds=remaining)
        if deadline is not None and deadline < latest:
            latest = deadline
        wake_at = outcome.wake_at
        if wake_at.tzinfo is None:
            wake_at = wake_at.replace(tzinfo=timezone.utc)
        if wake_at > latest:
            from dataclasses import replace

            outcome = replace(outcome, wake_at=latest)
        return outcome, remaining <= 0

    async def _settle(
        self,
        claim: ClaimedJob,
        outcome: Outcome,
        *,
        consumed_input_ids: list[str] | None = None,
        observed_input_ids: list[str] | None = None,
        cancel_known_at_claim: bool = False,
    ) -> None:
        try:
            await repo.settle_invocation(
                claim.job.id,
                claim.lease_token,
                outcome,
                attempt_id=claim.attempt_id,
                consumed_input_ids=consumed_input_ids or [],
                observed_input_ids=observed_input_ids or [],
                cancel_known_at_claim=cancel_known_at_claim,
            )
        except StaleLeaseError:
            log.warning(f"Settlement for {claim.job.id} rejected: stale lease")
            return
        except Exception as e:
            # Database/driver errors may quote JSON payloads or provider data.
            # The job id plus exception class is enough to correlate server
            # diagnostics without copying those values into general logs.
            log.error(
                f"Settlement for {claim.job.id} failed: {type(e).__name__}"
            )
            return

    async def _keep_lease(
        self, claim: ClaimedJob, invocation: asyncio.Task, lease_lost: asyncio.Event
    ) -> None:
        loop = asyncio.get_running_loop()
        interval = max(0.5, self.lease_seconds / 3)
        lease_expires_at = claim.job.lease_expires_at
        if lease_expires_at is not None and lease_expires_at.tzinfo is None:
            lease_expires_at = lease_expires_at.replace(tzinfo=timezone.utc)
        initial_remaining = (
            max(0.0, (lease_expires_at - datetime.now(timezone.utc)).total_seconds())
            if lease_expires_at is not None
            else 0.0
        )
        # The lease clock starts in the claim transaction, not when this task
        # happens to be scheduled. Using a fresh full duration here could let a
        # delayed batch invocation continue after its database lease expired.
        confirmed_until = loop.time() + initial_remaining
        while True:
            remaining = confirmed_until - loop.time()
            if remaining <= 0:
                lease_lost.set()
                invocation.cancel()
                return
            await asyncio.sleep(min(interval, max(0.1, remaining / 2)))
            try:
                alive = await asyncio.wait_for(
                    repo.heartbeat(
                        claim.job.id,
                        claim.lease_token,
                        extend_seconds=self.lease_seconds,
                        attempt_id=claim.attempt_id,
                    ),
                    timeout=max(0.1, confirmed_until - loop.time()),
                )
            except Exception as exc:
                log.warning(
                    f"Heartbeat for {claim.job.id} failed: {type(exc).__name__}"
                )
                if loop.time() >= confirmed_until:
                    lease_lost.set()
                    invocation.cancel()
                    return
                continue
            if not alive:
                lease_lost.set()
                invocation.cancel()
                return
            confirmed_until = loop.time() + self.lease_seconds
