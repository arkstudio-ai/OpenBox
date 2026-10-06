"""Private cron dispatch and actual Task settlement, never legacy execution."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from agent.inbox import schedule_inbox_wake
from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError, lock_actor
from assistant.schedule_commands import admit_run_locked, job_locked, validate_configuration
from core.identifier import generate_id
from core.log import create_logger
from cron.schedule import apply_stagger, as_aware_utc, compute_next_run_at, schedule_from_dict
from cron.timer import job_result_values
from db.base import get_db_session
from db.models.assistant import AssistantCommand, TaskSubmission
from db.models.cron import CronJob, CronRun
from session.internal_parts import begin_session_write
from session.session import _publish_session_created

log = create_logger("assistant.schedules")


async def validate_task_schedule_locked(db, task, *, snapshot_checks=None):
    from assistant.command_sources import group_proof, validation_original
    scope = (task.user_id, task.workspace_id, task.assistant_session_id)
    async def original():
        return await db.scalar(select(CronRun).where(CronRun.assistant_task_id == task.id))
    if snapshot_checks is None:
        run = await validation_original(db, "task_schedule_binding", scope, task.id, original)
        if run is not None:
            await _validate_schedule_run(db, task, run, None)
        return
    run = await snapshot_checks.check(db, "task_schedule_binding", scope, task.id, original)
    if run is not None:
        # Within one snapshot/boundary, a completed run check is the union of
        # its command proofs; its schedule/human-source rows are read once.
        await group_proof(db, snapshot_checks, ("task_schedule", *scope, task.id, run.id),
                          lambda: _validate_schedule_run(db, task, run, snapshot_checks))


async def _validate_schedule_run(db, task, run, snapshot_checks):
    main = await _authority(db, user_id=task.user_id, workspace_id=task.workspace_id,
                            main_id=task.assistant_session_id, snapshot_checks=snapshot_checks)
    job = await job_locked(db, main, run.job_id)
    await validate_configuration(db, main, job, run.assistant_configuration_id, snapshot_checks=snapshot_checks)
    submission = await db.get(TaskSubmission, run.assistant_submission_id)
    command = await db.get(AssistantCommand, submission.command_id) if submission else None
    if (command is None or command.action != "schedule_run" or command.receipt.get("cron_run_id") != run.id
            or command.receipt.get("task_id") != task.id):
        raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Run admission receipt is unavailable")
    # Manual run authority is separate from the schedule's original definition.
    from assistant.results import part_hash
    from db.models.part import Part
    for ref in command.source_ref.get("source_refs", []):
        part = await db.scalar(select(Part).where(Part.id == ref.get("part_id"), Part.session_id == main.id,
            Part.message_id == ref.get("message_id"), Part.user_id == main.user_id))
        if part is None or part_hash(part) != ref.get("content_hash"):
            raise AssistantError(410, "ASSISTANT_SCHEDULE_SOURCE_CHANGED", "Manual run authority changed")
    from assistant.command_sources import validate_command_derivation
    await validate_command_derivation(db, main, command, snapshot_checks=snapshot_checks)


async def submission_applied_locked(db, submission):
    run = await db.scalar(select(CronRun).where(CronRun.assistant_submission_id == submission.id,
                                               CronRun.ended_at.is_(None)).with_for_update())
    if run is not None:
        run.status = "running"


async def _finish_locked(db, run, *, status, result_id, now):
    if run.ended_at is not None:
        return
    job = await db.scalar(select(CronJob).where(CronJob.id == run.job_id).with_for_update())
    if job is None:
        raise AssistantError(410, "ASSISTANT_SCHEDULE_UNAVAILABLE", "Original schedule is unavailable")
    elapsed = max(0, int((as_aware_utc(now) - as_aware_utc(run.started_at)).total_seconds() * 1000))
    submission = await db.get(TaskSubmission, run.assistant_submission_id)
    command = await db.get(AssistantCommand, submission.command_id)
    result = {"status": status, "duration_ms": elapsed,
              "error": "ASSISTANT_SCHEDULE_EXECUTION_FAILED" if status == "error" else None}
    values, _ = job_result_values(job, result, now)
    # An older execution must not undo a concurrently accepted reconfiguration
    # or restart a disabled schedule. Never apply legacy delete-after-run here.
    if job.revision != command.receipt["schedule_revision"] or not job.enabled:
        for field in ("next_run_at", "enabled"):
            values.pop(field, None)
    values.pop("is_deleted", None)
    if values.get("enabled") is False and job.enabled:
        job.revision += 1
    for field, value in values.items():
        setattr(job, field, value)
    run.status, run.ended_at, run.duration_ms = status, now, elapsed
    run.assistant_result_id = result_id
    run.error_message = result["error"]
    # Output remains only in the private execution/result evidence. CronRun
    # keeps identities/status; no copy is exported into logs or notifications.


async def result_settled_locked(db, task, result, now):
    run = await db.scalar(select(CronRun).where(CronRun.assistant_task_id == task.id,
                                               CronRun.ended_at.is_(None)).with_for_update())
    if run is None:
        return
    submission = await db.get(TaskSubmission, run.assistant_submission_id)
    if submission.inbox_id not in result.consumed_inbox_ids:
        return
    if result.outcome == "aborted" and task.desired_state == "paused":
        # A pause may resume the same original turn in a new Driver. It must
        # not free the schedule to overlap that unfinished Task with a new one.
        run.status = "paused"
        return
    status = "ok" if result.outcome == "succeeded" else "skipped" if result.outcome == "aborted" else "error"
    await _finish_locked(db, run, status=status, result_id=result.id, now=now)


async def submission_canceled_locked(db, submission, now):
    run = await db.scalar(select(CronRun).where(CronRun.assistant_submission_id == submission.id,
                                               CronRun.ended_at.is_(None)).with_for_update())
    if run is not None:
        await _finish_locked(db, run, status="skipped", result_id=None, now=now)


async def task_canceled_locked(db, task, now):
    """A quiescent cancel can end an already-claimed, then paused, run."""
    run = await db.scalar(select(CronRun).where(CronRun.assistant_task_id == task.id,
        CronRun.ended_at.is_(None)).with_for_update())
    if run is not None:
        await _finish_locked(db, run, status="skipped", result_id=None, now=now)


async def _dispatch_one(job_id, *, now):
    published = receipt = None
    async with get_db_session() as db:
        await begin_session_write(db)
        original = await db.get(CronJob, job_id)
        if original is None or original.assistant_session_id is None:
            return False
        main = await _authority(db, user_id=original.user_id, workspace_id=original.workspace_id,
                                main_id=original.assistant_session_id)
        await lock_actor(db, main.user_id)
        main = await _authority(db, user_id=original.user_id, workspace_id=original.workspace_id,
                                main_id=original.assistant_session_id)
        from cron.records import lock_run_admission, has_run_capacity_locked
        await lock_run_admission(db)
        job = await job_locked(db, main, job_id, lock=True)
        due = as_aware_utc(job.next_run_at)
        if not job.enabled or job.running_at is not None or due is None or due > now:
            return False
        if not await has_run_capacity_locked(db, main.user_id, now):
            return False
        await validate_configuration(db, main, job)
        from core.config import get_config
        if now - due > timedelta(seconds=get_config().cron_missed_run_max_age_seconds):
            schedule = schedule_from_dict(job.schedule)
            job.next_run_at = apply_stagger(compute_next_run_at(schedule, now), schedule, job.id)
            if job.next_run_at is None:
                job.enabled = False
                job.revision += 1
            job.last_error = "ASSISTANT_SCHEDULE_MISSED"
            job.updated_at = now
            return False
        slot = command_digest({"job_id": job.id, "revision": job.revision, "due_at": due.isoformat()})
        prior = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == main.user_id,
            AssistantCommand.workspace_id == main.workspace_id, AssistantCommand.assistant_session_id == main.id,
            AssistantCommand.idempotency_key == slot))
        if prior:
            return False
        command = AssistantCommand(id=generate_id(), actor_user_id=main.user_id, workspace_id=main.workspace_id,
            assistant_session_id=main.id, idempotency_key=slot, action="schedule_run", target_type="schedule", target_id=job.id,
            payload_digest=slot, expected_revision=job.revision, source_ref={"entrypoint": "assistant_schedule_timer",
                "configuration_id": job.assistant_command_id, "due_at": due.isoformat()},
            state="accepted", receipt={}, created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        receipt, published = await admit_run_locked(db, main, job, command, slot=slot, now=now)
    try:
        _publish_session_created(published)
        schedule_inbox_wake(receipt["execution_session_id"], main.user_id)
    except Exception:
        log.exception("Accepted scheduled Task wake deferred command_id=%s", receipt["command_id"])
    return True


async def dispatch_due_schedules(*, now=None, limit=50):
    now = now or datetime.now(timezone.utc)
    async with get_db_session() as db:
        jobs = list((await db.scalars(select(CronJob).where(CronJob.assistant_session_id.isnot(None),
            CronJob.is_deleted.is_(False), CronJob.enabled.is_(True), CronJob.next_run_at <= now,
            CronJob.running_at.is_(None)).order_by(CronJob.next_run_at, CronJob.id).limit(limit))).all())
    dispatched = 0
    for selected in jobs:
        try:
            dispatched += await _dispatch_one(selected.id, now=now)
        except (AssistantError, ValueError) as exc:
            # Authority/quotas are not infrastructure retries. Park exactly the
            # rejected version; a concurrent user update must remain untouched.
            from sqlalchemy import update
            code = exc.code if isinstance(exc, AssistantError) else "ASSISTANT_SCHEDULE_INVALID"
            async with get_db_session() as db:
                await db.execute(update(CronJob).where(CronJob.id == selected.id,
                    CronJob.revision == selected.revision, CronJob.next_run_at == selected.next_run_at,
                    CronJob.running_at.is_(None)).values(enabled=False, next_run_at=None,
                        revision=selected.revision + 1, last_error=code, updated_at=now))
        except Exception:
            log.exception("Private schedule dispatch deferred for %s", selected.id)
    return dispatched
