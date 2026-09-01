"""Timer semantics: backoff, atomic claim, stuck reclaim, auto-disable, result state machine."""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.config import get_config
from cron.lease import (
    CronLease,
    CronLeaseLost,
    claim_job,
    renew_lease,
    run_with_heartbeat,
)
from cron.timer import TimerState, _apply_job_result, _claim_job, _collect_runnable_jobs, _execute_jobs_concurrent, _is_transient_error, error_backoff_ms

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def aware(dt):
    """sqlite hands back naive datetimes; production writes are all UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def test_backoff_sequence_matches_openclaw():
    assert [error_backoff_ms(n) for n in (1, 2, 3, 4, 5, 6, 99)] == [
        30_000, 60_000, 300_000, 900_000, 3_600_000, 3_600_000, 3_600_000
    ]


def test_transient_error_classification():
    assert _is_transient_error("429 Too Many Requests")
    assert _is_transient_error("fetch failed: ECONNRESET")
    assert _is_transient_error("upstream returned 503")
    assert _is_transient_error("request timeout")
    assert not _is_transient_error("SyntaxError: invalid prompt")
    assert not _is_transient_error(None)


async def test_initial_renewal_is_bounded_by_monotonic_ttl(monkeypatch):
    import cron.lease as lease_mod

    async def blocked_renew(_lease):
        await asyncio.Event().wait()

    monkeypatch.setattr(lease_mod, "renew_lease", blocked_renew)
    monkeypatch.setattr(lease_mod, "CRON_LEASE_TTL_SECONDS", 0.03)
    lease = CronLease(
        job_id="cron_blocked",
        token="token",
        generation=1,
        owner_id="worker",
        lease_expires_at=NOW() + timedelta(milliseconds=30),
    )
    started = False

    async def work():
        nonlocal started
        started = True
        return {"status": "ok"}

    with pytest.raises(CronLeaseLost):
        await run_with_heartbeat(lease, work, timeout=1)
    assert started is False


async def test_database_absolute_expiry_is_not_compared_to_replica_clock(
    monkeypatch,
):
    import cron.lease as lease_mod

    renewals = 0

    async def database_says_valid(_lease):
        nonlocal renewals
        renewals += 1
        # The database host is deliberately a day behind this replica. Its
        # returned absolute timestamp looks expired locally but the DB's
        # conditional UPDATE has authoritatively accepted the renewal.
        return NOW() - timedelta(days=1)

    monkeypatch.setattr(lease_mod, "renew_lease", database_says_valid)
    monkeypatch.setattr(lease_mod, "CRON_LEASE_TTL_SECONDS", 0.05)
    monkeypatch.setattr(lease_mod, "CRON_HEARTBEAT_SECONDS", 0.005)
    lease = CronLease(
        job_id="cron_db_clock_behind",
        token="token",
        generation=1,
        owner_id="worker",
        lease_expires_at=NOW() - timedelta(days=1),
    )

    async def work():
        await asyncio.sleep(0.03)
        return {"status": "ok"}

    assert await run_with_heartbeat(lease, work, timeout=1) == {"status": "ok"}
    assert renewals >= 2


async def test_database_rejected_initial_renewal_never_starts_work(monkeypatch):
    import cron.lease as lease_mod

    renewals = 0
    started = False

    async def rejected(_lease):
        nonlocal renewals
        renewals += 1
        return None

    async def work():
        nonlocal started
        started = True
        return {"status": "ok"}

    monkeypatch.setattr(lease_mod, "renew_lease", rejected)
    lease = CronLease(
        job_id="cron_db_rejected",
        token="token",
        generation=1,
        owner_id="worker",
        lease_expires_at=NOW() + timedelta(days=1),
    )

    with pytest.raises(CronLeaseLost):
        await run_with_heartbeat(lease, work, timeout=1)
    assert renewals == 1
    assert started is False


async def _insert_job(**overrides) -> str:
    from db.base import get_db_session
    from db.models.cron import CronJob

    job_id = "cron_" + uuid.uuid4().hex[:12]
    now = NOW()
    fields = dict(
        id=job_id,
        user_id="u_" + uuid.uuid4().hex[:8],
        session_id="sess_" + uuid.uuid4().hex[:8],
        name="job",
        enabled=True,
        schedule={"kind": "every", "every_ms": 600_000, "anchor_ms": int(now.timestamp() * 1000)},
        task_prompt="t",
        consecutive_errors=0,
        total_runs=0,
        total_successes=0,
        total_failures=0,
        delete_after_run=False,
        max_retries=3,
        next_run_at=now - timedelta(minutes=1),
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    async with get_db_session() as db:
        db.add(CronJob(**fields))
    return job_id


async def _fetch(job_id: str):
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import select

    async with get_db_session() as db:
        return (await db.execute(select(CronJob).where(CronJob.id == job_id))).scalar_one()


async def test_claim_is_atomic_and_single_winner():
    job_id = await _insert_job()
    assert await _claim_job(job_id) is True
    # Second claim loses: the marker is fresh
    assert await _claim_job(job_id) is False


async def test_claim_reclaims_stuck_markers():
    from cron.types import STUCK_RUN_MS

    stale = NOW() - timedelta(milliseconds=STUCK_RUN_MS * 2)
    job_id = await _insert_job(running_at=stale)
    assert await _claim_job(job_id) is True


async def test_claim_refuses_disabled_and_deleted():
    assert await _claim_job(await _insert_job(enabled=False)) is False
    assert await _claim_job(await _insert_job(is_deleted=True)) is False


async def test_claim_expiry_uses_database_clock_despite_fast_or_slow_replica(
    monkeypatch,
):
    import cron.lease as lease_mod
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    real_datetime = datetime

    healthy_id = await _insert_job()
    healthy = await claim_job(healthy_id, owner_id="healthy-db-clock")
    assert healthy is not None

    class FastReplicaClock:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz or timezone.utc) + timedelta(days=30)

    monkeypatch.setattr(lease_mod, "datetime", FastReplicaClock)
    assert await claim_job(healthy_id, owner_id="fast-replica") is None

    monkeypatch.setattr(lease_mod, "datetime", real_datetime)
    expired_id = await _insert_job()
    expired = await claim_job(expired_id, owner_id="expired-db-clock")
    assert expired is not None
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == expired_id)
            .values(lease_expires_at=NOW() - timedelta(minutes=1))
        )

    class SlowReplicaClock:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz or timezone.utc) - timedelta(days=30)

    monkeypatch.setattr(lease_mod, "datetime", SlowReplicaClock)
    replacement = await claim_job(expired_id, owner_id="slow-replica")
    assert replacement is not None
    assert replacement.generation == expired.generation + 1


async def test_deleted_job_cannot_renew_an_existing_claim():
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    job_id = await _insert_job()
    lease = await claim_job(job_id, owner_id="deleted-project-worker")
    assert lease is not None
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == job_id)
            .values(is_deleted=True, enabled=False)
        )

    assert await renew_lease(lease) is None


async def test_collect_respects_per_user_concurrency():
    user = "u_" + uuid.uuid4().hex[:8]
    cap = get_config().cron_max_concurrent_per_user
    for _ in range(cap):
        await _insert_job(user_id=user, running_at=NOW())
    due_id = await _insert_job(user_id=user)

    state = TimerState()
    due = await _collect_runnable_jobs(state)
    assert due_id not in [j["id"] for j in due]


async def test_timer_job_payload_inherits_project_id():
    project_id = "proj_" + uuid.uuid4().hex[:8]
    job_id = await _insert_job(project_id=project_id)

    due = await _collect_runnable_jobs(TimerState())
    job = next(item for item in due if item["id"] == job_id)
    assert job["project_id"] == project_id


async def test_stale_result_cannot_release_or_update_newer_claim():
    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun
    from sqlalchemy import select, update

    job_id = await _insert_job()
    old = await claim_job(job_id, owner_id="replica-old")
    assert old is not None
    old_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=old_run_id,
            job_id=job_id,
            user_id="u",
            status="running",
            claim_token=old.token,
            claim_generation=old.generation,
            claim_owner=old.owner_id,
            started_at=NOW(),
        ))

    # Simulate the old worker losing heartbeats, then another replica taking
    # the expired lease before the old result arrives.
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == job_id)
            .values(lease_expires_at=NOW() - timedelta(seconds=1))
        )

    # Expiry itself is a fence, even in the small window before takeover.
    assert await _apply_job_result(
        TimerState(), job_id, {"status": "ok", "duration_ms": 5}, claim=old
    ) is False

    started = False

    async def stale_work():
        nonlocal started
        started = True
        return {"status": "ok"}

    with pytest.raises(CronLeaseLost):
        await run_with_heartbeat(old, stale_work, timeout=1)
    assert started is False

    new = await claim_job(job_id, owner_id="replica-new")
    assert new is not None and new.generation == old.generation + 1
    async with get_db_session() as db:
        old_run = (
            await db.execute(select(CronRun).where(CronRun.id == old_run_id))
        ).scalar_one()
    assert old_run.status == "error"
    assert "generation" in (old_run.error_message or "")

    applied = await _apply_job_result(
        TimerState(), job_id, {"status": "ok", "duration_ms": 5}, claim=old
    )
    assert applied is False
    row = await _fetch(job_id)
    assert row.run_token == new.token
    assert row.total_runs == 0

    assert await _apply_job_result(
        TimerState(), job_id, {"status": "ok", "duration_ms": 5}, claim=new
    ) is True
    row = await _fetch(job_id)
    assert row.run_token is None
    assert row.total_runs == 1


async def test_worker_rechecks_due_slot_after_a_queued_job_was_completed():
    job_id = await _insert_job()
    queued = next(
        job for job in await _collect_runnable_jobs(TimerState())
        if job["id"] == job_id
    )

    other = await claim_job(job_id, owner_id="other-replica")
    assert other is not None
    assert await _apply_job_result(
        TimerState(), job_id, {"status": "ok", "duration_ms": 1}, claim=other
    ) is True

    calls = []
    state = TimerState()

    async def execute(job):
        calls.append(job)
        return {"status": "ok"}

    state.execute_job = execute
    assert await _execute_jobs_concurrent(state, [queued]) == []
    assert calls == []


async def test_queued_timer_jobs_are_claimed_only_when_a_worker_starts():
    config = get_config()
    original = config.cron_max_concurrent_jobs
    config.cron_max_concurrent_jobs = 1
    release = asyncio.Event()
    entered = asyncio.Event()
    task = None
    try:
        first_id = await _insert_job(next_run_at=NOW() - timedelta(minutes=2))
        second_id = await _insert_job(next_run_at=NOW() - timedelta(minutes=1))
        due = await _collect_runnable_jobs(TimerState())
        jobs = [job for job in due if job["id"] in {first_id, second_id}]
        assert [job["id"] for job in jobs] == [first_id, second_id]

        state = TimerState()
        calls = []

        async def execute(job):
            calls.append(job["id"])
            if len(calls) == 1:
                entered.set()
                await release.wait()
            return {"status": "ok"}

        state.execute_job = execute
        task = asyncio.create_task(
            _execute_jobs_concurrent(state, jobs)
        )
        await asyncio.wait_for(entered.wait(), timeout=1)

        assert (await _fetch(first_id)).run_token is not None
        assert (await _fetch(second_id)).run_token is None
        release.set()
        await task
        assert calls == [first_id, second_id]
    finally:
        release.set()
        if task is not None and not task.done():
            await task
        config.cron_max_concurrent_jobs = original


async def test_apply_ok_resets_errors_and_advances_schedule():
    job_id = await _insert_job(consecutive_errors=4)
    await _apply_job_result(TimerState(), job_id, {"status": "ok", "duration_ms": 5})
    row = await _fetch(job_id)
    assert row.consecutive_errors == 0
    assert row.last_status == "ok"
    assert row.running_at is None
    assert row.total_successes == 1
    assert row.next_run_at is not None and aware(row.next_run_at) > NOW() - timedelta(seconds=1)


async def test_apply_error_backs_off():
    job_id = await _insert_job()
    await _apply_job_result(TimerState(), job_id, {"status": "error", "error": "boom", "duration_ms": 5})
    row = await _fetch(job_id)
    assert row.consecutive_errors == 1
    assert row.last_error == "boom"
    assert row.enabled is True
    # next run is no earlier than the 30s first-error backoff
    assert aware(row.next_run_at) >= NOW() + timedelta(seconds=25)


async def test_auto_disable_after_threshold():
    config = get_config()
    original = config.cron_auto_disable_after
    config.cron_auto_disable_after = 3
    try:
        job_id = await _insert_job(consecutive_errors=2)
        await _apply_job_result(TimerState(), job_id, {"status": "error", "error": "still broken", "duration_ms": 5})
        row = await _fetch(job_id)
        assert row.enabled is False
        assert row.next_run_at is None
        assert row.consecutive_errors == 3
        assert "auto-disabled after 3" in (row.last_error or "")
    finally:
        config.cron_auto_disable_after = original


async def test_one_shot_success_disables_and_deletes_when_asked():
    at = (NOW() - timedelta(minutes=1)).isoformat()
    job_id = await _insert_job(schedule={"kind": "at", "at": at}, delete_after_run=True)
    await _apply_job_result(TimerState(), job_id, {"status": "ok", "duration_ms": 5})
    row = await _fetch(job_id)
    assert row.enabled is False
    assert row.next_run_at is None
    assert row.is_deleted is True


async def test_one_shot_transient_error_retries_then_permanent_disables():
    at = (NOW() - timedelta(minutes=1)).isoformat()
    job_id = await _insert_job(schedule={"kind": "at", "at": at})
    await _apply_job_result(TimerState(), job_id, {"status": "error", "error": "429 rate limit", "duration_ms": 5})
    row = await _fetch(job_id)
    assert row.enabled is True          # transient: retry scheduled
    assert row.next_run_at is not None

    job_id2 = await _insert_job(schedule={"kind": "at", "at": at})
    await _apply_job_result(TimerState(), job_id2, {"status": "error", "error": "prompt invalid", "duration_ms": 5})
    row2 = await _fetch(job_id2)
    assert row2.enabled is False        # permanent: disabled for inspection
    assert row2.next_run_at is None
