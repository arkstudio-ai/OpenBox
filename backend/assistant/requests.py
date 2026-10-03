"""Versioned, actor-bound decisions for existing Question checkpoints.

Questions remain the pending read model and continuation outbox. Commands own
the immutable human decision and its application receipt; neither reading a
card nor replaying a transport response grants authority to a later run.
"""
from sqlalchemy import select

from assistant.commands import _authority, command_digest, task_locked
from assistant.policy import AssistantError, lock_actor
from assistant.transactions import begin_snapshot
from core.identifier import generate_id
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.question import QuestionCheckpoint, SessionExecution
from question import runtime
from session.internal_parts import begin_session_write


async def question_task(db, row, *, lock=False):
    task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == row.session_id))
    saved = (row.continuation or {}).get("assistant_request")
    if task is None:
        if saved:
            raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
        return None
    if task.user_id != row.user_id:
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
    await _authority(db, user_id=row.user_id, workspace_id=task.workspace_id, main_id=task.assistant_session_id)
    task, session = await task_locked(db, user_id=row.user_id, workspace_id=task.workspace_id,
        main_id=task.assistant_session_id, task_id=task.id, lock=lock)
    if saved and (saved.get("task_id") != task.id or saved.get("assistant_session_id") != task.assistant_session_id
                  or saved.get("workspace_id") != task.workspace_id or saved.get("project_id") != task.project_id):
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
    return task, session


def _revision(row, binding):
    return command_digest({"kind": "question", "id": row.id, "actor": row.user_id,
        "session": row.session_id, "turn": row.generation, "message": row.message_id, "part": row.part_id,
        "task": binding["task_id"], "main": binding["assistant_session_id"], "workspace": binding["workspace_id"],
        "run": binding["run_id"], "generation": binding["generation"], "options": row.questions,
        "continuation_kind": row.continuation.get("kind"),
        "expires": runtime.utc(row.expires_at).isoformat() if row.expires_at else None})


async def bind_question(db, session, execution, row):
    linked = await question_task(db, row)
    if linked is None:
        return
    task, _ = linked
    driver = await db.get(AgentDriverState, session.id)
    if (driver is None or not driver.run_id or driver.run_id != execution.run_id
            or driver.user_id != row.user_id or driver.generation < 1):
        raise AssistantError(409, "ASSISTANT_REQUEST_RUN_REQUIRED", "A current execution run must ask the question")
    binding = {"kind": "question", "task_id": task.id, "assistant_session_id": task.assistant_session_id,
        "workspace_id": task.workspace_id, "project_id": task.project_id,
        "run_id": driver.run_id, "generation": driver.generation,
        "options_hash": command_digest({"questions": row.questions})}
    binding["request_revision"] = _revision(row, binding)
    row.continuation = {**row.continuation, "assistant_request": binding}


async def validate_question_read(db, row):
    linked = await question_task(db, row)
    if linked and not row.continuation.get("assistant_request"):
        from question.question import QuestionGone
        # Legacy pending linked calls did not save a Driver identity. Never
        # attach their approval to whatever run happens to be current now.
        raise QuestionGone("unverified")
    return linked


async def _fresh(db, row, execution, task):
    from question.question import QuestionGone
    binding = row.continuation.get("assistant_request")
    if not binding or binding.get("request_revision") != _revision(row, binding):
        raise QuestionGone("changed")
    if row.generation != execution.generation:
        raise QuestionGone("superseded")
    if task.desired_state == "canceled":
        raise QuestionGone("cancelled")
    driver = await db.get(AgentDriverState, row.session_id)
    if (driver is None or driver.user_id != row.user_id or driver.generation != binding["generation"]
            or driver.run_id not in (None, binding["run_id"]) or driver.abort_requested_at is not None):
        raise QuestionGone("superseded")
    return binding


