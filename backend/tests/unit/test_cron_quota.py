"""Atomic Cron concurrency quotas shared by timer and manual dispatch."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.config import get_config
from cron.lease import claim_job
from cron.timer import TimerState, _claim_job, _execute_jobs_concurrent


def now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def quota_config():
    config = get_config()
    old_global = config.cron_max_concurrent_jobs
    old_user = config.cron_max_concurrent_per_user
    try:
        yield config
    finally:
        config.cron_max_concurrent_jobs = old_global
        config.cron_max_concurrent_per_user = old_user


async def insert_job(
    user_id: str,
    *,
    enabled: bool = True,
    due_offset: timedelta = timedelta(minutes=-1),
) -> str:
    from db.base import get_db_session
    from db.models.cron import CronJob

    job_id = "cron_quota_" + uuid.uuid4().hex[:16]
    timestamp = now()
    async with get_db_session() as db:
        db.add(CronJob(
            id=job_id,
            user_id=user_id,
            name=job_id,
            enabled=enabled,
            schedule={
                "kind": "every",
                "every_ms": 600_000,
                "anchor_ms": int(timestamp.timestamp() * 1000),
            },
            task_prompt="quota test",
            next_run_at=timestamp + due_offset if enabled else None,
            created_at=timestamp,
            updated_at=timestamp,
        ))
    return job_id


async def release_claim(job_id: str, token: str | None = None) -> None:
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    predicates = [CronJob.id == job_id]
    if token is not None:
        predicates.append(CronJob.run_token == token)
    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(*predicates)
            .values(
                running_at=None,
                run_token=None,
                run_owner=None,
                lease_expires_at=None,
                heartbeat_at=None,
            )
        )


async def test_concurrent_different_job_claims_obey_global_and_user_caps(
    quota_config,
):
    quota_config.cron_max_concurrent_jobs = 2
    quota_config.cron_max_concurrent_per_user = 1
    users = ["quota-a", "quota-a", "quota-b", "quota-b", "quota-c"]
    jobs = [await insert_job(user) for user in users]

    leases = await asyncio.gather(*(
        claim_job(job_id, owner_id=f"replica-{index}")
        for index, job_id in enumerate(jobs)
    ))
    winners = [
        (users[index], lease)
        for index, lease in enumerate(leases)
        if lease is not None
    ]

    assert len(winners) == 2
    assert len({user for user, _lease in winners}) == 2


async def test_expired_claim_frees_quota_and_same_job_takeover_is_fenced(
    quota_config,
):
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import update

    quota_config.cron_max_concurrent_jobs = 1
    quota_config.cron_max_concurrent_per_user = 1
    first_id = await insert_job("takeover-a")
    waiting_id = await insert_job("takeover-b")

    first = await claim_job(first_id, owner_id="replica-old")
    assert first is not None
    assert await claim_job(waiting_id, owner_id="replica-waiting") is None

    async with get_db_session() as db:
        await db.execute(
            update(CronJob)
            .where(CronJob.id == first_id)
            .values(lease_expires_at=now() - timedelta(seconds=1))
        )

    replacement = await claim_job(first_id, owner_id="replica-new")
    assert replacement is not None
    assert replacement.generation == first.generation + 1
    assert await claim_job(waiting_id, owner_id="replica-waiting") is None

    await release_claim(first_id, replacement.token)
    assert await claim_job(waiting_id, owner_id="replica-waiting") is not None


async def test_manual_and_scheduled_paths_share_the_same_quota_gate(
    quota_config,
):
    from cron.service import CronService

    quota_config.cron_max_concurrent_jobs = 1
    quota_config.cron_max_concurrent_per_user = 1
    blocker_id = await insert_job("entry-blocker")
    scheduled_id = await insert_job("entry-scheduled")
    manual_id = await insert_job("entry-manual", enabled=False)
    blocker = await claim_job(blocker_id, owner_id="blocking-replica")
    assert blocker is not None

    service = CronService()

    async def execute(_job):
        return {"status": "ok"}

    service.set_executor(execute)
    assert await _claim_job(scheduled_id) is False
    assert await service.run(manual_id, "entry-manual") == {
        "ok": False,
        "reason": "already-running",
    }

    await release_claim(blocker_id, blocker.token)
    assert await _claim_job(scheduled_id) is True
    await release_claim(scheduled_id)

    assert await service.run(manual_id, "entry-manual") == {
        "ok": True,
        "status": "triggered",
    }
    for _ in range(100):
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select

        async with get_db_session() as db:
            token = await db.scalar(
                select(CronJob.run_token).where(CronJob.id == manual_id)
            )
        if token is None:
            break
        await asyncio.sleep(0.01)
    assert token is None


async def test_quota_denial_does_not_starve_later_eligible_user(
    quota_config,
):
    quota_config.cron_max_concurrent_jobs = 2
    quota_config.cron_max_concurrent_per_user = 1
    blocker_id = await insert_job("fair-a")
    denied_id = await insert_job("fair-a", due_offset=timedelta(minutes=-2))
    eligible_id = await insert_job("fair-b", due_offset=timedelta(minutes=-1))
    blocker = await claim_job(blocker_id, owner_id="fairness-blocker")
    assert blocker is not None

    class SerialTimerState(TimerState):
        @property
        def max_concurrent_jobs(self) -> int:
            return 1

    state = SerialTimerState()
    calls: list[str] = []

    async def execute(job):
        calls.append(job["id"])
        return {"status": "ok"}

    state.execute_job = execute
    results = await _execute_jobs_concurrent(
        state,
        [{"id": denied_id}, {"id": eligible_id}],
    )

    assert calls == [eligible_id]
    assert [job_id for job_id, _result in results] == [eligible_id]
