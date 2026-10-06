"""Durable task controls. Commands carry authority; they never create input.

Acceptance holds actor -> execution Session -> Task -> Driver. A resume of an
interrupted turn is a Command outbox, claimed atomically with its new Driver.
No transcript is rewritten and no synthetic human Message/Inbox is created.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, and_, or_, select, type_coerce

from assistant.commands import (
    ToolSource, _authority, _tool_source_locked, command_digest, task_locked, tool_command_key,
)
from assistant.policy import AssistantError, lock_actor, require_membership
from assistant.steering import ExpectedRun
from core.identifier import generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.external_effect import ExternalEffect
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from db.models.user import User
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import begin_session_write

log = create_logger("assistant.control")
CONTROL_ACTIONS = frozenset({"pause", "resume", "cancel"})
UNRESOLVED_EFFECTS = ("submitting", "accepted", "outcome_unknown", "manual_review")


async def session_tree_locked(db, execution):
    """Bounded persisted descendants, including deleted nodes with old work."""
    ids, frontier = {execution.id}, [execution.id]
    for _ in range(64):
        children = list((await db.scalars(select(Session.id).where(
            Session.parent_id.in_(frontier), Session.user_id == execution.user_id,
            Session.workspace_id == execution.workspace_id,
        ))).all())
        frontier = [child for child in children if child not in ids]
        if not frontier:
            return sorted(ids)
        ids.update(frontier)
        if len(ids) > 1024:
            break
    raise AssistantError(409, "ASSISTANT_CONTROL_SCOPE", "Task descendants require review before control")


async def pending_resume_locked(db, task_id):
    return await db.scalar(select(AssistantCommand).where(
        AssistantCommand.target_id == task_id, AssistantCommand.target_type == "task",
        AssistantCommand.action == "task_resume", AssistantCommand.state == "accepted",
    ).order_by(AssistantCommand.created_at, AssistantCommand.id).limit(1))


async def unresolved_effect_locked(db, execution, session_ids):
    effect = await db.scalar(select(ExternalEffect.id).where(
        ExternalEffect.tenant_id == execution.user_id, ExternalEffect.session_id.in_(session_ids),
        ExternalEffect.state.in_(UNRESOLVED_EFFECTS),
    ).limit(1))
    if effect:
        return True
    # A process interruption can leave an untracked tool outcome unknown.
    # Never silently upgrade that observation to permission to dispatch again.
    metadata = type_coerce(Part.data, JSON)["metadata"]
    return bool(await db.scalar(select(Part.id).where(
        Part.user_id == execution.user_id, Part.session_id.in_(session_ids), Part.type == "tool",
        or_(metadata["outcome_unknown"].as_boolean().is_(True),
            metadata["recovery_code"].as_string() == "tool_outcome_unknown",
            metadata["execution_outcome"].as_string() == "unknown"),
    ).limit(1)))


async def cancel_unclaimed_locked(db, task, execution, now):
    rows = (await db.scalars(select(AgentInboxItem).where(
        AgentInboxItem.session_id == execution.id, AgentInboxItem.user_id == execution.user_id,
        AgentInboxItem.state == "accepted",
    ).order_by(AgentInboxItem.created_at, AgentInboxItem.id).with_for_update())).all()
    for row in rows:
        row.state = row.outcome = "canceled"
        row.error = {"code": "ASSISTANT_TASK_CANCELED", "message": "Task canceled before this input was claimed"}
        row.canceled_at = row.updated_at = now
        submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.inbox_id == row.id))
        if submission is not None:
            submission.disposition = "canceled"
            from assistant.schedule_runs import submission_canceled_locked
            await submission_canceled_locked(db, submission, now)
        await append_agent_event_locked(db, execution, kind="inbox.canceled", payload={
            "item_id": row.id, "state": "canceled", "reason": row.error["message"], "code": row.error["code"],
        }, idempotency_key=f"inbox:{row.id}:canceled")
    return len(rows)


async def converge_locked(db, task, execution):
    """A stopped display requires durable quiescence, not an expired lease."""
    if task.desired_state == "running":
        return False
    if task.desired_state == "canceled":
        from assistant.suspended_control import cancel_suspended_locked
        await cancel_suspended_locked(db, execution)
    ids = await session_tree_locked(db, execution)
    drivers = (await db.scalars(select(AgentDriverState).where(
        AgentDriverState.session_id.in_(ids), AgentDriverState.user_id == task.user_id,
        AgentDriverState.phase != "idle",
    ))).all()
    claimed = await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id.in_(ids), AgentInboxItem.user_id == task.user_id,
        AgentInboxItem.state == "claimed",
    ).limit(1))
    uncanceled = task.desired_state == "canceled" and await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id.in_(ids), AgentInboxItem.user_id == task.user_id,
        AgentInboxItem.state == "accepted",
    ).limit(1))
    waiting = task.desired_state == "canceled" and await db.scalar(select(QuestionCheckpoint.id)
        .join(SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).where(
            QuestionCheckpoint.session_id.in_(ids), QuestionCheckpoint.user_id == task.user_id,
            QuestionCheckpoint.generation == SessionExecution.generation,
            QuestionCheckpoint.status.in_(("pending", "answered", "rejected")),
            or_(QuestionCheckpoint.applied.is_(False), SessionExecution.resume_pending.is_(True)),
        ).limit(1))
    if drivers or claimed or uncanceled or waiting:
        observed = "pausing" if task.desired_state == "paused" else "canceling"
    elif await unresolved_effect_locked(db, execution, ids):
        observed = "effect_unknown"
    else:
        observed = task.desired_state
    changed = task.observed_state != observed
    if changed:
        task.observed_state = observed
        task.control_revision += 1
        task.updated_at = datetime.now(timezone.utc)
        await append_agent_event_locked(db, execution, kind="assistant.control.observed", payload={
            "task_id": task.id, "desired_state": task.desired_state, "observed_state": observed,
            "task_revision": task.control_revision,
        })
    if observed in {"paused", "canceled"}:
        if observed == "canceled":
            from assistant.schedule_runs import task_canceled_locked
            await task_canceled_locked(db, task, datetime.now(timezone.utc))
        commands = (await db.scalars(select(AssistantCommand).where(
            AssistantCommand.target_id == task.id, AssistantCommand.target_type == "task",
            AssistantCommand.action == ("task_pause" if observed == "paused" else "task_cancel"),
            AssistantCommand.state == "accepted",
        ))).all()
        for command in commands:
            command.state, command.updated_at = "applied", datetime.now(timezone.utc)
    return changed


async def converge_session_locked(db, execution):
    # The owner already holds this Session. Descendant release is reconciled by
    # the bounded scanner so it never locks a parent while holding child Driver.
    task = await db.scalar(select(AssistantTask).where(
        AssistantTask.execution_session_id == execution.id, AssistantTask.user_id == execution.user_id,
    ).with_for_update())
    if task is not None:
        await converge_locked(db, task, execution)


def _conflict(task, code, message):
    error = AssistantError(409, code, message)
    error.current_task = {"id": task.id, "desired_state": task.desired_state,
                          "observed_state": task.observed_state, "control_revision": task.control_revision}
    return error


async def _resume_plan_locked(db, task, execution, ids):
    if await db.scalar(select(AgentDriverState.session_id).where(
        AgentDriverState.session_id.in_(ids), AgentDriverState.phase != "idle",
    ).limit(1)) or await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id.in_(ids), AgentInboxItem.state == "claimed",
    ).limit(1)):
        raise _conflict(task, "ASSISTANT_STILL_STOPPING", "Wait for the original runs to stop before continuing")
    if await unresolved_effect_locked(db, execution, ids):
        raise _conflict(task, "ASSISTANT_EFFECT_UNRESOLVED", "Verify outstanding external outcomes before continuing")
    pending = await db.scalar(select(QuestionCheckpoint.id).where(
        QuestionCheckpoint.session_id == execution.id, QuestionCheckpoint.user_id == task.user_id,
        QuestionCheckpoint.status == "pending",
    ).limit(1))
    if pending:
        return {"mode": "waiting_input"}
    question = await db.get(SessionExecution, execution.id)
    if question is not None and question.resume_pending:
        return {"mode": "question_continuation", "observed_state": "queued"}
    pause = await db.scalar(select(AssistantCommand).where(
        AssistantCommand.target_id == task.id, AssistantCommand.action == "task_pause",
    ).order_by(AssistantCommand.created_at.desc(), AssistantCommand.id.desc()).limit(1))
    previous = (pause.source_ref or {}).get("control", {}) if pause else {}
    target = previous.get("interrupted_run")
    if target and target.get("trigger_message_id"):
        result = await db.scalar(select(TaskResult).where(
            TaskResult.task_id == task.id, TaskResult.run_id == target["run_id"],
            TaskResult.generation == target["generation"],
        ).order_by(TaskResult.created_at.desc(), TaskResult.id.desc()).limit(1))
        if result is None or result.outcome != "succeeded":
            trigger = await db.scalar(select(Message.id).where(
                Message.id == target["trigger_message_id"], Message.session_id == execution.id,
                Message.user_id == task.user_id, Message.role == "user",
            ))
            if trigger is None:
                raise _conflict(task, "ASSISTANT_RESUME_UNAVAILABLE", "The original task input is unavailable")
            return {"mode": "original", "trigger_message_id": trigger, "interrupted_run": target}
    queued = await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id == execution.id, AgentInboxItem.state == "accepted",
    ).limit(1))
    latest = await db.get(TaskResult, task.latest_result_id) if task.latest_result_id else None
    return {"mode": "input_queue" if queued else "idle", "observed_state": "queued" if queued else (
        ("completed" if latest.outcome == "succeeded" else latest.outcome) if latest else previous.get("observed_state", "queued"))}


async def accept_control_command(*, user_id: str, workspace_id: str, main_id: str, task_id: str,
                                 idempotency_key: str, action: str, expected_revision: int,
                                 expected_run: dict | None = None, source: ToolSource | None = None):
    if action not in CONTROL_ACTIONS:
        raise ValueError("Unknown task control")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("A positive task revision is required")
    if expected_run is not None:
        expected_run = ExpectedRun.model_validate(expected_run).model_dump()
    if source is not None:
        idempotency_key = tool_command_key(main_id, source.part_id)
    if not idempotency_key or len(idempotency_key) > 64:
        raise ValueError("command key must be 1..64 characters")
    digest = command_digest({"action": action, "task_id": task_id, "expected_revision": expected_revision,
        "expected_run": expected_run, "source": {"part_id": source.part_id,
        "source_message_ids": list(source.source_message_ids)} if source else {"origin": "human"}})
    targets = []
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        from session.internal_parts import _lock_fenced
        main = await _lock_fenced(db, main_id, user_id)
        await require_membership(db, user_id, workspace_id)
        command = await db.scalar(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == user_id, AssistantCommand.workspace_id == workspace_id,
            AssistantCommand.assistant_session_id == main_id, AssistantCommand.idempotency_key == idempotency_key,
        ).with_for_update())
        if command is not None:
            if command.payload_digest != digest:
                raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
            await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=task_id)
            return dict(command.receipt)
        source_ref = (await _tool_source_locked(db, main, source, f"task_{action}") if source else
                      {"actor_user_id": user_id, "entrypoint": "assistant_command"})
        task, execution = await task_locked(db, user_id=user_id, workspace_id=workspace_id,
                                            main_id=main_id, task_id=task_id, lock=True)
        if task.control_revision != expected_revision:
            raise _conflict(task, "ASSISTANT_REVISION_CONFLICT", "Task revision changed; reload the task")
        ids = await session_tree_locked(db, execution)
        drivers = (await db.scalars(select(AgentDriverState).where(
            AgentDriverState.session_id.in_(ids), AgentDriverState.user_id == user_id,
        ).order_by(AgentDriverState.session_id).with_for_update())).all()
        driver = next((d for d in drivers if d.session_id == execution.id and d.phase != "idle"), None)
        actual = {"run_id": driver.run_id, "generation": driver.generation} if driver else None
        if actual != expected_run:
            raise _conflict(task, "ASSISTANT_RUN_CONFLICT", "The observed run changed; reload the task")
        if (action == "pause" and task.desired_state != "running"
                or action == "resume" and task.desired_state != "paused"
                or action == "cancel" and task.desired_state == "canceled"):
            raise _conflict(task, "ASSISTANT_CONTROL_CONFLICT", "This control no longer applies to the task")
        now = datetime.now(timezone.utc)
        context = {"observed_state": task.observed_state, "intent_revision": task.intent_revision}
        if action == "resume":
            context.update(await _resume_plan_locked(db, task, execution, ids))
            task.desired_state = "running"
            task.observed_state = ("resuming" if context["mode"] == "original" else
                                   "waiting_input" if context["mode"] == "waiting_input" else
                                   context.get("observed_state", "queued"))
        else:
            context["interrupted_run"] = ({**actual, "trigger_message_id": driver.trigger_message_id} if driver else None)
            task.desired_state = "paused" if action == "pause" else "canceled"
            for active in drivers:
                if active.phase != "idle":
                    active.abort_requested_at = active.updated_at = now
                    targets.append({"session_id": active.session_id, "run_id": active.run_id, "generation": active.generation})
            pending = await pending_resume_locked(db, task.id)
            if pending is not None:
                # Preserve the original continuation when a queued resume is
                # paused again before it could acquire capacity.
                context["interrupted_run"] = pending.source_ref["control"].get("interrupted_run")
                pending.state, pending.updated_at = "superseded", now
            if action == "cancel":
                await cancel_unclaimed_locked(db, task, execution, now)
                older = (await db.scalars(select(AssistantCommand).where(
                    AssistantCommand.target_id == task.id, AssistantCommand.action == "task_pause",
                    AssistantCommand.state == "accepted"))).all()
                for previous in older:
                    previous.state, previous.updated_at = "superseded", now
            task.observed_state = "pausing" if action == "pause" else "canceling"
        task.control_revision += 1
        task.updated_at = now
        from assistant.continuation import control_changed_locked
        await control_changed_locked(db, main, task, action, targets)
        command = AssistantCommand(id=generate_id(), actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, action=f"task_{action}",
            target_type="task", target_id=task.id, payload_digest=digest, expected_revision=expected_revision,
            source_ref={**source_ref, "control": context}, state="accepted", receipt={}, created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        if action != "resume":
            await converge_locked(db, task, execution)
        elif context["mode"] != "original":
            command.state = "applied"
        receipt = {"command_id": command.id, "task_id": task.id, "execution_session_id": execution.id,
            "action": action, "state": "accepted", "task_revision": task.control_revision,
            "desired_state": task.desired_state, "observed_state": task.observed_state, "expected_run": expected_run}
        command.receipt = receipt
        await append_agent_event_locked(db, execution, kind="assistant.control.accepted", payload=receipt,
                                         idempotency_key=f"assistant-control:{command.id}")
    # SQL already owns the interruption. This is only the exact local fast path.
    from agent.driver import request_abort
    for target in targets:
        try:
            await request_abort(target["session_id"], user_id,
                expected_run_id=target["run_id"], expected_generation=target["generation"])
        except Exception:
            log.exception("Task control interruption deferred command_id=%s", receipt["command_id"])
    return receipt


async def resume_binding_locked(db, session_id, run_id, generation):
    return await db.scalar(select(AgentEvent).where(
        AgentEvent.session_id == session_id, AgentEvent.run_id == run_id, AgentEvent.generation == generation,
        AgentEvent.kind == "assistant.control.resumed",
    ).order_by(AgentEvent.sequence).limit(1))


async def _bind_resume_locked(db, execution, state, command_id, trigger):
    fence = (execution.id, state.run_id, state.generation)
    await append_agent_event_locked(db, execution, kind="turn.started", payload={"message_id": trigger},
        run_fence=fence, turn_id=trigger, message_id=trigger)
    await append_agent_event_locked(db, execution, kind="assistant.control.resumed",
        payload={"command_id": command_id, "trigger_message_id": trigger}, run_fence=fence,
        turn_id=trigger, message_id=trigger)


async def claim_resume_locked(db, execution, state, command_id):
    """Inside reserve_run: the outbox and the Driver commit together."""
    command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id).with_for_update())
    if command is None or command.action != "task_resume" or command.state != "accepted":
        raise AssistantError(409, "ASSISTANT_RESUME_CLAIMED", "This continuation is no longer pending")
    task, _ = await task_locked(db, user_id=execution.user_id, workspace_id=execution.workspace_id,
        main_id=command.assistant_session_id, task_id=command.target_id, lock=True)
    main = await _authority(db, user_id=execution.user_id, workspace_id=execution.workspace_id, main_id=command.assistant_session_id)
    if not await db.scalar(select(User.id).where(User.id == execution.user_id,
            User.is_active.is_(True), User.is_deleted.is_(False))):
        raise AssistantError(403, "ASSISTANT_ACTOR_UNAVAILABLE", "Actor is unavailable")
    for ref in command.source_ref.get("source_refs", []):
        part = await db.scalar(select(Part.id).join(Message, Message.id == Part.message_id).where(
            Part.id == ref["part_id"], Part.message_id == ref["message_id"],
            Part.session_id == command.assistant_session_id, Part.user_id == execution.user_id,
            Message.role == "user", Message.session_id == command.assistant_session_id))
        if part is None:
            raise AssistantError(403, "ASSISTANT_SOURCE_UNVERIFIED", "The original control authority is unavailable")
    context = command.source_ref["control"]
    if (task.execution_session_id != execution.id or task.desired_state != "running"
            or context.get("mode") != "original" or task.intent_revision != context["intent_revision"]):
        raise _conflict(task, "ASSISTANT_RESUME_UNAVAILABLE", "Task continuation changed")
    if await unresolved_effect_locked(db, execution, await session_tree_locked(db, execution)):
        raise _conflict(task, "ASSISTANT_EFFECT_UNRESOLVED", "Verify outstanding external outcomes before continuing")
    state.trigger_message_id = context["trigger_message_id"]
    command.state, command.updated_at = "applied", datetime.now(timezone.utc)
    task.observed_state = "running"
    task.control_revision += 1
    task.updated_at = command.updated_at
    await _bind_resume_locked(db, execution, state, command.id, state.trigger_message_id)


async def rebind_resume_locked(db, execution, state, record):
    event = await resume_binding_locked(db, execution.id, record.run_id, record.generation)
    if event is not None:
        await _bind_resume_locked(db, execution, state, event.payload["command_id"], record.trigger_message_id)


async def run_resume(lease):
    """Use strict original attachments and the normal loop/finalizer."""
    from agent.recovery import _run_recovered_prompt, _trigger_state
    from agent.driver import RecoveredDriver
    try:
        async with get_db_session() as db:
            state = await db.get(AgentDriverState, lease.session_id)
            record = RecoveredDriver(session_id=lease.session_id, user_id=lease.user_id,
                run_id=lease.run_id, generation=lease.generation, phase="reserved",
                trigger_message_id=state.trigger_message_id)
        valid, _, assets = await _trigger_state(record)
        if not valid:
            raise AssistantError(409, "ASSISTANT_RESUME_UNAVAILABLE", "Original continuation input is unavailable")
        await _run_recovered_prompt(lease, assets)
    except BaseException:
        await lease.preserve_for_recovery(session_status="error")
        raise


async def cancel_descendant_inputs(snapshot):
    async with get_db_session() as db:
        execution = await db.get(Session, snapshot.execution_session_id)
        if execution is None:
            return
        ids = await session_tree_locked(db, execution)
    for session_id in ids:
        async with get_db_session() as db:
            await begin_session_write(db)
            execution = await db.scalar(select(Session).where(Session.id == session_id,
                Session.user_id == snapshot.user_id).with_for_update())
            from assistant.scheduling import held_task_locked
            hold = await held_task_locked(db, execution, lock=True)
            if hold is not None and hold.task_id == snapshot.id and hold.state == "canceled":
                await cancel_unclaimed_locked(db, snapshot, execution, datetime.now(timezone.utc))
                from assistant.suspended_control import cancel_suspended_locked
                await cancel_suspended_locked(db, execution)


async def block_resume(snapshot, code):
    """Expose failed revalidation without clearing the scheduling hold."""
    async with get_db_session() as db:
        await begin_session_write(db)
        task, execution = await task_locked(db, user_id=snapshot.actor_user_id, workspace_id=snapshot.workspace_id,
            main_id=snapshot.assistant_session_id, task_id=snapshot.target_id, lock=True)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == snapshot.id).with_for_update())
        if command.state != "accepted" or task.desired_state != "running" or task.observed_state != "resuming":
            return
        command.state, command.updated_at = "blocked", datetime.now(timezone.utc)
        task.desired_state = "paused"
        task.observed_state = "effect_unknown" if code == "ASSISTANT_EFFECT_UNRESOLVED" else "resume_blocked"
        task.control_revision += 1
        task.updated_at = command.updated_at
        await append_agent_event_locked(db, execution, kind="assistant.control.blocked", payload={
            "command_id": command.id, "task_id": task.id, "code": code, "task_revision": task.control_revision,
        }, idempotency_key=f"assistant-control-blocked:{command.id}")


#: Where the periodic control scan resumes. A bounded scan still reaches every
#: unresolved task in turn without rewriting updated_at, which the user reads
#: as "last changed" on the task card.
_CONTROL_SCAN_AFTER: str | None = None


async def recover_controls(*, task_id=None, launch=True, limit=50):
    """Bounded recovery for acceptance-to-wake gaps and stopped observations."""
    global _CONTROL_SCAN_AFTER
    from agent.driver import DriverBusyError, DriverQuotaExceededError, DriverRecoveryRequiredError, reserve_run
    from agent.inbox import schedule_inbox_wake
    import asyncio
    async with get_db_session() as db:
        suspended = select(Session.id).where(Session.id == AssistantTask.execution_session_id,
            Session.status.in_(("waiting_input", "queued"))).exists()
        query = select(AssistantTask).where(AssistantTask.desired_state != "running",
            or_(AssistantTask.observed_state.in_(("pausing", "canceling", "effect_unknown")),
                and_(AssistantTask.desired_state == "canceled", suspended))).order_by(AssistantTask.id)
        if task_id:
            tasks = list((await db.scalars(query.where(AssistantTask.id == task_id).limit(limit))).all())
        else:
            # Rotate fairly: continue after the last task scanned, then wrap.
            after = _CONTROL_SCAN_AFTER
            tasks = list((await db.scalars((query.where(AssistantTask.id > after) if after else query)
                                           .limit(limit))).all())
            if after and len(tasks) < limit:
                tasks += list((await db.scalars(query.where(AssistantTask.id <= after)
                                                .limit(limit - len(tasks)))).all())
            _CONTROL_SCAN_AFTER = tasks[-1].id if tasks else None
    changed = 0
    for snapshot in tasks:
        try:
            if snapshot.desired_state == "canceled":
                await cancel_descendant_inputs(snapshot)
            async with get_db_session() as db:
                await begin_session_write(db)
                task, execution = await task_locked(db, user_id=snapshot.user_id, workspace_id=snapshot.workspace_id,
                    main_id=snapshot.assistant_session_id, task_id=snapshot.id, lock=True)
                changed += await converge_locked(db, task, execution)
        except AssistantError:
            continue
    async with get_db_session() as db:
        query = select(AssistantCommand).where(AssistantCommand.action == "task_resume", AssistantCommand.state == "accepted")
        if task_id:
            query = query.where(AssistantCommand.target_id == task_id)
        commands = list((await db.scalars(query.order_by(AssistantCommand.created_at, AssistantCommand.id).limit(limit))).all())
    leases = []
    for command in commands:
        try:
            lease = await reserve_run(command.receipt["execution_session_id"], command.actor_user_id,
                                      assistant_resume_command_id=command.id)
        except AssistantError as exc:
            try:
                await block_resume(command, exc.code)
            except AssistantError:
                pass
            continue
        except (DriverBusyError, DriverQuotaExceededError, DriverRecoveryRequiredError, LookupError):
            continue
        leases.append(lease)
        if launch:
            from agent.recovery import _track_resume_task
            _track_resume_task(asyncio.create_task(run_resume(lease), name=f"assistant-resume:{command.id}"), lease)
    if task_id and launch:
        async with get_db_session() as db:
            task = await db.get(AssistantTask, task_id)
            if task and task.desired_state == "running":
                schedule_inbox_wake(task.execution_session_id, task.user_id)
    return changed, leases
