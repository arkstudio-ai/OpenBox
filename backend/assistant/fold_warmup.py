"""Fold the most recently active assistant conversations once at startup.

The assistant conversation is one event log that only grows. After a restart
the process-local fold cache is empty, so the user's first turn would replay
the whole history before answering. Folding the newest conversations in the
background moves that replay off the user's turn.
"""
import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from assistant.transactions import source_snapshot
from core.log import create_logger
from db.base import get_db_session
from db.models.session import Session
from session import agent_event_log

log = create_logger("assistant.fold_warmup")

WARM_LIMIT = 8
_task: asyncio.Task | None = None


async def warm_assistant_folds(limit: int = WARM_LIMIT) -> int:
    """Load and cache the folds of the newest assistant conversations.

    Returns how many ended up in the cache. Each fold is read in its own
    read-only snapshot; one that cannot be folded is skipped.
    """
    limit = min(limit, agent_event_log.FOLD_CACHE_SIZE)
    if limit <= 0:
        return 0
    async with get_db_session() as db:
        rows = (await db.execute(
            select(Session.id, Session.user_id)
            .where(Session.kind == "assistant", Session.is_deleted.is_not(True))
            .order_by(Session.updated_at.desc())
            .limit(limit)
        )).all()
    warmed = 0
    # Oldest first, so the most recently active stays freshest in the cache.
    for session_id, user_id in reversed(rows):
        try:
            async with source_snapshot() as (db, _checks):
                await agent_event_log.load_event_fold_locked(db, SimpleNamespace(id=session_id, user_id=user_id))
        except agent_event_log.AgentEventProjectionError:
            continue
        except Exception as exc:  # a cache only; the turn folds it anyway
            log.debug("assistant fold not warmed (%s)", type(exc).__name__)
            continue
        warmed += (session_id, user_id) in agent_event_log._FOLD_CACHE
    return warmed


async def _warm() -> None:
    try:
        warmed = await warm_assistant_folds()
        if warmed:
            log.info("Warmed %s assistant conversation fold(s)", warmed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("Assistant fold warm-up failed: %s", type(exc).__name__)


def schedule_fold_warmup() -> None:
    """Start the warm-up in the background; startup does not wait for it."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.get_running_loop().create_task(_warm())


async def stop_fold_warmup() -> None:
    global _task
    task, _task = _task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
