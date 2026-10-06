"""Atomic command acceptance shared by human inputs and assistant tools.

The actor row is a short cross-process admission lock. Claiming the command
precedes quota checks and execution Session creation; replay is a SQL read,
not a second input. No provider, sandbox or network call runs in this lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
from typing import Sequence

from sqlalchemy import and_, bindparam, func, select

from agent.inbox import _validate_input, accept_inbox_item_locked
from assistant.policy import AssistantError, lock_actor, require_membership
from assistant.identities import inbox_key
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskSubmission
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import begin_session_write
from session.session import _new_session_record, _publish_session_created


@dataclass(frozen=True)
class ToolSource:
    """IDs supplied by the executor, never model-selectable actor or run IDs."""
    part_id: str
    run_id: str
    generation: int
    source_message_ids: tuple[str, ...]
    coordination_inbox_id: str | None = None
    continuation_request_digest: str | None = None


def tool_command_key(main_id: str, part_id: str) -> str:
    return sha256(f"assistant-command:v1:{main_id}:{part_id}".encode()).hexdigest()


def command_digest(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode()).hexdigest()


@lru_cache(maxsize=1)
def _authority_statement():
    """Retain only SQL structure; every execution supplies new actor parameters."""
    from session.policy import active_membership
    user_id = bindparam("source_user_id", type_=Session.user_id.type)
    workspace_id = bindparam("source_workspace_id", type_=Session.workspace_id.type)
    main_id = bindparam("source_main_id", type_=Session.id.type)
    # The single membership row survives a missing main so refusal priority
    # stays membership -> private main. Put every authority field in current
    # SQL, including isolation when the identity map holds an older Session.
    # Do not refresh the ORM row: a locked caller may have pending title or
    # model changes under no_autoflush that this read must preserve.
    membership = select(active_membership(user_id, workspace_id).label("active")).subquery()
    return (select(membership.c.active, Session)
        .select_from(membership).outerjoin(Session, and_(
            Session.id == main_id, Session.user_id == user_id, Session.workspace_id == workspace_id,
            Session.is_deleted.is_(False), Session.kind == "assistant", Session.visibility == "private",
            Session.memory_policy == "assistant_isolated",
        )))


async def _authority(db, *, user_id: str, workspace_id: str, main_id: str, snapshot_checks=None):
    active, main = (await db.execute(_authority_statement(), {
        "source_user_id": user_id, "source_workspace_id": workspace_id, "source_main_id": main_id,
    })).one()
    if not active:
        raise AssistantError(403, "ASSISTANT_WORKSPACE_FORBIDDEN", "Active workspace membership is required")
    if main is None or main.memory_policy != "assistant_isolated":
        raise AssistantError(404, "ASSISTANT_UNAVAILABLE", "The private assistant is unavailable")
    return main


async def _project(db, project_id: str, user_id: str, workspace_id: str):
    row = await db.scalar(select(Project).where(
        Project.id == project_id, Project.user_id == user_id,
        Project.workspace_id == workspace_id, Project.is_deleted.is_(False),
    ))
    if row is None:
        raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
    return row


@lru_cache(maxsize=1)
def _task_scopes_statement():
    user_id = bindparam("source_user_id", type_=Session.user_id.type)
    workspace_id = bindparam("source_workspace_id", type_=Session.workspace_id.type)
    main_id = bindparam("source_main_id", type_=Session.id.type)
    return (select(AssistantTask, Session, Project.id).select_from(AssistantTask).where(
            AssistantTask.id.in_(bindparam("source_task_ids", expanding=True, type_=AssistantTask.id.type)),
            AssistantTask.user_id == user_id,
            AssistantTask.workspace_id == workspace_id, AssistantTask.assistant_session_id == main_id,
        ).outerjoin(Session, and_(
            Session.id == AssistantTask.execution_session_id, Session.user_id == user_id,
            Session.workspace_id == workspace_id, Session.project_id == AssistantTask.project_id,
            Session.is_deleted.is_(False), Session.kind == "normal",
        )).outerjoin(Project, and_(
            Project.id == AssistantTask.project_id, Project.user_id == user_id,
            Project.workspace_id == workspace_id, Project.is_deleted.is_(False),
        )).execution_options(populate_existing=True))


async def read_task_scopes(db, *, user_id: str, workspace_id: str, main_id: str, task_ids: Sequence[str]):
    """Read a bounded SQL batch while checking every requested task in order.

    These rows never survive this call as cached authority. A later admission
    or provider checkpoint queries again; writer lock ordering is separate.
    """
    if any(not isinstance(task_id, str) or not task_id for task_id in task_ids):
        raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
    ids = list(dict.fromkeys(task_ids))
    rows = {}
    for offset in range(0, len(ids), 100):
        rows.update((row[0].id, row) for row in (await db.execute(_task_scopes_statement(), {
            "source_user_id": user_id, "source_workspace_id": workspace_id, "source_main_id": main_id,
            "source_task_ids": ids[offset:offset + 100],
        })).all())
    scoped = []
    for task_id in task_ids:
        # Outer joins retain the original refusal priority for missing scope.
        row = rows.get(task_id)
        if row is None:
            raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
        task, execution, project_id = row
        if execution is None:
            raise AssistantError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "The original execution Session is unavailable")
        if project_id is None:
            raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
        scoped.append((task, execution))
    return scoped


async def task_locked(db, *, user_id: str, workspace_id: str, main_id: str,
                      task_id: str, lock: bool = False):
    if not lock:
        return (await read_task_scopes(db, user_id=user_id, workspace_id=workspace_id,
                                      main_id=main_id, task_ids=[task_id]))[0]
    statement = select(AssistantTask).where(
        AssistantTask.id == task_id, AssistantTask.user_id == user_id,
        AssistantTask.workspace_id == workspace_id, AssistantTask.assistant_session_id == main_id,
    )
    # Writers find the link first, then always lock Session before Task.
    task = await db.scalar(statement)
    if task is None:
        raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
    session_query = select(Session).where(
        Session.id == task.execution_session_id, Session.user_id == user_id,
        Session.workspace_id == workspace_id, Session.project_id == task.project_id,
        Session.is_deleted.is_(False), Session.kind == "normal",
    )
    execution = await db.scalar(session_query.with_for_update() if lock else session_query)
    if execution is None:
        raise AssistantError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "The original execution Session is unavailable")
    await _project(db, task.project_id, user_id, workspace_id)
    if lock:
        task = await db.scalar(statement.with_for_update().execution_options(populate_existing=True))
    return task, execution


async def _tool_source_locked(db, main: Session, source: ToolSource, action: str) -> dict:
    from agent.driver import assert_run_fence_locked
    await assert_run_fence_locked(db, session_id=main.id, user_id=main.user_id,
                                  run_id=source.run_id, generation=source.generation)
    part = await db.scalar(select(Part).join(Message, Message.id == Part.message_id).where(
        Part.id == source.part_id, Part.session_id == main.id, Part.user_id == main.user_id,
        Part.type == "tool", Message.session_id == main.id, Message.user_id == main.user_id,
        Message.role == "assistant", Message.finish.is_(None),
    ))
    expected_tool = {"task_create": "tasks.submit", "task_input": "tasks.followup", "task_link": "tasks.link_existing",
                     "task_finish_continuation": "tasks.next_step",
                     "task_pause": "tasks.pause", "task_resume": "tasks.resume", "task_cancel": "tasks.cancel",
                     "asset_attach": "assets.attach", "schedule_create": "schedules.create",
                     "schedule_update": "schedules.update", "schedule_run": "schedules.run",
                     "task_archive": "tasks.archive", "session_rename": "sessions.rename"}[action]
    if source.coordination_inbox_id and action == "task_input":
        expected_tool = "tasks.next_step"
    if (part is None or part.data.get("status") not in {"pending", "running"}
            or (part.canonical_tool_id or part.data.get("tool")) != expected_tool
            or not await db.scalar(select(AgentEvent.id).where(
                AgentEvent.session_id == main.id, AgentEvent.part_id == source.part_id,
                AgentEvent.run_id == source.run_id, AgentEvent.generation == source.generation,
            ).limit(1))):
        raise AssistantError(403, "ASSISTANT_CALL_UNVERIFIED", "A persisted tool call is required")
    # A report can contain imperatives and malicious quoted text. Its server
    # binding, not the model's interpretation, removes command authority.
    reports = await db.scalar(select(AgentInboxItem.id).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.run_id == source.run_id,
        AgentInboxItem.generation == source.generation, AgentInboxItem.origin == "task_result",
        AgentInboxItem.state.in_(("claimed", "settled")),
    ).limit(1))
    if reports:
        raise AssistantError(403, "ASSISTANT_REPORT_READ_ONLY", "Report-only turns cannot issue commands")
    from assistant.continuation import bound_coordination_locked, binding_ref
    coordination = await bound_coordination_locked(db, main, run_id=source.run_id, generation=source.generation)
    if coordination is not None or source.coordination_inbox_id is not None:
        if (coordination is None or coordination.inbox.id != source.coordination_inbox_id
                or action not in {"task_input", "task_finish_continuation"} or coordination.resolved):
            raise AssistantError(403, "ASSISTANT_CONTINUATION_SCOPE", "Coordination can only resolve its original task once")
        from assistant.continuation import require_observed_result
        await require_observed_result(db, main, coordination, source)
        from assistant.continuation_types import NextStepRequest
        request = NextStepRequest.model_validate(part.data.get("input"))
        request_digest = command_digest(request.model_dump(mode="json"))
        if (request_digest != source.continuation_request_digest
                or (request.decision == "continue") != (action == "task_input")):
            raise AssistantError(403, "ASSISTANT_CALL_UNVERIFIED", "Continuation must match its persisted tool input")
        return {"part_id": source.part_id, "run_id": source.run_id, "generation": source.generation,
            "source_refs": coordination.human_refs,
            "continuation_request_digest": request_digest,
            "continuation_authority": binding_ref(coordination)}
    if not 1 <= len(source.source_message_ids) <= 20:
        raise AssistantError(400, "ASSISTANT_SOURCE_REQUIRED", "Reference the original human input")
    references = []
    for message_id in dict.fromkeys(source.source_message_ids):
        inbox = await db.scalar(select(AgentInboxItem).where(
            AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
            AgentInboxItem.message_id == message_id, AgentInboxItem.origin == "human",
        ))
        parts = list((await db.scalars(select(Part).where(
            Part.message_id == message_id, Part.session_id == main.id,
            Part.user_id == main.user_id, Part.type == "text",
        ))).all())
        human_parts = [p for p in parts if p.data.get("origin") == "human"
                       and not p.data.get("ignored") and inbox is not None
                       and p.data.get("text") == inbox.prompt
                       and p.data.get("origin_ref", {}).get("actor_user_id") == main.user_id
                       and p.data.get("origin_ref", {}).get("inbox_id") == inbox.id]
        if inbox is None or not human_parts:
            raise AssistantError(403, "ASSISTANT_SOURCE_UNVERIFIED", "Human source is no longer available")
        for p in human_parts:
            from assistant.results import part_hash
            references.append({"session_id": main.id, "message_id": message_id, "part_id": p.id,
                               "origin": "human", "content_hash": part_hash(p)})
    return {"part_id": source.part_id, "run_id": source.run_id,
            "generation": source.generation, "source_refs": references}


async def create_task_locked(db, main, *, project_id, title, now, model=None, variant=None,
                             variant_explicit=False, video_model=None, video_resolution=None):
    """Shared atomic Task/Session admission, including each scheduled execution."""
    await _project(db, project_id, main.user_id, main.workspace_id)
    from core.config import get_config
    count = await db.scalar(select(func.count()).select_from(Session).where(
        Session.user_id == main.user_id, Session.is_deleted.is_(False),
        Session.kind.not_in(("cron", "assistant")),
    ))
    if count >= get_config().max_sessions_per_user:
        raise AssistantError(429, "SESSION_QUOTA_EXCEEDED", "Session quota exceeded")
    execution, published = _new_session_record(
        user_id=main.user_id, workspace_id=main.workspace_id, project_id=project_id,
        agent="build", model=model or main.model,
        variant=variant if variant_explicit or variant is not None else main.variant, title=title,
        parent_id=None, now=now, visibility="private", memory_policy="assistant_isolated")
    execution.video_model = video_model if video_model is not None else main.video_model
    execution.video_resolution = video_resolution if video_resolution is not None else main.video_resolution
    db.add(execution)
    await db.flush()
    task = AssistantTask(id=generate_id(), assistant_session_id=main.id,
        user_id=main.user_id, workspace_id=main.workspace_id, project_id=project_id,
        execution_session_id=execution.id, title=title, desired_state="running", observed_state="queued",
        control_revision=1, intent_revision=1, created_at=now, updated_at=now)
    db.add(task)
    await db.flush()
    return task, execution, published


async def accept_task_command(*, user_id: str, workspace_id: str, main_id: str,
                              idempotency_key: str, prompt: str, project_id: str | None = None,
                              task_id: str | None = None, title: str = "",
                              attachments: Sequence[str] = (), model: str | None = None,
                              variant: str | None = None, expected_revision: int | None = None,
                              source: ToolSource | None = None, variant_explicit: bool = False,
                              client_message_id: str | None = None, video_model: str | None = None,
                              video_resolution: str | None = None, delivery: str = "followup",
                              expected_run: dict | None = None, command_action: str | None = None,
                              continuation: dict | None = None) -> dict:
    """Create/queue a followup or steer exactly one still-live execution.

    Callers wake the receipt's execution Session after commit. Periodic Inbox
    recovery covers a crash before that best-effort notification.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task input text is required")
    if len(title) > 128:
        raise ValueError("task title must be at most 128 characters")
    _validate_input(prompt=prompt, attachments=attachments, client_id=client_message_id, output_format=None)
    if command_action is not None and (command_action != "asset_attach" or not task_id or not attachments):
        raise ValueError("Asset attachment requires an existing task and nonempty asset IDs")
    if source is not None:
        if client_message_id is not None:
            raise ValueError("Only direct human input can carry a client message identity")
        idempotency_key = (inbox_key("assistant-coordinate-command", source.coordination_inbox_id)
            if source.coordination_inbox_id else tool_command_key(main_id, source.part_id))
    if continuation is not None:
        from assistant.continuation_types import ContinuationRequest
        continuation = ContinuationRequest.model_validate(continuation).model_dump(mode="json")
    if source is not None and source.coordination_inbox_id and (
            not task_id or project_id is not None or title or attachments or delivery != "followup"
            or variant_explicit or model is not None or variant is not None
            or video_model is not None or video_resolution is not None or continuation is not None or command_action):
        raise AssistantError(403, "ASSISTANT_CONTINUATION_SCOPE", "Coordination keeps the original task and execution settings")
    if not idempotency_key or len(idempotency_key) > 64:
        raise ValueError("command key must be 1..64 characters")
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise ValueError("expected revision must be a positive integer")
    if delivery not in {"followup", "steer"} or (delivery == "steer" and not task_id):
        raise ValueError("Only an existing task can receive steer input")
    if (delivery == "steer") != (expected_run is not None):
        raise ValueError("Steer requires expected_run; followup must not specify it")
    if expected_run is not None:
        from assistant.steering import ExpectedRun
        expected_run = ExpectedRun.model_validate(expected_run).model_dump()
    action = command_action or ("task_input" if task_id else "task_create")
    digest = command_digest({"action": action, "target": task_id, "project_id": project_id,
        "prompt": prompt, "title": title, "attachments": list(attachments), "model": model,
        "variant": variant, "expected_revision": expected_revision, "delivery": delivery,
        "source": ({"coordination_inbox_id": source.coordination_inbox_id} if source and source.coordination_inbox_id
            else {"part_id": source.part_id, "source_message_ids": list(source.source_message_ids)} if source else {"origin": "human"})})
    if continuation is not None:
        digest = command_digest({"base": digest, "continuation": continuation})
    if expected_run is not None:
        digest = command_digest({"base": digest, "expected_run": expected_run})
    if variant_explicit:
        digest = command_digest({"base": digest, "variant_explicit": True})
    if client_message_id is not None:
        digest = command_digest({"base": digest, "client_message_id": client_message_id})
    if video_model is not None or video_resolution is not None:
        digest = command_digest({"base": digest, "video_model": video_model, "video_resolution": video_resolution})
    new_session = None
    interruption_targets = []
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
        # Continuation controls may write both main and execution state. Keep
        # the same main -> execution order as coordination and report delivery.
        from session.internal_parts import _lock_fenced
        main = await _lock_fenced(db, main_id, user_id)
        await require_membership(db, user_id, workspace_id)
        existing = await db.scalar(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == user_id, AssistantCommand.workspace_id == workspace_id,
            AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.idempotency_key == idempotency_key,
        ).with_for_update())
        if existing:
            if existing.payload_digest != digest:
                raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
            # Replay never bypasses revocation/deletion and never reapplies a
            # business revision or charges a second Session quota slot.
            await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                              task_id=existing.receipt["task_id"])
            return dict(existing.receipt)
        now = datetime.now(timezone.utc)
        command = AssistantCommand(id=generate_id(), actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, action=action,
            target_type="task", target_id=task_id, payload_digest=digest, expected_revision=expected_revision,
            source_ref={}, state="accepted", receipt={}, created_at=now, updated_at=now)
        db.add(command)
        await db.flush()  # The unique command is claimed before any creation.
        source_ref = (await _tool_source_locked(db, main, source, action) if source else
                      {"actor_user_id": user_id, "entrypoint": "assistant_command"})
        if client_message_id is not None:
            source_ref["client_message_id"] = client_message_id
        command.source_ref = source_ref
        if task_id:
            task, execution = await task_locked(db, user_id=user_id, workspace_id=workspace_id,
                                                main_id=main_id, task_id=task_id, lock=True)
            if project_id is not None and project_id != task.project_id:
                raise AssistantError(409, "ASSISTANT_PROJECT_CONFLICT", "A continuation keeps its original project")
            if expected_revision is None or task.control_revision != expected_revision:
                raise AssistantError(409, "ASSISTANT_REVISION_CONFLICT", "Task revision changed; reload the task")
            if task.desired_state != "running":
                raise AssistantError(409, "ASSISTANT_TASK_NOT_RUNNING", "Resume the task before adding input")
            if source is not None and source.coordination_inbox_id:
                from assistant.continuation import require_next_submission
                await require_next_submission(db, main, task, execution, source_ref)
            from assistant.scheduling import require_runnable_locked
            await require_runnable_locked(db, execution,
                replacing_continuation=not (source and source.coordination_inbox_id))
            if not (source and source.coordination_inbox_id):
                from assistant.continuation import replace_authority_locked
                await replace_authority_locked(db, main, task, interruption_targets)
            if expected_run is not None:
                from assistant.steering import require_steer_target_locked
                await require_steer_target_locked(db, execution, expected_run)
            task.control_revision += 1
            task.intent_revision += 1
            task.archived_at = None
            task.updated_at = now
        else:
            if not project_id:
                raise ValueError("creating a task requires a project")
            task, execution, new_session = await create_task_locked(db, main, project_id=project_id,
                title=title, now=now, model=model, variant=variant, variant_explicit=variant_explicit,
                video_model=video_model, video_resolution=video_resolution)
            command.target_id = task.id
        submission_id = generate_id()
        origin = "assistant_delegation" if source else "human"
        # Input provenance is a bounded reference to the human request, never a
        # copy of the material the main model consumed.
        origin_ref = {**{key: value for key, value in source_ref.items() if key != "derivation"},
                      "command_id": command.id, "task_id": task.id,
                      "submission_id": submission_id, "intent_revision": task.intent_revision}
        if expected_run is not None:
            origin_ref["expected_run"] = expected_run
        accepted = await accept_inbox_item_locked(db, execution, delivery=delivery,
            prompt=prompt, attachments=attachments, client_id=inbox_key("assistant-input", command.id),
            agent=execution.agent, model=model or execution.model,
            variant=variant if variant_explicit or variant is not None else execution.variant,
            video_model=video_model if video_model is not None else execution.video_model,
            video_resolution=video_resolution if video_resolution is not None else execution.video_resolution,
            origin=origin, origin_ref=origin_ref)
        db.add(TaskSubmission(id=submission_id, task_id=task.id, command_id=command.id,
            inbox_id=accepted.id, origin=origin, source_message_id=None, delivery=delivery,
            accepted_at=now, disposition="accepted"))
        # Busy is still busy until the Driver takes this queued input.
        if execution.status not in {"busy", "compacting"}:
            task.observed_state = "queued"
        receipt = {"command_id": command.id, "task_id": task.id,
                   "execution_session_id": execution.id, "submission_id": submission_id,
                   "inbox_id": accepted.id, "task_revision": task.control_revision,
                   "intent_revision": task.intent_revision, "state": "accepted", "delivery": delivery}
        if expected_run is not None:
            receipt["expected_run"] = expected_run
        command.receipt = receipt
        if source is not None and source.coordination_inbox_id:
            task.continuation_policy = {**task.continuation_policy,
                "followups_used": task.continuation_policy["followups_used"] + 1,
                "last_receipt": {"decision": "continue", "request_digest": source_ref["continuation_request_digest"], **receipt}}
        else:
            from assistant.continuation import grant_locked, public_policy
            await grant_locked(db, main, task, command, accepted.id, prompt, continuation, source=source)
            if continuation is not None:
                receipt["continuation"] = public_policy(task)
                command.receipt = dict(receipt)
        await append_agent_event_locked(db, execution, kind="assistant.submission.accepted",
            payload=receipt, idempotency_key=f"assistant-command:{command.id}")
    if new_session is not None:
        _publish_session_created(new_session)
    from agent.driver import request_abort
    for target in interruption_targets:
        try:
            await request_abort(target["session_id"], user_id,
                expected_run_id=target["run_id"], expected_generation=target["generation"])
        except Exception:
            from core.log import create_logger
            create_logger("assistant.continuation").exception("Replaced continuation interruption deferred")
    return receipt


