"""Shared owner scope for cron metadata; a workspace is not ownership."""
from sqlalchemy import select

from db.models.cron import CronJob
from session.policy import active_membership


def owned_jobs_query(user_id, workspace_id=None):
    query = select(CronJob).where(CronJob.user_id == user_id, CronJob.is_deleted.is_(False))
    if workspace_id is not None:
        query = query.where(CronJob.workspace_id == workspace_id,
                            active_membership(user_id, workspace_id))
    return query