async def emit_question_change(db, row, event_type):
    binding = (row.continuation or {}).get("assistant_request")
    if not binding:
        return
    from db.models.session import Session
    from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
    session = await db.get(Session, row.session_id)
    await ensure_surface_seed_locked(db, session)
    await append_agent_event_locked(db, session, kind="assistant.request.changed",
        payload={"task_id": binding["task_id"], "request_id": row.id, "request_kind": "question"},
        idempotency_key=f"assistant-request:{row.id}:{event_type}:{row.draft_revision}")
    if event_type == "question.cancelled":
        command = await decision_for(db, row.id)
        if command is not None and command.state == "accepted":
            finish_decision(command, "failed", error_code="QUESTION_GONE")


async def decision_for(db, request_id):
    return await db.scalar(select(AssistantCommand).where(AssistantCommand.action == "request_reply",
        AssistantCommand.target_type == "question", AssistantCommand.target_id == request_id))


def finish_decision(command, state, *, error_code=None):
    command.state, command.updated_at = state, runtime.now()
    command.receipt = {**command.receipt, "state": state,
        **({"error_code": error_code} if error_code else {}),
        **({"applied_at": command.updated_at.isoformat()} if state == "applied" else {})}


async def maybe_reply(row, *, answers, attachments=None, reply_id=None,
                      expected_request_revision=None, options_hash=None, source_ref=None):
    """Return None only for an unrelated legacy question; no implicit fallback."""
    async with get_db_session() as db:
        linked = await question_task(db, row)
    if linked is None:
        return None
    if not reply_id or not expected_request_revision or not options_hash:
        raise AssistantError(409, "ASSISTANT_REQUEST_VERSION_REQUIRED", "Reload this question before replying")
    if (not isinstance(reply_id, str) or len(reply_id) > 64
            or not isinstance(expected_request_revision, str) or len(expected_request_revision) != 64
            or not isinstance(options_hash, str) or len(options_hash) != 64):
        raise ValueError("Invalid request reply identity")
    if source_ref != {"kind": "card"}:
        raise AssistantError(403, "ASSISTANT_REPLY_SOURCE_REQUIRED", "Reply using the displayed question card")
    from question import question as questions
    digest = command_digest({"request_kind": "question", "request_id": row.id, "answers": answers,
        "attachments": attachments, "expected_request_revision": expected_request_revision, "options_hash": options_hash,
        "source_ref": source_ref})
    async with get_db_session() as db:
        await begin_session_write(db)
        # Match ordinary Command admission order: actor -> execution Session ->
        # Task. In particular, never acquire the main Session under execution.
        await lock_actor(db, row.user_id)
        task, session = await question_task(db, row, lock=True)
        saved = await db.get(QuestionCheckpoint, row.id, populate_existing=True)
        if saved is None or saved.user_id != row.user_id or saved.session_id != session.id:
            raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
        command = await db.scalar(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == row.user_id, AssistantCommand.workspace_id == task.workspace_id,
            AssistantCommand.assistant_session_id == task.assistant_session_id,
            AssistantCommand.idempotency_key == reply_id))
        if command is not None:
            if (command.payload_digest != digest or command.action != "request_reply"
                    or command.target_type != "question" or command.target_id != row.id):
                raise questions.QuestionConflict("Reply ID was used for different input")
            return dict(command.receipt)
        if await decision_for(db, row.id) is not None:
            raise questions.QuestionConflict("Another reply has already been accepted")
        execution = await runtime.execution_locked(db, session.id, row.user_id)
        binding = await _fresh(db, saved, execution, task)
        if expected_request_revision != binding["request_revision"] or options_hash != binding["options_hash"]:
            raise questions.QuestionGone("changed")
        from assistant.scheduling import require_runnable_locked
        await require_runnable_locked(db, session)
        questions._check_pending(saved, execution)
        await questions.resolve_locked(db, session, execution, saved, answers=answers, attachments=attachments)
        stamp = runtime.now()
        command_id = generate_id()
        receipt = {"ok": True, "command_id": command_id, "reply_id": reply_id, "request_id": saved.id,
            "request_kind": "question", "task_id": task.id, "session_id": session.id,
            "assistant_session_id": task.assistant_session_id, "request_revision": expected_request_revision,
            "options_hash": options_hash, "status": saved.status, "state": "accepted", "accepted_at": stamp.isoformat()}
        command = AssistantCommand(id=command_id, actor_user_id=row.user_id, workspace_id=task.workspace_id,
            assistant_session_id=task.assistant_session_id, idempotency_key=reply_id, action="request_reply",
            target_type="question", target_id=saved.id, payload_digest=digest, state="accepted", receipt=receipt,
            source_ref={"kind": "human_card", "actor_user_id": row.user_id, "request_id": saved.id,
                        "request_revision": expected_request_revision, "options_hash": options_hash,
                        "decision": {"answers": saved.answers, "attachments": saved.continuation.get("answer_attachments")
                                     or [[] for _ in saved.questions]}},
            created_at=stamp, updated_at=stamp)
        db.add(command)
        await db.flush()
        session_status = session.status
    from bus import bus
    bus.publish("question.rejected" if answers is None else "question.replied", questions._event(saved))
    await runtime.publish_status(session.id, row.user_id, session_status)
    return receipt


