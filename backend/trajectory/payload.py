"""Immutable contents: transactional database staging, then Blob archival.

New facts never depend on a remote request while holding a trajectory lock.
Pending bytes are durable and readable on every worker through the database.
"""
import asyncio
import hashlib
import inspect
import json
from uuid import uuid4
from sqlalchemy import select
from db.models.trajectory import TrajectoryPayload
from trajectory.types import CorruptContent, OwnershipError, canonical, now

_override = None
_store = None


def set_storage(storage) -> None:
    """Dependency injection for application bootstrap and isolated tests."""
    global _override
    _override = storage


def get_storage():
    global _store
    if _override is not None:
        return _override
    if _store is not None:
        return _store
    from core.config import get_config
    config = get_config()
    if config.blob_provider == "local" or not config.jwt_secret:
        from blob.local_blob import LocalBlobStorage
        path = config.blob_local_path if config.jwt_secret else ".openbox/trajectory-blobs"
        _store = LocalBlobStorage(path)
    elif config.blob_provider == "gcs":
        from blob.gcs_blob import GCSBlobStorage
        _store = GCSBlobStorage(config.gcs_bucket)
    elif config.blob_provider == "azure":
        if not config.blob_azure_connection_string:
            raise RuntimeError("Trajectory blob storage is not configured")
        from blob.azure_blob import AzureBlobStorage
        _store = AzureBlobStorage(config.blob_azure_connection_string, config.blob_azure_container)
    else:
        raise RuntimeError("Unsupported trajectory blob storage")
    return _store


async def upload_bytes(key: str, data: bytes, media_type: str) -> None:
    storage = get_storage()
    params = inspect.signature(storage.upload).parameters
    if "content_type" in params:
        await storage.upload(key, data, content_type=media_type)
    else:
        await storage.upload(key, data, metadata={"content_type": media_type, "sha256": hashlib.sha256(data).hexdigest()})


async def download_bytes(key: str) -> bytes:
    result = get_storage().download(key)
    if inspect.isawaitable(result):
        result = await result
    if hasattr(result, "__aiter__"):
        return b"".join([chunk async for chunk in result])
    return bytes(result)


def reference(row: TrajectoryPayload) -> dict:
    return {"payload_id": row.payload_id, "sha256": row.sha256,
            "size_bytes": row.size_bytes, "media_type": row.media_type,
            "availability": row.availability}


async def store_bytes(db, trajectory_id: str, content: bytes, *, first_seq: int,
                      media_type: str = "application/octet-stream", source_asset_id: str | None = None) -> dict:
    sha = hashlib.sha256(content).hexdigest()
    # Dedup stays inside the authorized trajectory. Explicitly removed copies
    # can never be resurrected by a delayed identical callback.
    existing = await db.scalar(select(TrajectoryPayload).where(
        TrajectoryPayload.trajectory_id == trajectory_id, TrajectoryPayload.sha256 == sha,
        TrajectoryPayload.media_type == media_type, TrajectoryPayload.source_asset_id == source_asset_id))
    if existing is not None:
        if existing.availability != "available":
            raise OwnershipError("Removed trajectory content cannot be recreated")
        return reference(existing)
    payload_id = f"pld_{uuid4().hex}"
    key = f"trajectories/{trajectory_id}/payloads/{payload_id}/{sha}"
    row = TrajectoryPayload(payload_id=payload_id, trajectory_id=trajectory_id, sha256=sha,
        storage_key=key, size_bytes=len(content), media_type=media_type,
        storage_status="pending", content=content,
        encoding="utf-8" if media_type == "application/json" else "binary",
        availability="available", first_seq=first_seq, source_asset_id=source_asset_id, created_at=now())
    db.add(row)
    await db.flush()
    return reference(row)


async def store_json(db, trajectory_id: str, value: dict, *, first_seq: int) -> dict:
    return {"$payload": await store_bytes(db, trajectory_id, canonical(value), first_seq=first_seq, media_type="application/json")}


async def validate_payload(db, trajectory_id: str, payload_id: str, *, through_seq: int) -> TrajectoryPayload:
    row = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
        TrajectoryPayload.payload_id == payload_id, TrajectoryPayload.first_seq <= through_seq))
    if row is None:
        raise LookupError("Trajectory payload is not available at this position")
    if row.availability != "available":
        raise FileNotFoundError("Trajectory content has been deleted")
    if row.source_asset_id:
        from db.models.file_asset import FileAsset
        asset = await db.get(FileAsset, row.source_asset_id)
        if asset is None or getattr(asset, "is_deleted", False) or getattr(asset, "deleted_at", None) or getattr(asset, "status", "ready") == "deleted":
            raise FileNotFoundError("Source attachment has been deleted")
    return row


async def read_payload(db, trajectory_id: str, payload_id: str, *, through_seq: int) -> tuple[TrajectoryPayload, bytes]:
    row = await validate_payload(db, trajectory_id, payload_id, through_seq=through_seq)
    try:
        content = bytes(row.content) if row.content is not None else await download_bytes(row.storage_key)
    except FileNotFoundError as exc:
        raise CorruptContent("Retained trajectory blob is missing") from exc
    if hashlib.sha256(content).hexdigest() != row.sha256:
        raise CorruptContent("Trajectory content digest mismatch")
    return row, content


