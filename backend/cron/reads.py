"""Shared owner scope for cron metadata; a workspace is not ownership."""
from sqlalchemy import exists, or_, select

from db.models.cron import CronJob
from db.models.session import Session
from session.policy import active_membership


def owned_jobs_query(user_id, workspace_id=None):
    query = select(CronJob).where(CronJob.user_id == user_id, CronJob.is_deleted.is_(False))
    if workspace_id is not None:
        query = query.where(CronJob.workspace_id == workspace_id,
                            active_membership(user_id, workspace_id))
    return query


def legacy_warmup_scope():
    # An old legacy job attached to a now-private lineage is refused by the
    # executor too; it must not allocate a desktop before that refusal.
    return (CronJob.assistant_session_id.is_(None), ~exists(select(Session.id).where(
        Session.id == CronJob.session_id,
        or_(Session.kind == "assistant", Session.memory_policy == "assistant_isolated"))))


async def currently_readable(db, job):
    if job is None:
        return False
    if job.assistant_session_id is None:
        return True
    from assistant.commands import _authority
    from assistant.policy import AssistantError
    from assistant.schedule_commands import job_locked, validate_configuration
    try:
        main = await _authority(db, user_id=job.user_id, workspace_id=job.workspace_id,
                                main_id=job.assistant_session_id)
        await job_locked(db, main, job.id)
        await validate_configuration(db, main, job)
        return True
    except AssistantError:
        return False
