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


class InvalidScope(Exception):
    """The requested session/project is absent, deleted, or not user-owned."""


async def _validated_scope(
    user_id: str,
    session_id: str | None,
    project_id: str | None,
) -> tuple[str | None, str | None]:
    from db.models.project import Project
    from db.models.session import Session

    resolved_project = project_id
    async with get_db_session() as db:
        if session_id:
            session_project = (
                await db.execute(
                    select(Session.project_id).where(
                        Session.id == session_id,
                        Session.user_id == user_id,
                        Session.is_deleted == False,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if session_project is None:
                raise InvalidScope("session is not owned by the current user")
            if resolved_project and resolved_project != session_project:
                raise InvalidScope("session does not belong to the requested project")
            resolved_project = session_project
        if resolved_project:
            owned_project = (
                await db.execute(
                    select(Project.id).where(
                        Project.id == resolved_project,
                        Project.user_id == user_id,
                        Project.is_deleted == False,  # noqa: E712
                    )
                )
            ).scalar_one_or_none()
            if owned_project is None:
                raise InvalidScope("project is not owned by the current user")
    return session_id, resolved_project


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
    from sqlalchemy import update
    from sqlalchemy.exc import IntegrityError

    values: dict = {"enabled": enabled, "updated_at": now}
    if settings_data is not None:
        values["settings_data"] = settings_data
    try:
        async with get_db_session() as db:
            result = await db.execute(
                update(UserSkillSetting)
                .where(
                    UserSkillSetting.user_id == user_id,
                    UserSkillSetting.skill_key == skill_key,
                )
                .values(**values)
            )
            if result.rowcount == 0:
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
                await db.flush()
    except IntegrityError as integrity_error:
        # Concurrent first writes: the unique key chooses one insert, and the
        # loser applies its requested final value as an ordinary update.
        async with get_db_session() as db:
            result = await db.execute(
                update(UserSkillSetting)
                .where(
                    UserSkillSetting.user_id == user_id,
                    UserSkillSetting.skill_key == skill_key,
                )
                .values(**values)
            )
            if result.rowcount == 0:
                # Not a concurrent unique-key race (for example a broken FK):
                # do not convert a real integrity failure into a silent no-op.
                raise integrity_error


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
    if op.outputSchema is not None and not isinstance(op.outputSchema, dict):
        raise ManifestError(
            "referenced outputSchema is not resolved by this runtime; use an inline schema"
        )
    if not idempotency_key or len(idempotency_key) > 180:
        raise ManifestError("idempotency_key must contain 1 to 180 characters")
    session_id, project_id = await _validated_scope(user_id, session_id, project_id)

    now = datetime.now(timezone.utc)
    return await repo.admit_job(
        user_id=user_id,
        skill_key=skill_key,
        operation=operation,
        idempotency_key=idempotency_key,
        input_data=input_data,
        output_schema=op.outputSchema or {},
        runtime_kind=manifest.runtime.kind,
        queue_name=op.queue,
        session_id=session_id,
        project_id=project_id,
        skill_version=manifest.version,
        max_attempts=op.maxAttempts,
        deadline_at=now + timedelta(seconds=op.maxTotalSeconds),
        handler_version=manifest.runtime.handlerVersion,
        invocation_timeout_seconds=op.invocationTimeoutSeconds,
        max_external_wait_seconds=op.maxExternalWaitSeconds,
        user_input_timeout_seconds=op.userInputTimeoutSeconds,
        cancel_requires_handler=op.cancelRequiresHandler,
    )


def iso_utc(dt: datetime | None) -> str | None:
    """ISO-8601 with an explicit offset. SQLite hands back naive datetimes for
    tz-aware columns; serializing them bare makes every client parse them as
    local time (hours of drift on a non-UTC host)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def job_snapshot(job) -> dict:
    """The authoritative card payload shared by the API, tool and WS."""
    manifest = get_manifest(job.skill_key)
    phase_label = (manifest.phases.get(job.phase) if manifest else None) or None
    external_wait_seconds = int(job.external_wait_seconds or 0)
    if job.status == "waiting_external" and job.external_wait_started_at is not None:
        started = job.external_wait_started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        external_wait_seconds += max(
            0,
            int((datetime.now(timezone.utc) - started).total_seconds()),
        )
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
        "retryCount": job.retry_count,
        "maxAttempts": job.max_attempts,
        "externalWaitSeconds": external_wait_seconds,
        "maxExternalWaitSeconds": job.max_external_wait_seconds,
        "sessionId": job.session_id,
        "queue": job.queue_name,
        "lastEventSeq": job.last_event_seq,
        "nextRunAt": iso_utc(job.next_run_at),
        "deadlineAt": iso_utc(job.deadline_at),
        "createdAt": iso_utc(job.created_at),
        "updatedAt": iso_utc(job.updated_at),
        "completedAt": iso_utc(job.completed_at),
    }
