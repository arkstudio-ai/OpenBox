"""Producer entry points kept for existing call sites: record, record_stream, flush.

They hand facts to the fail-open spool emitter (SPEC §5.3): no trajectory SQL,
no receipts, nothing raised into business code. A fact bound to a business
transaction is enqueued only when that transaction commits.
"""
from trajectory.config import enabled
from trajectory.context import TraceContext, current
from trajectory.emitter import completed_receipt, emit, emit_after_commit, emit_stream, flush_spool
from trajectory.types import OwnershipError


async def context_for_session(db, user_id: str, session_id: str, **ids) -> TraceContext:
    """The owned root context of a business session (a business read, never a trace read)."""
    from db.models.session import Session
    row = await db.get(Session, session_id)
    if row is None or row.user_id != user_id or row.is_deleted:
        raise OwnershipError("Trajectory source session is missing, deleted or belongs to another owner")
    inherited = current()
    if inherited is not None and inherited.user_id == user_id and inherited.source_session_id == session_id:
        return inherited.derive(**ids)
    return TraceContext(user_id=user_id, session_id=session_id, source_session_id=session_id,
                        workspace_id=row.workspace_id, **ids)


async def record(type: str, data: dict, *, context: TraceContext | None = None,
                 db=None, event_id: str | None = None, occurred_at=None, **ids) -> None:
    """One fact: enqueued when ``db`` commits, or at once without ``db``."""
    context = context or current()
    if context is None or not enabled(context.user_id):
        return None
    if db is not None:
        emit_after_commit(db, type, data, context=context, event_id=event_id, occurred_at=occurred_at, **ids)
    else:
        emit(type, data, context=context, event_id=event_id, occurred_at=occurred_at, **ids)
    return None


def record_stream(context: TraceContext, event: dict):
    """Enqueue one stream chunk; the returned receipt is already resolved with ``None``."""
    emit_stream(context, event)
    return completed_receipt()


async def flush(context: TraceContext | None = None) -> str:
    """Wait for the emitter to write what was enqueued so far (off the event loop)."""
    return await flush_spool()
