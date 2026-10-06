"""Durable human-input checkpoints. Asking suspends; replying schedules work."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from bus import bus
from core.identifier import generate_id
from db.base import get_db_session
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from question import runtime


#: The tools whose own part may carry a durable question, mapped to the
#: continuation kind their answer resumes. A tool that files a question through
#: ``ask()`` MUST appear here, or the tool call's part fails the guard below and
#: the question is never delivered. (The guard doubles as the batch check: a
#: batch-wrapped call's part is the ``batch`` tool, which is absent here.)
#: `desktop_takeover` files an ordinary question, so it resumes as "question".
QUESTION_TOOL_CONTINUATIONS: dict[str, str] = {
    "question": "question",
    "plan_enter": "plan_enter",
    "plan_exit": "plan_exit",
    "creator_context": "memory_proposal",
    "memory_forget": "memory_forget",
    "desktop_takeover": "question",
    # Writing into a workspace-visible conversation (assistant.confirmations).
    "tasks.followup": "question",
    "assets.attach": "question",
    # A sensitive memory the assistant may keep only after confirmation.
    "memory.remember": "memory_proposal",
}


class QuestionOption(BaseModel):
    label: str
    description: str = ""


class Question(BaseModel):
    question: str
    header: str = ""
    options: list[QuestionOption] = Field(default_factory=list)
    multiple: bool = False
    custom: bool = True
    allow_attachments: bool = False
    detail: dict[str, Any] | None = None


class DraftAnswer(BaseModel):
    selected: list[str] = Field(default_factory=list)
    custom: str = Field(default="", max_length=5000)
    use_custom: bool = False
    attachments: list[str] | None = Field(default=None, max_length=32)


class QuestionRequest(BaseModel):
    id: str
    user_id: str = "default"
    session_id: str
    questions: list[Question]
    tool: dict | None = None
    created_at: str = ""
    generation: int = 0
    status: str = "pending"
    draft: list[DraftAnswer] = Field(default_factory=list)
    draft_revision: int = 0
    expires_at: str | None = None
    assistant: dict | None = None


class QuestionReply(BaseModel):
    id: str
    answers: list[list[str]]


class QuestionRejectedError(Exception):
    pass


class QuestionSuspended(Exception):
    """Control flow, not a failed tool. The checkpoint is already committed."""
    def __init__(self, request_id: str):
        super().__init__("Waiting for user input")
        self.request_id = request_id


class QuestionGone(Exception):
    def __init__(self, status: str):
        super().__init__(f"Question is {status}")
        self.status = status


class QuestionConflict(Exception):
    pass


def _request(row: QuestionCheckpoint) -> QuestionRequest:
    return QuestionRequest(
        id=row.id, user_id=row.user_id, session_id=row.session_id,
        questions=row.questions, generation=row.generation, status=row.status,
        tool={"messageID": row.message_id, "callID": row.part_id} if row.part_id else None,
        draft=row.draft, draft_revision=row.draft_revision,
        created_at=runtime.utc(row.created_at).isoformat(),
        expires_at=runtime.utc(row.expires_at).isoformat() if row.expires_at else None,
        assistant=(row.continuation or {}).get("assistant_request"),
    )


def _event(row: QuestionCheckpoint) -> dict:
    return {"userId": row.user_id, "session_id": row.session_id,
            "id": row.id, "request_id": row.id, "status": row.status}


async def checkpoint_context(db, row: QuestionCheckpoint, execution: SessionExecution, *, adopt=True):
    """Carry the original call/task through saved answers and worker changes."""
    from trajectory import enabled, record
    from trajectory.producers import activity_context
    if not enabled(row.user_id):
        return None
    stored = row.continuation.get("trace_context")
    context = await activity_context(
        db, row.user_id, row.session_id, saved=stored or execution.trace_context,
        message_id=row.message_id, part_id=row.part_id,
    )
    if context is None:
        return None
    if not context.turn_id:
        context = context.derive(turn_id=f"question:{row.id}")
    if not context.call_id and row.part_id:
        context = context.derive(call_id=generate_id())
    if not stored:
        if adopt:
            await record("baseline.captured", {
                "origin": "preexisting_question", "preexisting": True,
                "pending_questions": [_request(row).model_dump()],
            }, db=db, context=context, event_id=f"question_adopt:{row.id}")
        row.continuation = {**row.continuation, "trace_context": context.to_dict()}
    if not execution.trace_context:
        execution.trace_context = context.derive(call_id=None, part_id=None, request_id=None,
                                                 step_id=None).to_dict()
    return context


async def record_checkpoint(db, row: QuestionCheckpoint, execution: SessionExecution,
                            event_type: str, data: dict | None = None):
    from assistant.requests import emit_question_change
    await emit_question_change(db, row, event_type)
    from trajectory import record
    context = await checkpoint_context(db, row, execution, adopt=event_type != "question.asked")
    if context is not None:
        await record(event_type, {
            "question_id": row.id, "questions": row.questions,
            "status": row.status, "draft_revision": row.draft_revision,
            "expires_at": runtime.utc(row.expires_at).isoformat() if row.expires_at else None,
            **(data or {}),
        }, db=db, context=context,
            event_id=f"{event_type}:{row.id}:{row.draft_revision}")
        takeover = next((q.get("detail") for q in row.questions
                         if (q.get("detail") or {}).get("kind") == "desktop_takeover"), None)
        if takeover and event_type in {"question.asked", "question.resolved", "question.cancelled"}:
            terminal = event_type != "question.asked"
            await record("takeover.finished" if terminal else "takeover.requested", {
                "takeover_id": row.id, "question_id": row.id, "detail": takeover,
                "answers": row.answers if terminal else None,
                "status": row.status, "source_kind": "user_report" if terminal else "agent",
            }, db=db, context=context, event_id=f"takeover:{event_type}:{row.id}")
    return context


def validate_answers(questions: list[Question], answers: list[list[str]], *, partial: bool = False,
                     attachments: list[list[str]] | None = None) -> list[list[str]]:
    if not isinstance(answers, list) or any(
        not isinstance(values, list) or any(not isinstance(value, str) for value in values)
        for values in answers
    ):
        raise ValueError("Answers must be arrays of strings")
    if len(questions) != len(answers):
        raise ValueError("Provide one answer array per question")
    clean = []
    for index, (question, values) in enumerate(zip(questions, answers)):
        labels = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        has_files = question.allow_attachments and bool(attachments and attachments[index])
        if not partial and not labels and not has_files:
            raise ValueError("Answer every question before submitting")
        if not question.multiple and len(labels) > 1:
            raise ValueError("This question accepts only one answer")
        if any(len(label) > 5000 for label in labels) or len(labels) > 100:
            raise ValueError("Answer is too long")
        if not question.custom and any(label not in {o.label for o in question.options} for label in labels):
            raise ValueError("Choose one of the offered options")
        clean.append(labels)
    return clean


def normalize_attachments(questions: list[Question], attachments: list[list[str]] | None) -> list[list[str]]:
    if attachments is None:
        return [[] for _ in questions]
    if len(attachments) != len(questions):
        raise ValueError("Provide one attachment array per question")
    clean = []
    for question, ids in zip(questions, attachments):
        if not isinstance(ids, list) or any(not isinstance(a, str) or not 1 <= len(a) <= 64 for a in ids):
            raise ValueError("Attachments must be arrays of asset IDs")
        if ids and not question.allow_attachments:
            raise ValueError("This question does not accept attachments")
        if len(ids) != len(set(ids)):
            raise ValueError("Attachment IDs must be unique within a question")
        clean.append(ids)
    if sum(map(len, clean)) > 32:
        raise ValueError("At most 32 attachments are allowed")
    return clean


async def validate_attachment_ownership(db, session, attachments):
    from agent.inbox import InboxAttachmentError, _validate_owned_attachments_locked
    try:
        return await _validate_owned_attachments_locked(
            db, user_id=session.user_id, workspace_id=session.workspace_id,
            attachment_ids=list(dict.fromkeys(a for ids in attachments for a in ids)),
        )
    except InboxAttachmentError as exc:
        raise ValueError("An attachment is unavailable or does not belong to this workspace") from exc


async def ask(
    session_id: str, questions: list[Question], tool: dict | None = None,
    user_id: str = "default", *, continuation: dict | None = None,
    expires_at: datetime | None = None,
) -> list[list[str]]:
    if not 1 <= len(questions) <= 4:
        raise ValueError("Ask between 1 and 4 questions at once")
    continuation = continuation or {"kind": "question"}
    if continuation.get("kind") not in {"question", "plan_enter", "plan_exit", "memory_proposal", "memory_forget"}:
        raise ValueError("Unsupported question continuation")
    ticket = runtime.current_run.get()
    part_id = (tool or {}).get("callID")
    message_id = (tool or {}).get("messageID")
    # Ownership is checked below with the strict lease rule, as QuestionGone.
    async with runtime.transaction(session_id, user_id, fence=False) as (db, session, execution):
        if session.parent_id and session.kind != "cron":
            raise ValueError("Return this clarification to the parent agent; subagents cannot ask the user directly")
        if ticket and (ticket.session_id != session_id or ticket.user_id != user_id or not runtime.owns(execution, ticket)):
            raise QuestionGone("superseded")
        if not part_id or not message_id:
            raise ValueError("A durable question requires a persisted tool call to receive its answer")
        request = None
        if part_id:
            existing = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.part_id == part_id))
            if existing:
                if existing.user_id != user_id or existing.session_id != session_id:
                    raise KeyError("Question not found")
                if existing.status != "pending":
                    raise QuestionGone(existing.status)
                if existing.generation != execution.generation:
                    raise QuestionGone("superseded")
                if existing.message_id != message_id or existing.questions != [q.model_dump() for q in questions]:
                    raise QuestionConflict("An existing question's content cannot be replaced")
                request = _request(existing)
        if request is None:
            if continuation["kind"] in {"memory_proposal", "memory_forget"}:
                continuation = {**continuation, "workspace_id": session.workspace_id}
            part = await db.get(Part, part_id) if part_id else None
            if part_id and (part is None or part.session_id != session_id or part.user_id != user_id or part.message_id != message_id):
                raise KeyError("Question tool call not found")
            part_tool = (part.canonical_tool_id or part.data.get("tool")) if part else None
            if part and part_tool not in QUESTION_TOOL_CONTINUATIONS:
                raise ValueError("Question tools must be called directly, not inside a batch")
            if part and continuation["kind"] != QUESTION_TOOL_CONTINUATIONS[part_tool]:
                raise ValueError("Saved continuation does not match the question's tool")
            row = QuestionCheckpoint(
                id=generate_id(), session_id=session_id, user_id=user_id,
                generation=execution.generation, message_id=message_id, part_id=part_id,
                status="pending", questions=[q.model_dump() for q in questions],
                draft=[DraftAnswer().model_dump(exclude_none=True) for _ in questions], draft_revision=0,
                continuation=continuation, applied=False,
                created_at=runtime.now(), updated_at=runtime.now(), expires_at=expires_at,
            )
            db.add(row)
            await db.flush()
            from assistant.requests import bind_question
            await bind_question(db, session, execution, row)
            trace = await record_checkpoint(db, row, execution, "question.asked")
            if part:
                from question import surface
                await surface.prepare(db, session)
                part.data = {**part.data, "status": "waiting_input", "title": "Waiting for your answer",
                             "metadata": {**(part.data.get("metadata") or {}),
                                 "question_id": row.id, "question_status": "pending",
                                 "questions": ["确认是否忘记这条记忆。"
                                     if (q.detail or {}).get("kind") == "memory_forget" else q.question
                                     for q in questions]}}
                await surface.part_updated(db, session, part)
                if trace:
                    from trajectory import record
                    await record("part.committed", {"part": part.data}, db=db, context=trace)
            if not runtime.is_live(execution):
                session.status = "waiting_input"
            request = _request(row)
            from notifications.events import question_waiting
            await question_waiting(db, session, row)
    bus.publish("question.asked", {**request.model_dump(), "userId": user_id})
    if request.tool:
        async with get_db_session() as db:
            saved_part = await db.get(Part, request.tool["callID"])
            if saved_part:
                bus.publish("part.updated", {"userId": user_id, "sessionId": session_id,
                    "messageId": saved_part.message_id, "part": saved_part.data})
    raise QuestionSuspended(request.id)


async def _owned_request(request_id: str, user_id: str) -> QuestionCheckpoint:
    async with get_db_session() as db:
        row = await db.scalar(select(QuestionCheckpoint).join(Session, Session.id == QuestionCheckpoint.session_id).where(
            QuestionCheckpoint.id == request_id, QuestionCheckpoint.user_id == user_id,
            Session.user_id == user_id, Session.is_deleted == False,  # noqa: E712
        ))
        if row is None:
            raise KeyError("Question not found")
        from assistant.requests import validate_question_read
        await validate_question_read(db, row)
        return row


def _check_pending(row: QuestionCheckpoint, execution: SessionExecution) -> None:
    if row.generation != execution.generation:
        raise QuestionGone("superseded")
    if row.status != "pending":
        raise QuestionGone(row.status)
    if row.expires_at and runtime.utc(row.expires_at) <= runtime.now():
        raise QuestionGone("expired")


async def reply(request_id: str, answers: list[list[str]], user_id: str = "default", *,
                attachments: list[list[str]] | None = None, **reply_binding) -> dict:
    return await _resolve(request_id, user_id, answers, attachments=attachments, **reply_binding)


async def get_request(request_id: str, user_id: str) -> QuestionRequest:
    return _request(await _owned_request(request_id, user_id))


async def reject(request_id: str, user_id: str = "default", **reply_binding) -> dict:
    return await _resolve(request_id, user_id, None, **reply_binding)


async def _resolve(request_id: str, user_id: str, answers: list[list[str]] | None, *,
                   attachments: list[list[str]] | None = None, **reply_binding) -> dict:
    owned = await _owned_request(request_id, user_id)
    from assistant.requests import maybe_reply, question_task
    receipt = await maybe_reply(owned, answers=answers, attachments=attachments, **reply_binding)
    if receipt is not None:
        return receipt
    async with runtime.transaction(owned.session_id, user_id, fence=False) as (db, session, execution):
        row = await db.get(QuestionCheckpoint, request_id)
        if await question_task(db, row) is not None:
            raise QuestionConflict("Question scope changed; reload before replying")
        changed = await resolve_locked(db, session, execution, row, answers=answers, attachments=attachments)
        if not changed:
            return {"ok": True, "status": row.status, "session_id": row.session_id}
        session_status = session.status
    bus.publish("question.rejected" if answers is None else "question.replied", _event(row))
    await runtime.publish_status(row.session_id, user_id, session_status)
    return {"ok": True, "status": row.status, "session_id": row.session_id}


async def resolve_locked(db, session, execution, row, *, answers, attachments=None):
    """Shared question mutation; caller owns the Session transaction."""
    status = "rejected" if answers is None else "answered"
    questions = [Question(**q) for q in row.questions]
    files = normalize_attachments(questions, attachments)
    clean = None if answers is None else validate_answers(questions, answers, attachments=files)
    if row.generation != execution.generation:
        raise QuestionGone("superseded")
    if row.status in ("answered", "rejected"):
        if (row.status != status or row.answers != clean
                or row.continuation.get("answer_attachments", [[] for _ in questions]) != files):
            raise QuestionConflict("An answer has already been accepted")
        return False
    _check_pending(row, execution)
    if any(files) and row.continuation.get("kind") != "question":
        raise ValueError("This confirmation does not accept attachments")
    await validate_attachment_ownership(db, session, files)
    await checkpoint_context(db, row, execution)
    if any(files):
        row.continuation = {**row.continuation, "answer_attachments": files}
    row.status, row.answers, row.updated_at = status, clean, runtime.now()
    await record_checkpoint(db, row, execution, "question.resolved",
                            {"answers": clean, "attachments": files, "decision": status, "source_kind": "user"})
    from notifications.events import cancel_event
    await cancel_event(db, row.user_id, f"question:{row.id}")
    execution.resume_pending = True
    execution.resume_error = None
    execution.next_attempt_at = None
    execution.updated_at = runtime.now()
    await db.flush()
    if not runtime.is_live(execution):
        session.status = await runtime.waiting_status(db, execution)
    return True


async def save_draft(request_id: str, draft: list[DraftAnswer], revision: int, user_id: str = "default") -> QuestionRequest:
    owned = await _owned_request(request_id, user_id)
    async with runtime.transaction(owned.session_id, user_id, fence=False) as (db, session, execution):
        row = await db.get(QuestionCheckpoint, request_id)
        from assistant.requests import validate_question_read
        await validate_question_read(db, row)
        _check_pending(row, execution)
        if revision != row.draft_revision:
            raise QuestionConflict("Draft changed in another tab; reload before saving")
        questions = [Question(**q) for q in row.questions]
        if len(draft) != len(questions):
            raise ValueError("Provide one draft per question")
        for question, item in zip(questions, draft):
            if item.custom and not question.custom:
                raise ValueError("Free text is not allowed for this question")
            validate_answers([question.model_copy(update={"custom": False})], [item.selected], partial=True)
        files = normalize_attachments(questions, [item.attachments or [] for item in draft])
        await validate_attachment_ownership(db, session, files)
        await checkpoint_context(db, row, execution)
        row.draft = [item.model_dump(exclude_none=True) for item in draft]
        row.draft_revision += 1
        row.updated_at = runtime.now()
        await record_checkpoint(db, row, execution, "question.draft_saved", {"draft": row.draft})
        request = _request(row)
    bus.publish("question.updated", {**request.model_dump(), "userId": user_id})
    return request


async def list_pending(user_id: str) -> list[QuestionRequest]:
    async with get_db_session() as db:
        rows = (await db.scalars(select(QuestionCheckpoint)
            .join(SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id)
            .join(Session, Session.id == QuestionCheckpoint.session_id)
            .where(QuestionCheckpoint.user_id == user_id, Session.user_id == user_id,
                   Session.is_deleted == False,  # noqa: E712
                   QuestionCheckpoint.status == "pending",
                   QuestionCheckpoint.generation == SessionExecution.generation)
            .order_by(QuestionCheckpoint.created_at))).all()
        from assistant.requests import validate_question_read
        from assistant.policy import AssistantError
        visible = []
        for row in rows:
            if row.expires_at and runtime.utc(row.expires_at) <= runtime.now():
                continue
            try:
                await validate_question_read(db, row)
            except (AssistantError, QuestionGone):
                continue
            visible.append(_request(row))
        return visible
