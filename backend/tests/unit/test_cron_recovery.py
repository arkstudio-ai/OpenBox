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

    job_id = await _insert_job(running_at=NOW() - timedelta(hours=1))
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
