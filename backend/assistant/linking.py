"""Adopt an existing isolated execution without copying history or starting it."""
from datetime import datetime, timezone

from sqlalchemy import JSON, func, select, type_coerce

from assistant.commands import (_authority, _project, _tool_source_locked, command_digest,
                                task_locked, tool_command_key)
from assistant.identities import inbox_key
from assistant.policy import AssistantError, lock_actor
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskSubmission
from db.models.memory_pipeline import MemoryTurnCompletion
from db.models.memory_v2 import MemoryDebugRun
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import begin_session_write


async def record_isolation_birth_locked(db, session, *, source_session_id=None):
    """Constructor evidence; copied histories retain their original source."""
    if session.visibility == "private" and session.memory_policy == "assistant_isolated":
        payload = {"version": 1, "parent_id": session.parent_id}
        if source_session_id:
            payload["source_session_id"] = source_session_id
        await append_agent_event_locked(db, session, kind="assistant.isolation.created",
            payload=payload,
            idempotency_key=f"assistant-isolation-created:{session.id}")


async def isolated_history(db, session, seen=None):
    seen = set() if seen is None else seen
    if session.id in seen or len(seen) >= 32:
        return False
    seen.add(session.id)
    if session.visibility != "private" or session.memory_policy != "assistant_isolated":
        return False
    try:
        await _project(db, session.project_id, session.user_id, session.workspace_id)
    except AssistantError:
        return False
    if await db.scalar(select(MemoryDebugRun.id).where(MemoryDebugRun.session_id == session.id).limit(1)):
        return False
    if await db.scalar(select(MemoryTurnCompletion.id).where(MemoryTurnCompletion.session_id == session.id).limit(1)):
        return False
    if session.kind == "assistant":
        return True
    # Task creation has always created private, isolated executions atomically.
    created = await db.scalar(select(AssistantCommand.id).join(AssistantTask,
        AssistantTask.id == AssistantCommand.target_id).where(
        AssistantTask.execution_session_id == session.id, AssistantTask.user_id == session.user_id,
        AssistantTask.workspace_id == session.workspace_id, AssistantCommand.action == "task_create"))
    birth = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == session.id,
        AgentEvent.user_id == session.user_id, AgentEvent.kind == "assistant.isolation.created"))
    source_id = None
    if not created:
        if (birth is None or birth.payload.get("version") != 1
                or birth.payload.get("parent_id") != session.parent_id):
            return False
        source_id = birth.payload.get("source_session_id")
    for parent_id in dict.fromkeys(filter(None, (session.parent_id, source_id))):
        parent = await db.get(Session, parent_id)
        if (parent is None or parent.is_deleted or parent.user_id != session.user_id
                or parent.workspace_id != session.workspace_id):
            return False
        if not await isolated_history(db, parent, set(seen)):
            return False
    return True


async def version(db, session):
    driver = await db.get(AgentDriverState, session.id)
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session.id))
    sequence = await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(
        AgentEvent.session_id == session.id))
    return command_digest({"id": session.id, "project": session.project_id, "title": session.title,
        "kind": session.kind, "visibility": session.visibility, "memory_policy": session.memory_policy,
        "parent": session.parent_id, "sequence": sequence,
        "task": [task.id, task.control_revision, task.archived_at.isoformat() if task.archived_at else None] if task else None,
        "run": [driver.run_id, driver.generation, driver.phase] if driver else None})


