"""Deleting a project, a conversation or a task on the user's explicit request.

docs/ASSISTANT_VOICE_FIX_PLAN.md 4 under the deletion boundary (backend/AGENTS.md,
docs/DELETION_BOUNDARY.md): nothing is deleted on inference. The model names one
of the user's own targets in this workspace and cites the user's words; the tool
reads the impact from the database and shows it on a confirmation card in the
main session (assistant.confirmations), which a call reads aloud. Only 确认 on
that card lets the same call through, once. The card freezes the reviewed scope:
a project whose conversations changed meanwhile is shown again.

Each deletion is one AssistantCommand keyed by the persisted tool call: accepted
before it runs, applied after. A repeated call returns its receipt and never
deletes twice; a call that failed after the card leaves the card usable.

- projects.delete: the project service deletes it, its conversations and its
  schedules. The default project and a project with active conversations are
  refused. Its folder is binned later by project.reclaim: the assistant never
  starts a cloud desktop to move it now.
- sessions.delete: the session service deletes the conversation; a watched one
  is no longer followed. The assistant's own conversation is refused.
- tasks.delete: running work is cancelled (assistant.control) and the task is
  no longer followed. Its conversation stays in its project.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select

from assistant.commands import ToolSource, _authority, _tool_source_locked, command_digest, task_locked, tool_command_key
from assistant.confirmations import CONFIRM_KIND, release_confirmation, require_card
from assistant.policy import AssistantError, lock_actor, main_session_locked
from core.identifier import generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.message import Message
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from session.internal_parts import begin_session_write

log = create_logger("assistant.delete_tools")
#: Session statuses with work under way (models.message.SessionStatus).
ACTIVE_STATUSES = frozenset({"busy", "retry", "compacting", "waiting_input", "queued"})


def _now():
    return datetime.now(timezone.utc)


def _source_digest(action: str, target_id: str, source: ToolSource) -> str:
    return command_digest({"action": action, "target_id": target_id, "source": {
        "part_id": source.part_id, "source_message_ids": list(source.source_message_ids)}})


async def _begin(db, ctx):
    await begin_session_write(db)
    await lock_actor(db, ctx.user_id)
    return await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)


async def _command(db, ctx, key: str):
    return await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == ctx.user_id,
        AssistantCommand.workspace_id == ctx.workspace_id, AssistantCommand.assistant_session_id == ctx.session_id,
        AssistantCommand.idempotency_key == key))


def _replayed(prior, digest: str) -> dict | None:
    """A finished call's receipt; None while it is still accepted (it resumes)."""
    if prior.payload_digest != digest:
        raise AssistantError(409, "ASSISTANT_COMMAND_CONFLICT", "Command key was used for different input")
    if prior.state == "failed":
        raise AssistantError(409, prior.receipt.get("error_code") or "ASSISTANT_COMMAND_FAILED",
                             "This call already failed; read the target again before asking the user again")
    return dict(prior.receipt) if prior.state == "applied" else None


async def _claim(ctx, source, *, key, digest, action, target_type, target_id, confirmation, impact, check):
    """Accept this call's command after the card, rechecking the target under the same locks."""
    async with get_db_session() as db:
        main = await _begin(db, ctx)
        prior = await _command(db, ctx, key)
        if prior is not None:
            return prior.id
        source_ref = await _tool_source_locked(db, main, source, action)
        await check(db)
        now = _now()
        command = AssistantCommand(id=generate_id(), actor_user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            assistant_session_id=ctx.session_id, idempotency_key=key, action=action, target_type=target_type,
            target_id=target_id, payload_digest=digest, state="accepted", receipt={"state": "accepted"},
            source_ref={**source_ref, "confirmation_id": confirmation, "impact": impact},
            created_at=now, updated_at=now)
        db.add(command)
        await db.flush()
        return command.id


async def _confirmed(ctx, source, *, action, target_type, target_id, key, digest, card, check) -> tuple[str, str]:
    """Ask for (or pass) the confirmation card, then accept the command: (command_id, card_id)."""
    confirmation = await require_card(ctx, kind=CONFIRM_KIND, action=action, **card)
    try:
        command_id = await _claim(ctx, source, key=key, digest=digest, action=action, target_type=target_type,
            target_id=target_id, confirmation=confirmation, impact=card["impact"], check=check)
    except BaseException:
        await release_confirmation(ctx, confirmation)
        raise
    return command_id, confirmation


