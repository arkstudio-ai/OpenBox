"""Phase 2 acceptance: a job runs to convergence without web/agent ownership."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from skill_runtime import reconciler, registry, repository as repo
from skill_runtime.types import Cancelled, JobStatus, Retry, Succeeded, WaitExternal, WaitUser
from skill_runtime.worker import SkillJobWorker

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _skill_key() -> str:
    return "builtin:test-" + uuid.uuid4().hex[:8]


async def _admit(skill_key, **overrides):
    kwargs = dict(
        user_id="u_" + uuid.uuid4().hex[:8],
        skill_key=skill_key,
        operation="step",
        idempotency_key="idem_" + uuid.uuid4().hex[:8],
        input_data={"payload": 1},
        runtime_kind="internal",
        queue_name="q_" + uuid.uuid4().hex[:8],
    )
    kwargs.update(overrides)
    job, _ = await repo.admit_job(**kwargs)
    return job


def _worker(job, **kw):
    kwargs = dict(queues=(job.queue_name,), concurrency=2, lease_seconds=60, per_user_limit=0)
    kwargs.update(kw)
    return SkillJobWorker(**kwargs)


async def _run_to_idle(worker):
    await worker.run_once()
    await worker.drain()


async def test_wait_external_then_success_without_agent():
    calls = []
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        calls.append(dict(checkpoint))
        if not checkpoint:
            return WaitExternal(checkpoint={"task": "t1"}, wake_at=NOW() - timedelta(seconds=1))
        assert checkpoint == {"task": "t1"}
        return Succeeded(result={"done": True})

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)

    await _run_to_idle(worker)
    mid = await repo.get_job(job.id, job.user_id)
    assert mid.status == JobStatus.WAITING_EXTERNAL.value
    assert mid.checkpoint_data == {"task": "t1"}

    # A lost wake is repaired by the periodic scan.
    assert await reconciler.requeue_due_external() == 1
    await _run_to_idle(worker)

    final = await repo.get_job(job.id, job.user_id)
    assert final.status == JobStatus.SUCCEEDED.value
    assert final.result_data == {"done": True}
    assert calls == [{}, {"task": "t1"}]


async def test_handler_exception_schedules_retry_with_checkpoint():
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        raise RuntimeError("provider hiccup")

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.RETRY_SCHEDULED.value
    assert after.error_code == "handler_exception"
    assert "provider hiccup" in after.error_message


async def test_retry_exhaustion_via_worker_path():
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        raise RuntimeError("always broken")

    registry.register_builtin(skill, handler)
    job = await _admit(skill, max_attempts=1)
    worker = _worker(job)
    await _run_to_idle(worker)

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.FAILED.value
    assert "retry budget exhausted" in after.error_message


async def test_manifest_operation_timeout_overrides_worker_default(monkeypatch):
    """A per-operation invocationTimeoutSeconds must win over the global
    default, or a long inline step (STT) loops forever on timeout."""
    from skill_runtime.worker import SkillJobWorker

    seen = {}

    async def handler(ctx, operation, payload, checkpoint):
        return Succeeded()

    skill = _skill_key()
    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = SkillJobWorker(queues=(job.queue_name,), per_user_limit=0, invocation_timeout=1)

    class _Op:
        invocationTimeoutSeconds = 600
        maxExternalWaitSeconds = 86400

    monkeypatch.setattr(SkillJobWorker, "_operation_spec", staticmethod(lambda j: _Op()))
    real_wait_for = asyncio.wait_for

    async def capture(awaitable, timeout=None):
        seen["timeout"] = timeout
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", capture)
    await worker.run_once()
    await worker.drain()
    assert seen["timeout"] == 600


async def test_external_wait_is_clamped_to_manifest_max(monkeypatch):
    """A handler cannot park past the operation's declared external-wait cap."""
    from skill_runtime.worker import SkillJobWorker

    async def handler(ctx, operation, payload, checkpoint):
        return WaitExternal(checkpoint={}, wake_at=NOW() + timedelta(days=30))

    skill = _skill_key()
    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)

    class _Op:
        invocationTimeoutSeconds = 120
        maxExternalWaitSeconds = 3600

    monkeypatch.setattr(SkillJobWorker, "_operation_spec", staticmethod(lambda j: _Op()))
    await _run_to_idle(worker)

    parked = await repo.get_job(job.id, job.user_id)
    next_run = parked.next_run_at
    if next_run.tzinfo is None:
        next_run = next_run.replace(tzinfo=timezone.utc)
    assert next_run <= NOW() + timedelta(seconds=3700)


