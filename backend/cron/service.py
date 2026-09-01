"""CronService — the main facade for cron job management.

Provides CRUD operations + scheduler start/stop.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from core.log import create_logger
from core.identifier import ascending
from cron.timer import TimerState, arm_timer, stop_timer
from cron.types import CronJobCreate, CronJobUpdate, CronJobStatus

log = create_logger("cron.service")


def _utc_iso(value: datetime | None) -> str | None:
    """Serialize database timestamps as unambiguous UTC ISO-8601.

    PostgreSQL commonly returns ``timestamp without time zone`` columns as
    naive ``datetime`` objects even though OpenBox stores UTC in them.  A
    bare ``isoformat()`` makes browsers interpret those values as local time,
    shifting every Cron timestamp by the viewer's UTC offset.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class CronService:
    """Session-level cron job scheduler."""

    def __init__(self):
        self._state = TimerState()
        from cron.outbox import OutboxWorker

        self._outbox_worker = OutboxWorker()
        self._started = False
        self._lifecycle_lock = asyncio.Lock()

    async def start(self) -> None:
        """Start the cron scheduler. Call during app lifespan startup."""
        async with self._lifecycle_lock:
            if self._started:
                return
            log.info("Cron scheduler starting...")

            try:
                # Desktop SQLite does not run Alembic and create_all cannot
                # retrofit existing tables. PostgreSQL remains exclusively
                # migration-driven.
                from cron.schema import ensure_desktop_cron_lease_schema
                await ensure_desktop_cron_lease_schema()

                # Recovery: clean stuck markers + replay missed jobs.
                from cron.recovery import recover_on_startup
                await recover_on_startup()

                # Recompute next_run_at for all enabled jobs.
                await self._recompute_all()

                from cron.outbox import (
                    materialize_legacy_pending_session_deliveries,
                )
                await materialize_legacy_pending_session_deliveries()

                # Pending deliveries from a previous process are claimable as
                # soon as their database lease expires (or immediately when
                # they were never claimed).
                await self._outbox_worker.start()

                # Dispatch the first timer arm before publishing readiness. If
                # this call itself fails, no observer may see a started service.
                arm_timer(self._state)
            except BaseException:
                # A partially armed callback must not survive a failed startup,
                # and the same service instance must remain retryable.
                try:
                    stop_timer(self._state)
                except BaseException as cleanup_error:
                    log.warning(
                        "Cron startup cleanup failed error_type=%s",
                        type(cleanup_error).__name__,
                    )
                try:
                    await self._outbox_worker.stop()
                except BaseException as cleanup_error:
                    log.warning(
                        "Cron outbox startup cleanup failed error_type=%s",
                        type(cleanup_error).__name__,
                    )
                self._state.last_tick_at_ms = None
                self._started = False
                raise

            # Health baseline: a successfully armed fresh scheduler is healthy
            # during the minute before its first actual tick.
            self._state.last_tick_at_ms = int(
                datetime.now(timezone.utc).timestamp() * 1000
            )
            self._started = True
            log.info("Cron scheduler started")

    async def stop(self) -> None:
        """Stop the cron scheduler. Call during app lifespan shutdown."""
        async with self._lifecycle_lock:
            if not self._started:
                return
            try:
                stop_timer(self._state)
            finally:
                try:
                    await self._outbox_worker.stop()
                finally:
                    self._started = False
                    self._state.last_tick_at_ms = None
            log.info("Cron scheduler stopped")

    def set_executor(self, executor) -> None:
        """Inject the job executor callback (set by executor.py)."""
        self._state.execute_job = executor

    def set_result_handler(self, handler) -> None:
        """Inject the result handler callback."""
        self._state.on_job_result = handler

    # ── CRUD ──

    async def add(self, user_id: str, create: CronJobCreate) -> dict:
        """Create a new cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from cron.schedule import apply_stagger, compute_next_run_at
        from cron.validation import validate_create

        await validate_create(user_id, create)

        now = datetime.now(timezone.utc)
        job_id = ascending("cron")

        # Determine delete_after_run default
        delete_after_run = create.delete_after_run
        if delete_after_run is None:
            delete_after_run = create.schedule.kind == "at"

        # Compute initial next_run_at
        schedule_dict = create.schedule.model_dump()
        schedule_obj = create.schedule

        # For "every" schedule, set anchor to now if not provided
        if schedule_obj.kind == "every" and not schedule_obj.anchor_ms:
            schedule_dict["anchor_ms"] = int(now.timestamp() * 1000)

        next_run = compute_next_run_at(schedule_obj, now) if create.enabled else None
        next_run = apply_stagger(next_run, schedule_obj, job_id)

        delivery_dict = create.delivery.model_dump() if create.delivery else {}

        async with get_db_session() as db:
            row = CronJob(
                id=job_id,
                user_id=user_id,
                project_id=create.project_id,
                session_id=create.session_id,
                name=create.name,
                description=create.description,
                enabled=create.enabled,
                schedule=schedule_dict,
                task_prompt=create.task_prompt,
                agent=create.agent,
                model=create.model,
                timeout_seconds=create.timeout_seconds,
                delivery=delivery_dict,
                delete_after_run=delete_after_run,
                max_retries=create.max_retries,
                next_run_at=next_run,
                created_at=now,
                updated_at=now,
            )
            db.add(row)

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

        return {"id": job_id, "next_run_at": _utc_iso(next_run)}

    async def update(self, job_id: str, user_id: str, patch: CronJobUpdate) -> dict:
        """Update an existing cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select, update
        from cron.validation import validate_update

        await validate_update(user_id, job_id, patch)

        now = datetime.now(timezone.utc)

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,
                )
            )
            job = result.scalar_one_or_none()
            if not job:
                raise ValueError(f"Cron job {job_id} not found")

            values: dict = {"updated_at": now}

            if patch.name is not None:
                values["name"] = patch.name
            if patch.description is not None:
                values["description"] = patch.description
            if patch.task_prompt is not None:
                values["task_prompt"] = patch.task_prompt
            if patch.agent is not None:
                values["agent"] = patch.agent
            if patch.model is not None:
                values["model"] = patch.model
            if patch.timeout_seconds is not None:
                values["timeout_seconds"] = patch.timeout_seconds
            if patch.delivery is not None:
                values["delivery"] = patch.delivery.model_dump()
            if patch.enabled is not None:
                values["enabled"] = patch.enabled

            # If schedule changed, recompute next_run_at
            if patch.schedule is not None:
                values["schedule"] = patch.schedule.model_dump()
                if patch.enabled is not False and (patch.enabled or job.enabled):
                    from cron.schedule import apply_stagger, compute_next_run_at
                    values["next_run_at"] = apply_stagger(
                        compute_next_run_at(patch.schedule, now), patch.schedule, job_id
                    )
                else:
                    values["next_run_at"] = None

            # If enabled changed, recompute
            if patch.enabled is not None and patch.schedule is None:
                if patch.enabled:
                    from cron.schedule import (
                        apply_stagger,
                        compute_next_run_at,
                        schedule_from_dict,
                    )
                    sobj = schedule_from_dict(job.schedule)
                    if sobj:
                        values["next_run_at"] = apply_stagger(
                            compute_next_run_at(sobj, now), sobj, job_id
                        )
                else:
                    values["next_run_at"] = None

            await db.execute(
                update(CronJob).where(CronJob.id == job_id).values(**values)
            )

        arm_timer(self._state)

        from bus import bus
        from bus.events import CRON_JOB_UPDATED
        bus.publish(CRON_JOB_UPDATED, {
            "userId": user_id,
            "jobId": job_id,
        })

        return {"ok": True}

    async def remove(self, job_id: str, user_id: str) -> dict:
        """Atomically cancel an exact active run and soft-delete its job."""
        from cron.lease import _database_now
        from db.base import get_db_session
        from db.models.cron import CronJob, CronRun
        from sqlalchemy import select, update

        async with get_db_session() as db:
            # Settlement, heartbeat, and deletion all serialize on this row in
            # PostgreSQL. Capturing the claim only after the lock ensures the
            # audit row below belongs to the generation being revoked.
            result = await db.execute(
                select(CronJob)
                .where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,  # noqa: E712
                )
                .with_for_update()
            )
            job = result.scalar_one_or_none()
            if job is None:
                raise ValueError(f"Cron job {job_id} not found")

            database_now = _database_now(db)
            run_ownership = None
            if job.run_token is not None:
                run_ownership = [
                    CronRun.job_id == job.id,
                    CronRun.status == CronJobStatus.RUNNING.value,
                    CronRun.claim_token == job.run_token,
                    CronRun.claim_generation == job.run_generation,
                    CronRun.claim_owner == job.run_owner,
                ]
            elif job.running_at is not None:
                # Legacy pre-lease rows have no stronger receipt. Restrict the
                # cancellation to the legacy identity and never touch a newer
                # fenced generation.
                run_ownership = [
                    CronRun.job_id == job.id,
                    CronRun.status == CronJobStatus.RUNNING.value,
                    CronRun.claim_token.is_(None),
                    CronRun.claim_generation.is_(None),
                    CronRun.claim_owner.is_(None),
                ]

            if run_ownership is not None:
                await db.execute(
                    update(CronRun)
                    .where(*run_ownership)
                    .values(
                        status=CronJobStatus.CANCELED.value,
                        error_message=(
                            "Execution canceled because the Cron job was deleted"
                        ),
                        ended_at=database_now,
                    )
                )

            # No delivery outbox is created for administrative cancellation.
            # Clearing the exact job claim in this same transaction fences the
            # worker's later settlement and heartbeat before either can commit.
            deleted = await db.execute(
                update(CronJob)
                .where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,  # noqa: E712
                )
                .values(
                    is_deleted=True,
                    enabled=False,
                    running_at=None,
                    run_token=None,
                    run_owner=None,
                    lease_expires_at=None,
                    heartbeat_at=None,
                    updated_at=database_now,
                )
            )
            if deleted.rowcount != 1:
                raise ValueError(f"Cron job {job_id} not found")

        arm_timer(self._state)
        return {"ok": True}

    async def run(self, job_id: str, user_id: str) -> dict:
        """Manually trigger a cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from cron.lease import claim_job, claimed_job_payload, is_live_claim
        from sqlalchemy import select

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,
                )
            )
            job = result.scalar_one_or_none()
            if not job:
                raise ValueError(f"Cron job {job_id} not found")
            live_before_claim = is_live_claim(job, datetime.now(timezone.utc))

        if not self._state.execute_job:
            # Preserve the old response precedence when dispatch is disabled;
            # actual executions always proceed to the atomic claim below.
            if live_before_claim:
                return {"ok": False, "reason": "already-running"}
            return {"ok": False, "reason": "no-executor"}

        # Manual and timer paths deliberately share this one conditional
        # UPDATE.  Disabled jobs remain manually runnable, as before.
        claim = await claim_job(
            job_id,
            user_id=user_id,
            require_enabled=False,
        )
        if claim is None:
            return {"ok": False, "reason": "already-running"}

        # Re-read after claim so an update racing the initial ownership check
        # cannot launch with an old project, prompt, model, or schedule.
        job_dict = await claimed_job_payload(claim)
        if job_dict is None:
            log.warning("Manual Cron claim vanished before dispatch job=%s", job_id)
            return {"ok": False, "reason": "already-running"}
        asyncio.create_task(self._run_manual(job_dict))
        return {"ok": True, "status": "triggered"}

    async def _run_manual(self, job_dict: dict) -> None:
        """Execute a manual job run (background task)."""
        import time as _time
        from cron.lease import CronLease, CronLeaseLost, run_with_heartbeat
        from cron.timer import _apply_job_result

        job_id = job_dict["id"]
        claim = CronLease.from_payload(job_dict.get("_cron_claim"))
        if claim is None:
            log.error("Manual Cron run %s has no valid lease claim", job_id)
            return

        start = _time.time()
        lease_lost = False
        try:
            result = await run_with_heartbeat(
                claim,
                lambda: self._state.execute_job(job_dict),
                timeout=job_dict.get("timeout_seconds", 1800),
            )
        except asyncio.TimeoutError:
            result = {"status": "error", "error": "Job execution timed out"}
        except CronLeaseLost as exc:
            lease_lost = True
            result = {"status": "error", "error": str(exc)}
        except Exception as e:
            result = {"status": "error", "error": str(e)}

        result["duration_ms"] = int((_time.time() - start) * 1000)
        result.setdefault("run_id", job_dict.get("_cron_run_id"))
        result.setdefault(
            "temp_session_id", job_dict.get("_cron_temp_session_id")
        )
        result.setdefault("started_at", job_dict.get("_cron_started_at"))
        result.setdefault("ended_at", datetime.now(timezone.utc))
        result.setdefault("locale", job_dict.get("_cron_locale") or "zh-CN")
        result.setdefault(
            "context_summary", job_dict.get("_cron_context_summary")
        )
        result.setdefault("tokens", job_dict.get("_cron_tokens") or {})
        result.setdefault("silent", False)

        if lease_lost:
            log.warning(
                "Discarded stale manual Cron result job=%s generation=%s",
                job_id,
                claim.generation,
            )
            return

        async with self._state.lock:
            applied = await _apply_job_result(
                self._state,
                job_id,
                result,
                claim=claim,
            )
        if not applied:
            log.warning(
                "Discarded fenced manual Cron result job=%s generation=%s",
                job_id,
                claim.generation,
            )

    async def pause_all(self, user_id: str, session_id: str | None = None) -> int:
        """Disable all of a user's jobs (optionally one session's). Returns count."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import update

        query = (
            update(CronJob)
            .where(
                CronJob.user_id == user_id,
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

    async def resume_all(self, user_id: str, session_id: str | None = None) -> int:
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
    ) -> list[dict]:
        """List cron jobs, optionally narrowed to one project or notify session."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select

        async with get_db_session() as db:
            query = select(CronJob).where(
                CronJob.user_id == user_id,
                CronJob.is_deleted == False,
            )
            if session_id:
                query = query.where(CronJob.session_id == session_id)
            if project_id:
                query = query.where(CronJob.project_id == project_id)
            query = query.order_by(CronJob.created_at.desc())

            result = await db.execute(query)
            rows = result.scalars().all()

        jobs = [_job_to_dict(row) for row in rows]

        # Which tenant/project directory each job runs in — the part you cannot
        # infer from the prompt.
        from project.workspace import workdir_for_identity
        for job in jobs:
            try:
                job["project_directory"] = await workdir_for_identity(
                    job["user_id"],
                    job["project_id"],
                )
            except Exception:
                job["project_directory"] = None
        return jobs

    async def get_job(self, job_id: str, user_id: str) -> dict | None:
        """Get a single cron job."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select

        async with get_db_session() as db:
            result = await db.execute(
                select(CronJob).where(
                    CronJob.id == job_id,
                    CronJob.user_id == user_id,
                    CronJob.is_deleted == False,
                )
            )
            job = result.scalar_one_or_none()

        return _job_to_dict(job) if job else None

    async def list_runs(self, job_id: str, user_id: str, limit: int = 20) -> list[dict]:
        """Get execution history for a cron job."""
        from db.base import get_db_session
        from db.models.cron import CronRun
        from sqlalchemy import select

        async with get_db_session() as db:
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

    async def status(self) -> dict:
        """Get scheduler status, including liveness for external monitoring."""
        from db.base import get_db_session
        from db.models.cron import CronJob
        from sqlalchemy import select, func

        async with get_db_session() as db:
            total = await db.execute(
                select(func.count()).select_from(CronJob).where(CronJob.is_deleted == False)
            )
            enabled = await db.execute(
                select(func.count()).select_from(CronJob).where(
                    CronJob.is_deleted == False, CronJob.enabled == True
                )
            )
            running = await db.execute(
                select(func.count()).select_from(CronJob).where(
                    CronJob.running_at.isnot(None)
                )
            )
            next_wake = await db.execute(
                select(func.min(CronJob.next_run_at)).where(
                    CronJob.is_deleted == False,
                    CronJob.enabled == True,
                    CronJob.next_run_at.isnot(None),
                )
            )

        readiness = self.readiness_status()

        next_wake_at = next_wake.scalar()
        return {
            "running": readiness["started"],
            "healthy": readiness["ready"],
            "last_tick_at": readiness["last_tick_at"],
            "next_run_at": _utc_iso(next_wake_at),
            "total_jobs": total.scalar() or 0,
            "enabled_jobs": enabled.scalar() or 0,
            "running_jobs": running.scalar() or 0,
        }

    def readiness_status(self) -> dict:
        """Return the process-local scheduler gate without touching the DB."""
        from cron.types import MAX_TIMER_DELAY_MS

        last_tick_ms = self._state.last_tick_at_ms
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        heartbeat_fresh = bool(
            last_tick_ms is not None
            and now_ms - last_tick_ms < 3 * MAX_TIMER_DELAY_MS
        )
        outbox = self._outbox_worker.readiness()
        return {
            "ready": self._started and heartbeat_fresh and outbox["ready"],
            "started": self._started,
            "heartbeat_fresh": heartbeat_fresh,
            "outbox_running": outbox["running"],
            "outbox_heartbeat_fresh": outbox["heartbeat_fresh"],
            "outbox_dispatch_healthy": outbox.get(
                "dispatch_healthy", outbox["ready"]
            ),
            "last_tick_at": _utc_iso(
                datetime.fromtimestamp(last_tick_ms / 1000, tz=timezone.utc)
                if last_tick_ms is not None
                else None
            ),
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
        "user_id": job.user_id,
        "project_id": job.project_id,
        "session_id": job.session_id,
        "name": job.name,
        "description": job.description,
        "enabled": job.enabled,
        "schedule": job.schedule,
        "task_prompt": job.task_prompt,
        "agent": job.agent,
        "model": job.model,
        "timeout_seconds": job.timeout_seconds,
        "delivery": job.delivery,
        "delete_after_run": job.delete_after_run,
        "next_run_at": _utc_iso(job.next_run_at),
        "last_run_at": _utc_iso(job.last_run_at),
        "last_status": job.last_status,
        "last_error": job.last_error,
        "last_duration_ms": job.last_duration_ms,
        "consecutive_errors": job.consecutive_errors,
        "total_runs": job.total_runs,
        "total_successes": job.total_successes,
        "total_failures": job.total_failures,
        "running": job.running_at is not None,
        "created_at": _utc_iso(job.created_at),
        "updated_at": _utc_iso(job.updated_at),
    }


def _run_to_dict(run) -> dict:
    """Convert CronRun ORM to dict."""
    return {
        "id": run.id,
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
        "started_at": _utc_iso(run.started_at),
        "ended_at": _utc_iso(run.ended_at),
    }


# Singleton
cron_service = CronService()
