"""Exact-plan review on the existing durable Question/Task continuation path."""
from sqlalchemy import select

from assistant.commands import command_digest, task_locked
from assistant.identities import inbox_key
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.assistant import AssistantTask
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session


class PlanReviewChanged(AssistantError):
    def __init__(self):
        super().__init__(409, "ASSISTANT_PLAN_CHANGED",
            "The reviewed plan or its approval changed. Send a new instruction to review the current plan.")


def plan_digest(part):
    return command_digest({"id": part.id, "session_id": part.session_id,
        "message_id": part.message_id, "path": part.data.get("path"),
        "content": part.data.get("content"), "review_via_question": part.data.get("review_via_question")})


async def review(ctx) -> bool:
    """Return False for ordinary sessions; a managed review suspends the run."""
    from assistant.scheduling import require_runnable_locked
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == ctx.session_id))
        if task is None:
            session = await db.get(Session, ctx.session_id)
            if session is not None and session.memory_policy == "assistant_isolated":
                raise AssistantError(409, "ASSISTANT_PLAN_PARENT_REQUIRED",
                    "Return the proposed plan to the parent task; only that task can request human review")
            return False
        _, session = await task_locked(db, user_id=ctx.user_id, workspace_id=task.workspace_id,
            main_id=task.assistant_session_id, task_id=task.id)
        await require_runnable_locked(db, session)
        if not ctx.part_id or not ctx.message_id:
            raise AssistantError(409, "ASSISTANT_PLAN_SOURCE_REQUIRED", "A persisted plan tool call is required")
        proposal_id = inbox_key("assistant-plan-review", ctx.part_id)
        proposal = await db.get(Part, proposal_id)
        if proposal is not None and (proposal.user_id != ctx.user_id or proposal.session_id != ctx.session_id
                                    or proposal.message_id != ctx.message_id or proposal.type != "plan"):
            raise AssistantError(409, "ASSISTANT_PLAN_SOURCE_REQUIRED", "The original plan is unavailable")
    if proposal is None:
        # Read the actual file through this tool's normal SandboxClient scope.
        # A previous write/edit call is not proof of the file's current bytes.
        from session.session import get_session, plan_path_for, save_part
        from models.message import PlanPart
        session_view = await get_session(ctx.session_id, user_id=ctx.user_id)
        if session_view is None:
            raise AssistantError(409, "ASSISTANT_PLAN_UNAVAILABLE", "The plan session is unavailable")
        path = await plan_path_for(session_view)
        client = ctx.sandbox
        if client is None:
            from sandbox import sandbox_manager
            client = await sandbox_manager.get_client(ctx.session_id, user_id=ctx.user_id)
        if client is None:
            raise AssistantError(409, "ASSISTANT_PLAN_UNAVAILABLE", "The plan file is unavailable")
        async def freeze():
            content = await client.read_file_raw(path)
            if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 65_536:
                raise AssistantError(409, "ASSISTANT_PLAN_UNAVAILABLE", "The review needs a nonempty plan of at most 64 KiB")
            await save_part(PlanPart(id=proposal_id, path=path, content=content, status="ready",
                review_via_question=True, session_id=ctx.session_id, message_id=ctx.message_id),
                is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)

        # Complete the physical read before QuestionSuspended unwinds the tool.
        # Waiting for a human is not an interrupted sandbox operation.
        import copy
        from sandbox.resource_operation import run_plan_review_scope
        snapshot_context = copy.copy(ctx)
        snapshot_context.sandbox = client
        await run_plan_review_scope(snapshot_context, path, freeze)
        async with get_db_session() as db:
            proposal = await db.get(Part, proposal_id)
    from question.question import ask, Question, QuestionOption
    await ask(ctx.session_id, [Question(
        question="Review this exact plan before implementation:\n\n" + proposal.data["content"],
        header="Plan review", custom=False,
        options=[QuestionOption(label="Approve", description="Implement this exact plan in build mode"),
                 QuestionOption(label="Revise", description="Stay in plan mode and revise the proposal")],
    )], tool={"messageID": ctx.message_id, "callID": ctx.part_id}, user_id=ctx.user_id,
        continuation={"kind": "plan_exit", "plan_part_id": proposal.id, "plan_digest": plan_digest(proposal)})
    return True


async def validate_review(db, row, *, pending=False):
    from question.question import QuestionGone
    proposal_id = row.continuation.get("plan_part_id")
    proposal = await db.get(Part, proposal_id) if proposal_id else None
    if (proposal is None or proposal.user_id != row.user_id or proposal.session_id != row.session_id
            or proposal.message_id != row.message_id or proposal.type != "plan"
            or proposal.data.get("review_via_question") is not True
            or plan_digest(proposal) != row.continuation.get("plan_digest")
            or (pending and proposal.data.get("status") != "ready")):
        raise QuestionGone("changed")
    return proposal


