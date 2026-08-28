"""Phase 1 acceptance: durable admission, idempotency, lease fencing, events."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from skill_runtime import repository as repo
from skill_runtime.types import (
    Cancelled,
    Failed,
    JobEventType,
    JobStatus,
    NeedsAgent,
    Retry,
    Succeeded,
    WaitExternal,
    WaitUser,
)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def _user() -> str:
    return "u_" + uuid.uuid4().hex[:8]


async def _admit(user_id=None, **overrides):
    kwargs = dict(
        user_id=user_id or _user(),
        skill_key="builtin:demo",
        operation="run_step",
        idempotency_key="idem_" + uuid.uuid4().hex[:8],
        input_data={"n": 1},
        runtime_kind="internal",
        # Tests share one database; a per-call queue keeps claim_next from
        # picking up an older test's leftover queued jobs.
        queue_name="q_" + uuid.uuid4().hex[:8],
    )
    kwargs.update(overrides)
    return await repo.admit_job(**kwargs)


async def _events(job_id, user_id):
    return await repo.get_events(job_id, user_id)


async def _claim(job, **kw):
    kwargs = dict(queues=(job.queue_name,), worker_id="w1", lease_seconds=60, limit=1)
    kwargs.update(kw)
    claimed = await repo.claim_next(**kwargs)
    assert len(claimed) == 1
    assert claimed[0].job.id == job.id
    return claimed[0]


async def _claim_none(job, **kw):
    kwargs = dict(queues=(job.queue_name,), worker_id="w1", limit=5)
    kwargs.update(kw)
    return await repo.claim_next(**kwargs)


# ---------------------------------------------------------------------------
# Admission & idempotency
# ---------------------------------------------------------------------------

async def test_admit_creates_job_with_created_event():
    job, created = await _admit()
    assert created is True
    assert job.status == JobStatus.QUEUED.value
    assert job.next_run_at is not None
    events = await _events(job.id, job.user_id)
    assert [e.event_type for e in events] == [JobEventType.CREATED.value]
    assert events[0].seq == 1
    assert events[0].published_at is None


async def test_admit_same_request_returns_existing():
    user = _user()
    job1, created1 = await _admit(user_id=user, idempotency_key="k1", input_data={"a": 1})
    job2, created2 = await _admit(user_id=user, idempotency_key="k1", input_data={"a": 1})
    assert created1 is True and created2 is False
    assert job1.id == job2.id
    assert len(await _events(job1.id, user)) == 1


async def test_admit_same_key_different_payload_conflicts():
    user = _user()
    await _admit(user_id=user, idempotency_key="k1", input_data={"a": 1})
    with pytest.raises(repo.IdempotencyConflict):
        await _admit(user_id=user, idempotency_key="k1", input_data={"a": 2})


async def test_same_key_different_user_is_separate():
    job1, _ = await _admit(user_id=_user(), idempotency_key="shared")
    job2, _ = await _admit(user_id=_user(), idempotency_key="shared")
    assert job1.id != job2.id


async def test_concurrent_admissions_create_one_job(tmp_path):
    from db.base import Base, close_engine, init_engine

    await close_engine()
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path}/concurrency.db")
    import db.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        user = _user()

        async def one():
            try:
                return await _admit(user_id=user, idempotency_key="same", input_data={"x": 1})
            except Exception as exc:  # sqlite write contention must not create rows
                return exc

        results = await asyncio.gather(*[one() for _ in range(100)])
        jobs = [r for r in results if isinstance(r, tuple)]
        created_flags = [created for _, created in jobs]
        assert created_flags.count(True) == 1
        assert len({job.id for job, _ in jobs}) == 1
        events = await _events(jobs[0][0].id, user)
        assert len(events) == 1
    finally:
        await close_engine()


# ---------------------------------------------------------------------------
# Claim / lease / fencing
# ---------------------------------------------------------------------------

async def test_claim_moves_to_running_with_attempt_and_event():
    job, _ = await _admit()
    claimed = await _claim(job)
    assert claimed.job.status == JobStatus.RUNNING.value
    assert claimed.job.lease_token == 1
    assert claimed.job.attempt_count == 1
    assert claimed.job.lease_owner == "w1"
    events = await _events(job.id, job.user_id)
    assert [e.event_type for e in events] == [
        JobEventType.CREATED.value,
        JobEventType.CLAIMED.value,
    ]
    assert [e.seq for e in events] == [1, 2]


async def test_second_claim_loses():
    job, _ = await _admit()
    await _claim(job)
    assert await _claim_none(job, worker_id="w2") == []


async def test_future_next_run_at_not_claimable():
    job, _ = await _admit()
    from db.base import get_db_session
    from db.models.skill_job import SkillJob
    from sqlalchemy import update

    async with get_db_session() as db:
        await db.execute(
            update(SkillJob)
            .where(SkillJob.id == job.id)
            .values(next_run_at=NOW() + timedelta(hours=1))
        )
    assert await _claim_none(job) == []


async def test_per_user_limit_skips_saturated_user():
    queue = "q_" + uuid.uuid4().hex[:8]
    user = _user()
    job1, _ = await _admit(user_id=user, idempotency_key="a", queue_name=queue)
    job2, _ = await _admit(user_id=user, idempotency_key="b", queue_name=queue)
    await _claim(job1, per_user_limit=1)
    assert (
        await repo.claim_next(queues=(queue,), worker_id="w1", per_user_limit=1, limit=1)
        == []
    )
    # Another user in the same queue is unaffected.
    other, _ = await _admit(queue_name=queue)
    claimed = await repo.claim_next(queues=(queue,), worker_id="w1", per_user_limit=1, limit=5)
    assert [c.job.id for c in claimed] == [other.id]


async def test_stale_lease_cannot_settle_new_claim():
    job, _ = await _admit()
    first = await _claim(job)
    stale_token = first.lease_token

    # The reconciler expires the lease and requeues; a new worker claims.
    await repo.settle_invocation(
        job.id, stale_token, Retry(checkpoint={}, error_code="lost", retry_at=NOW()),
        attempt_id=first.attempt_id,
    )
    second = await _claim(job, worker_id="w2")
    assert second.lease_token == 2

    with pytest.raises(repo.StaleLeaseError):
        await repo.settle_invocation(job.id, stale_token, Succeeded(result={"stale": True}))

    fresh = await repo.get_job(job.id, job.user_id)
    assert fresh.status == JobStatus.RUNNING.value
    assert fresh.result_data == {}


async def test_heartbeat_extends_only_live_lease():
    job, _ = await _admit()
    claimed = await _claim(job)
    assert await repo.heartbeat(job.id, claimed.lease_token, attempt_id=claimed.attempt_id)
    assert not await repo.heartbeat(job.id, claimed.lease_token + 1)


# ---------------------------------------------------------------------------
# Settlement outcomes
# ---------------------------------------------------------------------------

async def test_succeeded_settlement():
    job, _ = await _admit()
    claimed = await _claim(job)
    settled = await repo.settle_invocation(
        job.id, claimed.lease_token, Succeeded(result={"ok": 1}), attempt_id=claimed.attempt_id
    )
    assert settled.status == JobStatus.SUCCEEDED.value
    assert settled.result_data == {"ok": 1}
    assert settled.lease_owner is None
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.SUCCEEDED.value
    assert events[-1].seq == 3

    from db.base import get_db_session
    from db.models.skill_job_attempt import SkillJobAttempt
    from sqlalchemy import select

    async with get_db_session() as db:
        attempt = (
            await db.execute(
                select(SkillJobAttempt).where(SkillJobAttempt.id == claimed.attempt_id)
            )
        ).scalar_one()
    assert attempt.outcome == "succeeded"
    assert attempt.ended_at is not None


async def test_wait_external_releases_lease_and_schedules_wake():
    job, _ = await _admit()
    claimed = await _claim(job)
    wake_at = NOW() + timedelta(minutes=5)
    settled = await repo.settle_invocation(
        job.id,
        claimed.lease_token,
        WaitExternal(checkpoint={"task": "t1"}, wake_at=wake_at, external_handle="prov-1"),
        attempt_id=claimed.attempt_id,
        phase="provider_generate",
    )
    assert settled.status == JobStatus.WAITING_EXTERNAL.value
    assert settled.phase == "provider_generate"
    assert settled.checkpoint_data == {"task": "t1"}
    assert settled.lease_owner is None and settled.lease_expires_at is None
    # Not claimable before wake_at.
    assert await _claim_none(job) == []


async def test_wait_user_and_needs_agent_write_inbox():
    session_id = "sess_" + uuid.uuid4().hex[:8]
    job, _ = await _admit(session_id=session_id)
    claimed = await _claim(job)
    settled = await repo.settle_invocation(
        job.id,
        claimed.lease_token,
        NeedsAgent(checkpoint={"step": 2}, reason="summarize results", payload={"k": "v"}),
        attempt_id=claimed.attempt_id,
    )
    assert settled.status == JobStatus.WAITING_AGENT.value

    from db.base import get_db_session
    from db.models.session_inbox import SessionInbox
    from sqlalchemy import select

    async with get_db_session() as db:
        inbox = (
            await db.execute(select(SessionInbox).where(SessionInbox.source_job_id == job.id))
        ).scalar_one()
    assert inbox.session_id == session_id
    assert inbox.status == "pending"
    assert inbox.payload["reason"] == "summarize results"


async def test_retry_schedules_and_exhausts_to_failed():
    job, _ = await _admit(max_attempts=2)
    first = await _claim(job)
    retried = await repo.settle_invocation(
        job.id,
        first.lease_token,
        Retry(checkpoint={"step": 1}, error_code="oss_timeout", retry_at=NOW()),
        attempt_id=first.attempt_id,
    )
    assert retried.status == JobStatus.RETRY_SCHEDULED.value
    assert retried.error_code == "oss_timeout"

    second = await _claim(job, worker_id="w2")
    assert second.job.attempt_count == 2
    exhausted = await repo.settle_invocation(
        job.id,
        second.lease_token,
        Retry(checkpoint={"step": 1}, error_code="oss_timeout", retry_at=NOW()),
        attempt_id=second.attempt_id,
    )
    assert exhausted.status == JobStatus.FAILED.value
    assert "retry budget exhausted" in exhausted.error_message
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.FAILED.value


async def test_failed_and_cancelled_settlements():
    job, _ = await _admit()
    claimed = await _claim(job)
    settled = await repo.settle_invocation(
        job.id, claimed.lease_token, Failed(error_code="bad_input", message="nope"),
        attempt_id=claimed.attempt_id,
    )
    assert settled.status == JobStatus.FAILED.value
    assert settled.error_code == "bad_input"

    job2, _ = await _admit()
    claimed2 = await _claim(job2)
    settled2 = await repo.settle_invocation(
        job2.id, claimed2.lease_token, Cancelled(), attempt_id=claimed2.attempt_id
    )
    assert settled2.status == JobStatus.CANCELLED.value


async def test_wait_user_settlement_records_prompt_event():
    job, _ = await _admit()
    claimed = await _claim(job)
    await repo.settle_invocation(
        job.id,
        claimed.lease_token,
        WaitUser(checkpoint={}, prompt="pick a voice", input_schema={"type": "object"}),
        attempt_id=claimed.attempt_id,
    )
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.WAITING_USER.value
    assert events[-1].payload["prompt"] == "pick a voice"


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------

async def test_progress_updates_row_and_only_phase_change_emits_event():
    job, _ = await _admit()
    claimed = await _claim(job)
    await repo.update_progress(job.id, claimed.lease_token, progress_data={"done": 1})
    await repo.update_progress(job.id, claimed.lease_token, progress_data={"done": 2})
    events = await _events(job.id, job.user_id)
    assert len(events) == 2  # created + claimed only

    await repo.update_progress(
        job.id, claimed.lease_token, progress_data={"done": 3}, phase="asset_publish"
    )
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.PROGRESSED.value
    assert events[-1].payload["phase"] == "asset_publish"

    with pytest.raises(repo.StaleLeaseError):
        await repo.update_progress(job.id, claimed.lease_token + 1, progress_data={})


# ---------------------------------------------------------------------------
# Cancel / wake / inputs
# ---------------------------------------------------------------------------

async def test_cancel_unclaimed_settles_immediately():
    job, _ = await _admit()
    cancelled = await repo.request_cancel(job.id, job.user_id)
    assert cancelled.status == JobStatus.CANCELLED.value
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.CANCELLED.value


async def test_cancel_running_sets_desired_state_only():
    job, _ = await _admit()
    claimed = await _claim(job)
    result = await repo.request_cancel(job.id, job.user_id)
    assert result.status == JobStatus.RUNNING.value
    assert result.desired_state == "cancel"
    assert await repo.is_cancel_requested(job.id)
    events = await _events(job.id, job.user_id)
    assert events[-1].event_type == JobEventType.CANCEL_REQUESTED.value
    # The lease holder can still settle with the observed cancellation.
    settled = await repo.settle_invocation(
        job.id, claimed.lease_token, Cancelled(), attempt_id=claimed.attempt_id
    )
    assert settled.status == JobStatus.CANCELLED.value


async def test_cancel_wrong_user_raises():
    job, _ = await _admit()
    with pytest.raises(repo.JobNotFound):
        await repo.request_cancel(job.id, _user())


async def test_wake_moves_waiting_to_queued():
    job, _ = await _admit()
    claimed = await _claim(job)
    await repo.settle_invocation(
        job.id,
        claimed.lease_token,
        WaitExternal(checkpoint={}, wake_at=NOW() + timedelta(hours=1)),
        attempt_id=claimed.attempt_id,
    )
    assert await repo.wake_job(job.id, reason="provider_callback")
    fresh = await repo.get_job(job.id, job.user_id)
    assert fresh.status == JobStatus.QUEUED.value
    # Immediately claimable now.
    reclaimed = await _claim(job, worker_id="w2")
    assert reclaimed.lease_token == 2


async def test_wake_ignores_non_waiting():
    job, _ = await _admit()
    assert not await repo.wake_job(job.id, reason="noop")


async def test_add_input_idempotent_and_wakes():
    job, _ = await _admit()
    claimed = await _claim(job)
    await repo.settle_invocation(
        job.id,
        claimed.lease_token,
        WaitUser(checkpoint={}, prompt="answer me"),
        attempt_id=claimed.attempt_id,
    )
    row, created = await repo.add_input(
        job.id, job.user_id, kind="user_answer", payload={"a": 1}, idempotency_key="ans-1"
    )
    assert created is True
    assert (await repo.get_job(job.id, job.user_id)).status == JobStatus.QUEUED.value

    dup, created2 = await repo.add_input(
        job.id, job.user_id, kind="user_answer", payload={"a": 1}, idempotency_key="ans-1"
    )
    assert created2 is False and dup.id == row.id

    pending = await repo.unconsumed_inputs(job.id)
    assert [p.id for p in pending] == [row.id]
    await repo.mark_inputs_consumed([row.id])
    assert await repo.unconsumed_inputs(job.id) == []


async def test_add_input_wrong_user_raises():
    job, _ = await _admit()
    with pytest.raises(repo.JobNotFound):
        await repo.add_input(
            job.id, _user(), kind="user_answer", payload={}, idempotency_key="x"
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

async def test_get_job_is_user_scoped():
    job, _ = await _admit()
    assert await repo.get_job(job.id, job.user_id) is not None
    assert await repo.get_job(job.id, _user()) is None


async def test_list_jobs_filters():
    user = _user()
    session_id = "sess_" + uuid.uuid4().hex[:6]
    j1, _ = await _admit(user_id=user, idempotency_key="a", session_id=session_id)
    j2, _ = await _admit(user_id=user, idempotency_key="b")
    listed = await repo.list_jobs(user, session_id=session_id)
    assert [j.id for j in listed] == [j1.id]
    all_jobs = await repo.list_jobs(user)
    assert {j.id for j in all_jobs} == {j1.id, j2.id}
    assert await repo.list_jobs(_user()) == []