async def _failed(ctx, command_id: str, confirmation: str | None, error: BaseException) -> None:
    """Nothing (more) was deleted: record why and leave the user's confirmation usable."""
    try:
        async with get_db_session() as db:
            await begin_session_write(db)
            command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id)
                                      .with_for_update())
            if command is not None and command.state == "accepted":
                code = getattr(error, "code", None) or type(error).__name__
                command.state, command.updated_at = "failed", _now()
                command.receipt = {**command.receipt, "state": "failed", "error_code": code}
        await release_confirmation(ctx, confirmation)
    except Exception:
        log.exception("Recording a failed deletion deferred command_id=%s", command_id)


async def _applied(ctx, command_id: str, receipt: dict, step=None) -> dict:
    """Mark the command applied, after an optional final step under main's lock."""
    async with get_db_session() as db:
        await _begin(db, ctx)
        main = await main_session_locked(db, ctx.user_id, ctx.workspace_id, lock=True)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command_id).with_for_update())
        if command.state == "applied":
            return dict(command.receipt)
        extra = await step(db, main, command) if step is not None else {}
        receipt = {"command_id": command.id, **receipt, **(extra or {})}
        command.state, command.receipt, command.updated_at = "applied", receipt, _now()
        return receipt


# projects.delete -------------------------------------------------------------

async def _owned_project(db, ctx, project_id: str, *, deleted=False):
    from project.workspace import DEFAULT_SLUG
    query = select(Project).where(Project.id == project_id, Project.user_id == ctx.user_id,
                                  Project.workspace_id == ctx.workspace_id)
    project = await db.scalar(query if deleted else query.where(Project.is_deleted.is_(False)))
    if project is None:
        raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
    main = await db.get(Session, ctx.session_id)
    if project.slug == DEFAULT_SLUG or project.id == main.project_id:
        raise AssistantError(409, "ASSISTANT_PROJECT_PROTECTED", "The default project cannot be deleted")
    return project


async def _project_scope(db, ctx, project) -> dict:
    """What deleting it removes, read now; a busy project is refused before any card."""
    from db.models.cron import CronJob
    busy = list((await db.execute(select(Session.title, Session.status).where(
        Session.project_id == project.id, Session.user_id == ctx.user_id, Session.workspace_id == ctx.workspace_id,
        Session.is_deleted.is_(False), Session.status != "idle").order_by(Session.id).limit(100))).all())
    if busy:
        names = "、".join(f"「{title or '未命名会话'}」({status})" for title, status in busy[:5])
        raise AssistantError(409, "ASSISTANT_PROJECT_BUSY",
            f"{len(busy)} conversation(s) in this project are not idle: {names}. Tell the user; they can be "
            "stopped first (tasks.cancel or tasks.delete for watched ones) or deleted with sessions.delete.")
    sessions = list((await db.scalars(select(Session.id).where(Session.project_id == project.id,
        Session.user_id == ctx.user_id, Session.workspace_id == ctx.workspace_id, Session.is_deleted.is_(False),
        Session.kind == "normal", Session.parent_id.is_(None)).order_by(Session.id))).all())
    schedules = await db.scalar(select(func.count()).select_from(CronJob).where(CronJob.project_id == project.id,
        CronJob.user_id == ctx.user_id, CronJob.workspace_id == ctx.workspace_id, CronJob.is_deleted.is_(False)))
    watched = await db.scalar(select(func.count()).select_from(AssistantTask).join(
        Session, Session.id == AssistantTask.execution_session_id).where(
        AssistantTask.project_id == project.id, AssistantTask.assistant_session_id == ctx.session_id,
        AssistantTask.user_id == ctx.user_id, AssistantTask.archived_at.is_(None), Session.is_deleted.is_(False)))
    return {"sessions": sessions, "schedules": schedules or 0, "watched": watched or 0}


def _project_impact(scope: dict) -> str:
    count = len(scope["sessions"])
    parts = [f"项目和其中 {count} 个会话会一起删除，无法在界面恢复" if count else "项目会被删除，无法在界面恢复"]
    if scope["schedules"]:
        parts.append(f"它的 {scope['schedules']} 个定时任务停用")
    if scope["watched"]:
        parts.append(f"其中 {scope['watched']} 个我在跟进的任务不再跟进")
    parts.append("项目文件夹之后会移到云电脑的回收站，一段时间内还能从那里找回文件")
    return "；".join(parts) + "。"