async def test_user_wait_ttl_requests_cancel(monkeypatch):
    """An unanswered question times out into a cancel request, never a retry."""

    async def handler(ctx, operation, payload, checkpoint):
        return WaitUser(checkpoint={}, prompt="anyone there?")

    skill = _skill_key()
    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.WAITING_USER.value

    class _Op:
        userInputTimeoutSeconds = 1

    class _Manifest:
        def operation(self, name):
            return _Op()

    monkeypatch.setattr("skill_runtime.manifest.get_manifest", lambda key: _Manifest())

    from db.base import get_db_session
    from db.models.skill_job import SkillJob
    from sqlalchemy import update as sa_update

    async with get_db_session() as db:
        await db.execute(
            sa_update(SkillJob).where(SkillJob.id == job.id)
            .values(updated_at=NOW() - timedelta(hours=1))
        )
    assert await reconciler.expire_user_waits() >= 1
    after = await repo.get_job(job.id, job.user_id)
    assert after.desired_state == "cancel"
    assert after.status == JobStatus.QUEUED.value


async def test_invocation_timeout_becomes_retry():
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        await asyncio.sleep(5)
        return Succeeded()

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job, invocation_timeout=0.05)
    await _run_to_idle(worker)

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.RETRY_SCHEDULED.value
    assert after.error_code == "invocation_timeout"


async def test_unregistered_handler_fails_terminally():
    job = await _admit("builtin:never-registered-" + uuid.uuid4().hex[:6])
    worker = _worker(job)
    await _run_to_idle(worker)

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.FAILED.value
    assert after.error_code == "handler_not_found"


async def test_running_cancel_observed_by_handler():
    skill = _skill_key()
    started = asyncio.Event()
    proceed = asyncio.Event()

    async def handler(ctx, operation, payload, checkpoint):
        started.set()
        await proceed.wait()
        if await ctx.is_cancel_requested():
            return Cancelled()
        return Succeeded()

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await worker.run_once()
    await asyncio.wait_for(started.wait(), timeout=5)
    await repo.request_cancel(job.id, job.user_id)
    proceed.set()
    await worker.drain()

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.CANCELLED.value


async def test_wait_user_answer_reaches_next_invocation_and_is_consumed():
    skill = _skill_key()
    seen_inputs = []

    async def handler(ctx, operation, payload, checkpoint):
        if not checkpoint:
            return WaitUser(checkpoint={"asked": True}, prompt="which voice?")
        seen_inputs.extend([(i.kind, i.payload) for i in ctx.inputs])
        return Succeeded(result={"answered": True})

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.WAITING_USER.value

    await repo.add_input(
        job.id, job.user_id, kind="user_answer",
        payload={"voice": "warm"}, idempotency_key="answer-1",
    )
    await _run_to_idle(worker)

    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.SUCCEEDED.value
    assert seen_inputs == [("user_answer", {"voice": "warm"})]
    assert await repo.unconsumed_inputs(job.id) == []


# ---------------------------------------------------------------------------
# Reconciler
# ---------------------------------------------------------------------------

async def _force_lease_expiry(job_id):
    from db.base import get_db_session
    from db.models.skill_job import SkillJob
    from sqlalchemy import update

    async with get_db_session() as db:
        await db.execute(
            update(SkillJob)
            .where(SkillJob.id == job_id)
            .values(lease_expires_at=NOW() - timedelta(seconds=5))
        )


async def test_expired_lease_reschedules_and_marks_attempt_lost():
    job = await _admit(_skill_key())
    claimed = (await repo.claim_next(queues=(job.queue_name,), worker_id="w1"))[0]
    await _force_lease_expiry(job.id)

    assert await reconciler.expire_stale_running() == 1
    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.RETRY_SCHEDULED.value
    assert after.error_code == "worker_lost"

    from db.base import get_db_session
    from db.models.skill_job_attempt import SkillJobAttempt
    from sqlalchemy import select

    async with get_db_session() as db:
        attempt = (
            await db.execute(
                select(SkillJobAttempt).where(SkillJobAttempt.id == claimed.attempt_id)
            )
        ).scalar_one()
    assert attempt.outcome == "lost"
    assert attempt.ended_at is not None

    # The stale worker's late settlement must be rejected.
    import pytest

    with pytest.raises(repo.StaleLeaseError):
        await repo.settle_invocation(job.id, claimed.lease_token, Succeeded())


