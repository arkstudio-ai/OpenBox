"""Creator-context tool: persona assembly and memory writes for skills.

Ported from bossip's creator-context MCP server, reshaped as a native
native tool. Identity always comes from ToolContext — there is no
user id argument, which removes the impersonation surface the MCP
version had to guard with token plumbing.

The propose→confirm channel is synchronous here: bossip parked proposals
as PENDING_NOTE rows for a later web confirmation, while OpenBox has
interactive approval cards, so `propose_memory` writes the pending row
and immediately asks the user. A dismissed card leaves the row pending —
and PENDING_NOTE rows never enter assembled context.
"""
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from core.log import create_logger
from memory import service as memory_service
from memory.context import assemble_user_context
from question import question as question_mod
from question.question import Question, QuestionOption, QuestionRejectedError
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.creator_context")

CREATOR_CONTEXT_DESCRIPTION = """Read the current creator's persona and memories.
Get context before drafting; boundaries are hard constraints. Propose one stable
fact through a confirmation card; USER_NOTE cannot be written directly. Other
direct writes are CANDIDATE typed or short-lived impressions. Data never crosses users."""


class CreatorContextArgs(BaseModel):
    action: Literal[
        "get_user_context",
        "write_memory",
        "propose_memory",
        "search_memories",
        "list_active_memories",
    ]
    # write_memory
    scope: Literal["SHORT_TERM", "LONG_TERM"] | None = None
    type: str | None = Field(default=None, max_length=32)
    value: dict | None = None
    owner: Literal["USER_CONFIRMED", "SYSTEM_INFERRED", "OPERATOR_CONFIRMED"] | None = None
    confidence: int | None = Field(default=None, ge=0, le=100)
    evidence: dict | None = None
    ttl_seconds: int | None = Field(default=None, gt=0)
    # propose_memory
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    # search_memories
    status: Literal["CANDIDATE", "ACTIVE", "EXPIRED", "DEPRECATED"] | None = None
    limit: int = Field(default=20, ge=1, le=100)
    # get_user_context
    volatile_limit: int = Field(default=5, ge=0, le=20)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action == "write_memory":
            missing = [
                name
                for name, val in (
                    ("scope", self.scope),
                    ("type", self.type),
                    ("value", self.value),
                    ("owner", self.owner),
                )
                if not val
            ]
            if missing:
                raise ValueError(f"write_memory requires: {', '.join(missing)}")
            if self.type in {
                memory_service.PENDING_NOTE_TYPE,
                memory_service.USER_NOTE_TYPE,
            }:
                raise ValueError(
                    f"{self.type} cannot be written directly; use propose_memory"
                )
        if self.action == "propose_memory" and not self.summary:
            raise ValueError("propose_memory requires summary")
        return self


def _dump(rows: Any) -> str:
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