async def delete_project(ctx, *, project_id: str, source: ToolSource) -> dict:
    key, digest = tool_command_key(ctx.session_id, source.part_id), _source_digest("project_delete", project_id, source)
    async with get_db_session() as db:
        main = await _begin(db, ctx)
        prior = await _command(db, ctx, key)
        if prior is not None and (receipt := _replayed(prior, digest)) is not None:
            return receipt
        if prior is None:
            await _tool_source_locked(db, main, source, "project_delete")
        project = await _owned_project(db, ctx, project_id, deleted=prior is not None)
        name, gone = project.name, project.is_deleted
        scope = await _project_scope(db, ctx, project) if not gone else {"sessions": [], "schedules": 0, "watched": 0}
    scope_digest = command_digest({"action": "project_delete", "project_id": project_id, "sessions": scope["sessions"]})
    if prior is None:
        impact = _project_impact(scope)

        async def check(db):
            current = await _owned_project(db, ctx, project_id)
            now_scope = await _project_scope(db, ctx, current)
            if command_digest({"action": "project_delete", "project_id": project_id,
                               "sessions": now_scope["sessions"]}) != scope_digest:
                raise AssistantError(409, "ASSISTANT_DELETE_SCOPE_CHANGED",
                    "The project changed after the user reviewed it; call again to show the new impact")
        command_id, confirmation = await _confirmed(ctx, source, action="project_delete", target_type="project",
            target_id=project_id, key=key, digest=digest, check=check, card={
                "digest": scope_digest, "prompt": f"删除项目「{name}」。", "impact": impact, "header": "确认删除",
                "description": "由个人助理删除这个项目", "target": {"project_id": project_id}})
    else:
        command_id, confirmation = prior.id, prior.source_ref.get("confirmation_id")
    if not gone:
        from project import workspace
        try:
            await workspace.delete_project(project_id, ctx.user_id, ctx.workspace_id, sandbox=None)
        except workspace.ProjectError as exc:
            error = (AssistantError(409, "ASSISTANT_PROJECT_BUSY", str(exc))
                     if await workspace.active_session_count(project_id, ctx.user_id, ctx.workspace_id)
                     else AssistantError(409, "ASSISTANT_PROJECT_UNAVAILABLE", str(exc)))
            await _failed(ctx, command_id, confirmation, error)
            raise error from exc
        except BaseException as exc:
            await _failed(ctx, command_id, confirmation, exc)
            raise
    return await _applied(ctx, command_id, {"project_id": project_id, "name": name, "state": "deleted",
        "conversations_deleted": len(scope["sessions"]), "schedules_stopped": scope["schedules"],
        "note": "The project and its conversations are gone from the interface; its folder is binned later."})


# sessions.delete -------------------------------------------------------------

async def _owned_conversation(db, ctx, session_id: str):
    from assistant.session_tools import owned_session
    row = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == ctx.user_id,
        Session.workspace_id == ctx.workspace_id, Session.is_deleted.is_(False)))
    if row is not None and (row.kind == "assistant" or row.id == ctx.session_id):
        raise AssistantError(409, "ASSISTANT_SESSION_PROTECTED", "The personal assistant conversation cannot be deleted")
    return await owned_session(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, session_id=session_id)


async def _session_facts(db, ctx, session) -> dict:
    from db.models.cron import CronJob
    project = await db.get(Project, session.project_id)
    execution = await db.get(SessionExecution, session.id)
    driver = await db.get(AgentDriverState, session.id)
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session.id,
        AssistantTask.assistant_session_id == ctx.session_id, AssistantTask.user_id == ctx.user_id))
    return {
        "title": session.title or "未命名会话", "project": project.name if project else "",
        "visibility": session.visibility,
        "messages": await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == session.id, Message.user_id == ctx.user_id)) or 0,
        "running": session.status in ACTIVE_STATUSES or bool(driver and driver.phase != "idle"),
        "waiting": await db.scalar(select(func.count()).select_from(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == session.id, QuestionCheckpoint.status == "pending",
            QuestionCheckpoint.generation == (execution.generation if execution else 0))) or 0,
        "watched": bool(task and task.archived_at is None),
        "schedules": await db.scalar(select(func.count()).select_from(CronJob).where(
            CronJob.session_id == session.id, CronJob.user_id == ctx.user_id, CronJob.is_deleted.is_(False))) or 0,
    }