def review_instruction(row, proposal):
    approved = row.status == "answered" and row.answers == [["Approve"]]
    if approved:
        return ("The user approved the exact plan below through the recorded plan review. "
            "Switch to build mode and implement this version. Treat the text below as the approved plan; "
            "a later change to its file is not approval of different work.\n\n" + proposal.data["content"])
    if row.status == "rejected":
        return "The user dismissed the plan review. No implementation was approved. Stay in plan mode and wait for new instructions."
    return "The user requested a revision of the reviewed plan. Stay in plan mode, revise the proposal, then ask for a new review before implementation."


async def apply_review(db, session, row):
    """Apply only a checked decision; a mutable remote path is not approval."""
    proposal = await validate_review(db, row, pending=True)
    approved = row.status == "answered" and row.answers == [["Approve"]]
    from question import surface
    proposal.data = {**proposal.data, "status": "accepted" if approved else "rejected"}
    await surface.part_updated(db, session, proposal)
    events = [{"type": "part.updated", "data": {"userId": row.user_id, "sessionId": session.id,
        "messageId": proposal.message_id, "part": proposal.data}}]
    return approved, review_instruction(row, proposal), events


async def _applied_decision(db, row):
    from assistant.requests import _revision, decision_for, question_task
    from assistant.request_reply import validate_saved_source
    linked = await question_task(db, row)
    binding = row.continuation.get("assistant_request")
    command = await decision_for(db, row.id)
    expected = {"answers": row.answers,
                "attachments": row.continuation.get("answer_attachments") or [[] for _ in row.questions]}
    if (not linked or not binding or not row.applied or row.status not in {"answered", "rejected"}
            or binding.get("request_revision") != _revision(row, binding)
            or command is None or command.state != "applied" or command.actor_user_id != row.user_id
            or command.workspace_id != linked[0].workspace_id
            or command.assistant_session_id != linked[0].assistant_session_id
            or command.receipt.get("request_revision") != binding["request_revision"]
            or command.source_ref.get("decision") != expected):
        raise PlanReviewChanged()
    await validate_saved_source(db, linked[0], command.source_ref)
    if row.continuation["kind"] == "plan_exit":
        from question.question import QuestionGone
        try:
            proposal = await validate_review(db, row)
        except QuestionGone as error:
            raise PlanReviewChanged() from error
        approved = row.status == "answered" and row.answers == [["Approve"]]
        if proposal.data.get("status") != ("accepted" if approved else "rejected"):
            raise PlanReviewChanged()
    return command


async def agent_for_turn(session, user_message_id):
    """An applied decision changes mode without inventing a new human turn."""
    if session.memory_policy != "assistant_isolated" or session.kind == "assistant":
        return None
    async with get_db_session() as db:
        rows = (await db.scalars(select(QuestionCheckpoint).join(Message,
            Message.id == QuestionCheckpoint.message_id).where(
                QuestionCheckpoint.session_id == session.id, QuestionCheckpoint.user_id == session.user_id,
                QuestionCheckpoint.applied.is_(True), Message.parent_id == user_message_id,
            ).order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()))).all()
        for row in rows:
            kind = row.continuation.get("kind")
            if kind not in {"plan_enter", "plan_exit"} or not row.continuation.get("assistant_request"):
                continue
            # A declined entry does not override an earlier effective switch.
            if kind == "plan_enter" and (row.status != "answered" or row.answers != [["Yes"]]):
                continue
            await _applied_decision(db, row)
            return "build" if kind == "plan_exit" and row.status == "answered" and row.answers == [["Approve"]] else "plan"
    return None


async def validate_context(messages, ctx):
    """Revalidate reviewed plans in the actual request, including retries."""
    calls = []
    for message in messages:
        for raw in message.parts:
            part = raw if isinstance(raw, dict) else raw.model_dump()
            if part.get("type") == "tool" and (part.get("canonical_tool_id") or part.get("tool")) == "plan_exit":
                calls.append(part)
    if not calls:
        return False
    found = False
    async with get_db_session() as db:
        for part in calls:
            row = await db.scalar(select(QuestionCheckpoint).where(QuestionCheckpoint.part_id == part["id"],
                QuestionCheckpoint.user_id == ctx.user_id, QuestionCheckpoint.session_id == ctx.session_id))
            if row is None or not row.continuation.get("assistant_request"):
                continue
            found = True
            from question.question import QuestionGone
            try:
                proposal = await validate_review(db, row)
            except QuestionGone as error:
                raise PlanReviewChanged() from error
            if row.applied and row.status in {"answered", "rejected"}:
                await _applied_decision(db, row)
                expected = review_instruction(row, proposal)
                state_output = (part.get("state") or {}).get("output")
                if part.get("output") != expected or state_output and state_output != expected:
                    raise PlanReviewChanged()
    return found
