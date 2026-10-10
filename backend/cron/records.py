"""Transaction-local cron writes shared by REST and private assistant commands."""
from datetime import timedelta

from sqlalchemy import func, or_, select, text

from core.identifier import ascending
from cron.schedule import apply_stagger, compute_next_run_at, schedule_from_dict
from db.models.cron import CronJob


async def lock_run_admission(db):
    """Serialize shared cron capacity checks before locking a job.

    SQLite callers already hold BEGIN IMMEDIATE. The PostgreSQL lock spans
    replicas and is separate from Driver admission, which happens after commit.
    """
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x43524F4E434150})


async def has_run_capacity_locked(db, user_id, now):
    from core.config import get_config
    from cron.timer import STUCK_RUN_MS

    config = get_config()
    active = select(func.count(), func.count().filter(CronJob.user_id == user_id)).where(CronJob.is_deleted.is_(False),
        CronJob.running_at.isnot(None), or_(CronJob.assistant_session_id.isnot(None),
            CronJob.running_at >= now - timedelta(milliseconds=STUCK_RUN_MS)))
    total, owned = (await db.execute(active)).one()
    return (total < max(1, config.cron_max_concurrent_jobs)
            and owned < max(1, config.cron_max_concurrent_per_user))


async def check_quota_locked(db, user_id, project_id):
    from core.config import get_config
    from db.models.project import Project
    # Caller serializes the owner first. The project lock also protects the
    # project-wide cap when two different members schedule in the same project.
    await db.scalar(select(Project.id).where(Project.id == project_id).with_for_update())
    config = get_config()
    for field, value, cap in ((CronJob.user_id, user_id, config.cron_max_jobs_per_user),
                              (CronJob.project_id, project_id, config.cron_max_jobs_per_project)):
        count = await db.scalar(select(func.count()).select_from(CronJob).where(
            field == value, CronJob.is_deleted.is_(False)))
        if count >= cap:
            raise ValueError("Cron job quota exceeded")


def new_job(user_id, workspace_id, create, now):
    job_id = ascending("cron")
    schedule = create.schedule.model_dump()
    if create.schedule.kind == "every" and not create.schedule.anchor_ms:
        schedule["anchor_ms"] = int(now.timestamp() * 1000)
    normalized = schedule_from_dict(schedule)
    next_run = apply_stagger(compute_next_run_at(normalized, now), normalized, job_id) if create.enabled else None
    return CronJob(id=job_id, user_id=user_id, workspace_id=workspace_id,
        project_id=create.project_id, session_id=create.session_id, name=create.name,
        description=create.description, enabled=create.enabled, schedule=schedule,
        task_prompt=create.task_prompt, agent=create.agent, model=create.model,
        timeout_seconds=create.timeout_seconds, delivery=create.delivery.model_dump() if create.delivery else {},
        template=create.template or None,
        delete_after_run=create.delete_after_run if create.delete_after_run is not None else create.schedule.kind == "at",
        max_retries=create.max_retries, next_run_at=next_run, revision=1, created_at=now, updated_at=now)


def update_job(job, patch, now):
    values = {"updated_at": now, "revision": job.revision + 1}
    for key in ("name", "description", "task_prompt", "agent", "model", "timeout_seconds", "enabled"):
        if getattr(patch, key) is not None:
            values[key] = getattr(patch, key)
    if patch.delivery is not None:
        values["delivery"] = patch.delivery.model_dump()
    if patch.template is not None:
        values["template"] = patch.template or None
    if patch.schedule is not None:
        values["schedule"] = patch.schedule.model_dump()
        if patch.schedule.kind == "every" and not patch.schedule.anchor_ms:
            values["schedule"]["anchor_ms"] = int(now.timestamp() * 1000)
    if patch.schedule is not None or patch.enabled is not None:
        schedule = schedule_from_dict(values.get("schedule", job.schedule))
        values["next_run_at"] = (apply_stagger(compute_next_run_at(schedule, now), schedule, job.id)
            if schedule and values.get("enabled", job.enabled) else None)
    for key, value in values.items():
        setattr(job, key, value)


def require_legacy_job(job):
    if job.assistant_session_id is not None:
        raise ValueError("ASSISTANT_SCHEDULE_COMMAND_REQUIRED: use the private assistant schedule command with its current revision")
