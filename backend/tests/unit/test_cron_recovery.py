"""Startup recovery: stuck markers, interrupted runs, missed-run staleness policy."""
import uuid
from datetime import datetime, timedelta, timezone

from core.config import get_config
from cron.recovery import recover_on_startup

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


def aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


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


async def test_stuck_markers_cleared_and_running_runs_marked():
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    # Legacy rows have no heartbeat, so recovery intentionally waits for the
    # generous legacy stuck TTL instead of clearing them at every startup.
    job_id = await _insert_job(running_at=NOW() - timedelta(hours=3))
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id, job_id=job_id, user_id="u", session_id="s",
            status="running", started_at=NOW() - timedelta(hours=1),
        ))

    await recover_on_startup()

    assert (await _fetch(job_id)).running_at is None
    async with get_db_session() as db:
        run = (await db.execute(select(CronRun).where(CronRun.id == run_id))).scalar_one()
    assert run.status == "error"
    assert "restarted" in (run.error_message or "")


async def test_startup_does_not_clear_a_healthy_replica_lease():
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    token = "healthy-token"
    job_id = await _insert_job(
        running_at=NOW(),
        run_generation=7,
        run_token=token,
        run_owner="replica-b",
        heartbeat_at=NOW(),
        lease_expires_at=NOW() + timedelta(minutes=2),
    )
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id, job_id=job_id, user_id="u", session_id="s",
            claim_token=token, claim_generation=7, claim_owner="replica-b",
            status="running", started_at=NOW(),
        ))

    await recover_on_startup()

    job = await _fetch(job_id)
    assert job.running_at is not None
    assert job.run_token == token
    async with get_db_session() as db:
        run = (await db.execute(select(CronRun).where(CronRun.id == run_id))).scalar_one()
    assert run.status == "running"


async def test_recovery_marks_only_the_exact_expired_generation():
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    old_token = "expired-token"
    job_id = await _insert_job(
        running_at=NOW() - timedelta(minutes=5),
        run_generation=3,
        run_token=old_token,
        run_owner="dead-replica",
        heartbeat_at=NOW() - timedelta(minutes=5),
        lease_expires_at=NOW() - timedelta(minutes=1),
    )
    old_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    other_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add_all([
            CronRun(
                # Even if the expired worker recorded success just before
                # recovery, its unfenced result is not authoritative.
                id=old_run_id, job_id=job_id, user_id="u", status="ok",
                claim_token=old_token, claim_generation=3,
                claim_owner="dead-replica",
                started_at=NOW() - timedelta(minutes=5),
            ),
            CronRun(
                id=other_run_id, job_id=job_id, user_id="u", status="running",
                claim_token="newer-token", claim_generation=4,
                claim_owner="newer-replica",
                started_at=NOW(),
            ),
        ])

    await recover_on_startup()

    # A late success from the fenced worker cannot undo recovery's error.
    from cron.executor import _update_run_entry
    await _update_run_entry(
        old_run_id,
        job_id,
        None,
        status="ok",
        summary_text="late stale result",
        ended_at=NOW(),
    )

    async with get_db_session() as db:
        runs = {
            row.id: row
            for row in (await db.execute(
                select(CronRun).where(CronRun.id.in_([old_run_id, other_run_id]))
            )).scalars().all()
        }
    assert runs[old_run_id].status == "error"
    assert runs[other_run_id].status == "running"


async def test_recovery_crash_between_job_and_run_updates_rolls_back_both():
    from cron.recovery import _clear_stuck_running_markers
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select
    import pytest

    token = "expired-atomic-token"
    job_id = await _insert_job(
        running_at=NOW() - timedelta(minutes=5),
        run_generation=8,
        run_token=token,
        run_owner="dead-replica",
        heartbeat_at=NOW() - timedelta(minutes=5),
        lease_expires_at=NOW() - timedelta(minutes=1),
    )
    run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=run_id,
            job_id=job_id,
            user_id="u",
            status="running",
            claim_token=token,
            claim_generation=8,
            claim_owner="dead-replica",
            started_at=NOW() - timedelta(minutes=5),
        ))

    def crash(point):
        assert point == "after_claim_clear"
        raise RuntimeError("simulated recovery crash")

    with pytest.raises(RuntimeError, match="simulated"):
        await _clear_stuck_running_markers(_fault=crash)

    job = await _fetch(job_id)
    async with get_db_session() as db:
        run = (await db.execute(
            select(CronRun).where(CronRun.id == run_id)
        )).scalar_one()
    assert job.run_token == token
    assert run.status == "running"


