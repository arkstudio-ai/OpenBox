"""Startup recovery — clean stuck jobs and replay missed jobs.

Called once during CronService.start() before the timer is armed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.log import create_logger

log = create_logger("cron.recovery")


@dataclass(frozen=True)
class ExpiredClaim:
    """Identity of the exact execution fenced out during recovery."""

    job_id: str
    token: str | None
    generation: int | None


async def recover_on_startup() -> None:
    """Full startup recovery sequence."""
    log.info("Running cron startup recovery...")

    interrupted = await _clear_stuck_running_markers()
    interrupted_ids = {claim.job_id for claim in interrupted}
    missed_count = await _replay_missed_jobs(skip_ids=interrupted_ids)

    log.info(
        f"Recovery complete: {len(interrupted_ids)} stuck markers cleared, "
        f"{missed_count} missed jobs replayed"
    )


async def _clear_stuck_running_markers(*, _fault=None) -> list[ExpiredClaim]:
    """Atomically clear expired claims and close their exact run rows.

    A conditional update re-checks expiry while holding the row lock, so a
    concurrent heartbeat that renewed first wins and remains untouched.  Job
    and run changes intentionally share this transaction: a restart at either
    statement cannot strand a permanently-running CronRun.
    """
    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun
    from cron.lease import (
        _database_legacy_cutoff,
        _database_now,
        expired_claim_clause,
    )
    from sqlalchemy import select, update

    expired: list[ExpiredClaim] = []
    async with get_db_session() as db:
        database_now = _database_now(db)
        legacy_cutoff = _database_legacy_cutoff(db)
        result = await db.execute(
            select(CronJob)
            .where(
                expired_claim_clause(
                    CronJob,
                    database_now,
                    legacy_cutoff=legacy_cutoff,
                )
            )
            .with_for_update(skip_locked=True)
        )
        for job in result.scalars().all():
            ownership = [
                CronJob.id == job.id,
                expired_claim_clause(
                    CronJob,
                    database_now,
                    legacy_cutoff=legacy_cutoff,
                ),
            ]
            if job.run_token is None:
                ownership.append(CronJob.run_token.is_(None))
                generation = None
            else:
                ownership.extend([
                    CronJob.run_token == job.run_token,
                    CronJob.run_generation == job.run_generation,
                    CronJob.run_owner == job.run_owner,
                ])
                generation = int(job.run_generation)

            cleared = await db.execute(
                update(CronJob)
                .where(*ownership)
                .values(
                    running_at=None,
                    run_token=None,
                    run_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    updated_at=database_now,
                )
                # SQLite returns timezone-naive ORM values; evaluating the
                # expiry predicate again in Python would compare them with an
                # aware UTC cutoff. The database is the source of truth here.
                .execution_options(synchronize_session=False)
            )
            if cleared.rowcount != 1:
                continue

            if _fault is not None:
                _fault("after_claim_clear")

            run_ownership = [CronRun.job_id == job.id]
            if job.run_token is None:
                run_ownership.extend([
                    CronRun.status == "running",
                    CronRun.claim_token.is_(None),
                    CronRun.claim_generation.is_(None),
                ])
            else:
                run_ownership.extend([
                    CronRun.claim_token == job.run_token,
                    CronRun.claim_generation == generation,
                    CronRun.claim_owner == job.run_owner,
                ])
            await db.execute(
                update(CronRun)
                .where(*run_ownership)
                .values(
                    status="error",
                    error_message=(
                        "Server restarted or worker lease expired during execution"
                    ),
                    ended_at=database_now,
                )
            )

            expired.append(ExpiredClaim(job.id, job.run_token, generation))
            log.warning(
                "Cleared expired Cron claim job=%s generation=%s",
                job.id,
                generation if generation is not None else "legacy",
            )

    return expired


async def _replay_missed_jobs(skip_ids: set[str]) -> int:
    """Detect and replay missed cron jobs (one execution per missed job, not per slot).

    A job is "missed" if: enabled, next_run_at < now, and last_run_at < next_run_at.

    A run that is missed by more than the staleness window is rescheduled to
    its next natural slot instead: a "morning report" fired at 11 pm after a
    long outage is worse than no report, and each replay costs a full agent
    run on the shared sandbox.
    """
    from core.config import get_config
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import select, update
    from cron.schedule import apply_stagger, compute_next_run_at, schedule_from_dict

    now = datetime.now(timezone.utc)
    max_age = timedelta(seconds=get_config().cron_missed_run_max_age_seconds)
    replayed = 0
    rescheduled = 0

    async with get_db_session() as db:
        result = await db.execute(
            select(CronJob).where(
                CronJob.enabled == True,
                CronJob.is_deleted == False,
                CronJob.next_run_at < now,
                CronJob.running_at.is_(None),
                CronJob.run_token.is_(None),
            )
        )
        candidates = result.scalars().all()

    from cron.schedule import as_aware_utc

    for job in candidates:
        if job.id in skip_ids:
            continue

        # Check if it was actually missed (last_run_at < next_run_at)
        if job.last_run_at and job.next_run_at:
            if job.last_run_at >= job.next_run_at:
                continue  # Already ran for this slot

        due_at = as_aware_utc(job.next_run_at)
        overdue = now - due_at if due_at else timedelta(0)
        if overdue > max_age:
            sobj = schedule_from_dict(job.schedule)
            next_run = (
                apply_stagger(compute_next_run_at(sobj, now), sobj, job.id)
                if sobj
                else None
            )
            values: dict = {"next_run_at": next_run, "updated_at": now}
            if next_run is None:
                # One-shot whose moment has passed: park it disabled for
                # inspection rather than leaving it enabled with no schedule.
                values["enabled"] = False
                values["last_error"] = "Missed one-shot run (server was down); disabled"
            async with get_db_session() as db:
                await db.execute(
                    update(CronJob)
                    .where(CronJob.id == job.id)
                    .values(**values)
                )
            log.info(
                f"Missed job {job.id} ({job.name}) was due {overdue} ago — "
                f"rescheduled to {next_run} instead of replaying"
            )
            rescheduled += 1
            continue

        log.info(f"Replaying missed job {job.id} ({job.name}), was due at {job.next_run_at}")

        # Don't execute here — just mark it as due so the timer picks it up.
        # The next on_timer tick will collect and execute it.
        # This avoids blocking startup with potentially long-running jobs.
        replayed += 1

    if rescheduled:
        log.info(f"Rescheduled {rescheduled} stale missed job(s)")
    return replayed
