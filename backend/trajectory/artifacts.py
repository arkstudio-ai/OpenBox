"""Asset references for trajectory producers: no trajectory tables, no bytes.

``artifact.recorded`` carries an ``asset_ref`` that the worker turns into a
payload reference bound to the business asset, so deleting the asset revokes
every recorded use (``asset.deleted``).
"""
from __future__ import annotations

import hashlib
from datetime import datetime

from trajectory.config import enabled
from trajectory.context import TraceContext, current
from trajectory.emitter import emit, emit_after_commit, emit_control
from trajectory.types import iso, now


def artifact_event_id(root_session_id: str, asset_id: str) -> str:
    """One ``artifact.recorded`` per asset per trajectory; assets are immutable objects."""
    return "asset:" + hashlib.sha256(f"{root_session_id}:{asset_id}".encode()).hexdigest()


def _reference(context: TraceContext, *, asset_id: str, oss_key: str, media_type: str | None,
               size_bytes: int | None, name: str | None, role: str) -> tuple[dict, dict, str]:
    media_type = media_type or "application/octet-stream"
    ref = {"asset_id": asset_id, "oss_key": oss_key, "media_type": media_type, "size_bytes": size_bytes}
    if name:
        ref["name"] = name
    event_id = artifact_event_id(context.session_id, asset_id)
    data = {"artifact_id": asset_id, "name": name, "media_type": media_type, "size_bytes": size_bytes,
            "role": role, "availability": "available", "source_asset_id": asset_id, "asset_ref": ref}
    reference = {"artifact_id": asset_id, "availability": "available", "source_asset_id": asset_id,
                 "media_type": media_type, "size_bytes": size_bytes, "event_id": event_id}
    return reference, data, event_id


async def _record_use(db, context: TraceContext, data: dict, event_id: str) -> None:
    """Enqueue the first use of an asset in a trajectory; later uses add no fact.

    All uses share one event id, so a later use (another turn, role or period
    baseline) only reached the worker as a keep-first conflict, and the first
    fact stays what the worker projects. A use counts once its transaction
    commits, or once the emitter accepts it without ``db``. Across processes
    the worker still keeps the first copy.
    """
    from trajectory.producers import claim_once, release_claim
    key = ("artifact.recorded", event_id)
    if not claim_once(db, key):
        return
    if db is None:
        enqueued = emit("artifact.recorded", data, context=context, event_id=event_id)
    else:
        enqueued = emit_after_commit(db, "artifact.recorded", data, context=context, event_id=event_id)
    if enqueued is None:
        release_claim(db, key)


def _unavailable(context: TraceContext, asset) -> dict | None:
    """The reference of an asset use that is not recorded, or None when it is."""
    if asset.user_id != context.user_id and not (
            context.workspace_id and asset.workspace_id == context.workspace_id):
        return {"artifact_id": asset.id, "availability": "not_recorded", "reason": "outside_execution_scope"}
    if asset.is_deleted or asset.deleted_at:
        return {"artifact_id": asset.id, "availability": "deleted"}
    if asset.status != "ready":
        return {"artifact_id": asset.id, "availability": "pending"}
    return None


async def capture_asset_in_tx(db, context: TraceContext | None, asset, *, role: str = "result") -> dict | None:
    """Record one use of an asset when ``db`` commits."""
    if context is None or not enabled(context.user_id):
        return None
    skipped = _unavailable(context, asset)
    if skipped is not None:
        return skipped
    reference, data, event_id = _reference(context, asset_id=asset.id, oss_key=asset.oss_key,
                                           media_type=asset.mime, size_bytes=asset.size,
                                           name=asset.name, role=role)
    await _record_use(db, context, data, event_id)
    return reference


async def capture_asset(context: TraceContext | None, asset, *, role: str = "input") -> dict | None:
    """Record one use of an asset that no business write carries (a provider dispatch)."""
    if context is None or not enabled(context.user_id):
        return None
    skipped = _unavailable(context, asset)
    if skipped is not None:
        return skipped
    reference, data, event_id = _reference(context, asset_id=asset.id, oss_key=asset.oss_key,
                                           media_type=asset.mime, size_bytes=asset.size,
                                           name=asset.name, role=role)
    await _record_use(None, context, data, event_id)
    return reference


async def capture_asset_reference(context: TraceContext | None, *, asset_id: str, oss_key: str,
                                  media_type: str | None, size_bytes: int | None, name: str | None = None,
                                  role: str = "input") -> dict | None:
    """Record one use of an owned, ready asset whose row the caller already checked."""
    if context is None or not enabled(context.user_id):
        return None
    reference, data, event_id = _reference(context, asset_id=asset_id, oss_key=oss_key, media_type=media_type,
                                           size_bytes=size_bytes, name=name, role=role)
    await _record_use(None, context, data, event_id)
    return reference


async def capture_asset_ids_in_tx(db, context: TraceContext | None, asset_ids: list[str], *,
                                  role: str = "input") -> dict:
    from sqlalchemy import select
    from db.models.file_asset import FileAsset
    if context is None or not enabled(context.user_id) or not asset_ids:
        return {}
    identifiers = list(dict.fromkeys(asset_ids))
    rows = {row.id: row for row in (await db.scalars(select(FileAsset).where(FileAsset.id.in_(identifiers)))).all()}
    result = {}
    for asset_id in identifiers:
        asset = rows.get(asset_id)
        result[asset_id] = (await capture_asset_in_tx(db, context, asset, role=role) if asset is not None else
                            {"artifact_id": asset_id, "availability": "not_recorded", "reason": "asset_missing"})
    return result


async def capture_result_asset_in_tx(db, ctx, asset, *, request_id: str | None = None):
    """A tool output keeps the exact producer identity, including callbacks."""
    if not getattr(ctx, "session_id", None) or not enabled(ctx.user_id):
        return None
    from trajectory.producers import activity_context, saved_context
    trace = await activity_context(db, ctx.user_id, ctx.session_id,
                                   saved=saved_context(getattr(ctx, "trace_context", None)))
    if trace is not None and request_id is not None:
        trace = trace.derive(request_id=request_id)
    return await capture_asset_in_tx(db, trace, asset)


def revoke_asset_in_tx(db, asset_id: str, *, user_id: str, deleted_at: datetime | None = None) -> bool:
    """Report an asset deletion when ``db`` commits; the worker revokes every recorded use."""
    return emit_control({"type": "asset.deleted", "asset_id": asset_id, "user_id": user_id,
                         "deleted_at": iso(deleted_at or now())}, db=db)
