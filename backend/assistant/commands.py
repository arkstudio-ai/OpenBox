"""Atomic command acceptance shared by human inputs and assistant tools.

The actor row is a short cross-process admission lock. Claiming the command
precedes quota checks and execution Session creation; replay is a SQL read,
not a second input. No provider, sandbox or network call runs in this lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Sequence

from sqlalchemy import func, select

from agent.inbox import _validate_input, accept_inbox_item_locked
from assistant.policy import AssistantError, lock_actor, main_session_locked, require_membership
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


def tool_command_key(main_id: str, part_id: str) -> str:
    return sha256(f"assistant-command:v1:{main_id}:{part_id}".encode()).hexdigest()


def command_digest(payload: dict) -> str:
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode()).hexdigest()


async def _authority(db, *, user_id: str, workspace_id: str, main_id: str):
    await require_membership(db, user_id, workspace_id)
    main = await main_session_locked(db, user_id, workspace_id)
    if main is None or main.id != main_id or main.memory_policy != "assistant_isolated":
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


async def task_locked(db, *, user_id: str, workspace_id: str, main_id: str,
                      task_id: str, lock: bool = False):
    # Find the link without locking it, then always lock Session before Task.
    statement = select(AssistantTask).where(
        AssistantTask.id == task_id, AssistantTask.user_id == user_id,
        AssistantTask.workspace_id == workspace_id, AssistantTask.assistant_session_id == main_id,
    )
    task = await db.scalar(statement)
    if task is None:
        raise AssistantError(404, "ASSISTANT_TASK_UNAVAILABLE", "Task is unavailable")
    session_query = select(Session).where(
        Session.id == task.execution_session_id, Session.user_id == user_id,
        Session.workspace_id == workspace_id, Session.project_id == task.project_id,
        Session.is_deleted.is_(False), Session.visibility == "private",
        Session.memory_policy == "assistant_isolated", Session.kind == "normal",
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
                     "task_pause": "tasks.pause", "task_resume": "tasks.resume", "task_cancel": "tasks.cancel"}[action]
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


async def accept_task_command(*, user_id: str, workspace_id: str, main_id: str,
                              idempotency_key: str, prompt: str, project_id: str | None = None,
                              task_id: str | None = None, title: str = "",
                              attachments: Sequence[str] = (), model: str | None = None,
                              variant: str | None = None, expected_revision: int | None = None,
                              source: ToolSource | None = None, variant_explicit: bool = False,
                              client_message_id: str | None = None, video_model: str | None = None,
                              video_resolution: str | None = None, delivery: str = "followup",
                              expected_run: dict | None = None) -> dict:
    """Create/queue a followup or steer exactly one still-live execution.

    Callers wake the receipt's execution Session after commit. Periodic Inbox
    recovery covers a crash before that best-effort notification.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("task input text is required")
    if len(title) > 128:
        raise ValueError("task title must be at most 128 characters")
    _validate_input(prompt=prompt, attachments=attachments, client_id=client_message_id, output_format=None)
    if source is not None:
        if client_message_id is not None:
            raise ValueError("Only direct human input can carry a client message identity")
        idempotency_key = tool_command_key(main_id, source.part_id)
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
    action = "task_input" if task_id else "task_create"
    digest = command_digest({"action": action, "target": task_id, "project_id": project_id,
        "prompt": prompt, "title": title, "attachments": list(attachments), "model": model,
        "variant": variant, "expected_revision": expected_revision, "delivery": delivery,
        "source": {"part_id": source.part_id, "source_message_ids": list(source.source_message_ids)} if source else {"origin": "human"}})
    if expected_run is not None:
        digest = command_digest({"base": digest, "expected_run": expected_run})
    if variant_explicit:
        digest = command_digest({"base": digest, "variant_explicit": True})
    if client_message_id is not None:
        digest = command_digest({"base": digest, "client_message_id": client_message_id})
    if video_model is not None or video_resolution is not None:
        digest = command_digest({"base": digest, "video_model": video_model, "video_resolution": video_resolution})
    new_session = None
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        await lock_actor(db, user_id)
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
            from assistant.scheduling import require_runnable_locked
            await require_runnable_locked(db, execution)
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
            await _project(db, project_id, user_id, workspace_id)
            from core.config import get_config
            count = await db.scalar(select(func.count()).select_from(Session).where(
                Session.user_id == user_id, Session.is_deleted.is_(False),
                Session.kind.not_in(("cron", "assistant")),
            ))
            if count >= get_config().max_sessions_per_user:
                raise AssistantError(429, "SESSION_QUOTA_EXCEEDED", "Session quota exceeded")
            execution, new_session = _new_session_record(
                user_id=user_id, workspace_id=workspace_id, project_id=project_id,
                agent="build", model=model or main.model,
                variant=variant if variant_explicit or variant is not None else main.variant, title=title,
                parent_id=None, now=now, visibility="private", memory_policy="assistant_isolated",
            )
            execution.video_model = video_model if video_model is not None else main.video_model
            execution.video_resolution = video_resolution if video_resolution is not None else main.video_resolution
            db.add(execution)
            await db.flush()
            task = AssistantTask(id=generate_id(), assistant_session_id=main_id,
                user_id=user_id, workspace_id=workspace_id, project_id=project_id,
                execution_session_id=execution.id, title=title, desired_state="running",
                observed_state="queued", control_revision=1, intent_revision=1,
                created_at=now, updated_at=now)
            db.add(task)
            await db.flush()
            command.target_id = task.id
        submission_id = generate_id()
        origin = "assistant_delegation" if source else "human"
        origin_ref = {**source_ref, "command_id": command.id, "task_id": task.id,
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
        await append_agent_event_locked(db, execution, kind="assistant.submission.accepted",
            payload=receipt, idempotency_key=f"assistant-command:{command.id}")
    if new_session is not None:
        _publish_session_created(new_session)
    return receipt


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
    if submission.origin == "human":
        submission.source_message_id = inbox.message_id
    task.control_revision += 1
    if task.desired_state == "running":
        task.observed_state = "running"
    task.updated_at = now