async def apply_guard(db, session, execution, row):
    """Recheck a saved decision before its existing SQL continuation applies."""
    linked = await question_task(db, row)
    if linked is None:
        return None
    task, _ = linked
    binding = await _fresh(db, row, execution, task)
    command = await decision_for(db, row.id)
    expected = {"answers": row.answers, "attachments": row.continuation.get("answer_attachments") or [[] for _ in row.questions]}
    if (command is None or command.state != "accepted" or command.actor_user_id != row.user_id
            or command.workspace_id != task.workspace_id or command.assistant_session_id != task.assistant_session_id
            or command.source_ref.get("kind") != "human_card" or command.source_ref.get("decision") != expected
            or command.receipt.get("request_revision") != binding["request_revision"]):
        from question.question import QuestionGone
        raise QuestionGone("unverified")
    return command


async def list_requests(*, user_id, workspace_id, main_id, cursor=None, limit=20):
    from question import question as questions
    if not 1 <= limit <= 50:
        raise ValueError("Invalid page size")
    async with get_db_session() as db:
        await begin_snapshot(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        query = select(QuestionCheckpoint).join(AssistantTask,
            AssistantTask.execution_session_id == QuestionCheckpoint.session_id).join(SessionExecution,
            SessionExecution.session_id == QuestionCheckpoint.session_id).where(
                AssistantTask.user_id == user_id, AssistantTask.workspace_id == workspace_id,
                AssistantTask.assistant_session_id == main_id, QuestionCheckpoint.user_id == user_id,
                QuestionCheckpoint.status == "pending", QuestionCheckpoint.generation == SessionExecution.generation)
        if cursor:
            query = query.where(QuestionCheckpoint.id > cursor)
        rows = list((await db.scalars(query.order_by(QuestionCheckpoint.id).limit(limit + 1))).all())
        items = []
        for row in rows[:limit]:
            try:
                task, _ = await validate_question_read(db, row)
                await _fresh(db, row, await db.get(SessionExecution, row.session_id), task)
                questions._check_pending(row, await db.get(SessionExecution, row.session_id))
            except (AssistantError, questions.QuestionGone):
                continue
            from db.models.project import Project
            project = await db.get(Project, task.project_id)
            items.append({**questions._request(row).model_dump(), "task_title": task.title, "project_name": project.name})
        receipts = []
        commands = (await db.scalars(select(AssistantCommand).where(
            AssistantCommand.actor_user_id == user_id, AssistantCommand.workspace_id == workspace_id,
            AssistantCommand.assistant_session_id == main_id, AssistantCommand.action == "request_reply",
            AssistantCommand.target_type == "question").order_by(AssistantCommand.created_at.desc()).limit(10))).all()
        for command in commands:
            row = await db.get(QuestionCheckpoint, command.target_id)
            if row is None:
                continue
            try:
                await question_task(db, row)
            except AssistantError:
                continue
            receipts.append(dict(command.receipt))
        return {"items": items, "next_cursor": rows[limit - 1].id if len(rows) > limit else None,
                "receipts": receipts}
