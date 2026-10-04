"""CronService — the main facade for cron job management.

Provides CRUD operations + scheduler start/stop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from core.log import create_logger
from cron.timer import TimerState, arm_timer, stop_timer
from cron.types import CronJobCreate, CronJobUpdate

log = create_logger("cron.service")


class CronService:
    """Session-level cron job scheduler."""

    def __init__(self):
        self._state = TimerState()
        self._started = False

    async def start(self) -> None:
        """Start the cron scheduler. Call during app lifespan startup."""
        if self._started:
            return
        self._started = True
        log.info("Cron scheduler starting...")

        # Health baseline: "alive as of start", so a fresh scheduler is not
        # reported unhealthy during the minute before its first tick.
        self._state.last_tick_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

        # Recovery: clean stuck markers + replay missed jobs
        from cron.recovery import recover_on_startup
        await recover_on_startup()

        # Recompute next_run_at for all enabled jobs
        await self._recompute_all()

        # Arm the timer
        arm_timer(self._state)
        log.info("Cron scheduler started")

    async def stop(self) -> None:
        """Stop the cron scheduler. Call during app lifespan shutdown."""
        if not self._started:
            return
        stop_timer(self._state)
        self._started = False
        log.info("Cron scheduler stopped")

    def set_executor(self, executor) -> None:
        """Inject the job executor callback (set by executor.py)."""
        self._state.execute_job = executor

    def set_result_handler(self, handler) -> None:
        """Inject the result handler callback."""
        self._state.on_job_result = handler

    # ── CRUD ──

    async def add(
        self, user_id: str, create: CronJobCreate, workspace_id: str | None = None
    ) -> dict:
        """Create a new cron job."""
        from db.base import get_db_session
        from cron.validation import validate_create

        await validate_create(user_id, create)

        if not workspace_id:
            from db.models.user import User
            from sqlalchemy import select
            async with get_db_session() as db:
                workspace_id = (
                    await db.execute(
                        select(User.default_workspace_id).where(User.id == user_id)
                    )
                ).scalar_one_or_none() or "ws_default"

        now = datetime.now(timezone.utc)
        from cron.records import check_quota_locked, new_job
        from db.models.user import User
        from sqlalchemy import select
        from session.internal_parts import begin_session_write
        async with get_db_session() as db:
            await begin_session_write(db)
            await db.scalar(select(User.id).where(User.id == user_id).with_for_update(key_share=True))
            await check_quota_locked(db, user_id, create.project_id)
            row = new_job(user_id, workspace_id, create, now)
            db.add(row)
            job_id, next_run = row.id, row.next_run_at

        # Re-arm timer
        arm_timer(self._state)

        log.info(f"Created cron job {job_id} ({create.name}) for project {create.project_id}")

        # Publish event
        from bus import bus
        from bus.events import CRON_JOB_CREATED
        bus.publish(CRON_JOB_CREATED, {
            "userId": user_id,
            "jobId": job_id,
            "projectId": create.project_id,
            "sessionId": create.session_id,
            "name": create.name,
        })

        return {"id": job_id, "next_run_at": next_run.isoformat() if next_run else None}

    async def update(
        self, job_id: str, user_id: str, patch: CronJobUpdate,
        workspace_id: str | None = None
    ) -> dict:
        """Update an existing cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select
        from cron.validation import validate_update

        await validate_update(user_id, job_id, patch)

        now = datetime.now(timezone.utc)

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                    CronJob.is_deleted == False,
                ).with_for_update()
            )
            job = result.scalar_one_or_none()
            if not job:
                raise ValueError(f"Cron job {job_id} not found")

            from cron.records import require_legacy_job, update_job
            require_legacy_job(job)
            update_job(job, patch, now)

        arm_timer(self._state)

        from bus import bus
        from bus.events import CRON_JOB_UPDATED
        bus.publish(CRON_JOB_UPDATED, {
            "userId": user_id,
            "jobId": job_id,
        })

        return {"ok": True}

    async def remove(
        self, job_id: str, user_id: str, workspace_id: str | None = None
    ) -> dict:
        """Soft-delete a cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import update

        now = datetime.now(timezone.utc)

        async with get_db_session() as db:
            result = await db.execute(
                update(CronJob)
                .where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.assistant_session_id.is_(None),
                    *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                    CronJob.is_deleted == False,
                )
                .values(is_deleted=True, enabled=False, updated_at=now)
            )
            if result.rowcount == 0:
                raise ValueError(f"Cron job {job_id} not found")

        arm_timer(self._state)
        return {"ok": True}

    async def run(
        self, job_id: str, user_id: str, workspace_id: str | None = None
    ) -> dict:
        """Manually trigger a cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.assistant_session_id.is_(None),
                    *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                    CronJob.is_deleted == False,
                )
            )
            job = result.scalar_one_or_none()
            if not job:
                raise ValueError(f"Cron job {job_id} not found")

            if job.running_at is not None:
                return {"ok": False, "reason": "already-running"}

        # Build job dict and execute
        job_dict = {
            "id": job.id,
            "user_id": job.user_id,
            "workspace_id": job.workspace_id,
            "project_id": job.project_id,
            "session_id": job.session_id,
            "name": job.name,
            "schedule": job.schedule,
            "task_prompt": job.task_prompt,
            "agent": job.agent,
            "model": job.model,
            "timeout_seconds": job.timeout_seconds,
            "delivery": job.delivery,
            "template": job.template,
            "delete_after_run": job.delete_after_run,
            "max_retries": job.max_retries,
            "consecutive_errors": job.consecutive_errors,
            "summary_cache": job.summary_cache,
            "summary_cache_msg_id": job.summary_cache_msg_id,
        }

        if self._state.execute_job:
            asyncio.create_task(self._run_manual(job_dict))
            return {"ok": True, "status": "triggered"}
        else:
            return {"ok": False, "reason": "no-executor"}

    async def _run_manual(self, job_dict: dict) -> None:
        """Execute a manual job run (background task)."""
        import time as _time
        from cron.timer import _apply_job_result

        job_id = job_dict["id"]

        from cron.timer import _claim_job
        if not await _claim_job(job_id, manual=True):
            return

        start = _time.time()
        try:
            result = await asyncio.wait_for(
                self._state.execute_job(job_dict),
                timeout=job_dict.get("timeout_seconds", 1800),
            )
        except asyncio.TimeoutError:
            result = {"status": "error", "error": "Job execution timed out"}
        except Exception as e:
            result = {"status": "error", "error": str(e)}

        result["duration_ms"] = int((_time.time() - start) * 1000)

        # Apply result (don't advance schedule for manual runs)
        async with self._state.lock:
            await _apply_job_result(self._state, job_id, result)

    async def pause_all(
        self, user_id: str, session_id: str | None = None,
        workspace_id: str | None = None
    ) -> int:
        """Disable all of a user's jobs (optionally one session's). Returns count."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import update

        query = (
            update(CronJob)
            .where(
                CronJob.user_id == user_id,
                CronJob.assistant_session_id.is_(None),
                *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                CronJob.is_deleted == False,  # noqa: E712
                CronJob.enabled == True,  # noqa: E712
            )
        )
        if session_id:
            query = query.where(CronJob.session_id == session_id)
        # next_run_at is cleared like single-job disable does — otherwise a
        # slot that lapses while paused fires immediately on resume.
        query = query.values(
            enabled=False, next_run_at=None, updated_at=datetime.now(timezone.utc)
        )

        async with get_db_session() as db:
            result = await db.execute(query)

        arm_timer(self._state)
        return result.rowcount

    async def resume_all(
        self, user_id: str, session_id: str | None = None,
        workspace_id: str | None = None
    ) -> int:
        """Re-enable all of a user's jobs and recompute their next fire times."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select, update
        from cron.schedule import apply_stagger, compute_next_run_at, schedule_from_dict

        now = datetime.now(timezone.utc)

        query = (
            update(CronJob)
            .where(
                CronJob.user_id == user_id,
                CronJob.assistant_session_id.is_(None),
                *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                CronJob.is_deleted == False,  # noqa: E712
                CronJob.enabled == False,  # noqa: E712
            )
        )
        if session_id:
            query = query.where(CronJob.session_id == session_id)
        query = query.values(enabled=True, updated_at=now)

        async with get_db_session() as db:
            result = await db.execute(query)

        async with get_db_session() as db:
            q = select(CronJob).where(
                CronJob.user_id == user_id,
                CronJob.assistant_session_id.is_(None),
                *([CronJob.workspace_id == workspace_id] if workspace_id else []),
                CronJob.is_deleted == False,  # noqa: E712
                CronJob.enabled == True,  # noqa: E712
                CronJob.next_run_at.is_(None),
            )
            if session_id:
                q = q.where(CronJob.session_id == session_id)
            rows = (await db.execute(q)).scalars().all()
            for job in rows:
                sobj = schedule_from_dict(job.schedule)
                if sobj:
                    next_run = apply_stagger(compute_next_run_at(sobj, now), sobj, job.id)
                    await db.execute(
                        update(CronJob)
                        .where(CronJob.id == job.id)
                        .values(next_run_at=next_run, updated_at=now)
                    )

        arm_timer(self._state)
        return result.rowcount

    async def list_jobs(
        self,
        user_id: str,
        session_id: str | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict]:
        """List cron jobs, optionally narrowed to one project or notify session."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from cron.reads import owned_jobs_query, currently_readable

        async with get_db_session() as db:
            query = owned_jobs_query(user_id, workspace_id)
            if session_id:
                query = query.where(CronJob.session_id == session_id)
            if project_id:
                query = query.where(CronJob.project_id == project_id)
            query = query.order_by(CronJob.created_at.desc())

            result = await db.execute(query)
            rows = result.scalars().all()
            rows = [row for row in rows if await currently_readable(db, row)]

        jobs = [_job_to_dict(row) for row in rows]

        # Which directory each job runs in — the part you cannot infer from
        # the prompt. Resolved through the cached slug lookup.
        from project.workspace import project_directory, slug_for
        for job in jobs:
            try:
                slug = await slug_for(job["project_id"])
                job["project_directory"] = project_directory(slug)
            except Exception:
                job["project_directory"] = None
        return jobs

    async def get_job(
        self, job_id: str, user_id: str, workspace_id: str | None = None
    ) -> dict | None:
        """Get a single cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from cron.reads import owned_jobs_query, currently_readable

        async with get_db_session() as db:
            result = await db.execute(
                owned_jobs_query(user_id, workspace_id).where(CronJob.id == job_id)
            )
            job = result.scalar_one_or_none()
            if not await currently_readable(db, job):
                return None

        return _job_to_dict(job) if job else None

    async def list_runs(
        self, job_id: str, user_id: str, limit: int = 20,
        workspace_id: str | None = None
    ) -> list[dict]:
        """Get execution history for a cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob, CronRun
        from cron.reads import owned_jobs_query, currently_readable
        from sqlalchemy import select

        async with get_db_session() as db:
            owned = (
                await db.execute(
                    owned_jobs_query(user_id, workspace_id)
                    .where(CronJob.id == job_id)
                )
            ).scalar_one_or_none()
            if not await currently_readable(db, owned):
                return []
            result = await db.execute(
                select(CronRun)
                .where(
                    CronRun.job_id == job_id,
                    CronRun.user_id == user_id,
                )
                .order_by(CronRun.started_at.desc())
                .limit(limit)
            )
            rows = result.scalars().all()

        return [_run_to_dict(row) for row in rows]

    async def status(self, user_id: str | None = None, workspace_id: str | None = None) -> dict:
        """Liveness is global; authenticated inventory statistics are owner scoped.

        Internal monitoring may omit the actor. HTTP callers must pass both
        actor and current workspace, just like the job listing.
        """
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select, func, case
        from cron.reads import owned_jobs_query
        from cron.types import MAX_TIMER_DELAY_MS

        async with get_db_session() as db:
            if user_id is None and workspace_id is not None:
                raise ValueError("Scoped cron status requires an actor")
            query = (owned_jobs_query(user_id, workspace_id) if user_id is not None else
                     select(CronJob).where(CronJob.is_deleted.is_(False)))
            metrics = (await db.execute(query.with_only_columns(
                func.count(CronJob.id).label("total"),
                func.sum(case((CronJob.enabled.is_(True), 1), else_=0)).label("enabled"),
                func.sum(case((CronJob.running_at.isnot(None), 1), else_=0)).label("running"),
                func.min(case((CronJob.enabled.is_(True), CronJob.next_run_at))).label("next_wake"),
            ))).one()

        # The timer promises a tick at least every MAX_TIMER_DELAY; if several
        # windows pass without one, the scheduler is wedged — the exact failure
        # mode that goes unnoticed when only in-process watchdogs exist.
        last_tick_ms = self._state.last_tick_at_ms
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        healthy = self._started and (
            last_tick_ms is not None and now_ms - last_tick_ms < 3 * MAX_TIMER_DELAY_MS
        )

        next_wake_at = metrics.next_wake
        return {
            "running": self._started,
            "healthy": healthy,
            "last_tick_at": (
                datetime.fromtimestamp(last_tick_ms / 1000, tz=timezone.utc).isoformat()
                if last_tick_ms
                else None
            ),
            "next_run_at": next_wake_at.isoformat() if next_wake_at else None,
            "total_jobs": metrics.total or 0,
            "enabled_jobs": metrics.enabled or 0,
            "running_jobs": metrics.running or 0,
        }

    # ── Internal ──

    async def _recompute_all(self) -> None:
        """Recompute next_run_at for all enabled jobs."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select, update
        from cron.schedule import apply_stagger, compute_next_run_at, schedule_from_dict

        now = datetime.now(timezone.utc)

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.enabled == True,
                    CronJob.assistant_session_id.is_(None),
                    CronJob.is_deleted == False,
                )
            )
            jobs = result.scalars().all()

            from cron.schedule import as_aware_utc

            for job in jobs:
                sobj = schedule_from_dict(job.schedule)
                if sobj:
                    next_run = apply_stagger(compute_next_run_at(sobj, now), sobj, job.id)
                    # A job already due keeps its overdue next_run_at: recovery
                    # decided whether it replays, and recomputing here would
                    # silently skip the slot.
                    stored_next = as_aware_utc(job.next_run_at)
                    if stored_next and stored_next <= now:
                        continue
                    if next_run != job.next_run_at:
                        await db.execute(
                            update(CronJob)
                            .where(CronJob.id == job.id)
                            .values(next_run_at=next_run, updated_at=now)
                        )


