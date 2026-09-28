"""Question tool: LLM asks user structured questions."""
from pydantic import BaseModel, Field

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
- Ask related independent questions together in one call (1-4 questions); do not put this tool inside batch
- Recommendations are suggestions, not user answers. Never label an option as already selected or treat a default as submitted
- If you recommend a specific option, make that the first option in the list and add "(Recommended)" at the end of the label\
"""


class QuestionOption(BaseModel):
    label: str
    description: str = ""


class QuestionItem(BaseModel):
    question: str
    header: str = ""
    options: list[QuestionOption] = []
    multiple: bool = False
    #: False restricts the answer to the listed options (a price quote, a
    #: yes/no gate); the default keeps the "type your own answer" choice.
    custom: bool = True


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

    questions = [
        QModel(
            question=q.question,
            header=q.header,
            options=[QOpt(label=o.label, description=o.description) for o in q.options],
            multiple=q.multiple,
            custom=q.custom,
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
        for i, q in enumerate(args.questions)
    )

    return ToolResult(
        title=f"Asked {len(args.questions)} question{'s' if len(args.questions) > 1 else ''}",
        output=f"User has answered your questions: {formatted}. You can now continue with the user's answers in mind.",
        # Both halves, so the conversation can show what was asked next to what
        # was chosen. Reconstructing the pairing from the output string means
        # parsing quotes back out of prose the model also reads.
        metadata={
            "answers": answers,
            "questions": [q.question for q in args.questions],
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
