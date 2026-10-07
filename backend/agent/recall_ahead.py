"""Start a woken turn's memory recall when its input is claimed.

Between claiming a turn's input and reaching its own recall, a woken run
reserves, loads the conversation and prepares its first step. Recall (the
routing call and retrieval) runs during that time instead. The run adopts the
early recall only when it would ask exactly the same question (text, recent
exchange, scope, run and model); otherwise the early one is dropped and the run
recalls as before, so the result never differs from recalling in the run.
"""
import asyncio
from dataclasses import dataclass
import time
from typing import Any

from core.log import create_logger

log = create_logger("agent.recall_ahead")

# An unclaimed early recall outlives its turn only briefly, and only so many.
MAX_AGE_SECONDS = 120.0
MAX_ENTRIES = 64


@dataclass(frozen=True)
class RecallInputs:
    """Everything a turn's recall depends on; equal inputs give the same recall."""
    query: str
    recent: tuple[tuple[str, str], ...]
    user_id: str
    workspace_id: str
    project_id: str | None
    include_all_projects: bool
    run_id: str
    model_id: str


@dataclass
class _Early:
    started: float
    inputs: RecallInputs | None = None
    task: asyncio.Task | None = None


_early: dict[tuple[str, str], _Early] = {}


def recall_inputs(session, user_message, history, *, user_id: str, assistant_scope: bool,
                  run_id: str, model_id: str) -> RecallInputs:
    from agent.loop import _recent_exchange, _visible_text
    return RecallInputs(
        query=_visible_text(user_message),
        recent=tuple((item["role"], item["text"]) for item in _recent_exchange(history, user_message.id)),
        user_id=user_id, workspace_id=session.workspace_id,
        project_id=None if assistant_scope else session.project_id,
        include_all_projects=assistant_scope, run_id=run_id, model_id=model_id,
    )


async def recall(inputs: RecallInputs, *, session_id: str, turn_id: str, config) -> dict[str, Any]:
    """The turn's memory context for these inputs (the run's own recall uses this too)."""
    from db.base import get_db_session
    from memory.orchestrator import run_memory_context
    from memory.policy import resolve_access_scope
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=inputs.user_id, workspace_id=inputs.workspace_id,
            project_id=inputs.project_id, include_all_projects=inputs.include_all_projects)
    return await run_memory_context(
        inputs.query, scope, config, session_id=session_id, turn_id=turn_id,
        input_metadata={"run_id": inputs.run_id, "main_model": inputs.model_id},
        recent_context=[{"role": role, "text": text} for role, text in inputs.recent])


def start(lease, batch) -> None:
    """Begin the claimed turn's recall in the background (best effort)."""
    message_ids = [receipt.message_id for receipt in batch.receipts if receipt.message_id]
    if not message_ids:
        return
    _prune()
    key = (lease.session_id, message_ids[-1])
    if key in _early:
        return
    entry = _Early(started=time.monotonic())
    entry.task = asyncio.create_task(_recall_claimed(entry, lease, message_ids[-1]),
                                     name=f"recall-ahead:{lease.session_id}")
    # Unused or failed, it must not leave an unread exception behind.
    entry.task.add_done_callback(lambda done: done.cancelled() or done.exception())
    _early[key] = entry


async def _recall_claimed(entry: _Early, lease, message_id: str) -> dict[str, Any] | None:
    from agent.model_resolve import resolve as resolve_model
    from core.config import get_config
    from db.base import get_db_session
    from db.models.session import Session
    from memory.session_policy import memory_isolated
    from session.agent_event_log import load_canonical_model_surface
    config = get_config()
    user_id = lease.user_id
    if not config.memory.enabled("retrieval_v2", user_id):
        return None
    async with get_db_session() as db:
        session = await db.get(Session, lease.session_id)
    if session is None or session.user_id != user_id:
        return None
    assistant_scope = session.kind == "assistant"
    if memory_isolated(session) and not assistant_scope:
        return None
    # Read only: the run repairs the tail itself; any difference in the recent
    # exchange just means the run will not adopt this recall.
    surface = await load_canonical_model_surface(lease.session_id, user_id=user_id, repair_tail=False)
    history = list(surface.messages)
    user_message = next((message for message in history if message.id == message_id), None)
    if user_message is None:
        return None
    model_id, _ = resolve_model(session.model, config, context=f"session {lease.session_id}")
    entry.inputs = recall_inputs(session, user_message, history, user_id=user_id,
        assistant_scope=assistant_scope, run_id=lease.run_id, model_id=model_id)
    return await recall(entry.inputs, session_id=lease.session_id, turn_id=message_id, config=config.memory)


def adopt(session_id: str, message_id: str, inputs: RecallInputs) -> asyncio.Task | None:
    """The early recall for this turn if it asked the same question, else None (and it is dropped)."""
    entry = _early.pop((session_id, message_id), None)
    if entry is None or entry.task is None:
        return None
    task = entry.task
    if entry.inputs != inputs or task.cancelled() or (task.done() and task.exception() is not None):
        task.cancel()
        return None
    return task


def discard(session_id: str) -> None:
    """Drop this session's unadopted early recalls (its run has ended)."""
    for key in [key for key in _early if key[0] == session_id]:
        entry = _early.pop(key)
        if entry.task is not None:
            entry.task.cancel()


def _prune() -> None:
    now = time.monotonic()
    stale = [key for key, entry in _early.items() if now - entry.started > MAX_AGE_SECONDS]
    excess = max(0, len(_early) - len(stale) - MAX_ENTRIES + 1)
    stale += [key for key in _early if key not in stale][:excess]
    for key in stale:
        entry = _early.pop(key)
        if entry.task is not None:
            entry.task.cancel()
