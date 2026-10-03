"""Creator-context tool: persona assembly and memory writes for skills.

Ported from bossip's creator-context MCP server, reshaped as a native
native tool. Identity always comes from ToolContext — there is no
user id argument, which removes the impersonation surface the MCP
version had to guard with token plumbing.

The propose→confirm channel uses durable human input: bossip parked proposals
as PENDING_NOTE rows for a later web confirmation, while OpenBox has
interactive approval cards, so `propose_memory` writes the pending row
and immediately asks the user. A dismissed card leaves the row pending —
and PENDING_NOTE rows never enter assembled context.
"""
import json
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from core.log import create_logger
from memory import service as memory_service
from memory.transient_tools import LEGACY_READ_ACTIONS
from question import question as question_mod
from question.question import Question, QuestionOption, QuestionRejectedError
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.creator_context")

CREATOR_CONTEXT_DESCRIPTION = """Read the current creator's persona and memories.
Get context before drafting; boundaries are hard constraints. What the user says
about themselves is verified by background memory processing in automatic mode,
which needs no write_memory call. To keep content you produced (a plan, summary
or draft) or found in a file when the user asks you to remember it, call
propose_memory with a concise self-contained summary; the user confirms it on a
card. Do not ask users to manage a review queue. USER_NOTE cannot be written directly. write_memory
requires value.summary: a concise statement supported by the user's input.
Preserve its subject, relationship, conditions and scope; ownership of a memory
does not identify its semantic subject. Do not infer additional relationships
or turn a temporary task request into a durable preference.
Model-supplied owner never grants confirmation. Data never crosses users."""


class CreatorMemoryValue(BaseModel):
    model_config = ConfigDict(extra="allow")
    summary: str = Field(min_length=1, max_length=2000,
        description="Required concise memory statement from the user's input, e.g. 用户偏好简短中文回复。Additional structured fields may accompany it.")

    @field_validator("summary")
    @classmethod
    def nonempty_summary(cls, value):
        if not value.strip():
            raise ValueError("value.summary must not be blank")
        return value


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
    value: CreatorMemoryValue | None = Field(default=None,
        description="Required for write_memory. Include the mandatory summary field; structured details are optional.")
    owner: Literal["USER_CONFIRMED", "SYSTEM_INFERRED", "OPERATOR_CONFIRMED"] | None = Field(default=None,
        description="Compatibility metadata only; it never grants authority. Automatic mode verifies the original user statement in the background.")
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
            # Classification metadata grants no authority. A concise summary
            # is enough; avoid a failed tool round trip for optional metadata.
            self.scope = self.scope or "LONG_TERM"
            self.type = self.type or "REFERENCE"
            self.owner = self.owner or "SYSTEM_INFERRED"
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
    from memory.settings import saving_paused
    if await saving_paused(user_id, ctx.session_id or None):
        return ToolResult(title="Saving paused", output="The user turned memory saving off for this chat or for "
                          "good. Nothing was saved: tell them saving is paused and can be turned back on.",
                          metadata={"decision": "paused"})
    try:
        proposal = await memory_service.propose_note(
            user_id=user_id,
            workspace_id=ctx.workspace_id or None,
            project_id=ctx.project_id or None,
            summary=args.summary or "",
            session_id=ctx.session_id or None,
        )
    except memory_service.MemorySensitiveContent:
        return ToolResult(title="Not saved", output="Memory never keeps passwords, identity or card numbers, phone "
                          "numbers, email addresses or house numbers. Nothing was saved: tell the user you won't keep "
                          "it, for their safety, and offer to save the rest without it.",
                          metadata={"decision": "sensitive"})
    if proposal["status"] == "ACTIVE":
        return ToolResult(title="Memory already confirmed", output="This fact is already saved as a confirmed memory.",
                          metadata={"memory": proposal, "decision": "already_confirmed"})
    decision_key = sha256(f"{ctx.session_id}|{ctx.part_id}|{proposal['id']}".encode()).hexdigest()
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
            continuation={"kind": "memory_proposal", "memory_id": proposal["id"],
                          "workspace_id": proposal.get("workspace_id") or ctx.workspace_id,
                          "expected_revision": proposal["revision"]},
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
            proposal_id=proposal["id"], expected_revision=proposal["revision"],
            request_id=f"tool-confirm:{decision_key}"
        )
        return ToolResult(
            title="Memory saved",
            output=f"Saved as a long-term memory: {args.summary}",
            metadata={"memory": confirmed, "decision": "confirmed"},
        )
    if answer == "不用记":
        await memory_service.reject_note(
            user_id=user_id, workspace_id=ctx.workspace_id or None,
            proposal_id=proposal["id"], expected_revision=proposal["revision"],
            request_id=f"tool-reject:{decision_key}"
        )
        return ToolResult(
            title="Memory rejected",
            output="The user declined. Do not save this and do not propose it again.",
            metadata={"memory_id": proposal["id"], "decision": "rejected"},
        )
    # Custom text: the user rephrased the memory — confirm with their wording.
    confirmed = await memory_service.confirm_note(
        user_id=user_id, workspace_id=ctx.workspace_id or None,
        proposal_id=proposal["id"], edited_summary=answer, expected_revision=proposal["revision"],
        request_id=f"tool-confirm:{decision_key}"
    )
    return ToolResult(
        title="Memory saved (edited)",
        output=f"Saved with the user's wording: {answer}",
        metadata={"memory": confirmed, "decision": "confirmed_edited"},
    )