async def test_expired_lease_with_spent_budget_fails():
    job = await _admit(_skill_key(), max_attempts=1)
    await repo.claim_next(queues=(job.queue_name,), worker_id="w1")
    await _force_lease_expiry(job.id)

    assert await reconciler.expire_stale_running() == 1
    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.FAILED.value
    assert after.error_code == "worker_lost"


async def test_live_lease_left_alone():
    job = await _admit(_skill_key())
    await repo.claim_next(queues=(job.queue_name,), worker_id="w1", lease_seconds=120)
    assert await reconciler.expire_stale_running() == 0
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.RUNNING.value


async def test_deadline_settles_unclaimed_and_cancels_running():
    overdue = await _admit(_skill_key(), deadline_at=NOW() - timedelta(minutes=1))
    running = await _admit(_skill_key(), deadline_at=NOW() - timedelta(minutes=1))
    await repo.claim_next(queues=(running.queue_name,), worker_id="w1", lease_seconds=120)

    settled = await reconciler.enforce_deadlines()
    assert settled == 2

    dead = await repo.get_job(overdue.id, overdue.user_id)
    assert dead.status == JobStatus.FAILED.value
    assert dead.error_code == "deadline_exceeded"

    still_running = await repo.get_job(running.id, running.user_id)
    assert still_running.status == JobStatus.RUNNING.value
    assert still_running.desired_state == "cancel"


async def test_cancel_waiting_job_reaches_handler():
    """§7.4: waiting states are not settled directly — the cancel wakes the
    job so the handler can unwind external side effects."""
    skill = _skill_key()
    saw_cancel = []

    async def handler(ctx, operation, payload, checkpoint):
        if not checkpoint:
            return WaitExternal(checkpoint={"step": 1}, wake_at=NOW() + timedelta(hours=1))
        saw_cancel.append(await ctx.is_cancel_requested())
        return Cancelled(result={"provider_cancelled": True})

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.WAITING_EXTERNAL.value

    cancelled = await repo.request_cancel(job.id, job.user_id)
    assert cancelled.status == JobStatus.QUEUED.value  # woken, not settled
    assert cancelled.desired_state == "cancel"

    await _run_to_idle(worker)
    final = await repo.get_job(job.id, job.user_id)
    assert final.status == JobStatus.CANCELLED.value
    assert saw_cancel == [True]
    assert final.result_data == {"provider_cancelled": True}


async def test_naive_handler_waiting_outcome_overridden_on_cancel():
    """A handler that ignores the flag must still converge: the worker turns
    its waiting outcome into Cancelled."""
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        return WaitExternal(checkpoint={"step": 1}, wake_at=NOW() + timedelta(hours=1))

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)
    await repo.request_cancel(job.id, job.user_id)
    await _run_to_idle(worker)
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.CANCELLED.value


async def test_mid_invocation_cancel_preserves_checkpoint_then_converges():
    """A cancel that lands DURING an invocation must not discard the waiting
    outcome — the checkpoint links external side effects. The wait settles,
    the job wakes immediately, and the next invocation runs the handler's own
    cancel semantics with the checkpoint in hand."""
    skill = _skill_key()
    submitted = asyncio.Event()
    cancelled_now = asyncio.Event()
    cancel_seen_with_checkpoint = []

    async def handler(ctx, operation, payload, checkpoint):
        if not checkpoint:
            submitted.set()
            await cancelled_now.wait()  # the cancel lands while we work
            return WaitExternal(
                checkpoint={"provider_task": "paid-1"},
                wake_at=NOW() + timedelta(hours=1),
            )
        cancel_seen_with_checkpoint.append(
            (await ctx.is_cancel_requested(), dict(checkpoint))
        )
        return Cancelled(result={"provider_cancelled": True})

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await worker.run_once()
    await asyncio.wait_for(submitted.wait(), timeout=5)
    await repo.request_cancel(job.id, job.user_id)  # running → flag only
    cancelled_now.set()
    await worker.drain()

    # The wait was settled (checkpoint persisted), then woken for cancel.
    mid = await repo.get_job(job.id, job.user_id)
    assert mid.checkpoint_data == {"provider_task": "paid-1"}
    assert mid.status == JobStatus.QUEUED.value
    assert mid.desired_state == "cancel"

    await _run_to_idle(worker)
    final = await repo.get_job(job.id, job.user_id)
    assert final.status == JobStatus.CANCELLED.value
    assert cancel_seen_with_checkpoint == [(True, {"provider_task": "paid-1"})]


