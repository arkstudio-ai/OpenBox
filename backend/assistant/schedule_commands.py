"""Atomic schedule definitions and run admission; no model or remote IO here."""
from datetime import datetime, timezone

from sqlalchemy import select

from agent.inbox import accept_inbox_item_locked, schedule_inbox_wake
from assistant.commands import (_authority, _project, _tool_source_locked, command_digest,
                                create_task_locked, task_locked, tool_command_key)
from assistant.identities import inbox_key
from assistant.policy import AssistantError, lock_actor
from core.identifier import ascending, generate_id
from core.log import create_logger
from cron.records import check_quota_locked, new_job, update_job
from cron.types import CronDeliveryConfig, CronJobCreate, CronJobUpdate
from cron.validation import validate_create_fields, validate_update_fields
from db.base import get_db_session
from db.models.assistant import AssistantCommand, TaskSubmission
from db.models.cron import CronJob, CronRun
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import begin_session_write
from session.session import _publish_session_created

log = create_logger("assistant.schedule_commands")


def definition(job):
    return {"project_id": job.project_id, "name": job.name, "instructions": job.task_prompt,
            "schedule": job.schedule}


async def job_locked(db, main, job_id, *, lock=False):
    query = select(CronJob).where(CronJob.id == job_id, CronJob.user_id == main.user_id,
        CronJob.workspace_id == main.workspace_id, CronJob.is_deleted.is_(False))
    job = await db.scalar(query.with_for_update().execution_options(populate_existing=True) if lock else query)
    if job is None:
        raise AssistantError(404, "ASSISTANT_SCHEDULE_UNAVAILABLE", "Schedule is unavailable")
    if job.assistant_session_id != main.id:
        raise AssistantError(409, "ASSISTANT_SCHEDULE_LEGACY", "This schedule belongs to the existing schedule manager; no private Task association exists")
    await _project(db, job.project_id, main.user_id, main.workspace_id)
    return job


async def validate_configuration(db, main, job, configuration_id=None, *, include_sources=False,
                                 snapshot_checks=None):
    """An update retains the human sources of unchanged configuration fields."""
    from db.models.part import Part
    key = configuration_id or job.assistant_command_id
    seen, first, all_refs = set(), None, []
    while key:
        if key in seen or len(seen) >= 64:
            raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Schedule authority chain is unavailable")
        seen.add(key)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == key,
            AssistantCommand.actor_user_id == main.user_id, AssistantCommand.workspace_id == main.workspace_id,
            AssistantCommand.assistant_session_id == main.id, AssistantCommand.target_id == job.id,
            AssistantCommand.action.in_(("schedule_create", "schedule_update")), AssistantCommand.state == "applied"))
        source = command.source_ref if command else {}
        configured = source.get("definition")
        if (not isinstance(configured, dict) or configured.get("project_id") != job.project_id
                or command_digest(configured) != source.get("definition_digest")):
            raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Schedule definition is unavailable")
        if first is None:
            first = command
        refs = source.get("source_refs", [])
        if not refs and not (source.get("actor_user_id") == main.user_id
                             and source.get("entrypoint") == "assistant_schedule_http"):
            raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Original human authority is unavailable")
        for ref in refs:
            part = await db.scalar(select(Part).where(Part.id == ref.get("part_id"),
                Part.message_id == ref.get("message_id"), Part.session_id == main.id, Part.user_id == main.user_id))
            if ref.get("session_id") != main.id or part is None or part.data.get("origin") != "human":
                raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Original human authority changed")
            if ref not in all_refs:
                all_refs.append(ref)
        key = source.get("parent_configuration_id")
    if first is None or configuration_id is None and first.source_ref["definition"] != definition(job):
        raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Current definition does not match its accepted command")
    return (first, all_refs) if include_sources else first