async def test_recovery_expiry_uses_database_clock_under_replica_skew(monkeypatch):
    import cron.recovery as recovery_mod
    from cron.recovery import _clear_stuck_running_markers
    from db.base import get_db_session
    from db.models.cron import CronRun
    from sqlalchemy import select

    real_datetime = datetime

    healthy_token = "db-clock-healthy"
    healthy_id = await _insert_job(
        running_at=NOW(),
        run_generation=21,
        run_token=healthy_token,
        run_owner="healthy-owner",
        heartbeat_at=NOW(),
        lease_expires_at=NOW() + timedelta(minutes=2),
    )
    healthy_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=healthy_run_id,
            job_id=healthy_id,
            user_id="u",
            status="running",
            claim_token=healthy_token,
            claim_generation=21,
            claim_owner="healthy-owner",
            started_at=NOW(),
        ))

    class FastReplicaClock:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz or timezone.utc) + timedelta(days=30)

    monkeypatch.setattr(recovery_mod, "datetime", FastReplicaClock)
    initially_cleared = await _clear_stuck_running_markers()
    assert healthy_id not in {claim.job_id for claim in initially_cleared}

    monkeypatch.setattr(recovery_mod, "datetime", real_datetime)
    expired_token = "db-clock-expired"
    expired_id = await _insert_job(
        running_at=NOW() - timedelta(minutes=5),
        run_generation=22,
        run_token=expired_token,
        run_owner="expired-owner",
        heartbeat_at=NOW() - timedelta(minutes=5),
        lease_expires_at=NOW() - timedelta(minutes=1),
    )
    expired_run_id = "cron_run_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(CronRun(
            id=expired_run_id,
            job_id=expired_id,
            user_id="u",
            status="running",
            claim_token=expired_token,
            claim_generation=22,
            claim_owner="expired-owner",
            started_at=NOW() - timedelta(minutes=5),
        ))

    class SlowReplicaClock:
        @classmethod
        def now(cls, tz=None):
            return real_datetime.now(tz or timezone.utc) - timedelta(days=30)

    monkeypatch.setattr(recovery_mod, "datetime", SlowReplicaClock)
    cleared = await _clear_stuck_running_markers()
    assert expired_id in {claim.job_id for claim in cleared}

    async with get_db_session() as db:
        healthy_run = (
            await db.execute(select(CronRun).where(CronRun.id == healthy_run_id))
        ).scalar_one()
        expired_run = (
            await db.execute(select(CronRun).where(CronRun.id == expired_run_id))
        ).scalar_one()
    assert healthy_run.status == "running"
    assert expired_run.status == "error"


async def test_fresh_missed_run_stays_due_for_replay():
    due_at = NOW() - timedelta(minutes=30)  # inside the staleness window
    job_id = await _insert_job(next_run_at=due_at, last_run_at=due_at - timedelta(hours=1))

    await recover_on_startup()

    row = await _fetch(job_id)
    # Still parked in the past: the timer will pick it up and run it once.
    assert row.next_run_at is not None and aware(row.next_run_at) <= NOW()


async def test_stale_missed_run_reschedules_instead_of_replaying():
    max_age = get_config().cron_missed_run_max_age_seconds
    due_at = NOW() - timedelta(seconds=max_age * 2)
    job_id = await _insert_job(next_run_at=due_at, last_run_at=due_at - timedelta(hours=1))

    await recover_on_startup()

    row = await _fetch(job_id)
    assert row.enabled is True
    assert row.next_run_at is not None and aware(row.next_run_at) > NOW()


async def test_stale_missed_one_shot_is_disabled():
    max_age = get_config().cron_missed_run_max_age_seconds
    due_at = NOW() - timedelta(seconds=max_age * 2)
    job_id = await _insert_job(
        schedule={"kind": "at", "at": due_at.isoformat()},
        next_run_at=due_at,
        last_run_at=None,
    )

    await recover_on_startup()

    row = await _fetch(job_id)
    assert row.enabled is False
    assert row.next_run_at is None
    assert "one-shot" in (row.last_error or "")