async def candidate(db, session):
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session.id))
    code = None
    if session.visibility != "private":
        code = "ASSISTANT_LINK_SHARED"
    elif session.memory_policy != "assistant_isolated":
        code = "ASSISTANT_LINK_MEMORY_POLICY"
    elif task is None and not await isolated_history(db, session):
        code = "ASSISTANT_LINK_HISTORY_UNVERIFIED"
    elif task is None:
        # Old checkpoints/permission waiters lack a Task-bound decision scope.
        # Finish them in the original session rather than silently rebinding.
        question = await db.scalar(select(QuestionCheckpoint.id).where(
            QuestionCheckpoint.session_id == session.id,
            (QuestionCheckpoint.status == "pending") | (
                QuestionCheckpoint.status.in_(("answered", "rejected")) & QuestionCheckpoint.applied.is_(False)),
        ).limit(1))
        active_tool = await db.scalar(select(Part.id).join(Message, Message.id == Part.message_id).where(
            Part.session_id == session.id, Part.user_id == session.user_id, Part.type == "tool",
            Message.finish.is_(None), type_coerce(Part.data, JSON)["status"].as_string().in_(("pending", "running"))).limit(1))
        if question:
            code = "ASSISTANT_LINK_PENDING_REQUEST"
        elif active_tool:
            code = "ASSISTANT_LINK_TOOL_ACTIVE"
        else:
            continuation = await db.get(SessionExecution, session.id)
            driver = await db.get(AgentDriverState, session.id)
            pending = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session.id,
                AgentInboxItem.state.in_(("accepted", "claimed"))).limit(1))
            if (continuation and continuation.resume_pending or driver and driver.phase != "idle" and not pending):
                code = "ASSISTANT_LINK_CONTINUATION"
    return {"available": code is None, "reason_code": code, "version": await version(db, session),
        "task_id": task.id if task else None, "archived": task.archived_at is not None if task else False}


async def _adopt_pending(db, execution, task, command, now):
    """Preserve original Inbox IDs, authorship, bodies, clocks and run fences.

    Adoption Commands describe a linkage, never a newly received human input.
    Each existing pending Inbox gets its own Submission so ordinary claim and
    settlement hooks continue to maintain the Task revision and result outbox.
    """
    rows = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == execution.id,
        AgentInboxItem.user_id == execution.user_id, AgentInboxItem.state.in_(("accepted", "claimed")))
        .order_by(AgentInboxItem.created_at, AgentInboxItem.id).with_for_update())).all())
    for inbox in rows:
        child_id, submission_id = generate_id(), generate_id()
        source = {"entrypoint": "assistant_link_input", "link_command_id": command.id,
            "inbox_id": inbox.id, "original_origin": inbox.origin, "original_origin_ref": dict(inbox.origin_ref or {})}
        receipt = {"command_id": child_id, "task_id": task.id, "execution_session_id": execution.id,
            "submission_id": submission_id, "inbox_id": inbox.id, "task_revision": 1, "intent_revision": 1,
            "state": "linked", "delivery": inbox.delivery}
        db.add(AssistantCommand(id=child_id, actor_user_id=execution.user_id, workspace_id=execution.workspace_id,
            assistant_session_id=task.assistant_session_id, idempotency_key=inbox_key("assistant-link-input", inbox.id),
            action="task_link_input", target_type="task", target_id=task.id, payload_digest=command_digest(source),
            source_ref=source, state="applied", receipt=receipt, created_at=now, updated_at=now))
        await db.flush()
        db.add(TaskSubmission(id=submission_id, task_id=task.id, command_id=child_id, inbox_id=inbox.id,
            origin=inbox.origin, source_message_id=inbox.message_id if inbox.origin == "human" else None,
            delivery=inbox.delivery, accepted_at=inbox.accepted_at or inbox.created_at,
            applied_at=inbox.claimed_at if inbox.state == "claimed" else None,
            disposition="applied" if inbox.state == "claimed" else "accepted"))
        inbox.origin_ref = {**(inbox.origin_ref or {}), "assistant_link_command_id": command.id,
            "task_id": task.id, "submission_id": submission_id, "intent_revision": 1}
    return len(rows)