async def _handle_proposal(args: CreatorContextArgs, ctx: ToolContext) -> ToolResult:
    user_id = ctx.user_id or "default"
    proposal = await memory_service.propose_note(
        user_id=user_id,
        workspace_id=ctx.workspace_id or None,
        project_id=ctx.project_id or None,
        summary=args.summary or "",
        session_id=ctx.session_id or None,
    )
    try:
        answers = await question_mod.ask(
            session_id=ctx.session_id,
            user_id=user_id,
            questions=[
                Question(
                    question=f"要我记住这条吗?\n「{args.summary}」",
                    header="记忆确认",
                    options=[
                        QuestionOption(label="记住", description="确认保存为长期记忆"),
                        QuestionOption(label="不用记", description="不保存,且不再提议这条"),
                    ],
                    multiple=False,
                    custom=True,
                    detail={
                        "kind": "memory_proposal",
                        "summary": args.summary,
                        "memory_id": proposal["id"],
                    },
                )
            ],
            tool={"messageID": ctx.message_id, "callID": ctx.part_id}
            if ctx.part_id
            else None,
        )
    except QuestionRejectedError:
        return ToolResult(
            title="Memory proposal parked",
            output=(
                "The user dismissed the card without deciding. The memory was NOT "
                "saved and stays pending; do not treat it as remembered and do not "
                "re-propose it in this conversation."
            ),
            metadata={"memory_id": proposal["id"], "decision": "dismissed"},
        )

    answer = (answers[0][0] if answers and answers[0] else "").strip()
    if answer == "记住":
        confirmed = await memory_service.confirm_note(
            user_id=user_id, workspace_id=ctx.workspace_id or None,
            proposal_id=proposal["id"]
        )
        return ToolResult(
            title="Memory saved",
            output=f"Saved as a long-term memory: {args.summary}",
            metadata={"memory": confirmed, "decision": "confirmed"},
        )
    if answer == "不用记":
        await memory_service.reject_note(
            user_id=user_id, workspace_id=ctx.workspace_id or None,
            proposal_id=proposal["id"]
        )
        return ToolResult(
            title="Memory rejected",
            output="The user declined. Do not save this and do not propose it again.",
            metadata={"memory_id": proposal["id"], "decision": "rejected"},
        )
    # Custom text: the user rephrased the memory — confirm with their wording.
    confirmed = await memory_service.confirm_note(
        user_id=user_id, workspace_id=ctx.workspace_id or None,
        proposal_id=proposal["id"], edited_summary=answer
    )
    return ToolResult(
        title="Memory saved (edited)",
        output=f"Saved with the user's wording: {answer}",
        metadata={"memory": confirmed, "decision": "confirmed_edited"},
    )


async def execute_creator_context(args: CreatorContextArgs, ctx: ToolContext) -> ToolResult:
    user_id = ctx.user_id or "default"
    project_id = ctx.project_id or None

    if args.action == "get_user_context":
        assembled = await assemble_user_context(
            user_id=user_id, workspace_id=ctx.workspace_id or None,
            project_id=project_id, volatile_limit=args.volatile_limit
        )
        if not assembled["context"]:
            return ToolResult(
                title="No creator context yet",
                output=(
                    "No persona or memories are stored for this user yet. Proceed "
                    "without persona assumptions; propose_memory when the user states "
                    "stable facts about themselves."
                ),
                metadata={"stats": assembled["stats"]},
            )
        return ToolResult(
            title="Creator context",
            output=assembled["context"],
            metadata={"stats": assembled["stats"]},
        )

    if args.action == "write_memory":
        row = await memory_service.write_memory(
            user_id=user_id,
            workspace_id=ctx.workspace_id or None,
            project_id=project_id,
            scope=args.scope or "SHORT_TERM",
            type=args.type or "",
            value=args.value or {},
            owner=args.owner or "SYSTEM_INFERRED",
            confidence=args.confidence if args.confidence is not None else 50,
            evidence={**(args.evidence or {}), "session_id": ctx.session_id or None},
            ttl_seconds=args.ttl_seconds,
        )
        return ToolResult(
            title="Memory written (candidate)",
            output=_dump(row),
            metadata={"memory": row},
        )

    if args.action == "propose_memory":
        return await _handle_proposal(args, ctx)

    if args.action == "search_memories":
        rows = await memory_service.search_memories(
            user_id=user_id,
            workspace_id=ctx.workspace_id or None,
            project_id=project_id,
            type=args.type,
            scope=args.scope,
            status=args.status,
            limit=args.limit,
        )
        return ToolResult(
            title=f"Memories ({len(rows)})",
            output=_dump(rows),
            metadata={"count": len(rows)},
        )

    if args.action == "list_active_memories":
        rows = await memory_service.list_active_memories(
            user_id=user_id, workspace_id=ctx.workspace_id or None,
            project_id=project_id
        )
        return ToolResult(
            title=f"Active memories ({len(rows)})",
            output=_dump(rows),
            metadata={"count": len(rows)},
        )

    return ToolResult(title="Unknown action", output=f"Unsupported action: {args.action}")


creator_context_tool = define_tool(
    "creator_context",
    description=CREATOR_CONTEXT_DESCRIPTION,
    parameters=CreatorContextArgs,
    execute=execute_creator_context,
    sandbox_required=False,
    parallel_safe=False,
)
