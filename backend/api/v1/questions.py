"""``/v1/sessions/{id}/questions/{qid}``: answer or reject a confirmation card."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.v1.deps import load_session_row, require_scope
from api.v1.errors import ApiError
from api.v1.ids import internal_id
from question import question as q_mod

router = APIRouter(prefix="/sessions/{session_id}/questions", tags=["questions"])


class AnswerBody(BaseModel):
    answers: list[list[str]]


async def _resolve(session_id: str, question_id: str, identity: dict, answers: list[list[str]] | None) -> dict:
    row = await load_session_row(session_id, identity, write=True)
    request_id = internal_id(question_id, "question")
    try:
        request = await q_mod.get_request(request_id, row.user_id)
    except KeyError:
        raise ApiError(404, "NOT_FOUND", "Question not found")
    if request.session_id != row.id:
        raise ApiError(404, "NOT_FOUND", "Question not found in this session")
    try:
        if answers is None:
            await q_mod.reject(request_id, user_id=row.user_id)
        else:
            await q_mod.reply(request_id, answers, user_id=row.user_id)
    except KeyError:
        raise ApiError(404, "NOT_FOUND", "Question not found")
    except (q_mod.QuestionGone, q_mod.QuestionConflict) as exc:
        raise ApiError(409, "INTERACTION_RESOLVED", str(exc)) from exc
    except ValueError as exc:
        raise ApiError(400, "INVALID_REQUEST", str(exc)) from exc
    return {"ok": True}


@router.post("/{question_id}")
async def answer_question(
    session_id: str,
    question_id: str,
    body: AnswerBody,
    identity: dict = Depends(require_scope("sessions:write")),
):
    """Submit one label array per question, in card order."""
    return await _resolve(session_id, question_id, identity, body.answers)


@router.post("/{question_id}/reject")
async def reject_question(
    session_id: str,
    question_id: str,
    identity: dict = Depends(require_scope("sessions:write")),
):
    """Decline the card; the agent stops and explains, and a new message resumes."""
    return await _resolve(session_id, question_id, identity, None)
