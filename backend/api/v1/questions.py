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
    if request.status != "pending":
        # The contract answers every repeat with 409, including a retry of
        # the same answer; the caller refreshes the card's status instead.
        raise ApiError(409, "INTERACTION_RESOLVED", f"Question is already {request.status}")
    from billing.credit_cap import CreditCapExceeded
    from billing.plan_quote import PlanInsufficientCredits, gate_answer

    try:
        if answers is None:
            await q_mod.reject(request_id, user_id=row.user_id)
        else:
            # The same gate runs inside reply(); checking here first keeps the
            # partner's refusal independent of how the card is then resolved.
            await gate_answer(row.id, getattr(request, "questions", None) or [], answers)
            await q_mod.reply(request_id, answers, user_id=row.user_id)
    except KeyError:
        raise ApiError(404, "NOT_FOUND", "Question not found")
    except (q_mod.QuestionGone, q_mod.QuestionConflict) as exc:
        raise ApiError(409, "INTERACTION_RESOLVED", str(exc)) from exc
    except PlanInsufficientCredits as exc:
        # Confirming the shots + price card commits to the whole plan: the
        # balance must cover it now, not just be above zero.
        raise ApiError(402, exc.code, str(exc), details={
            "required": _credits(exc.required), "available": _credits(exc.available),
            "shortfall": _credits(exc.shortfall)}) from exc
    except CreditCapExceeded as exc:
        raise ApiError(402, exc.code, str(exc), details={
            "step": exc.step, "expected": _credits(exc.expected), "spent": _credits(exc.spent),
            "max_credits": _credits(exc.cap)}) from exc
    except ValueError as exc:
        raise ApiError(400, "INVALID_REQUEST", str(exc)) from exc
    return {"ok": True}


def _credits(value) -> str:
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


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