async def test_input_arriving_mid_invocation_wakes_job():
    """An answer admitted while the invocation runs must not strand until a
    second input arrives."""
    skill = _skill_key()
    started = asyncio.Event()
    proceed = asyncio.Event()
    seen = []

    async def handler(ctx, operation, payload, checkpoint):
        if not checkpoint:
            started.set()
            await proceed.wait()
            return WaitUser(checkpoint={"asked": True}, prompt="answer?")
        seen.extend([i.payload for i in ctx.inputs])
        return Succeeded(result={"done": True})

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await worker.run_once()
    await asyncio.wait_for(started.wait(), timeout=5)
    # Lands mid-invocation: job is RUNNING, so wake_job inside add_input no-ops.
    await repo.add_input(
        job.id, job.user_id, kind="user_answer", payload={"a": 1}, idempotency_key="mid-1"
    )
    proceed.set()
    await worker.drain()

    woken = await repo.get_job(job.id, job.user_id)
    assert woken.status == JobStatus.QUEUED.value  # post-settle wake caught it

    await _run_to_idle(worker)
    final = await repo.get_job(job.id, job.user_id)
    assert final.status == JobStatus.SUCCEEDED.value
    assert seen == [{"a": 1}]


async def test_deadline_wakes_executed_jobs_with_cancel():
    """Past-deadline jobs that ever ran must reach their handler (§7.4), not
    be settled to failed behind its back."""
    job = await _admit(_skill_key())
    claimed = (await repo.claim_next(queues=(job.queue_name,), worker_id="w1"))[0]
    await repo.settle_invocation(
        job.id, claimed.lease_token,
        WaitExternal(checkpoint={"t": 1}, wake_at=NOW() + timedelta(hours=1)),
        attempt_id=claimed.attempt_id,
    )
    from db.base import get_db_session
    from db.models.skill_job import SkillJob
    from sqlalchemy import update as sa_update

    async with get_db_session() as db:
        await db.execute(
            sa_update(SkillJob).where(SkillJob.id == job.id)
            .values(deadline_at=NOW() - timedelta(minutes=1))
        )
    assert await reconciler.enforce_deadlines() == 1
    woken = await repo.get_job(job.id, job.user_id)
    assert woken.status == JobStatus.QUEUED.value
    assert woken.desired_state == "cancel"
    assert woken.checkpoint_data == {"t": 1}


async def test_acknowledged_wait_survives_cancel():
    """acknowledges_cancel keeps a must-finish wait alive (paid output mid-copy)."""
    skill = _skill_key()

    async def handler(ctx, operation, payload, checkpoint):
        return WaitExternal(
            checkpoint={"step": 1},
            wake_at=NOW() + timedelta(hours=1),
            acknowledges_cancel=True,
        )

    registry.register_builtin(skill, handler)
    job = await _admit(skill)
    worker = _worker(job)
    await _run_to_idle(worker)
    await repo.request_cancel(job.id, job.user_id)
    await _run_to_idle(worker)
    after = await repo.get_job(job.id, job.user_id)
    assert after.status == JobStatus.WAITING_EXTERNAL.value
    assert after.desired_state == "cancel"


async def test_waiting_user_is_never_deadline_free_but_not_retried():
    """waiting_user only falls to the deadline, never to retry machinery."""
    job = await _admit(_skill_key())
    claimed = (await repo.claim_next(queues=(job.queue_name,), worker_id="w1"))[0]
    await repo.settle_invocation(
        job.id, claimed.lease_token,
        WaitUser(checkpoint={}, prompt="answer?"), attempt_id=claimed.attempt_id,
    )
    result = await reconciler.reconcile_once()
    assert result["lost_leases"] == 0
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.WAITING_USER.value
