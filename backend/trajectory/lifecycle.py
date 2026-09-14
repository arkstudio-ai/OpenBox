"""Explicit deletion and durable cleanup of retained trajectory content."""
from sqlalchemy import delete, select
from db.base import get_db_session
from db.models.trajectory import (SessionTrajectory, TrajectoryEvent, TrajectoryRecord,
    TrajectoryCheckpoint, TrajectorySessionSummary, TrajectoryPayload, TrajectoryExport)
from trajectory.payload import get_storage
from trajectory.types import OwnershipError, now


async def delete_trajectory_in_tx(db, session_id: str, user_id: str) -> None:
    row = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.session_id == session_id).with_for_update())
    if row is None:
        return
    if row.user_id != user_id:
        raise OwnershipError("Cannot delete another owner's trajectory")
    row.deleted_at, row.recording_status = now(), "deleted"
    # Keep a tombstone to reject late callbacks. A child session has no root
    # container of its own, so deleting it cannot erase its parent's facts.
    for model in (TrajectoryEvent, TrajectoryRecord, TrajectoryCheckpoint, TrajectorySessionSummary):
        await db.execute(delete(model).where(model.trajectory_id == row.id))
    for payload in (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == row.id))).all():
        payload.availability, payload.deleted_at = "deleted", now()
        payload.content = None
    for export in (await db.scalars(select(TrajectoryExport).where(TrajectoryExport.trajectory_id == row.id))).all():
        export.status, export.updated_at = "deleted", now()
    db.sync_session.info.setdefault("trajectory_notifications", {})[row.id] = {
        "user_id": user_id, "owner_user_id": user_id, "session_id": session_id,
        "trajectory_id": row.id, "committed_seq": str(row.committed_seq), "deleted": True}


async def purge_deleted_content(limit: int = 100) -> int:
    """Retryable physical GC; no live reference is selected for deletion.

    The availability tombstone commits first. Blob I/O uses no database row
    lock, and an unsuccessful delete retains the durable cleanup work item.
    """
    async with get_db_session() as db:
        payloads = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.availability == "deleted",
            TrajectoryPayload.storage_key != "").limit(limit))).all()
        exports = (await db.scalars(select(TrajectoryExport).where(TrajectoryExport.status == "deleted",
            TrajectoryExport.storage_key.is_not(None)).limit(limit))).all()
        jobs = [("payload", row.payload_id, row.storage_key) for row in payloads] + [("export", row.id, row.storage_key) for row in exports]
    removed = 0
    for kind, identity, key in jobs:
        try:
            await get_storage().delete(key)
        except Exception:
            continue
        async with get_db_session() as db:
            if kind == "payload":
                row = await db.get(TrajectoryPayload, identity)
                if row is not None and row.availability == "deleted":
                    row.storage_key = ""
            else:
                row = await db.get(TrajectoryExport, identity)
                if row is not None and row.status == "deleted":
                    row.storage_key = None
        removed += 1
    return removed
