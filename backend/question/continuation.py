"""Apply known approval continuations atomically, then schedule a fresh run.

This is deliberately not a generic 'execute this tool again' mechanism: a
completed tool or an external paid operation must never be replayed here.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import or_, select

from bus import bus
from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from question import runtime

log = create_logger("question.continuation")


async def _apply(db, session, row: QuestionCheckpoint) -> tuple[dict, list[dict]]:
    questions = [q["question"] for q in row.questions]
    answers = row.answers or [[] for _ in questions]
    metadata = {"questions": questions, "answers": answers, "question_id": row.id,
                "question_status": row.status}
    events = []
    kind = row.continuation.get("kind")
    if row.status == "rejected":
        if kind == "memory_proposal" and row.continuation.get("memory_ids"):
            return {"title": "Persona bundle parked",
                "output": "The user dismissed the persona card. Nothing was saved; the facts stay pending. Do not re-propose them in this conversation.",
                "metadata": {**metadata, "rejected": True, "memory_ids": row.continuation["memory_ids"], "decision": "dismissed"}}, events
        if kind == "memory_proposal":
            return {"title": "Memory proposal parked",
                "output": "The user dismissed the confirmation. The proposal stays pending, not approved. Do not re-propose it in this conversation.",
                "metadata": {**metadata, "rejected": True, "memory_id": row.continuation["memory_id"], "decision": "dismissed"}}, events
        return {"title": "Question skipped", "output": "The user skipped these questions. No approval was granted.",
                "metadata": {**metadata, "rejected": True}}, events
    if kind == "question":
        text = "; ".join(f"{q}: {', '.join(a)}" for q, a in zip(questions, answers))
        return {"title": f"Answered {len(questions)} questions", "output": f"User answers: {text}",
                "metadata": metadata}, events
    if kind == "plan_enter":
        if answers[0] != ["Yes"]:
            return {"title": "Staying in build mode", "output": "User chose not to enter plan mode.",
                    "metadata": {**metadata, "rejected": True}}, events
        message_id, part_id = ascending("message"), ascending("part")
        content = "User has requested to enter plan mode. Switch to plan mode and begin planning."
        part_data = {"type": "text", "id": part_id, "session_id": row.session_id,
                     "message_id": message_id, "text": content, "synthetic": True}
        message = Message(id=message_id, session_id=row.session_id, user_id=row.user_id,
                       role="user", agent="plan", model=session.model,
                       client_message_id=f"ask:{row.id}", created_at=runtime.now())
        part = Part(id=part_id, message_id=message_id, session_id=row.session_id,
                    user_id=row.user_id, type="text", data=part_data, created_at=runtime.now())
        db.add_all([message, part])
        await db.flush()
        from session.agent_event_log import append_message_events_locked, append_part_event_locked
        await append_message_events_locked(db, session, message, operation="created", run_fence=None)
        await append_part_event_locked(db, session, part, message, operation="created", run_fence=None)
        session.agent = "plan"
        events.append({"type": "message.created", "data": {"userId": row.user_id, "sessionId": row.session_id,
            "message": {"id": message_id, "session_id": row.session_id, "role": "user", "agent": "plan",
                        "created_at": runtime.now().isoformat(), "parts": [part_data]}}})
        events.append({"type": "session.updated", "data": {"userId": row.user_id, "sessionId": row.session_id, "agent": "plan"}})
        return {"title": "Switching to plan agent", "output": "User approved entering plan mode. Begin planning.",
                "metadata": metadata}, events
    if kind == "memory_proposal" and row.continuation.get("memory_ids"):
        return await _apply_bundle(db, session, row, answers, metadata), events
    if kind == "memory_proposal":
        from db.models.memory import UserMemory
        from memory.service import PENDING_NOTE_TYPE, USER_NOTE_TYPE, _slim, _truncate_value
        memory = await db.scalar(select(UserMemory).where(
            UserMemory.id == row.continuation["memory_id"], UserMemory.user_id == row.user_id,
            UserMemory.workspace_id == row.continuation["workspace_id"],
            UserMemory.type == PENDING_NOTE_TYPE,
        ).with_for_update())
        if memory is None or memory.status == "DEPRECATED":
            raise ValueError("The memory proposal is no longer available; ask for fresh confirmation")
        detail = row.questions[0].get("detail") or {}
        if "summary" in detail and (memory.value or {}).get("summary") != detail["summary"]:
            raise ValueError("The memory proposal changed after this question was asked; request fresh confirmation")
        answer = answers[0][0]
        if answer == "不用记":
            memory.status = "DEPRECATED"
            title, output = "Memory rejected", "The user declined. Do not save or re-propose this memory."
            metadata.update(memory_id=memory.id, decision="rejected")
        else:
            memory.type, memory.owner, memory.status, memory.confidence = USER_NOTE_TYPE, "USER_CONFIRMED", "ACTIVE", 90
            if answer != "记住":
                memory.value = _truncate_value({**(memory.value or {}), "summary": answer})
            memory.evidence = {**(memory.evidence or {}), "awaiting_confirm": False}
            title, output = "Memory saved", f"Saved the confirmed memory: {(memory.value or {}).get('summary', '')}"
            metadata.update(memory=_slim(memory), decision="confirmed" if answer == "记住" else "confirmed_edited")
        memory.updated_at = runtime.now()
        return {"title": title, "output": output, "metadata": metadata}, events
    raise ValueError("Unknown saved question continuation")


async def _apply_bundle(db, session, row: QuestionCheckpoint, answers, metadata: dict) -> dict:
    """Several CANDIDATE persona rows confirmed (or postponed) by one card."""
    from memory.service import apply_bundle_answer
    workspace_id = row.continuation.get("workspace_id") or session.workspace_id
    answer = answers[0][0] if answers and answers[0] else ""
    result = await apply_bundle_answer(db, user_id=row.user_id, workspace_id=workspace_id,
                                       memory_ids=list(row.continuation["memory_ids"]), answer=answer)
    metadata.update(decision=result["decision"], memory_ids=row.continuation["memory_ids"],
                    edited=result["edited"], note=result["note"])
    if result["decision"] == "confirmed":
        from store import service as store_service
        store = await store_service.set_persona_state(db, workspace_id, status="active")
        if store is not None:
            try:
                from notifications.events import emit
                await emit(db, user_id=row.user_id, workspace_id=workspace_id, kind="persona_ready",
                           event_key=f"persona_ready:{store.id}:{row.id}", name=store.name, session_id=row.session_id)
            except Exception:
                log.exception("persona_ready notification failed for %s", workspace_id)
        saved = "\n".join(f"- {m['type']}: {(m.get('value') or {}).get('summary', '')}" for m in result["memories"])
        return {"title": "Persona confirmed", "output": f"The user confirmed the store persona. Saved as ACTIVE memories:\n{saved}\n"
                "Reply with one short closing line: the persona is saved, the home page suggestions now follow it, and they can edit it later in 运营中心.",
                "metadata": metadata}
    if result["decision"] == "postponed":
        return {"title": "Persona postponed", "output": "The user chose 稍后. The facts stay pending (not saved). Reply with one short line saying they can confirm later, then stop.",
                "metadata": metadata}
    return {"title": "Persona feedback", "output": f"The user replied with text instead of confirming: {result['note']}\n"
            "Treat it as corrections. Revise the facts accordingly and call creator_context propose_bundle again once with the corrected items.",
            "metadata": metadata}


async def apply_answers(session_id: str, user_id: str) -> int | None:
    """Apply accepted decisions exactly once; return the generation to resume."""
    events = []
    async with runtime.transaction(session_id, user_id, fence=False) as (db, session, execution):
        if not execution.resume_pending or runtime.is_live(execution):
            return None
        rows = (await db.scalars(select(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == session_id, QuestionCheckpoint.user_id == user_id,
            QuestionCheckpoint.generation == execution.generation,
            QuestionCheckpoint.status.in_(("answered", "rejected")),
            QuestionCheckpoint.applied == False,  # noqa: E712
        ).order_by(QuestionCheckpoint.created_at))).all()
        from question import surface
        if rows:
            await surface.prepare(db, session)
        for row in rows:
            from question.question import checkpoint_context
            context = await checkpoint_context(db, row, execution)
            result, extra_events = await _apply(db, session, row)
            events.extend(extra_events)
            if row.part_id:
                part = await db.get(Part, row.part_id)
                if part is None or part.user_id != user_id or part.session_id != session_id:
                    raise ValueError("Question checkpoint lost its tool call")
                part.data = {**part.data, **result, "status": "completed", "error": None}
                await surface.part_updated(db, session, part)
                events.append({"type": "part.updated", "data": {"userId": user_id,
                    "sessionId": session_id, "messageId": row.message_id, "part": part.data}})
                message = await db.get(Message, row.message_id)
                if message and message.user_id == user_id:
                    message.finish = "tool_calls"
                    await surface.message_updated(db, session, message)
                from trajectory import record
                if context:
                    await record("part.committed", {"part": part.data}, db=db, context=context)
                    await record("tool.finished", {
                        "status": "completed", "output": result.get("output", ""),
                        "model_output": result.get("output", ""),
                        "question_id": row.id, "reason": "user_answer_applied",
                    }, db=db, context=context, event_id=f"question_tool_finish:{row.id}")
            if context:
                from trajectory import record
                await record("input.injected", {
                    "source_kind": "question", "question_id": row.id,
                    "answers": row.answers, "decision": row.status,
                }, db=db, context=context, event_id=f"question_inject:{row.id}")
                for event in extra_events:
                    event_data = event["data"]
                    if event["type"] == "message.created":
                        message_data = event_data["message"]
                        await record("message.committed", {"message": message_data}, db=db,
                                     context=context.derive(message_id=message_data["id"], part_id=None))
                    elif event["type"] == "session.updated":
                        await record("session.settings_changed", {"after": {"agent": session.agent},
                                                                   "source_kind": "question"},
                                     db=db, context=context)
            row.applied = True
            row.updated_at = runtime.now()
        await db.flush()
        pending = await db.scalar(select(QuestionCheckpoint.id).where(
            QuestionCheckpoint.session_id == session_id,
            QuestionCheckpoint.generation == execution.generation,
            QuestionCheckpoint.status == "pending",
        ).limit(1))
        if pending:
            execution.resume_pending = False
            session.status = "waiting_input"
            generation = None
        else:
            session.status = "queued"
            generation = execution.generation
        status = session.status
    for event in events:
        bus.publish(event["type"], event["data"])
    await runtime.publish_status(session_id, user_id, status)
    return generation


async def expire_questions() -> None:
    async with get_db_session() as db:
        rows = (await db.execute(select(QuestionCheckpoint.session_id, QuestionCheckpoint.user_id).where(
            QuestionCheckpoint.status == "pending", QuestionCheckpoint.expires_at <= runtime.now(),
        ).distinct())).all()
    for session_id, user_id in rows:
        try:
            async with runtime.transaction(session_id, user_id, fence=False) as (db, session, execution):
                due = (await db.scalars(select(QuestionCheckpoint).where(
                    QuestionCheckpoint.session_id == session_id,
                    QuestionCheckpoint.status == "pending", QuestionCheckpoint.expires_at <= runtime.now(),
                ))).all()
                from question import surface
                if due:
                    await surface.prepare(db, session)
                for row in due:
                    from question.question import checkpoint_context, record_checkpoint
                    await checkpoint_context(db, row, execution)
                    row.status, row.applied, row.updated_at = "expired", True, runtime.now()
                    await record_checkpoint(db, row, execution, "question.cancelled",
                                            {"status": "cancelled", "reason": "expired"})
                    part = await db.get(Part, row.part_id) if row.part_id else None
                    if part:
                        part.data = {**part.data, "status": "error", "error": "Question expired; no approval was granted.",
                                     "metadata": {**(part.data.get("metadata") or {}), "question_status": "expired"}}
                        await surface.part_updated(db, session, part)
                await db.flush()
                if not runtime.is_live(execution):
                    session.status = await runtime.waiting_status(db, execution)
                status = session.status
            runtime.publish_invalidated(due)
            await runtime.publish_status(session_id, user_id, status)
        except LookupError:
            continue
        except Exception:
            # One session's failure must not keep every other question from expiring.
            log.exception("Question expiry failed for %s", session_id)


class QuestionContinuationWorker:
    """One shared poller, not one coroutine/connection per waiting question."""
    def __init__(self):
        self.task: asyncio.Task | None = None
        self.runs: dict[str, asyncio.Task] = {}

    def start(self):
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        for task in self.runs.values():
            task.cancel()
        await asyncio.gather(*self.runs.values(), return_exceptions=True)
        self.runs.clear()

    async def tick(self):
        # Recovery, expiry and resume are independent: one failing phase must
        # not stall the others for every session.
        for phase, sweep in (("lease recovery", runtime.recover_expired_runs),
                             ("question expiry", expire_questions),
                             ("question resume", self._resume_answered)):
            try:
                await sweep()
            except Exception:
                log.exception("Question continuation %s sweep failed", phase)

    async def _resume_answered(self):
        self.runs = {key: task for key, task in self.runs.items() if not task.done()}
        async with get_db_session() as db:
            candidates = (await db.execute(select(SessionExecution.session_id, SessionExecution.user_id, SessionExecution.generation).where(
                SessionExecution.resume_pending == True,  # noqa: E712
                or_(SessionExecution.lease_until.is_(None), SessionExecution.lease_until <= runtime.now()),
                or_(SessionExecution.next_attempt_at.is_(None), SessionExecution.next_attempt_at <= runtime.now()),
            ).order_by(SessionExecution.updated_at).limit(100))).all()
        for session_id, user_id, candidate_generation in candidates:
            if session_id in self.runs:
                continue
            try:
                await self._resume_candidate(session_id, user_id, candidate_generation)
            except Exception:
                log.exception("Question continuation handling failed for %s", session_id)

    async def _resume_candidate(self, session_id, user_id, candidate_generation):
        try:
            generation = await apply_answers(session_id, user_id)
            if generation is not None:
                self.runs[session_id] = asyncio.create_task(self._resume(session_id, user_id, generation))
        except LookupError:
            return
        except ValueError as exc:
            log.exception("Question continuation failed for %s", session_id)
            async with runtime.transaction(session_id, user_id, fence=False) as (_, session, execution):
                if execution.generation != candidate_generation or runtime.is_live(execution):
                    return
                execution.resume_pending = False
                execution.resume_error = str(exc)
                session.status = "error"
            await runtime.publish_status(session_id, user_id, "error", error={
                "code": "QUESTION_RESUME_FAILED",
                "message": "Your answers are saved, but continuation failed. Send a message to continue.",
            })
        except Exception:
            await self._retry_later(session_id, user_id, candidate_generation)

    async def _retry_later(self, session_id, user_id, candidate_generation):
        log.exception("Transient question continuation failure for %s; keeping delivery intent", session_id)
        async with runtime.transaction(session_id, user_id, fence=False) as (_, _, execution):
            if execution.generation == candidate_generation and execution.resume_pending:
                execution.next_attempt_at = runtime.now() + timedelta(seconds=10)

    async def _resume(self, session_id, user_id, generation):
        from agent.loop import run_loop
        try:
            await run_loop(session_id, user_id=user_id, expected_generation=generation)
        except Exception:
            log.exception("Question resumed run failed for %s", session_id)

    async def _loop(self):
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("Question continuation sweep failed")
            await asyncio.sleep(2)


question_worker = QuestionContinuationWorker()
