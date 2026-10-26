"""Main-assistant decisions that survive a change of conversational topic.

Only task preparation can detach. Execution, plan, side-effect confirmations,
permission and memory checkpoints keep their original generation fences.
Detached answers never run an old tool: they record a choice and give the
current assistant the original tool arguments to continue through normal admission.
"""
from sqlalchemy import and_, or_, select

from assistant.commands import ToolSource, _authority, _tool_source_locked
from assistant.policy import AssistantError
from assistant.requests import question_version
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from question import runtime

KINDS = frozenset({"assistant_task_setup", "assistant_placement"})


def detachable(row, session) -> bool:
    continuation = row.continuation or {}
    return (session.kind == "assistant" and continuation.get("kind") == "question"
            and not continuation.get("assistant_request")
            and any(continuation.get(kind) for kind in KINDS))


async def detach_locked(db, session, row):
    """Leave a preparation card answerable without blocking another decision."""
    from question import surface
    if not detachable(row, session):
        raise ValueError("Only task preparation decisions can detach")
    row.detached = True
    part = await db.get(Part, row.part_id) if row.part_id else None
    if part is not None:
        part.data = {**part.data, "status": "completed", "error": None,
            "output": "This decision is still pending independently. The work has not started. "
                      "Use cards.list / cards.answer to continue this exact request.",
            "metadata": {**(part.data.get("metadata") or {}), "question_status": "pending"}}
        await surface.part_updated(db, session, part)


async def finish_locked(db, session, execution, row, *, enqueue=True):
    """Apply only the saved choice, in the same transaction as its durable wake."""
    from question import surface
    from question.continuation import _apply
    from agent.inbox import accept_inbox_item_locked
    if not row.detached or not detachable(row, session):
        raise ValueError("Only independent main-assistant decisions can use this path")
    part = await db.get(Part, row.part_id)
    if part is None or part.session_id != session.id or part.user_id != session.user_id:
        raise ValueError("The original decision tool is unavailable")
    result, events = await _apply(db, session, row)
    if events:
        raise ValueError("A detached choice cannot execute an approval continuation")
    await surface.prepare(db, session)
    part.data = {**part.data, **result, "status": "completed", "error": None}
    await surface.part_updated(db, session, part)
    row.applied = True
    # Source tool input is immutable evidence; only normal tools may execute it.
    original = {"tool": part.canonical_tool_id or part.data.get("tool"),
                "arguments": part.data.get("input") or {}}
    if enqueue:
        # A click on saved decision A must not wait behind preparation card B.
        # Preserve B as an independent choice; never unblock execution approvals.
        waiting = await runtime.pending_question_locked(db, execution)
        if waiting is not None and detachable(waiting, session):
            await detach_locked(db, session, waiting)
        await accept_inbox_item_locked(db, session, delivery="followup", agent="assistant",
            client_id=f"assistant-card:{row.id}", model=session.model, variant=session.variant,
            prompt="A user answered a saved assistant decision. Read the bound decision context and continue "
                   "only its original request through the normal tools; the choice itself executed no work.",
            origin="system_recovery", origin_ref={"entrypoint": "assistant_card_answer", "question_id": row.id})
        from agent.inbox import schedule_inbox_wake
        runtime._after_commit(db, lambda: schedule_inbox_wake(session.id, session.user_id))
    return {"state": "decision_recorded", "execution_confirmed": False,
            "request_id": row.id, "result": result, "original_call": original}


async def context(db, main, *, request_id=None, limit=20):
    query = select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == main.id,
        QuestionCheckpoint.user_id == main.user_id)
    query = query.where(QuestionCheckpoint.id == request_id) if request_id else query.where(
        QuestionCheckpoint.status == "pending")
    rows = list((await db.scalars(query.order_by(QuestionCheckpoint.created_at.desc(),
        QuestionCheckpoint.id.desc()).limit(limit + 1))).all())
    items = []
    for row in rows[:limit]:
        if not detachable(row, main) or (row.expires_at and runtime.utc(row.expires_at) <= runtime.now()):
            continue
        part = await db.get(Part, row.part_id)
        if part is None or part.user_id != main.user_id or part.session_id != main.id:
            continue
        items.append({"request_id": row.id, "version": question_version(row), "state": row.status,
            "questions": row.questions, "answers": row.answers,
            "original_call": {"tool": part.canonical_tool_id or part.data.get("tool"),
                              "arguments": part.data.get("input") or {}},
            "result": part.data.get("output") if row.status != "pending" else None,
            "execution_confirmed": False})
    return {"items": items, "has_more": len(rows) > limit, "untrusted_data": True}


