"""Timer-based job scheduler — translated from OpenClaw's timer.ts.

Core loop: arm_timer → on_timer → collect_runnable → execute → apply_result → re-arm.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from core.log import create_logger
from cron.types import (
    BACKOFF_SCHEDULE_MS, MAX_TIMER_DELAY_MS, MIN_REFIRE_GAP_MS,
    TRANSIENT_PATTERNS, CronJobStatus,
)
from cron.schedule import compute_next_run_at
from cron.lease import (
    _database_legacy_cutoff,
    _database_now,
    CronLease,
    CronLeaseLost,
    claim_job,
    claimed_job_payload,
    claimable_clause,
    live_claim_clause,
    run_with_heartbeat,
)

log = create_logger("cron.timer")

TIMER_HEARTBEAT_SECONDS = 30


class TimerState:
    """Mutable state for the timer loop."""

    def __init__(self):
        self.running = False
        self.timer_handle: asyncio.TimerHandle | None = None
        self.watchdog_handle: asyncio.TimerHandle | None = None
        self.lock = asyncio.Lock()
        self.last_tick_at_ms: int | None = None

        # Callbacks (injected by CronService)
        self.execute_job: Any = None  # async (job_row) -> dict
        self.on_job_result: Any = None  # async (job_id, result) -> None

    @property
    def max_concurrent_jobs(self) -> int:
        from core.config import get_config

        return max(1, get_config().cron_max_concurrent_jobs)


def arm_timer(state: TimerState) -> None:
    """Schedule the next timer tick based on earliest due job."""
    _cancel_timer(state)

    loop = asyncio.get_event_loop()
    if loop.is_closed():
        return

    # Find next wake time
    next_wake_coro = _find_next_wake_ms(state)
    loop.create_task(_arm_timer_async(state, next_wake_coro))


async def _arm_timer_async(state: TimerState, next_wake_coro) -> None:
    """Async helper for arm_timer — needs DB access."""
    try:
        next_wake_at = await next_wake_coro
        now_ms = _now_ms()

        if next_wake_at is None:
            # No jobs scheduled, wake in MAX_TIMER_DELAY to check again
            delay_s = MAX_TIMER_DELAY_MS / 1000
        else:
            delay_ms = max(0, next_wake_at - now_ms)
            # Floor: prevent tight loops
            if delay_ms == 0:
                delay_ms = MIN_REFIRE_GAP_MS
            # Ceiling: wake at least once per minute
            delay_ms = min(delay_ms, MAX_TIMER_DELAY_MS)
            delay_s = delay_ms / 1000

        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            state.timer_handle = loop.call_later(delay_s, lambda: loop.create_task(on_timer(state)))

    except Exception as e:
        log.error(f"arm_timer error: {e}")
        # Fallback: retry in 60s
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            state.timer_handle = loop.call_later(60, lambda: loop.create_task(on_timer(state)))


async def on_timer(state: TimerState) -> None:
    """Main timer tick — collect and execute due jobs."""
    if state.running:
        _arm_watchdog(state)
        return

    state.running = True
    state.last_tick_at_ms = _now_ms()
    _arm_watchdog(state)
    heartbeat_stop = asyncio.Event()
    heartbeat = asyncio.create_task(
        _timer_liveness_heartbeat(state, heartbeat_stop)
    )

    try:
        async with state.lock:
            due_jobs = await _collect_runnable_jobs(state)

        if not due_jobs:
            return

        # Workers claim only when they are ready to start. Claiming a large
        # queue up front would let waiting leases expire without heartbeats.
        await _execute_jobs_concurrent(state, due_jobs)

    except Exception as e:
        log.error(f"on_timer error: {e}")
    finally:
        # Piggyback maintenance tasks on timer tick
        try:
            from cron.reaper import sweep_if_due
            await sweep_if_due()
        except Exception as e:
            log.debug(f"Reaper sweep error: {e}")

        try:
            from cron.warmup import check_warmup, update_keepalive_users
            await check_warmup()
            await update_keepalive_users()
        except Exception as e:
            log.debug(f"Warmup check error: {e}")

        try:
            from video.job_recovery import sweep_if_due as video_sweep_if_due
            await video_sweep_if_due()
        except Exception as e:
            log.debug(f"Video job recovery sweep error: {e}")

        heartbeat_stop.set()
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat
        state.running = False
        arm_timer(state)


def stop_timer(state: TimerState) -> None:
    """Stop the timer and watchdog."""
    _cancel_timer(state)
    _cancel_watchdog(state)
    state.running = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


async def _timer_liveness_heartbeat(
    state: TimerState,
    stop: asyncio.Event,
) -> None:
    """Keep readiness fresh while a legitimate long Cron job is executing."""
    while not stop.is_set():
        state.last_tick_at_ms = _now_ms()
        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=TIMER_HEARTBEAT_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


def _cancel_timer(state: TimerState) -> None:
    if state.timer_handle is not None:
        state.timer_handle.cancel()
        state.timer_handle = None


def _cancel_watchdog(state: TimerState) -> None:
    if state.watchdog_handle is not None:
        state.watchdog_handle.cancel()
        state.watchdog_handle = None


def _arm_watchdog(state: TimerState) -> None:
    """Re-arm watchdog to re-check timer in 60s (prevents scheduler death)."""
    _cancel_watchdog(state)
    loop = asyncio.get_event_loop()
    if not loop.is_closed():
        state.watchdog_handle = loop.call_later(
            60, lambda: loop.create_task(on_timer(state))
        )


async def _find_next_wake_ms(state: TimerState) -> int | None:
    """Find the earliest next_run_at across all enabled jobs."""
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import select

    async with get_db_session() as db:
        result = await db.execute(
            select(CronJob.next_run_at)
            .where(
                CronJob.enabled == True,
                CronJob.is_deleted == False,
                CronJob.next_run_at.isnot(None),
            )
            .order_by(CronJob.next_run_at.asc())
            .limit(1)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        # The column is timezone-naive; .timestamp() on a naive value applies
        # the SERVER'S local timezone, which skews every wake-up by the UTC
        # offset (hours of timer drift on a non-UTC host).
        from cron.schedule import as_aware_utc
        return int(as_aware_utc(row).timestamp() * 1000)


async def _collect_runnable_jobs(state: TimerState) -> list[dict]:
    """Query DB for due jobs that should be executed."""
    from core.config import get_config
    from db.base import get_db_session
    from db.models.cron import CronJob
    from sqlalchemy import select

    now = datetime.now(timezone.utc)

    async with get_db_session() as db:
        database_now = _database_now(db)
        legacy_cutoff = _database_legacy_cutoff(db)
        result = await db.execute(
            select(CronJob)
            .where(
                CronJob.enabled == True,
                CronJob.is_deleted == False,
                CronJob.next_run_at <= now,
                claimable_clause(
                    CronJob,
                    database_now,
                    legacy_cutoff=legacy_cutoff,
                ),
            )
            .order_by(CronJob.next_run_at.asc())
        )
        rows = result.scalars().all()

    max_per_user = max(1, get_config().cron_max_concurrent_per_user)
    user_running_count: dict[str, int] = {}

    # Count already-running (non-stuck) jobs per user
    async with get_db_session() as db:
        database_now = _database_now(db)
        legacy_cutoff = _database_legacy_cutoff(db)
        running_result = await db.execute(
            select(CronJob.user_id)
            .where(
                live_claim_clause(
                    CronJob,
                    database_now,
                    legacy_cutoff=legacy_cutoff,
                )
            )
        )
        for r in running_result.all():
            user_running_count[r[0]] = user_running_count.get(r[0], 0) + 1

    due = []
    for row in rows:
        # Check backoff window
        if _is_in_backoff(row):
            continue
        # Per-user concurrency check
        user_running = user_running_count.get(row.user_id, 0)
        if user_running >= max_per_user:
            continue
        user_running_count[row.user_id] = user_running + 1
        due.append({
            "id": row.id,
            "user_id": row.user_id,
            "project_id": row.project_id,
            "session_id": row.session_id,
            "name": row.name,
            "schedule": row.schedule,
            "task_prompt": row.task_prompt,
            "agent": row.agent,
            "model": row.model,
            "timeout_seconds": row.timeout_seconds,
            "delivery": row.delivery,
            "delete_after_run": row.delete_after_run,
            "max_retries": row.max_retries,
            "consecutive_errors": row.consecutive_errors,
            "summary_cache": row.summary_cache,
            "summary_cache_msg_id": row.summary_cache_msg_id,
        })

    return due


def _is_in_backoff(job) -> bool:
    """Check if job is in error backoff window."""
    from cron.schedule import as_aware_utc

    errors = job.consecutive_errors or 0
    if errors == 0:
        return False
    last_run = as_aware_utc(job.last_run_at)
    if last_run is None:
        return False
    backoff_ms = error_backoff_ms(errors)
    backoff_until = last_run + timedelta(milliseconds=backoff_ms)
    return datetime.now(timezone.utc) < backoff_until


async def _claim_job(job_id: str) -> bool:
    """Compatibility wrapper for tests and older internal callers."""
    return await claim_job(
        job_id,
        due_before=datetime.now(timezone.utc),
    ) is not None


async def _execute_jobs_concurrent(
    state: TimerState, jobs: list[dict]
) -> list[tuple[str, dict]]:
    """Execute jobs with concurrency control (worker pool pattern)."""
    results: list[tuple[str, dict]] = []
    cursor = 0
    cursor_lock = asyncio.Lock()

    async def worker():
        nonlocal cursor
        while True:
            async with cursor_lock:
                if cursor >= len(jobs):
                    return
                idx = cursor
                cursor += 1

            queued_job = jobs[idx]
            claim = CronLease.from_payload(queued_job.get("_cron_claim"))
            if claim is None:
                try:
                    claim = await claim_job(
                        queued_job["id"],
                        due_before=datetime.now(timezone.utc),
                    )
                except Exception as exc:
                    log.error(
                        "Failed to claim Cron job %s: %s",
                        queued_job["id"],
                        exc,
                    )
                    continue
            if claim is None:
                continue

            # Collection can be arbitrarily old while workers are busy. Read
            # project/prompt/model/schedule again under the acquired fence.
            job = await claimed_job_payload(claim)
            if job is None:
                log.warning(
                    "Cron claim vanished before execution job=%s generation=%s",
                    queued_job["id"],
                    claim.generation,
                )
                continue
            start = time.time()
            lease_lost = False
            try:
                if state.execute_job:
                    if claim is not None:
                        result = await run_with_heartbeat(
                            claim,
                            lambda: state.execute_job(job),
                            timeout=job.get("timeout_seconds", 1800),
                        )
                    else:
                        result = await asyncio.wait_for(
                            state.execute_job(job),
                            timeout=job.get("timeout_seconds", 1800),
                        )
                else:
                    result = {"status": "error", "error": "No executor configured"}
            except asyncio.TimeoutError:
                result = {"status": "error", "error": "Job execution timed out"}
            except CronLeaseLost as exc:
                lease_lost = True
                result = {"status": "error", "error": str(exc)}
            except Exception as e:
                result = {"status": "error", "error": str(e)}

            result["duration_ms"] = int((time.time() - start) * 1000)
            # execute_cron_job records these identities on the claimed payload
            # before cancellation can interrupt it.  A timer timeout therefore
            # still finalizes the exact CronRun in the settlement transaction.
            result.setdefault("run_id", job.get("_cron_run_id"))
            result.setdefault(
                "temp_session_id", job.get("_cron_temp_session_id")
            )
            result.setdefault("started_at", job.get("_cron_started_at"))
            result.setdefault("ended_at", datetime.now(timezone.utc))
            result.setdefault("locale", job.get("_cron_locale") or "zh-CN")
            result.setdefault(
                "context_summary", job.get("_cron_context_summary")
            )
            result.setdefault("tokens", job.get("_cron_tokens") or {})
            result.setdefault("silent", False)
            results.append((job["id"], result))
            if lease_lost:
                log.warning(
                    "Discarded stale Cron result job=%s generation=%s",
                    job["id"],
                    claim.generation if claim else "legacy",
                )
                continue
            try:
                async with state.lock:
                    applied = await _apply_job_result(
                        state,
                        job["id"],
                        result,
                        claim=claim,
                    )
                if not applied:
                    log.warning(
                        "Discarded fenced Cron result job=%s generation=%s",
                        job["id"],
                        claim.generation if claim else "legacy",
                    )
            except Exception as exc:
                log.error(f"Failed to apply result for job {job['id']}: {exc}")

    concurrency = min(state.max_concurrent_jobs, len(jobs))
    workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
    await asyncio.gather(*workers, return_exceptions=True)

    return results


async def _apply_job_result(
    state: TimerState,
    job_id: str,
    result: dict,
    *,
    claim: CronLease | None = None,
) -> bool:
    """Atomically settle the exact claim, CronRun, and delivery outbox."""
    from db.base import get_db_session
    from db.models.cron import CronJob, CronRun
    from sqlalchemy import select, update

    now = datetime.now(timezone.utc)
    status = result.get("status", "error")
    error = result.get("error")
    duration_ms = result.get("duration_ms", 0)

    deliveries_created = False
    async with get_db_session() as db:
        database_now = _database_now(db)
        ownership = [
            CronJob.id == job_id,
            CronJob.is_deleted == False,  # noqa: E712
        ]
        if claim is None:
            # Compatibility for old rows/tests must never bypass a modern
            # owner's token.
            ownership.append(CronJob.run_token.is_(None))
        else:
            ownership.extend([
                CronJob.run_token == claim.token,
                CronJob.run_generation == claim.generation,
                CronJob.run_owner == claim.owner_id,
                # Ownership expires even when no replacement has claimed yet.
                # This prevents a paused replica from committing late state.
                CronJob.lease_expires_at.isnot(None),
                CronJob.lease_expires_at >= database_now,
            ])
        row = await db.execute(
            select(CronJob).where(*ownership).with_for_update()
        )
        job = row.scalar_one_or_none()
        if not job:
            return False

        run_id = result.get("run_id")
        run_predicates = [CronRun.job_id == job_id]
        if run_id:
            run_predicates.append(CronRun.id == run_id)
        else:
            run_predicates.append(CronRun.status == "running")
        if claim is not None:
            run_predicates.extend([
                CronRun.claim_token == claim.token,
                CronRun.claim_generation == claim.generation,
                CronRun.claim_owner == claim.owner_id,
            ])
        run = (
            await db.execute(
                select(CronRun)
                .where(*run_predicates)
                .order_by(CronRun.started_at.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        # A production executor that advertised a run receipt must not settle
        # a different/missing audit row and manufacture delivery side effects.
        if run_id and run is None:
            return False

        values: dict = {
            "running_at": None,
            "run_token": None,
            "run_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "last_run_at": now,
            "last_status": status,
            "last_duration_ms": duration_ms,
            "updated_at": now,
            "total_runs": job.total_runs + 1,
        }

        auto_disabled = False
        if status == "ok":
            values["consecutive_errors"] = 0
            values["last_error"] = None
            values["total_successes"] = job.total_successes + 1
        elif status == "error":
            consecutive = job.consecutive_errors + 1
            values["consecutive_errors"] = consecutive
            values["last_error"] = error
            values["total_failures"] = job.total_failures + 1

            # A job failing this many times in a row is broken, not unlucky.
            # Backoff caps at 60 minutes, so without this the job would retry
            # hourly forever, spending tokens and a Wuying lease each time.
            from core.config import get_config
            threshold = get_config().cron_auto_disable_after
            if threshold > 0 and consecutive >= threshold:
                auto_disabled = True
                values["enabled"] = False
                values["next_run_at"] = None
                values["last_error"] = (
                    f"[auto-disabled after {consecutive} consecutive failures] {error}"
                )

        # Compute next_run_at
        schedule = job.schedule
        schedule_kind = schedule.get("kind") if isinstance(schedule, dict) else None
        if auto_disabled:
            schedule_kind = None  # skip schedule advancement entirely

        if schedule_kind == "at":
            # One-shot job
            if status == "ok" or status == "skipped":
                values["enabled"] = False
                values["next_run_at"] = None
            elif status == "error":
                consecutive = values.get("consecutive_errors", job.consecutive_errors + 1)
                if _is_transient_error(error) and consecutive <= job.max_retries:
                    backoff = error_backoff_ms(consecutive)
                    values["next_run_at"] = now + timedelta(milliseconds=backoff)
                else:
                    values["enabled"] = False
                    values["next_run_at"] = None
        elif schedule_kind is not None:
            # Recurring job
            from cron.schedule import apply_stagger, compute_next_run_at as _compute, schedule_from_dict

            sched = schedule_from_dict(schedule)

            if sched:
                natural_next = apply_stagger(_compute(sched, now), sched, job_id)
                if status == "error" and job.enabled:
                    backoff = error_backoff_ms(values.get("consecutive_errors", 1))
                    backoff_next = now + timedelta(milliseconds=backoff)
                    if natural_next:
                        values["next_run_at"] = max(natural_next, backoff_next)
                    else:
                        values["next_run_at"] = backoff_next
                elif natural_next:
                    # Ensure MIN_REFIRE_GAP
                    min_next = now + timedelta(milliseconds=MIN_REFIRE_GAP_MS)
                    values["next_run_at"] = max(natural_next, min_next)
                else:
                    values["next_run_at"] = None

        # Handle delete_after_run
        if schedule_kind == "at" and job.delete_after_run and status == "ok":
            values["is_deleted"] = True

        updated = await db.execute(
            update(CronJob)
            .where(*ownership)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if updated.rowcount != 1:
            return False

        if run is not None:
            from cron.i18n import is_silent

            tokens = result.get("tokens") or {}
            silent = bool(result.get("silent")) if "silent" in result else (
                status == "ok" and is_silent(result.get("summary_text"))
            )
            needs_session_delivery = bool(
                status == "ok"
                and not silent
                and job.session_id
            )
            run.status = status
            run.temp_session_id = (
                result.get("temp_session_id") or run.temp_session_id
            )
            run.summary_text = result.get("summary_text")
            run.context_summary = result.get("context_summary")
            run.error_message = error if status == "error" else None
            run.duration_ms = duration_ms
            run.input_tokens = int(tokens.get("input_tokens") or 0)
            run.output_tokens = int(tokens.get("output_tokens") or 0)
            run.total_tokens = int(tokens.get("total_tokens") or 0)
            run.ended_at = result.get("ended_at") or now
            run.injected = not needs_session_delivery
            run.injected_at = database_now if run.injected else None
            await db.flush()

            # These rows are added only after both exact ownership checks have
            # succeeded; commit/rollback covers Job, Run, and all deliveries.
            from cron.outbox import build_delivery_rows

            deliveries = build_delivery_rows(job, run, result, now)
            db.add_all(deliveries)
            deliveries_created = bool(deliveries)

    if auto_disabled:
        from bus import bus
        from bus.events import CRON_JOB_AUTO_DISABLED

        bus.publish(CRON_JOB_AUTO_DISABLED, {
            "userId": job.user_id,
            "jobId": job_id,
            "sessionId": job.session_id,
            "jobName": job.name,
            "consecutiveErrors": values.get("consecutive_errors"),
            "error": error,
        })
        log.warning(
            f"Auto-disabled cron job {job_id} ({job.name}) after "
            f"{values.get('consecutive_errors')} consecutive failures"
        )

    if deliveries_created:
        from cron.outbox import notify_outbox_workers

        notify_outbox_workers()

    # Notify via callback
    if state.on_job_result:
        try:
            await state.on_job_result(job_id, result)
        except Exception as e:
            log.error(f"on_job_result callback error: {e}")
    return True


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def error_backoff_ms(consecutive_errors: int) -> int:
    """Compute backoff delay based on consecutive error count."""
    idx = min(consecutive_errors - 1, len(BACKOFF_SCHEDULE_MS) - 1)
    return BACKOFF_SCHEDULE_MS[max(0, idx)]


def _is_transient_error(error: str | None) -> bool:
    """Check if an error is transient (retryable)."""
    if not error:
        return False
    return any(
        re.search(pattern, error)
        for pattern in TRANSIENT_PATTERNS.values()
    )
