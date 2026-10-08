"""Bounded service between ordinary and report-only main turns.

The existing main Session lock serializes selection and claim. A committed
claim event is the scheduling history, including after a worker restart.
Waiting budgets apply at the next free turn boundary; they never preempt a
running turn or mix a report with human input.
"""
from datetime import timezone

from sqlalchemy import select

from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from session.agent_event_log import append_agent_event_locked

MAX_CONSECUTIVE = {"ordinary": 2, "report_only": 1}
MAX_WAIT_SECONDS = 30


def _mode(row):
    return "report_only" if row.origin == "task_result" else "ordinary"


async def select_main_turn_locked(db, main, *, include_next_turn):
    if not include_next_turn:
        return []
    if await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.user_id == main.user_id, AgentInboxItem.state == "claimed").limit(1)):
        return []
    heads = {}
    for mode in MAX_CONSECUTIVE:
        query = select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.user_id == main.user_id, AgentInboxItem.state == "accepted",
            AgentInboxItem.target.in_(("next-turn", "next-step")),
            AgentInboxItem.origin == "task_result" if mode == "report_only" else AgentInboxItem.origin != "task_result")
        row = await db.scalar(query.order_by(AgentInboxItem.created_at, AgentInboxItem.id).limit(1).with_for_update())
        if row is not None:
            heads[mode] = row
    if len(heads) < 2:
        return list(heads.values())
    history = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == "assistant.queue.claimed")
        .order_by(AgentEvent.sequence.desc()).limit(max(MAX_CONSECUTIVE.values())))).all())
    previous = history[0].payload.get("mode") if history else None
    streak = 0
    for event in history:
        if event.payload.get("mode") != previous:
            break
        streak += 1
    other = "report_only" if previous == "ordinary" else "ordinary"
    if previous in MAX_CONSECUTIVE and streak >= MAX_CONSECUTIVE[previous]:
        return [heads[other]]
    from agent.inbox import _database_utcnow
    now = await _database_utcnow(db)
    overdue = {mode for mode, row in heads.items()
               if (now - row.created_at.replace(tzinfo=row.created_at.tzinfo or timezone.utc)).total_seconds() >= MAX_WAIT_SECONDS}
    if other in overdue:
        return [heads[other]]
    if overdue:
        return [heads[next(iter(overdue))]]
    return [heads["ordinary"]]


async def record_main_claim_locked(db, main, rows, *, run_fence, turn_id):
    if main.kind != "assistant" or not rows:
        return
    modes = {_mode(row) for row in rows}
    if len(rows) != 1 or len(modes) != 1:
        raise ValueError("An assistant turn owns exactly one ordinary or report-only input")
    await append_agent_event_locked(db, main, kind="assistant.queue.claimed",
        payload={"mode": _mode(rows[0]), "inbox_id": rows[0].id}, run_fence=run_fence,
        turn_id=turn_id, idempotency_key=f"assistant-queue:{run_fence[1]}:{run_fence[2]}")
