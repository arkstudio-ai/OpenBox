"""Question tool: LLM asks user structured questions."""
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tool.tool import ToolResult, ToolContext, define_tool

QUESTION_DESCRIPTION = """\
Use this tool when you need to ask the user questions during execution. This allows you to:
1. Gather user preferences or requirements
2. Clarify ambiguous instructions
3. Get decisions on implementation choices as you work
4. Offer choices to the user about what direction to take.

Usage notes:
- When `custom` is enabled (default), a "Type your own answer" option is added automatically; don't include "Other" or catch-all options
- Answers are returned as arrays of labels; set `multiple: true` to allow selecting more than one
- Set `allow_attachments: true` on questions asking for pictures, video, audio or documents. The card then offers resource-library selection and local upload; attached assets arrive with the answer. A file-only answer is valid. Do not ask the user to paste file paths instead.
- Ask related independent questions together in one call (1-4 questions); do not put this tool inside batch
- Recommendations are suggestions, not user answers. Never label an option as already selected or treat a default as submitted
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label
- When asking the user to approve or review a draft, script or plan, set purpose="review" and put the complete current proposal in review_content. The card must show what is being reviewed; "confirm this version" is not a proposal. After a revision, show the newly revised content, never assume an older draft is the new version.
- For a revision, change only the requested parts of the exact previous proposal. Preserve all unaffected wording, timing, materials and constraints. The server checks revised cards before displaying them; correct any reported mismatch instead of asking the user to accept an unintended change.
- Use purpose="clarification" only to collect missing information before a proposal can be written. Reviewing a script does not authorize generating media or spending; classify consequential approval separately with requires_confirmation.\
"""


class QuestionOption(BaseModel):
    label: str
    description: str = ""


class QuestionItem(BaseModel):
    purpose: Literal["clarification", "review"] = Field(default="clarification",
        description="clarification collects missing information; review asks the user to assess the proposal supplied in review_content")
    review_content: str | None = Field(default=None, max_length=24000,
        description="For purpose=review, the full current draft/script/plan shown in this card, including its conditions. Do not refer to an unseen or superseded version.")
    requires_confirmation: bool = Field(default=False, description=(
        "True when answering authorizes a consequential action such as payment, publication, account access or deletion. "
        "An assistant answering on the user's behalf must then obtain confirmation of the exact answer. "
        "Classify the consequence, not keywords in the question."))
    question: str
    header: str = ""
    options: list[QuestionOption] = []
    multiple: bool = False
    custom: bool = Field(default=True, description="Allow free-text answers; set false to require one of the offered options")
    allow_attachments: bool = Field(default=False, description="Offer resource-library selection and local upload for this question")

    @model_validator(mode="after")
    def review_is_visible(self):
        if self.purpose == "review" and not (self.review_content or "").strip():
            raise ValueError("A review must include the complete current proposal in review_content")
        if self.purpose != "review" and self.review_content is not None:
            raise ValueError("Set purpose=review when supplying review_content")
        return self


class QuestionArgs(BaseModel):
    questions: list[QuestionItem] = Field(min_length=1, max_length=4, description="1-4 questions to ask the user together in one card")


async def _interaction_timeout(session_id: str) -> int | None:
    if not session_id:
        return None
    try:
        from auth.api_key import interaction_timeout_for_session

        return await interaction_timeout_for_session(session_id)
    except Exception:
        # Desktop mode has no api_keys table; a card without a deadline is
        # the historical behaviour there.
        return None


async def execute(args: QuestionArgs, ctx: ToolContext) -> ToolResult:
    """Ask the user questions and wait for answers."""
    from question.question import ask, Question as QModel, QuestionOption as QOpt

    from assistant.review_guard import guard_review
    rejection = await guard_review(args, ctx)
    if rejection is not None:
        return rejection

    questions = [
        QModel(
            question=(q.review_content.strip() + "\n\n" + q.question) if q.purpose == "review" else q.question,
            header=q.header,
            options=[QOpt(label=o.label, description=o.description)
                     for o in q.options],
            multiple=q.multiple,
            custom=q.custom,
            allow_attachments=q.allow_attachments,
            requires_confirmation=q.requires_confirmation,
        )
        for q in args.questions
    ]

    # Unattended callers (API keys) get a deadline from their key policy;
    # an expired card counts as rejected and the run carries on.
    expires_at = None
    timeout = await _interaction_timeout(ctx.session_id)
    if timeout:
        from datetime import datetime, timedelta, timezone

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=timeout)

    answers = await ask(
        session_id=ctx.session_id,
        questions=questions,
        tool={"messageID": ctx.message_id, "callID": ctx.part_id} if ctx.part_id else None,
        user_id=ctx.user_id or "default",
        expires_at=expires_at,
    )

    def format_answer(answer):
        if not answer:
            return "Unanswered"
        return ", ".join(answer)

    formatted = ", ".join(
        f'"{q.question}"="{format_answer(answers[i] if i < len(answers) else None)}"'
        for i, q in enumerate(questions)
    )

    return ToolResult(
        title=f"Asked {len(args.questions)} question{'s' if len(args.questions) > 1 else ''}",
        output=f"User has answered your questions: {formatted}. You can now continue with the user's answers in mind.",
        # Both halves, so the conversation can show what was asked next to what
        # was chosen. Reconstructing the pairing from the output string means
        # parsing quotes back out of prose the model also reads.
        metadata={
            "answers": answers,
            "questions": [q.question for q in questions],
        },
    )


question_tool = define_tool(
    "question",
    description=QUESTION_DESCRIPTION,
    parameters=QuestionArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
)