async def _command_locked(db, main, *, action, key, payload, expected_revision, source, job_id):
    digest = command_digest({"action": action, "payload": payload, "expected_revision": expected_revision,
        "source": {"part_id": source.part_id, "message_ids": list(source.source_message_ids)} if source else {"origin": "human"}})
    existing = await db.scalar(select(AssistantCommand).where(
        AssistantCommand.actor_user_id == main.user_id, AssistantCommand.workspace_id == main.workspace_id,
        AssistantCommand.assistant_session_id == main.id, AssistantCommand.idempotency_key == key).with_for_update())
    if existing:
        if existing.payload_digest != digest:
            raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
        await job_locked(db, main, existing.receipt["job_id"])
        if existing.receipt.get("task_id"):
            await task_locked(db, user_id=main.user_id, workspace_id=main.workspace_id,
                main_id=main.id, task_id=existing.receipt["task_id"])
        return existing, True
    now = datetime.now(timezone.utc)
    command = AssistantCommand(id=generate_id(), actor_user_id=main.user_id, workspace_id=main.workspace_id,
        assistant_session_id=main.id, idempotency_key=key, action=action, target_type="schedule", target_id=job_id,
        payload_digest=digest, expected_revision=expected_revision, source_ref={}, state="accepted",
        receipt={}, created_at=now, updated_at=now)
    db.add(command)
    await db.flush()
    command.source_ref = (await _tool_source_locked(db, main, source, action) if source else
                          {"actor_user_id": main.user_id, "entrypoint": "assistant_schedule_http"})
    return command, False


def _key(main_id, idempotency_key, source):
    key = tool_command_key(main_id, source.part_id) if source else idempotency_key
    if not isinstance(key, str) or not 1 <= len(key) <= 64:
        raise ValueError("Command key must be 1..64 characters")
    return key


def _revision(job, revision):
    if type(revision) is not int or job.revision != revision:
        raise AssistantError(409, "ASSISTANT_SCHEDULE_REVISION_CONFLICT", "Schedule changed; read its current revision")


def _configure(command, job, parent_id=None):
    value = definition(job)
    command.source_ref = {**command.source_ref, "definition": value, "definition_digest": command_digest(value),
                          "parent_configuration_id": parent_id}
    command.target_id, command.state = job.id, "applied"
    command.receipt = {"command_id": command.id, "job_id": job.id, "schedule_revision": job.revision,
        "state": "applied", "enabled": job.enabled,
        "next_run_at": job.next_run_at.isoformat() if job.next_run_at else None}
    job.assistant_command_id = command.id
    return dict(command.receipt)


async def create_schedule(*, user_id, workspace_id, main_id, idempotency_key, project_id,
                           name, instructions, schedule, enabled=True, source=None):
    create = CronJobCreate(project_id=project_id, session_id=None, name=name, task_prompt=instructions,
        schedule=schedule, enabled=enabled, max_retries=0, delete_after_run=False,
        delivery=CronDeliveryConfig(mode="none", notifications_enabled=False))
    payload = {"project_id": project_id, "name": name, "instructions": instructions,
               "schedule": create.schedule.model_dump(), "enabled": enabled}
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        command, replay = await _command_locked(db, main, action="schedule_create",
            key=_key(main_id, idempotency_key, source), payload=payload, expected_revision=None, source=source, job_id=None)
        if replay:
            return dict(command.receipt)
        await _project(db, project_id, user_id, workspace_id)
        validate_create_fields(create)
        await check_quota_locked(db, user_id, project_id)
        job = new_job(user_id, workspace_id, create, command.created_at)
        job.assistant_session_id = main.id
        receipt = _configure(command, job)
        db.add(job)
        await db.flush()
    return receipt


async def update_schedule(*, user_id, workspace_id, main_id, idempotency_key, job_id,
                           expected_revision, patch, source=None):
    if not isinstance(patch, dict) or not patch or set(patch) - {"name", "instructions", "schedule", "enabled"} or any(v is None for v in patch.values()):
        raise ValueError("Provide a nonempty schedule patch with supported fields")
    parsed = CronJobUpdate(**{("task_prompt" if key == "instructions" else key): value for key, value in patch.items()})
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        command, replay = await _command_locked(db, main, action="schedule_update",
            key=_key(main_id, idempotency_key, source), payload={"job_id": job_id, "patch": patch},
            expected_revision=expected_revision, source=source, job_id=job_id)
        if replay:
            return dict(command.receipt)
        job = await job_locked(db, main, job_id, lock=True)
        _revision(job, expected_revision)
        await validate_configuration(db, main, job)
        validate_update_fields(parsed)
        if parsed.enabled is True and parsed.schedule is None:
            from core.config import get_config
            from cron.schedule import schedule_from_dict
            from cron.validation import _check_schedule
            _check_schedule(schedule_from_dict(job.schedule), get_config())
        prior = job.assistant_command_id
        update_job(job, parsed, command.created_at)
        receipt = _configure(command, job, prior)
    return receipt


