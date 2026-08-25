"""Timer semantics: backoff, atomic claim, stuck reclaim, auto-disable, result state machine."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.config import get_config
from cron.timer import TimerState, _apply_job_result, _claim_job, _collect_runnable_jobs, _is_transient_error, error_backoff_ms

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


async def test_collect_respects_per_user_concurrency():
    user = "u_" + uuid.uuid4().hex[:8]
    cap = get_config().cron_max_concurrent_per_user
    for _ in range(cap):
        await _insert_job(user_id=user, running_at=NOW())
    due_id = await _insert_job(user_id=user)

    state = TimerState()
    due = await _collect_runnable_jobs(state)
    assert due_id not in [j["id"] for j in due]


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
