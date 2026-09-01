"""Real PostgreSQL multi-connection proof for atomic Cron claim quotas.

Run with ``OPENBOX_TEST_POSTGRES_URL`` pointing at a disposable-capable test
database. Each test creates and drops its own schema; no application rows are
touched.
"""
from __future__ import annotations

import asyncio
import os
import secrets
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.config import get_config
from cron.lease import _claim_job_in_transaction, _renew_lease_in_transaction
from db.models.cron import CronJob, CronRun


POSTGRES_URL = os.getenv("OPENBOX_TEST_POSTGRES_URL")


@pytest.fixture
async def postgres_cron_sessions():
    if not POSTGRES_URL:
        pytest.skip("set OPENBOX_TEST_POSTGRES_URL for PostgreSQL quota tests")

    schema = "cron_quota_" + uuid.uuid4().hex
    admin_engine = create_async_engine(POSTGRES_URL, pool_size=2)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_engine(
        POSTGRES_URL,
        pool_size=12,
        max_overflow=0,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(CronJob.__table__.create)
            await connection.run_sync(CronRun.__table__.create)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def _insert_jobs(session_factory, users: list[str]) -> list[str]:
    timestamp = datetime.now(timezone.utc)
    job_ids: list[str] = []
    async with session_factory() as db, db.begin():
        for index, user_id in enumerate(users):
            job_id = "cron_pg_" + uuid.uuid4().hex[:16]
            job_ids.append(job_id)
            db.add(CronJob(
                id=job_id,
                user_id=user_id,
                name=f"postgres quota {index}",
                schedule={
                    "kind": "every",
                    "every_ms": 600_000,
                    "anchor_ms": int(timestamp.timestamp() * 1000),
                },
                task_prompt="postgres quota contention",
                next_run_at=timestamp - timedelta(minutes=1),
                created_at=timestamp,
                updated_at=timestamp,
            ))
    return job_ids


async def _claim_on_checked_out_connection(
    session_factory,
    job_id: str,
    owner_id: str,
    ready: list[int],
    ready_lock: asyncio.Lock,
    start: asyncio.Event,
    contender_count: int,
):
    async with session_factory() as db, db.begin():
        backend_pid = int(await db.scalar(select(func.pg_backend_pid())))
        async with ready_lock:
            ready.append(backend_pid)
            if len(ready) == contender_count:
                start.set()
        await asyncio.wait_for(start.wait(), timeout=5)
        lease = await _claim_job_in_transaction(
            db,
            job_id,
            token=secrets.token_hex(24),
            user_id=None,
            require_enabled=True,
            due_before=datetime.now(timezone.utc),
            owner_id=owner_id,
        )
    return lease


async def test_postgres_multi_connection_quota_release_expiry_and_takeover(
    postgres_cron_sessions,
):
    config = get_config()
    old_global = config.cron_max_concurrent_jobs
    old_user = config.cron_max_concurrent_per_user
    config.cron_max_concurrent_jobs = 3
    config.cron_max_concurrent_per_user = 1
    try:
        users = ["pg-a", "pg-a", "pg-b", "pg-b", "pg-c", "pg-d"]
        jobs = await _insert_jobs(postgres_cron_sessions, users)
        ready: list[int] = []
        ready_lock = asyncio.Lock()
        start = asyncio.Event()
        leases = await asyncio.gather(*(
            _claim_on_checked_out_connection(
                postgres_cron_sessions,
                job_id,
                f"postgres-replica-{index}",
                ready,
                ready_lock,
                start,
                len(jobs),
            )
            for index, job_id in enumerate(jobs)
        ))

        # All contenders had distinct, simultaneously checked-out PostgreSQL
        # backends before entering the advisory-lock protocol.
        assert len(set(ready)) == len(jobs)
        winners = [
            (index, lease)
            for index, lease in enumerate(leases)
            if lease is not None
        ]
        assert len(winners) == 3
        assert max(Counter(users[index] for index, _ in winners).values()) == 1

        released_index, released = winners[0]
        async with postgres_cron_sessions() as db, db.begin():
            await db.execute(
                update(CronJob)
                .where(
                    CronJob.id == jobs[released_index],
                    CronJob.run_token == released.token,
                )
                .values(
                    running_at=None,
                    run_token=None,
                    run_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                )
            )

        winning_users = {users[index] for index, _ in winners}
        waiting_index = next(
            index
            for index, lease in enumerate(leases)
            if lease is None and users[index] not in winning_users
        )
        first_waiting_claim = await _claim_on_checked_out_connection(
            postgres_cron_sessions,
            jobs[waiting_index],
            "postgres-released-slot",
            [],
            asyncio.Lock(),
            _already_set_event(),
            0,
        )
        assert first_waiting_claim is not None

        async with postgres_cron_sessions() as db, db.begin():
            await db.execute(
                update(CronJob)
                .where(CronJob.id == jobs[waiting_index])
                .values(
                    lease_expires_at=(
                        func.clock_timestamp() - text("INTERVAL '1 second'")
                    )
                )
            )

        takeover = await _claim_on_checked_out_connection(
            postgres_cron_sessions,
            jobs[waiting_index],
            "postgres-takeover",
            [],
            asyncio.Lock(),
            _already_set_event(),
            0,
        )
        assert takeover is not None
        assert takeover.generation == first_waiting_claim.generation + 1

        # Prove an UPDATE that renewed before the old deadline but has not yet
        # committed cannot disappear from a competing claim's MVCC snapshot.
        # The renewal holds the same advisory locks until commit, so the claim
        # waits, then counts the newly committed lease and remains denied.
        async with postgres_cron_sessions() as db, db.begin():
            await db.execute(
                update(CronJob)
                .where(CronJob.id == jobs[waiting_index])
                .values(
                    lease_expires_at=(
                        func.clock_timestamp()
                        + text("INTERVAL '250 milliseconds'")
                    )
                )
            )

        renewal_updated = asyncio.Event()
        allow_renewal_commit = asyncio.Event()

        async def delayed_renewal():
            async with postgres_cron_sessions() as db, db.begin():
                renewed_until = await _renew_lease_in_transaction(db, takeover)
                assert renewed_until is not None
                renewal_updated.set()
                await asyncio.wait_for(allow_renewal_commit.wait(), timeout=5)

        renewal_task = asyncio.create_task(delayed_renewal())
        await asyncio.wait_for(renewal_updated.wait(), timeout=5)
        await asyncio.sleep(0.3)
        blocked_claim = asyncio.create_task(
            _claim_on_checked_out_connection(
                postgres_cron_sessions,
                jobs[released_index],
                "postgres-mvcc-contender",
                [],
                asyncio.Lock(),
                _already_set_event(),
                0,
            )
        )
        try:
            await asyncio.sleep(0.05)
            assert blocked_claim.done() is False
        finally:
            allow_renewal_commit.set()
        await renewal_task
        assert await blocked_claim is None
    finally:
        config.cron_max_concurrent_jobs = old_global
        config.cron_max_concurrent_per_user = old_user


def _already_set_event() -> asyncio.Event:
    event = asyncio.Event()
    event.set()
    return event