def _session_impact(facts: dict) -> str:
    parts = ["会话和它的消息记录会从列表中删除，无法在界面恢复"]
    if facts["running"]:
        parts.append("它正在进行的工作会停止")
    if facts["waiting"]:
        parts.append(f"{facts['waiting']} 个在等你回答的问题作废")
    if facts["watched"]:
        parts.append("我不再跟进它")
    if facts["schedules"]:
        parts.append(f"{facts['schedules']} 个定时任务不再往这个会话里汇报")
    if facts["visibility"] == "workspace":
        parts.append("工作区成员也将看不到它")
    return "；".join(parts) + "。"


async def _stop_following_deleted(db, main, command, session_id: str) -> dict:
    """A deleted conversation's task is archived: no late result is reported."""
    if await db.scalar(select(Session.id).where(Session.id == session_id).with_for_update()) is None:
        return {"task_archived": False}
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session_id,
        AssistantTask.assistant_session_id == main.id, AssistantTask.user_id == main.user_id).with_for_update())
    if task is None or task.archived_at is not None:
        return {"task_archived": False}
    from assistant.linking import archive_locked
    await archive_locked(db, main, task, command_id=command.id, now=_now())
    return {"task_archived": True, "task_id": task.id}


async def delete_session(ctx, *, session_id: str, source: ToolSource) -> dict:
    key, digest = tool_command_key(ctx.session_id, source.part_id), _source_digest("session_delete", session_id, source)
    async with get_db_session() as db:
        main = await _begin(db, ctx)
        prior = await _command(db, ctx, key)
        if prior is not None and (receipt := _replayed(prior, digest)) is not None:
            return receipt
        if prior is None:
            await _tool_source_locked(db, main, source, "session_delete")
            facts = await _session_facts(db, ctx, await _owned_conversation(db, ctx, session_id))
        else:
            row = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == ctx.user_id))
            facts = {"title": row.title if row else ""}
    if prior is None:
        async def check(db):
            await _owned_conversation(db, ctx, session_id)
        command_id, confirmation = await _confirmed(ctx, source, action="session_delete", target_type="session",
            target_id=session_id, key=key, digest=digest, check=check, card={
                "digest": command_digest({"action": "session_delete", "session_id": session_id}),
                "prompt": f"删除会话「{facts['title']}」（项目「{facts['project']}」，{facts['messages']} 条消息）。",
                "impact": _session_impact(facts), "header": "确认删除", "description": "由个人助理删除这个会话",
                "target": {"session_id": session_id}})
    else:
        command_id, confirmation = prior.id, prior.source_ref.get("confirmation_id")
    from session import session as sessions
    try:
        deleted = await sessions.delete_session(session_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        if not deleted:
            raise AssistantError(404, "ASSISTANT_SESSION_UNAVAILABLE", "Owned conversation is unavailable")
    except ValueError as exc:
        error = AssistantError(409, "ASSISTANT_SESSION_PROTECTED", str(exc))
        await _failed(ctx, command_id, confirmation, error)
        raise error from exc
    except BaseException as exc:
        await _failed(ctx, command_id, confirmation, exc)
        raise

    async def stop_following(db, main, command):
        return await _stop_following_deleted(db, main, command, session_id)
    return await _applied(ctx, command_id, {"session_id": session_id, "title": facts["title"], "state": "deleted",
        "note": "The conversation is gone from the interface" + (
            "; its running work was asked to stop." if facts.get("running") else ".")}, stop_following)


# tasks.delete ----------------------------------------------------------------

async def _active(db, task, execution) -> bool:
    """Work is under way: a live run, queued input or a question waiting."""
    if task.desired_state == "canceled":
        return False
    driver = await db.get(AgentDriverState, execution.id)
    if execution.status in ACTIVE_STATUSES or driver is not None and driver.phase != "idle":
        return True
    return bool(await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == execution.id,
        AgentInboxItem.state.in_(("accepted", "claimed"))).limit(1)))