async def record_submission_canceled_locked(db, execution, inbox, *, now) -> None:
    """Keep pre-claim cancellation and the Task receipt in the same transaction."""
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == execution.id,
        AssistantTask.user_id == execution.user_id).with_for_update())
    if task is None:
        return
    submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == task.id,
        TaskSubmission.inbox_id == inbox.id))
    if submission is None or submission.disposition != "accepted":
        return
    submission.disposition = "canceled"
    from assistant.schedule_runs import submission_canceled_locked
    await submission_canceled_locked(db, submission, now)
    task.control_revision += 1
    task.updated_at = now
    # An invalid queued attachment is not ongoing work. Other queued/claimed
    # inputs and an existing waiting/running turn keep their own observation.
    pending = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == execution.id,
        AgentInboxItem.state.in_(("accepted", "claimed"))).limit(1))
    if task.desired_state == "running" and task.observed_state == "queued" and not pending:
        task.observed_state = "waiting_input" if execution.status == "waiting_input" else "idle"


async def record_submission_applied_locked(db, execution, inbox, *, now) -> None:
    """Claim proves materialization, not that the provider understood the input."""
    submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.inbox_id == inbox.id))
    if submission is None:
        return
    task = await db.scalar(select(AssistantTask).where(
        AssistantTask.id == submission.task_id,
        AssistantTask.execution_session_id == execution.id,
    ).with_for_update())
    if task is None:
        raise AssistantError(409, "ASSISTANT_TASK_UNAVAILABLE", "Task link is unavailable")
    submission.applied_at = now
    submission.disposition = "applied"
    from assistant.schedule_runs import submission_applied_locked
    await submission_applied_locked(db, submission)
    if submission.origin == "human":
        submission.source_message_id = inbox.message_id
    task.control_revision += 1
    if task.desired_state == "running":
        task.observed_state = "running"
    task.updated_at = now