async def admit_run_locked(db, main, job, command, *, slot, now):
    configuration, refs = await validate_configuration(db, main, job, include_sources=True)
    if job.running_at is not None or await db.scalar(select(CronRun.id).where(
            CronRun.job_id == job.id, CronRun.assistant_task_id.isnot(None), CronRun.ended_at.is_(None))):
        raise AssistantError(409, "ASSISTANT_SCHEDULE_RUNNING", "This schedule already has an unfinished execution")
    run_id, submission_id = ascending("cron_run"), generate_id()
    task, execution, published = await create_task_locked(db, main, project_id=job.project_id,
                                                         title=job.name[:128], now=now)
    refs += [ref for ref in command.source_ref.get("source_refs", []) if ref not in refs]
    origin_ref = {"command_id": command.id, "task_id": task.id, "submission_id": submission_id,
        "intent_revision": task.intent_revision, "source_refs": refs, "cron_run_id": run_id,
        "schedule_id": job.id, "schedule_configuration_id": configuration.id}
    accepted = await accept_inbox_item_locked(db, execution, delivery="followup", prompt=job.task_prompt,
        attachments=(), client_id=inbox_key("assistant-schedule-run", command.id),
        agent=execution.agent, model=execution.model, variant=execution.variant,
        origin="assistant_delegation", origin_ref=origin_ref)
    db.add(TaskSubmission(id=submission_id, task_id=task.id, command_id=command.id, inbox_id=accepted.id,
        origin="assistant_delegation", delivery="followup", accepted_at=now, disposition="accepted"))
    await db.flush()
    db.add(CronRun(id=run_id, job_id=job.id, user_id=main.user_id, project_id=job.project_id,
        session_id=main.id, assistant_task_id=task.id, assistant_submission_id=submission_id,
        assistant_configuration_id=configuration.id, assistant_slot=slot,
        status="queued", injected=True, started_at=now))
    job.running_at = now
    receipt = {"command_id": command.id, "job_id": job.id, "cron_run_id": run_id,
        "schedule_revision": job.revision, "task_id": task.id, "execution_session_id": execution.id,
        "submission_id": submission_id, "inbox_id": accepted.id, "task_revision": 1, "intent_revision": 1,
        "state": "accepted", "delivery": "followup"}
    command.receipt = receipt
    await append_agent_event_locked(db, execution, kind="assistant.submission.accepted", payload=receipt,
        idempotency_key=f"assistant-command:{command.id}")
    return receipt, published


async def run_schedule(*, user_id, workspace_id, main_id, idempotency_key, job_id,
                        expected_revision, source=None):
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        command, replay = await _command_locked(db, main, action="schedule_run",
            key=_key(main_id, idempotency_key, source), payload={"job_id": job_id},
            expected_revision=expected_revision, source=source, job_id=job_id)
        if replay:
            return dict(command.receipt)
        from cron.records import lock_run_admission, has_run_capacity_locked
        await lock_run_admission(db)
        job = await job_locked(db, main, job_id, lock=True)
        _revision(job, expected_revision)
        if not await has_run_capacity_locked(db, user_id, command.created_at):
            raise AssistantError(429, "ASSISTANT_SCHEDULE_CAPACITY", "Scheduled execution capacity is full; retry this command later")
        receipt, published = await admit_run_locked(db, main, job, command, slot=command.id, now=command.created_at)
    try:
        _publish_session_created(published)
        schedule_inbox_wake(receipt["execution_session_id"], user_id)
    except Exception:
        log.exception("Accepted scheduled Task wake deferred command_id=%s", receipt["command_id"])
    return receipt
