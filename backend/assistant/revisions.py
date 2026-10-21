"""Replace a pending task plan and persist its new input in one transaction.

This is an explicit human-requested revision, not an answer or a permission
grant. Ordinary delegated followups still wait behind cards. Existing shared
conversation send consent, owner checks and execution fences remain in force.
"""
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from assistant.commands import ToolSource, _authority, _project, _tool_source_locked, command_digest
from assistant.policy import AssistantError
from assistant.requests import question_version
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from question import runtime
from core.log import create_logger

log = create_logger("assistant.revisions")


class RevisionTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=64)
    version: str = Field(pattern=r"^[0-9a-f]{64}$")


async def target(db, main, request_id, *, lock=False):
    row = await db.get(QuestionCheckpoint, request_id)
    if row is None or row.user_id != main.user_id:
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
    query = select(Session).where(Session.id == row.session_id, Session.user_id == main.user_id,
        Session.workspace_id == main.workspace_id, Session.kind == "normal", Session.parent_id.is_(None),
        Session.is_deleted.is_(False))
    session = await db.scalar(query.with_for_update() if lock else query)
    if session is None:
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Owned conversation is unavailable")
    await _project(db, session.project_id, main.user_id, main.workspace_id)
    part = await db.get(Part, row.part_id) if row.part_id else None
    if (part is None or part.session_id != session.id or part.user_id != main.user_id
            or (part.canonical_tool_id or part.data.get("tool")) != "question"
            or (row.continuation or {}).get("kind") != "question"
            or any((q.get("detail") or {}).get("kind") for q in row.questions)):
        raise AssistantError(403, "ASSISTANT_REVISION_HUMAN_ONLY",
                             "This is an execution approval or human-only action, not a task-plan question")
    return row, session


async def prepare_locked(db, main, binding, command, now):
    """Caller owns main/actor locks and commits the replacement Inbox together."""
    from core.identifier import generate_id
    from assistant.identities import inbox_key
    from assistant.linking import _adopt_pending
    from session.agent_event_log import append_agent_event_locked
    row, session = await target(db, main, binding["request_id"], lock=True)
    driver = await db.scalar(select(AgentDriverState).where(
        AgentDriverState.session_id == session.id).with_for_update())
    execution = await runtime.execution_locked(db, session.id, main.user_id)
    # A real answer or another device may already have resumed the old plan.
    # Never invalidate a live tool or quietly move this revision to a new run.
    await db.refresh(row)
    if (row.status != "pending" or row.generation != execution.generation or row.detached
            or question_version(row) != binding["version"]
            or row.expires_at and runtime.utc(row.expires_at) <= runtime.now()
            or execution.run_id or execution.resume_pending
            or driver and (driver.phase != "idle" or driver.run_id)):
        raise AssistantError(409, "ASSISTANT_REVISION_CHANGED",
                             "The pending plan changed; read its current state before submitting a revision")
    task = await db.scalar(select(AssistantTask).where(
        AssistantTask.execution_session_id == session.id).with_for_update())
    if task and (task.user_id != main.user_id or task.workspace_id != main.workspace_id
                 or task.assistant_session_id != main.id or task.desired_state != "running"):
        raise AssistantError(409, "ASSISTANT_REVISION_TASK_HELD", "The task is unavailable or paused")
    if task is None:
        # Linking a waiting plan is safe only here: the exact old cards are
        # superseded atomically, before any Task-bound input can be admitted.
        task = AssistantTask(id=generate_id(), assistant_session_id=main.id, user_id=main.user_id,
            workspace_id=main.workspace_id, project_id=session.project_id, execution_session_id=session.id,
            title=(session.title or "")[:128], desired_state="running", observed_state="queued",
            control_revision=1, intent_revision=1, created_at=now, updated_at=now)
        db.add(task)
        await db.flush()
        link = AssistantCommand(id=generate_id(), actor_user_id=main.user_id, workspace_id=main.workspace_id,
            assistant_session_id=main.id, idempotency_key=inbox_key("revision-link", command.id),
            action="task_link", target_type="task", target_id=task.id,
            payload_digest=command_digest({"revision_command": command.id, "session": session.id}),
            source_ref={**command.source_ref, "revision_command_id": command.id}, state="applied",
            receipt={"task_id": task.id, "execution_session_id": session.id, "state": "linked"},
            created_at=now, updated_at=now)
        db.add(link)
        await db.flush()
        await _adopt_pending(db, session, task, link, now)
        await append_agent_event_locked(db, session, kind="assistant.task.linked", payload=link.receipt,
                                        idempotency_key=f"assistant-command:{link.id}")
    row.continuation = {**row.continuation, "revision_command_id": command.id}
    invalidated = await runtime.invalidate_locked(db, execution)
    session.status = "idle"
    runtime._after_commit(db, lambda: runtime.publish_invalidated(invalidated))
    return task, session


async def revise(ctx, *, request_id, version, instructions, attachment_ids, source_message_ids):
    from assistant.commands import accept_task_command
    from assistant.confirmations import require_card, release_confirmation
    from assistant.identities import inbox_key
    from agent.inbox import schedule_inbox_wake
    binding = RevisionTarget(request_id=request_id, version=version).model_dump()
    source = ToolSource(ctx.part_id, ctx.run_id, ctx.run_generation, tuple(source_message_ids))
    async with get_db_session() as db:
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await _tool_source_locked(db, main, source, "task_revise")
        _, session = await target(db, main, request_id)
        shared, title, session_id = session.visibility == "workspace", session.title, session.id
        replay = await db.scalar(select(AssistantCommand.id).where(
            AssistantCommand.actor_user_id == ctx.user_id,
            AssistantCommand.workspace_id == ctx.workspace_id,
            AssistantCommand.assistant_session_id == ctx.session_id,
            AssistantCommand.idempotency_key == inbox_key("question-revision", request_id, version)))
    confirmation = None
    if shared and replay is None:
        confirmation = await require_card(ctx,
            digest=command_digest({"revision": binding, "text": instructions, "attachments": attachment_ids}),
            prompt=f"把这些修改发送到工作区可见的会话「{title}」吗？工作区成员能看到这条消息及所选附件。\n\n{instructions}",
            header="确认修改任务", description="更新原任务要求，重新规划，不同意旧方案的执行或费用",
            target={"session_id": session_id, "request_id": request_id})
    try:
        value = await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, idempotency_key="revision", prompt=instructions,
            attachments=attachment_ids, source=source, revision_request=binding)
    except BaseException:
        await release_confirmation(ctx, confirmation)
        raise
    try:
        schedule_inbox_wake(value["execution_session_id"], ctx.user_id)
    except Exception:
        # The durable input is already committed. Recovery will wake it; a
        # notification failure must not tell the model to submit it again.
        log.exception("Accepted revision wake deferred command_id=%s", value["command_id"])
    return value