async def _read(args: CreatorContextArgs, ctx: ToolContext) -> ToolResult:
    from memory.context import legacy_read
    from tool.memory_tools import _transient_boundary

    arguments = args.model_dump(include={"action", "type", "scope", "status", "limit", "volatile_limit"})
    result = await legacy_read(arguments, user_id=ctx.user_id or "default", workspace_id=ctx.workspace_id or None,
                               project_id=ctx.project_id or None)
    # Chat history keeps only these references (others can open this chat);
    # the assistant's next step re-reads the text under current permissions.
    refs = await _transient_boundary(ctx, "creator_context", result["references"], arguments=arguments)
    if args.action != "get_user_context":
        rows = result["items"]
        title = f"Memories ({len(rows)})" if args.action == "search_memories" else f"Active memories ({len(rows)})"
        return ToolResult(title=title, output=_dump(rows), metadata={"count": len(rows), "transient_memory_refs": refs})
    if not result["context"]:
        return ToolResult(
            title="No creator context yet",
            output=(
                "No persona or memories are stored for this user yet. Proceed "
                "without persona assumptions; propose_memory when the user states "
                "stable facts about themselves."
            ),
            metadata={"stats": result["stats"], "transient_memory_refs": refs},
        )
    return ToolResult(title="Creator context", output=result["context"],
                      metadata={"stats": result["stats"], "transient_memory_refs": refs})


async def execute_creator_context(args: CreatorContextArgs, ctx: ToolContext) -> ToolResult:
    user_id = ctx.user_id or "default"
    project_id = ctx.project_id or None
    # The user's own words are saved by the background pipeline; a proposal is
    # how content they did not write (an assistant plan, a file excerpt) is kept.
    if args.action == "write_memory":
        from memory.jobs import automatic_saving
        if ctx.session_id and automatic_saving(user_id):
            # Completion schedules canonical user evidence in the durable
            # pipeline. A generated tool summary must not preempt extraction.
            return ToolResult(title="Memory processing in background",
                output="Saving happens automatically after this reply, once the user's own words are checked. "
                    "Acknowledge it as something you will remember, e.g. \"好的，我会记住……\" or \"Got it, I'll keep "
                    "that in mind\". Do not say it is already saved, remembered or updated (no \"已记住\", \"记住了\", "
                    "\"已保存\", \"已更新\"), and do not ask the user to confirm.",
                metadata={"status": "automatic_pending", "confirmation_required": False})

    if args.action in LEGACY_READ_ACTIONS:
        return await _read(args, ctx)

    if args.action == "write_memory":
        row = await memory_service.write_memory(
            user_id=user_id,
            workspace_id=ctx.workspace_id or None,
            project_id=project_id,
            scope=args.scope or "SHORT_TERM",
            type=args.type or "",
            value=args.value.model_dump() if args.value else {},
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

    return ToolResult(title="Unknown action", output=f"Unsupported action: {args.action}")


creator_context_tool = define_tool(
    "creator_context",
    description=CREATOR_CONTEXT_DESCRIPTION,
    parameters=CreatorContextArgs,
    execute=execute_creator_context,
    sandbox_required=False,
    parallel_safe=False,
)
