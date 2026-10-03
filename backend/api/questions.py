"""Question routes."""
from fastapi import APIRouter, Depends, HTTPException
from auth.middleware import get_current_user
from pydantic import BaseModel, Field

from question import question as q_mod
from assistant.policy import AssistantError

router = APIRouter(dependencies=[Depends(get_current_user)])


class QuestionReplyBinding(BaseModel):
    reply_id: str | None = Field(default=None, min_length=1, max_length=64)
    expected_request_revision: str | None = Field(default=None, min_length=64, max_length=64)
    options_hash: str | None = Field(default=None, min_length=64, max_length=64)
    source_ref: dict | None = None

    def binding(self):
        return {key: getattr(self, key) for key in QuestionReplyBinding.model_fields}


class QuestionReplyBody(QuestionReplyBinding):
    answers: list[list[str]] = Field(min_length=1, max_length=4)
    attachments: list[list[str]] | None = Field(default=None, min_length=1, max_length=4)


class QuestionDraftBody(BaseModel):
    draft: list[q_mod.DraftAnswer] = Field(min_length=1, max_length=4)
    revision: int = Field(ge=0)


async def _question_action(action):
    try:
        return await action
    except LookupError:
        raise HTTPException(404, "Question request not found")
    except PermissionError:
        raise HTTPException(403, "Question request does not belong to current user")
    except AssistantError as exc:
        raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
    except q_mod.QuestionGone as exc:
        raise HTTPException(410, {"code": "QUESTION_GONE", "status": exc.status})
    except q_mod.QuestionConflict as exc:
        raise HTTPException(409, {"code": "QUESTION_CONFLICT", "message": str(exc)})
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@router.get("/question")
async def list_questions(current_user: dict = Depends(get_current_user)):
    """List all pending questions."""
    user_id = current_user["user_id"]
    return await q_mod.list_pending(user_id=user_id)


@router.post("/question/{request_id}")
async def reply_question(
    request_id: str,
    body: QuestionReplyBody,
    current_user: dict = Depends(get_current_user),
):
    """Reply to a question from the AI."""
    user_id = current_user["user_id"]
    return await _question_action(q_mod.reply(request_id, body.answers, user_id=user_id,
                                              attachments=body.attachments, **body.binding()))


@router.get("/question/{request_id}")
async def get_question(request_id: str, current_user: dict = Depends(get_current_user)):
    return await _question_action(q_mod.get_request(request_id, current_user["user_id"]))


@router.post("/question/{request_id}/reject")
async def reject_question(request_id: str, current_user: dict = Depends(get_current_user),
                          body: QuestionReplyBinding | None = None):
    """Reject/dismiss a question from the AI."""
    user_id = current_user["user_id"]
    return await _question_action(q_mod.reject(request_id, user_id=user_id,
        **(body.binding() if body else {})))


@router.put("/question/{request_id}/draft")
async def save_question_draft(request_id: str, body: QuestionDraftBody,
                              current_user: dict = Depends(get_current_user)):
    return await _question_action(q_mod.save_draft(request_id, body.draft, body.revision,
                                                  user_id=current_user["user_id"]))
