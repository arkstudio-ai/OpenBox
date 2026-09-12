"""Explicit identity propagation for business operations and durable callbacks.

These helpers do not listen to the UI bus. Callers invoke them at the actual
transaction or dispatch boundary and keep the returned context with the job.
"""
from __future__ import annotations

from trajectory import TraceContext, context_for_session, current, enabled, record


async def activity_context(db, user_id: str, session_id: str, *, saved: dict | None = None,
                           **ids) -> TraceContext | None:
    if not enabled(user_id):
        return None
    context = TraceContext.from_dict(saved) if saved else current()
    if context is not None:
        if context.user_id != user_id:
            raise ValueError("Trajectory activity owner does not match")
        if (context.source_session_id or context.session_id) == session_id:
            return context.derive(**ids)
        if saved:
            raise ValueError("Stored trajectory activity source does not match")
    return await context_for_session(db, user_id, session_id, **ids)


async def append_activity(db, event_type: str, data: dict, *, user_id: str, session_id: str,
                          saved: dict | None = None, event_id: str | None = None, **ids):
    context = await activity_context(db, user_id, session_id, saved=saved, **ids)
    if context is not None:
        await record(event_type, data, db=db, context=context, event_id=event_id)
    return context


def saved_context(context: TraceContext | None = None) -> dict | None:
    context = context or current()
    return context.to_dict() if context and enabled(context.user_id) else None