async def _cancel(ctx, task_id: str, *, key: str, via: dict) -> dict | None:
    """Cancel running work as one step of this deletion; None when nothing runs any more."""
    from assistant.control import accept_control_command, recover_controls
    scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    for attempt in range(3):
        async with get_db_session() as db:
            prior = await _command(db, ctx, key)
            if prior is not None:
                return dict(prior.receipt)
            task, execution = await task_locked(db, **scope, task_id=task_id)
            if not await _active(db, task, execution):
                return None
            driver = await db.get(AgentDriverState, execution.id)
            run = ({"run_id": driver.run_id, "generation": driver.generation}
                   if driver is not None and driver.phase != "idle" else None)
            revision = task.control_revision
        try:
            receipt = await accept_control_command(**scope, task_id=task_id, idempotency_key=key, action="cancel",
                                                   expected_revision=revision, expected_run=run, via=via)
        except AssistantError as exc:
            # The run or revision moved between reading and accepting: read again.
            if exc.code in {"ASSISTANT_REVISION_CONFLICT", "ASSISTANT_RUN_CONFLICT"} and attempt < 2:
                continue
            if exc.code == "ASSISTANT_CONTROL_CONFLICT":
                return None
            raise
        try:
            await recover_controls(task_id=task_id)
        except Exception:
            log.exception("Accepted cancel wake deferred command_id=%s", receipt["command_id"])
        return receipt
    return None


async def delete_task(ctx, *, task_id: str, expected_revision: int, source: ToolSource) -> dict:
    key, digest = tool_command_key(ctx.session_id, source.part_id), _source_digest("task_delete", task_id, source)
    scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    async with get_db_session() as db:
        main = await _begin(db, ctx)
        prior = await _command(db, ctx, key)
        if prior is not None and (receipt := _replayed(prior, digest)) is not None:
            return receipt
        if prior is None:
            await _tool_source_locked(db, main, source, "task_delete")
        task, execution = await task_locked(db, **scope, task_id=task_id)
        project = await db.get(Project, task.project_id)
        title, project_name = execution.title or task.title or "未命名任务", project.name if project else ""
        running, archived = await _active(db, task, execution), task.archived_at is not None
        if prior is None and not (archived and not running) and task.control_revision != expected_revision:
            raise AssistantError(409, "ASSISTANT_TASK_REVISION", "Task changed; read tasks.get again")
    if prior is None and archived and not running:
        return {"task_id": task_id, "state": "archived", "canceled": False,
                "note": "This task was already not followed and nothing runs in it; nothing changed."}
    if prior is None:
        async def check(db):
            await task_locked(db, **scope, task_id=task_id)
        impact = ("正在做的会停下；已经完成的改动保留；会话仍留在项目里，之后还能打开。" if running else
                  "不再向你汇报它的结果；已经完成的改动保留；会话仍留在项目里，之后还能打开。")
        command_id, confirmation = await _confirmed(ctx, source, action="task_delete", target_type="task",
            target_id=task_id, key=key, digest=digest, check=check, card={
                "digest": command_digest({"action": "task_delete", "task_id": task_id}),
                "prompt": f"停止并不再跟进任务「{title}」（项目「{project_name}」）。", "impact": impact,
                "header": "确认停止", "description": "由个人助理停止并不再跟进这个任务", "target": {"task_id": task_id}})
    else:
        command_id, confirmation = prior.id, prior.source_ref.get("confirmation_id")
    via = {"action": "task_delete", "command_id": command_id, "part_id": source.part_id,
           "source_message_ids": list(source.source_message_ids), "confirmation_id": confirmation}
    try:
        canceled = await _cancel(ctx, task_id, via=via, key=command_digest(
            {"domain": "assistant-task-delete-cancel", "main_id": ctx.session_id, "part_id": source.part_id}))
    except BaseException as exc:
        await _failed(ctx, command_id, confirmation, exc)
        raise

    async def stop_following(db, main, command):
        from assistant.linking import archive_locked
        current, _ = await task_locked(db, **scope, task_id=task_id, lock=True)
        await archive_locked(db, main, current, command_id=command.id, now=_now())
        return {"task_revision": current.control_revision, "desired_state": current.desired_state,
                "observed_state": current.observed_state}
    try:
        return await _applied(ctx, command_id, {"task_id": task_id, "execution_session_id": execution.id,
            "state": "archived", "canceled": canceled is not None,
            **({"cancel_command_id": canceled["command_id"]} if canceled else {}),
            "note": ("Canceling is a request: the work stops shortly. " if canceled else "")
                    + "The task is no longer followed; its conversation stays in its project."}, stop_following)
    except BaseException as exc:
        await _failed(ctx, command_id, confirmation, exc)
        raise
