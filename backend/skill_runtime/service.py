"""Manifest-aware admission: the single doorway API and the agent tool share.

Resolves the skill's manifest, checks the per-user enable switch, validates
the operation and input, then derives queue / timeouts / deadline / handler
version from the manifest — the model can never override the runtime (§8.1).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.log import create_logger
from db.base import get_db_session
from db.models.user_skill_setting import UserSkillSetting
from skill_runtime import repository as repo
from skill_runtime.manifest import ManifestError, SkillManifest, get_manifest, validate_input

log = create_logger("skill_runtime.service")


class SkillDisabled(Exception):
    pass


class UnknownSkill(Exception):
    pass


class UnknownOperation(Exception):
    pass


async def is_skill_enabled(user_id: str, manifest: SkillManifest) -> bool:
    async with get_db_session() as db:
        setting = (
            await db.execute(
                select(UserSkillSetting).where(
                    UserSkillSetting.user_id == user_id,
                    UserSkillSetting.skill_key == manifest.skill_key,
                )
            )
        ).scalar_one_or_none()
    if setting is None:
        return manifest.default_enabled
    return bool(setting.enabled)


async def set_skill_enabled(user_id: str, skill_key: str, enabled: bool, settings_data: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        setting = (
            await db.execute(
                select(UserSkillSetting).where(
                    UserSkillSetting.user_id == user_id,
                    UserSkillSetting.skill_key == skill_key,
                )
            )
        ).scalar_one_or_none()
        if setting is None:
            db.add(
                UserSkillSetting(
                    user_id=user_id,
                    skill_key=skill_key,
                    enabled=enabled,
                    settings_data=settings_data or {},
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            setting.enabled = enabled
            if settings_data is not None:
                setting.settings_data = settings_data
            setting.updated_at = now


async def start_job(
    *,
    user_id: str,
    skill_key: str,
    operation: str,
    input_data: dict,
    idempotency_key: str,
    session_id: str | None = None,
    project_id: str | None = None,
):
    """Admit a job under the skill's manifest. Returns (job, created)."""
    from core.config import get_config

    if not get_config().skill_jobs_enabled:
        raise SkillDisabled("skill jobs are disabled on this deployment")

    manifest = get_manifest(skill_key)
    if manifest is None:
        raise UnknownSkill(skill_key)
    if not await is_skill_enabled(user_id, manifest):
        raise SkillDisabled(f"skill {skill_key} is disabled for this user")
    op = manifest.operation(operation)
    if op is None:
        raise UnknownOperation(f"{skill_key} has no operation {operation!r}")
    if op.enabledConfigFlag and not bool(getattr(get_config(), op.enabledConfigFlag, False)):
        raise SkillDisabled(
            f"operation {operation} is not enabled on this deployment ({op.enabledConfigFlag})"
        )
    validate_input(op, input_data)

    now = datetime.now(timezone.utc)
    return await repo.admit_job(
        user_id=user_id,
        skill_key=skill_key,
        operation=operation,
        idempotency_key=idempotency_key,
        input_data=input_data,
        runtime_kind=manifest.runtime.kind,
        queue_name=op.queue,
        session_id=session_id,
        project_id=project_id,
        skill_version=manifest.version,
        max_attempts=op.maxAttempts,
        deadline_at=now + timedelta(seconds=op.maxTotalSeconds),
        handler_version=manifest.runtime.handlerVersion,
    )


def job_snapshot(job) -> dict:
    """The authoritative card payload shared by the API, tool and WS."""
    manifest = get_manifest(job.skill_key)
    phase_label = (manifest.phases.get(job.phase) if manifest else None) or None
    return {
        "jobId": job.id,
        "skillKey": job.skill_key,
        "operation": job.operation,
        "status": job.status,
        "phase": job.phase or None,
        "phaseLabelKey": phase_label,
        "desiredState": job.desired_state,
        "progress": job.progress_data or {},
        "result": job.result_data or {},
        "errorCode": job.error_code,
        "errorMessage": job.error_message,
        "attempt": job.attempt_count,
        "maxAttempts": job.max_attempts,
        "sessionId": job.session_id,
        "queue": job.queue_name,
        "lastEventSeq": job.last_event_seq,
        "nextRunAt": job.next_run_at.isoformat() if job.next_run_at else None,
        "deadlineAt": job.deadline_at.isoformat() if job.deadline_at else None,
        "createdAt": job.created_at.isoformat() if job.created_at else None,
        "updatedAt": job.updated_at.isoformat() if job.updated_at else None,
        "completedAt": job.completed_at.isoformat() if job.completed_at else None,
    }
