"""Small SQL change clock; notifications are hints, never the freshness authority."""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.models.memory_protocol import MemoryChangeClock, MemoryWriteReceipt
from memory.redaction import json_hash


async def version(db, user_id: str) -> int:
    return int(await db.scalar(select(MemoryChangeClock.revision).where(
        MemoryChangeClock.user_id == user_id)) or 0)


async def revocation_epoch(db, user_id: str, workspace_id: str) -> int:
    """Immutable revoke/correction receipts fence transient voice history.

    Additions and profile edits advance the normal clock but must not discard
    an otherwise valid conversation. Tombstones persist after index cleanup.
    """
    from db.models.memory_v2 import MemoryTombstone
    return int(await db.scalar(select(func.count()).select_from(MemoryTombstone).where(
        MemoryTombstone.user_id == user_id, MemoryTombstone.workspace_id == workspace_id)) or 0)


async def changed(db, user_id: str) -> int:
    """Call inside the authority transaction, after acquiring its actor lock."""
    insert = sqlite_insert if db.get_bind().dialect.name == "sqlite" else pg_insert
    statement = insert(MemoryChangeClock).values(user_id=user_id, revision=1,
        updated_at=datetime.now(timezone.utc)).on_conflict_do_update(index_elements=["user_id"],
        set_={"revision": MemoryChangeClock.revision + 1, "updated_at": datetime.now(timezone.utc)})
    return int((await db.execute(statement.returning(MemoryChangeClock.revision))).scalar_one())


async def previous_receipt(db, user_id: str, operation_id: str, payload: dict) -> dict | None:
    from memory.service import MemoryConflict
    row = await db.get(MemoryWriteReceipt, (user_id, operation_id))
    if row is None:
        return None
    if row.payload_hash != json_hash(payload):
        raise MemoryConflict("This operation ID already belongs to a different command")
    return dict(row.result)


async def record_receipt(db, *, user_id: str, operation_id: str, kind: str, payload: dict,
                         result: dict, source_refs=(), occurred_at=None) -> dict:
    receipt = {**result, "operation_id": operation_id}
    db.add(MemoryWriteReceipt(user_id=user_id, operation_id=operation_id, kind=kind,
        payload_hash=json_hash(payload), state=receipt["state"], result=receipt,
        source_refs=list(source_refs), occurred_at=occurred_at, created_at=datetime.now(timezone.utc)))
    await db.flush()
    return receipt
