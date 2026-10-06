"""The personal assistant answers ordinary questions for the user (V2 D6).

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 10: a question an agent asks in one of
the user's own conversations may be answered by their personal assistant; the
conversation shows "由个人助理代答". Decisions stay with the user, who gets a
link instead: permission approvals, plan reviews, memory confirmations,
resource selections and desktop takeovers. Answering in a workspace-visible
conversation is shown to the user on a card first (D4).
"""
from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.part import Part
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session

#: Tools whose questions the assistant may answer. Others ask for a decision
#: or an action only the user can give.
ANSWERABLE_TOOLS = frozenset({"question"})
ANSWERED_BY = "assistant"


def link(session_id: str) -> str:
    return f"/app/s/{session_id}"


def human_only(session_id: str, reason: str) -> AssistantError:
    return AssistantError(403, "ASSISTANT_ANSWER_HUMAN_ONLY",
        f"{reason} Only the user can answer it: tell them and give the link {link(session_id)}")


async def answerable(db, row: QuestionCheckpoint, session: Session) -> str | None:
    """Why the user must answer this question themselves, or None."""
    if (row.continuation or {}).get("kind") != "question":
        return "It is an approval or a confirmation, not an ordinary question."
    tool = None
    if row.part_id:
        part = await db.get(Part, row.part_id)
        if part is not None and part.session_id == session.id:
            tool = part.canonical_tool_id or (part.data or {}).get("tool")
    if tool not in ANSWERABLE_TOOLS:
        return "It asks the user for a decision or an action."
    for question in row.questions or []:
        if question.get("allow_attachments"):
            return "It asks the user to choose files."
        if (question.get("detail") or {}).get("kind"):
            return "It is a request for the user's own action."
    return None


async def _target(db, ctx, request_id: str):
    row = await db.get(QuestionCheckpoint, request_id)
    session = await db.get(Session, row.session_id) if row is not None else None
    if (row is None or session is None or row.user_id != ctx.user_id or session.user_id != ctx.user_id
            or session.workspace_id != ctx.workspace_id or session.is_deleted or session.kind == "assistant"
            or session.id == ctx.session_id):
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Question is unavailable")
    execution = await db.get(SessionExecution, row.session_id)
    if row.status != "pending" or execution is None or row.generation != execution.generation:
        raise AssistantError(410, "ASSISTANT_REQUEST_UNAVAILABLE", "The question is no longer waiting for an answer")
    return row, session


def _answers_text(questions, answers) -> str:
    return "\n".join(f"{question.get('question', '')}\n→ {'、'.join(values)}"
                     for question, values in zip(questions, answers))


async def answer_question(ctx, *, request_id: str, answers: list[list[str]]) -> dict:
    from question import question as questions
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        row, session = await _target(db, ctx, request_id)
        reason = await answerable(db, row, session)
        if reason:
            raise human_only(session.id, reason)
        try:
            clean = questions.validate_answers([questions.Question(**item) for item in row.questions], answers)
        except ValueError as exc:
            raise AssistantError(400, "ASSISTANT_ANSWER_INVALID", str(exc)) from exc
        title, visibility, items = session.title or "", session.visibility, list(row.questions)
    if visibility == "workspace":
        from assistant.confirmations import require_card
        # Members can read this conversation: the user sees the exact answer first.
        await require_card(ctx, digest=command_digest({"answer": request_id, "answers": clean}),
            prompt=(f"以你的名义在工作区可见的会话「{title}」里回答下面的问题吗？工作区成员都能看到。\n\n"
                    + _answers_text(items, clean)),
            header="确认代答", description="由个人助理代你回答", confirm="确认代答",
            target={"request_id": request_id})
    answered_by = {"kind": ANSWERED_BY, "main_session_id": ctx.session_id, "part_id": ctx.part_id}
    from assistant.requests import assistant_answer
    receipt = await assistant_answer(row, clean, answered_by=answered_by)
    return {"state": "answered", "request_id": request_id, "session_id": session.id, "session_title": title,
            "answers": clean, "link": link(session.id),
            "note": "The conversation shows this as answered by the personal assistant (由个人助理代答) and continues.",
            **({"command_id": receipt["command_id"]} if receipt and receipt.get("command_id") else {})}


async def list_waiting(*, user_id: str, workspace_id: str, main_id: str, limit: int = 30) -> list[dict]:
    """Pending questions across all the user's own conversations, newest first."""
    from assistant.task_context import _iso
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = (await db.execute(select(QuestionCheckpoint, Session, Project.name).join(
            Session, Session.id == QuestionCheckpoint.session_id).join(
            SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).outerjoin(
            Project, Project.id == Session.project_id).where(
            QuestionCheckpoint.user_id == user_id, QuestionCheckpoint.status == "pending",
            QuestionCheckpoint.generation == SessionExecution.generation,
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
            Session.kind != "assistant", Session.id != main_id)
            .order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()).limit(limit))).all()
        items = []
        for row, session, project_name in rows:
            reason = await answerable(db, row, session)
            items.append({
                "kind": "question", "id": row.id, "session_id": session.id,
                "session_title": session.title or "", "project_id": session.project_id,
                "project_name": project_name, "visibility": session.visibility,
                "asked_at": _iso(row.created_at), "link": link(session.id),
                "questions": [{"header": item.get("header", ""), "question": str(item.get("question", ""))[:600],
                               "options": [option.get("label") for option in item.get("options") or []],
                               "multiple": bool(item.get("multiple")), "custom": item.get("custom", True)}
                              for item in row.questions or []],
                "assistant_may_answer": reason is None,
                **({"user_only_reason": reason} if reason else {}),
            })
        return items