async def link_existing(*, user_id, workspace_id, main_id, session_id, expected_version,
                        idempotency_key, source=None):
    if source is not None:
        idempotency_key = tool_command_key(main_id, source.part_id)
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 64:
        raise ValueError("A stable link command key is required")
    if (not isinstance(expected_version, str) or len(expected_version) != 64
            or any(char not in "0123456789abcdef" for char in expected_version)):
        raise ValueError("The inspected conversation version is required")
    digest = command_digest({"action": "task_link", "session_id": session_id, "expected_version": expected_version,
        "source": {"part_id": source.part_id, "source_message_ids": list(source.source_message_ids)} if source else {"origin": "human"}})
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        prior = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.idempotency_key == idempotency_key))
        if prior:
            if prior.payload_digest != digest:
                raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
            await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=prior.target_id)
            return dict(prior.receipt)
        # Model commands share the established main -> execution lock order.
        source_ref = (await _tool_source_locked(db, main, source, "task_link") if source else
                      {"actor_user_id": user_id, "entrypoint": "assistant_link_existing"})
        source_ref["execution_session_id"] = session_id
        execution = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id,
            Session.workspace_id == workspace_id, Session.kind == "normal").with_for_update())
        if execution is None:
            raise AssistantError(404, "ASSISTANT_SESSION_UNAVAILABLE", "Owned execution conversation is unavailable")
        if execution.is_deleted:
            raise AssistantError(410, "ASSISTANT_SESSION_DELETED", "The original conversation was deleted")
        await _project(db, execution.project_id, user_id, workspace_id)
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session_id).with_for_update())
        if task and (task.assistant_session_id != main_id or task.user_id != user_id or task.workspace_id != workspace_id):
            raise AssistantError(409, "ASSISTANT_LINK_CONFLICT", "The original conversation already belongs to another task")
        inspected = await candidate(db, execution)
        if not inspected["available"]:
            messages = {"ASSISTANT_LINK_SHARED": "Only an already private conversation can be linked",
                "ASSISTANT_LINK_MEMORY_POLICY": "This conversation uses ordinary memory and cannot be linked",
                "ASSISTANT_LINK_HISTORY_UNVERIFIED": "The history's memory isolation cannot be verified",
                "ASSISTANT_LINK_PENDING_REQUEST": "Finish the pending request in the original conversation first",
                "ASSISTANT_LINK_TOOL_ACTIVE": "Wait for the original tool or permission request to finish first",
                "ASSISTANT_LINK_CONTINUATION": "Wait for the original continuation to finish before linking"}
            raise AssistantError(409, inspected["reason_code"], messages[inspected["reason_code"]])
        if (task is None or task.archived_at is not None) and expected_version != inspected["version"]:
            raise AssistantError(409, "ASSISTANT_LINK_CHANGED", "Conversation state changed; inspect it again")
        now, command_id = datetime.now(timezone.utc), generate_id()
        created = task is None
        if created:
            driver = await db.get(AgentDriverState, session_id)
            pending = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session_id,
                AgentInboxItem.state.in_(("accepted", "claimed"))).limit(1))
            task = AssistantTask(id=generate_id(), assistant_session_id=main_id, user_id=user_id,
                workspace_id=workspace_id, project_id=execution.project_id, execution_session_id=session_id,
                title=(execution.title or "")[:128], desired_state="running",
                observed_state="running" if driver and driver.phase != "idle" else "queued" if pending else "idle",
                control_revision=1, intent_revision=1, created_at=now, updated_at=now)
            db.add(task)
            await db.flush()
        elif task.archived_at is not None:
            task.archived_at, task.updated_at = None, now
            task.control_revision += 1
        command = AssistantCommand(id=command_id, actor_user_id=user_id, workspace_id=workspace_id,
            assistant_session_id=main_id, idempotency_key=idempotency_key, action="task_link", target_type="task",
            target_id=task.id, payload_digest=digest, source_ref=source_ref, state="applied", receipt={},
            created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        adopted = await _adopt_pending(db, execution, task, command, now) if created else 0
        receipt = {"command_id": command.id, "task_id": task.id, "execution_session_id": session_id,
            "project_id": task.project_id, "task_revision": task.control_revision,
            "state": "linked", "created": created, "adopted_inputs": adopted}
        command.receipt = receipt
        await append_agent_event_locked(db, execution, kind="assistant.task.linked", payload=receipt,
            idempotency_key=f"assistant-command:{command.id}")
        return receipt