def _job_to_dict(job) -> dict:
    """Convert CronJob ORM to dict."""
    return {
        "id": job.id,
        "management": "assistant" if job.assistant_session_id else "legacy",
        "revision": job.revision,
        "user_id": job.user_id,
        "project_id": job.project_id,
        "session_id": job.session_id,
        "name": job.name,
        "description": job.description,
        "enabled": job.enabled,
        "schedule": job.schedule,
        "task_prompt": "" if job.assistant_session_id else job.task_prompt,
        "agent": job.agent,
        "model": job.model,
        "timeout_seconds": None if job.assistant_session_id else job.timeout_seconds,
        "delivery": job.delivery,
        "template": job.template,
        "delete_after_run": job.delete_after_run,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None,
        "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
        "last_status": job.last_status,
        "last_error": job.last_error,
        "last_duration_ms": job.last_duration_ms,
        "consecutive_errors": job.consecutive_errors,
        "total_runs": job.total_runs,
        "total_successes": job.total_successes,
        "total_failures": job.total_failures,
        "running": job.running_at is not None,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


def _run_to_dict(run) -> dict:
    """Convert CronRun ORM to dict."""
    return {
        "id": run.id,
        "assistant_task_id": run.assistant_task_id,
        "assistant_submission_id": run.assistant_submission_id,
        "assistant_result_id": run.assistant_result_id,
        "job_id": run.job_id,
        "temp_session_id": run.temp_session_id,
        "status": run.status,
        "error_message": run.error_message,
        "task_prompt": run.task_prompt,
        "summary_text": run.summary_text,
        "injected": run.injected,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "total_tokens": run.total_tokens,
        "duration_ms": run.duration_ms,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
    }


# Singleton
cron_service = CronService()