async def expand(db, trajectory_id: str, data: dict, *, through_seq: int) -> dict:
    if "$payload" not in data:
        return await visible_references(db, trajectory_id, data, through_seq=through_seq)
    info = data["$payload"]
    try:
        _, content = await read_payload(db, trajectory_id, info["payload_id"], through_seq=through_seq)
    except FileNotFoundError:
        return {"$payload": {**info, "availability": "deleted", "reason": "explicitly_deleted"}}
    try:
        result = json.loads(content)
    except (ValueError, UnicodeDecodeError) as exc:
        raise CorruptContent("Invalid JSON in trajectory payload") from exc
    if not isinstance(result, dict):
        raise CorruptContent("Trajectory JSON payload is not an object")
    return await visible_references(db, trajectory_id, result, through_seq=through_seq)


async def visible_references(db, trajectory_id: str, value, *, through_seq: int, _cache=None):
    """Current deletion rules apply even while viewing a historical prefix."""
    cache = {} if _cache is None else _cache
    if isinstance(value, list):
        return [await visible_references(db, trajectory_id, child, through_seq=through_seq, _cache=cache) for child in value]
    if not isinstance(value, dict):
        return value
    if isinstance(value.get("payload_id"), str) and "sha256" in value:
        identity = value["payload_id"]
        if identity not in cache:
            cache[identity] = await db.scalar(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
                TrajectoryPayload.payload_id == identity, TrajectoryPayload.first_seq <= through_seq))
        row = cache[identity]
        return {**value, "availability": row.availability if row else "not_recorded",
                **({"reason": "explicitly_deleted"} if row and row.availability == "deleted" else {})}
    result = {key: await visible_references(db, trajectory_id, child, through_seq=through_seq, _cache=cache) for key, child in value.items()}
    if isinstance(result.get("payload"), dict) and result["payload"].get("availability") == "deleted":
        result["availability"] = "deleted"
    return result


async def expand_pages(db, trajectory_id, references, *, through_seq):
    ids = [item["$payload"]["payload_id"] for item in references]
    rows = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.trajectory_id == trajectory_id,
        TrajectoryPayload.payload_id.in_(ids), TrajectoryPayload.first_seq <= through_seq))).all()
    by_id = {row.payload_id: row for row in rows}
    semaphore = asyncio.Semaphore(8)
    async def page(identity):
        row = by_id.get(identity)
        if row is None or row.availability != "available":
            raise CorruptContent("Checkpoint page is unavailable")
        async with semaphore:
            content = bytes(row.content) if row.content is not None else await download_bytes(row.storage_key)
        if hashlib.sha256(content).hexdigest() != row.sha256:
            raise CorruptContent("Checkpoint page digest mismatch")
        try:
            value = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise CorruptContent("Invalid checkpoint page") from exc
        if not isinstance(value, dict) or not isinstance(value.get("records"), dict):
            raise CorruptContent("Invalid checkpoint page shape")
        return value
    return await asyncio.gather(*(page(identity) for identity in ids))


async def delete_for_asset(db, asset_id: str) -> None:
    """Called in the attachment deletion transaction; hide before physical GC."""
    rows = (await db.scalars(select(TrajectoryPayload).where(TrajectoryPayload.source_asset_id == asset_id))).all()
    for row in rows:
        row.availability = "deleted"
        row.deleted_at = now()
        row.content = None


async def drain_payloads(limit: int = 100) -> dict:
    """Archive committed staging rows without retaining any DB transaction.

Failures preserve the authoritative database bytes for the next attempt.
Updates are conditional so concurrent explicit deletion always wins.
"""
    from db.base import get_db_session
    from sqlalchemy import update
    async with get_db_session() as db:
        rows = (await db.scalars(select(TrajectoryPayload).where(
            TrajectoryPayload.storage_status == "pending", TrajectoryPayload.availability == "available")
            .order_by(TrajectoryPayload.created_at).limit(limit))).all()
        jobs = [(row.payload_id, row.storage_key, bytes(row.content), row.media_type, row.sha256)
                for row in rows if row.content is not None]
    archived, failed = 0, 0
    for identity, key, content, media_type, sha in jobs:
        try:
            if hashlib.sha256(content).hexdigest() != sha:
                raise CorruptContent("Staged trajectory payload digest mismatch")
            await upload_bytes(key, content, media_type)
            if hashlib.sha256(await download_bytes(key)).hexdigest() != sha:
                raise CorruptContent("Archived trajectory blob digest mismatch")
            async with get_db_session() as db:
                result = await db.execute(update(TrajectoryPayload).where(
                    TrajectoryPayload.payload_id == identity, TrajectoryPayload.sha256 == sha,
                    TrajectoryPayload.availability == "available", TrajectoryPayload.storage_status == "pending")
                    .values(content=None, storage_status="stored"))
                archived += result.rowcount
                still_live = await db.scalar(select(TrajectoryPayload.availability).where(TrajectoryPayload.payload_id == identity))
            if still_live != "available":
                await get_storage().delete(key)
        except Exception:
            failed += 1
    return {"archived": archived, "failed": failed}


_archive_task = None


async def start_archive_worker():
    global _archive_task
    if _archive_task is not None and not _archive_task.done():
        return _archive_task
    async def work():
        from core.log import create_logger
        from trajectory.lifecycle import purge_deleted_content
        from trajectory.repository import drain_checkpoints
        log = create_logger("trajectory.archive")
        while True:
            try:
                result = await drain_payloads()
                await drain_checkpoints()
                await purge_deleted_content()
                if result["failed"]:
                    log.warning("Trajectory archival retry pending count=%s", result["failed"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trajectory archive worker retry error_type=%s", type(exc).__name__)
            await asyncio.sleep(5)
    _archive_task = asyncio.create_task(work())
    return _archive_task


async def stop_archive_worker():
    global _archive_task
    if _archive_task is not None:
        _archive_task.cancel()
        try:
            await _archive_task
        except asyncio.CancelledError:
            pass
        _archive_task = None