async def current_answers(db, main, *, run_id, generation):
    """Exact answered-card receipts for this claimed continuation, not recall.

    Normal continuations bind the original execution generation; detached
    preparation cards bind their exact ID. Never fall back to the pending
    directory when either binding is absent. Reads do not execute the saved
    tool or enlarge the permission granted by its original confirmation.
    """
    inputs = list(await db.scalars(select(AgentInboxItem).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
        AgentInboxItem.run_id == run_id, AgentInboxItem.generation == generation,
        AgentInboxItem.state == "claimed")))
    if not inputs or any(item.origin != "system_recovery" for item in inputs):
        return []
    targets = []
    for item in inputs:
        ref = item.origin_ref or {}
        if ref.get("entrypoint") == "assistant_card_answer":
            identity = ref.get("question_id")
            if isinstance(identity, str) and identity:
                targets.append(and_(QuestionCheckpoint.id == identity,
                                    QuestionCheckpoint.detached.is_(True)))
        elif ref.get("entrypoint") == "question_answer":
            original = ref.get("generation")
            if type(original) is int and original >= 0:
                targets.append(and_(QuestionCheckpoint.generation == original,
                                    QuestionCheckpoint.detached.is_(False)))
    if not targets:
        return []
    rows = list((await db.execute(select(QuestionCheckpoint, Part).join(
        Part, Part.id == QuestionCheckpoint.part_id).where(
        QuestionCheckpoint.session_id == main.id, QuestionCheckpoint.user_id == main.user_id,
        QuestionCheckpoint.status.in_(("answered", "rejected")), QuestionCheckpoint.applied.is_(True),
        Part.session_id == main.id, Part.user_id == main.user_id, Part.type == "tool",
        or_(*targets)).order_by(QuestionCheckpoint.created_at, QuestionCheckpoint.id))).all())
    receipts = []
    for row, part in rows:
        data = part.data or {}
        if data.get("status") != "completed" or (data.get("metadata") or {}).get("question_id") != row.id:
            continue
        receipts.append({"request_id": row.id, "state": row.status, "questions": row.questions,
            "answers": row.answers, "original_call": {
                "tool": part.canonical_tool_id or data.get("tool"), "arguments": data.get("input") or {}},
            "result": data.get("output"), "decision_only": detachable(row, main),
            **({"execution_confirmed": False} if detachable(row, main) else {})})
    return receipts


async def list_cards(*, user_id, workspace_id, main_id, request_id=None):
    async with get_db_session() as db:
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        return await context(db, main, request_id=request_id)


async def answer(ctx, *, request_id, version, answers, source_message_ids, project_name=None):
    """The same main model interprets both spoken and typed answers; SQL fences the target."""
    from question.question import QuestionConflict, QuestionGone, resolve_locked
    source = ToolSource(ctx.part_id, ctx.run_id, ctx.run_generation, tuple(source_message_ids))
    async with runtime.transaction(ctx.session_id, ctx.user_id, fence=False) as (db, main, execution):
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await _tool_source_locked(db, main, source, "main_card_answer")
        row = await db.get(QuestionCheckpoint, request_id)
        if row is None or row.session_id != main.id or row.user_id != main.user_id or not detachable(row, main):
            raise AssistantError(404, "ASSISTANT_CARD_UNAVAILABLE", "The saved decision is unavailable")
        if version != question_version(row):
            raise AssistantError(409, "ASSISTANT_CARD_CHANGED", "Read the exact card again")
        # Canonical order, not wall-clock timestamps (SQLite clocks can be
        # second-resolution and concurrent hosts need not share a clock).
        card_events = (await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
            AgentEvent.part_id == row.part_id, AgentEvent.kind == "tool.updated")
            .order_by(AgentEvent.sequence))).all()
        shown = next((e.sequence for e in card_events
                      if (((e.payload.get("part") or {}).get("data") or {}).get("metadata") or {}).get("question_id") == row.id), None)
        messages = (await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
            AgentEvent.message_id.in_(source_message_ids), AgentEvent.kind == "message.created"))).all()
        if shown is None or len(messages) != len(set(source_message_ids)) or any(e.sequence <= shown for e in messages):
            raise AssistantError(403, "ASSISTANT_CARD_ANSWER_REQUIRED", "Cite the human answer after this card was presented")
        # A new human turn normally detached it during admission. An explicit
        # answer from within a current turn is also safe to finish without replay.
        row.detached = True
        try:
            changed = await resolve_locked(db, main, execution, row, answers=answers,
                placement_project_name=project_name, enqueue_detached=False)
        except (QuestionConflict, QuestionGone) as exc:
            raise AssistantError(409, "ASSISTANT_CARD_CHANGED", str(exc)) from exc
        if changed:
            from bus import bus
            from question.question import _event
            payload = _event(row)
            runtime._after_commit(db, lambda: bus.publish("question.replied", payload))
        part = await db.get(Part, row.part_id)
        return {"state": "decision_recorded", "applied": changed, "execution_confirmed": False,
            "request_id": row.id, "result": part.data.get("output"),
            "original_call": {"tool": part.canonical_tool_id or part.data.get("tool"),
                              "arguments": part.data.get("input") or {}}}
