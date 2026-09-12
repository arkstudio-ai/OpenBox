"""Retained attachment versions, distinct from mutable sandbox files and URLs."""
from __future__ import annotations

import hashlib
import base64
import binascii
import re

import httpx
from sqlalchemy import select
from sqlalchemy.orm import defer

from trajectory import TraceContext, enabled, ensure_trajectory_in_tx, record
from trajectory.payload import delete_for_asset, reference as payload_reference, store_bytes
from trajectory.types import OwnershipError


async def read_asset_bytes(asset) -> bytes:
    """Read only the server-owned object's key; never follow an arbitrary URL."""
    from core.oss import get_oss
    url = get_oss().presign_get(asset.oss_key, expires_sec=120)
    async with httpx.AsyncClient(timeout=120, trust_env=False, follow_redirects=False) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


async def _retained_asset(db, context: TraceContext, asset_id: str, content: bytes | None = None):
    from db.models.trajectory import SessionTrajectory, TrajectoryPayload, TrajectoryEvent
    query = select(TrajectoryPayload).options(defer(TrajectoryPayload.content)).join(
        SessionTrajectory, SessionTrajectory.id == TrajectoryPayload.trajectory_id).where(
            SessionTrajectory.session_id == context.session_id,
            SessionTrajectory.user_id == context.user_id,
            SessionTrajectory.deleted_at.is_(None),
            TrajectoryPayload.source_asset_id == asset_id)
    if content is not None:
        query = query.where(TrajectoryPayload.sha256 == hashlib.sha256(content).hexdigest())
    # A request may retain derived frames under the original source asset for
    # deletion. Only a producer's artifact.recorded fact identifies an actual
    # version of that asset; a frame must never be reused as the source video.
    candidates = (await db.scalars(query.order_by(TrajectoryPayload.first_seq.desc()))).all()
    for offset in range(0, len(candidates), 200):
        group = candidates[offset:offset + 200]
        identities = {f"asset:{hashlib.sha256(f'{row.trajectory_id}:{asset_id}:{row.sha256}'.encode()).hexdigest()}": row
                      for row in group}
        recorded = set((await db.scalars(select(TrajectoryEvent.event_id).where(
            TrajectoryEvent.event_id.in_(identities), TrajectoryEvent.type == "artifact.recorded"))).all())
        for identity, row in identities.items():
            if identity in recorded:
                return row
    return None


async def prepare_asset_ids(user_id: str, workspace_id: str | None, asset_ids: list[str], *,
                            root_session_id: str | None = None) -> dict[str, bytes]:
    """Fetch new source objects before entering a session's write transaction.

    The transaction still rechecks the asset's ownership and deletion state;
    previously retained immutable versions need no source-network request.
    """
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    if not enabled(user_id) or not asset_ids:
        return {}
    pending = []
    async with get_db_session() as db:
        for asset_id in dict.fromkeys(asset_ids):
            asset = await db.get(FileAsset, asset_id)
            if asset is None or asset.is_deleted or asset.deleted_at or asset.status != "ready":
                continue
            if asset.user_id != user_id and not (workspace_id and asset.workspace_id == workspace_id):
                raise OwnershipError("Attachment does not belong to the execution scope")
            if root_session_id and await _retained_asset(db, TraceContext(user_id, root_session_id), asset_id):
                continue
            pending.append(asset)
    return {asset.id: await read_asset_bytes(asset) for asset in pending}


async def capture_asset_in_tx(db, context: TraceContext | None, asset, *, content: bytes | None = None,
                              role: str = "result") -> dict | None:
    if context is None or not enabled(context.user_id):
        return None
    if asset.user_id != context.user_id and not (
            context.workspace_id and asset.workspace_id == context.workspace_id):
        raise OwnershipError("Attachment does not belong to the execution scope")
    if asset.is_deleted or asset.deleted_at:
        return {"artifact_id": asset.id, "availability": "deleted"}
    if asset.status != "ready":
        return {"artifact_id": asset.id, "availability": "pending"}
    existing = await _retained_asset(db, context, asset.id, content)
    if existing is not None:
        return payload_reference(existing)
    if content is None:
        from trajectory.types import RecordingError
        raise RecordingError("Attachment bytes must be prepared before the recording transaction")
    trajectory = await ensure_trajectory_in_tx(db, context)
    reference = await store_bytes(db, trajectory.id, content, first_seq=trajectory.next_seq,
                                  media_type=asset.mime, source_asset_id=asset.id)
    data = {"artifact_id": asset.id, "name": asset.name, "media_type": asset.mime,
            "size_bytes": len(content), "role": role, "availability": "available",
            "sha256": reference["sha256"], "payload": reference, "source_asset_id": asset.id}
    identity = hashlib.sha256(f"{trajectory.id}:{asset.id}:{reference['sha256']}".encode()).hexdigest()
    await record("artifact.recorded", data, context=context, db=db, event_id=f"asset:{identity}")
    return reference


async def capture_asset_ids_in_tx(db, context: TraceContext | None, asset_ids: list[str], *,
                                role="input", prepared: dict[str, bytes] | None = None):
    from db.models.file_asset import FileAsset
    if context is None or not enabled(context.user_id):
        return {}
    result = {}
    for asset_id in dict.fromkeys(asset_ids):
        asset = await db.get(FileAsset, asset_id)
        result[asset_id] = (await capture_asset_in_tx(db, context, asset, role=role,
                                                     content=(prepared or {}).get(asset_id)) if asset else
                            {"artifact_id": asset_id, "availability": "not_recorded", "reason": "asset_missing"})
    return result


async def capture_result_asset_in_tx(db, ctx, asset, *, content: bytes | None,
                                   request_id: str | None = None):
    """A tool output keeps the exact producer identity, including callbacks."""
    if not getattr(ctx, "session_id", None) or not enabled(ctx.user_id):
        return None
    from trajectory.producers import activity_context, saved_context
    trace = await activity_context(db, ctx.user_id, ctx.session_id,
                                   saved=saved_context(getattr(ctx, "trace_context", None)))
    if trace is not None and request_id is not None:
        trace = trace.derive(request_id=request_id)
    return await capture_asset_in_tx(db, trace, asset, content=content)


async def retain_request_media_in_tx(db, context: TraceContext, snapshot, *, first_seq: int,
                                     source_asset_ids: dict[str, str] | None = None):
    """Replace inline media only in the retained request, preserving deletion.

    Inputs have already reached the model adapter. Decode locally without OSS
    I/O; the caller commits these bytes and request.prepared together. A known
    deleted source must never become a fresh, unbound copy of the same image.
    Producers of transformed media supply digest -> original asset identity.
    """
    from db.models.file_asset import FileAsset
    from db.models.trajectory import TrajectoryPayload

    trajectory = await ensure_trajectory_in_tx(db, context)
    sources = source_asset_ids or {}
    cache = {}

    async def retain(encoded: str, media_type: str):
        cache_key = (encoded, media_type)
        if cache_key in cache:
            return cache[cache_key]
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            return {"$media": {"availability": "not_recorded", "reason": "invalid_base64",
                               "media_type": media_type}}
        sha = hashlib.sha256(content).hexdigest()
        source_id = sources.get(sha)
        candidates = (await db.scalars(select(TrajectoryPayload).options(defer(TrajectoryPayload.content)).where(
            TrajectoryPayload.trajectory_id == trajectory.id,
            TrajectoryPayload.sha256 == sha,
            TrajectoryPayload.source_asset_id.is_not(None),
        ))).all()
        candidate_sources = {row.source_asset_id for row in candidates}
        if source_id is None and len(candidate_sources) == 1:
            source_id = next(iter(candidate_sources))
        if source_id is None and len(candidate_sources) > 1:
            # Without a producer binding, selecting an identical live copy
            # could bypass deletion of the source actually used by the model.
            result = {"$media": {"availability": "not_recorded", "reason": "ambiguous_asset_source",
                                  "sha256": sha, "media_type": media_type},
                      "source_kind": "ambiguous_asset", "original_encoding": "base64"}
            cache[cache_key] = result
            return result
        if source_id is not None:
            asset = await db.get(FileAsset, source_id)
            if asset is None or asset.is_deleted or asset.deleted_at or asset.status == "deleted":
                result = {"$media": {"availability": "deleted", "reason": "source_attachment_deleted",
                                      "sha256": sha, "media_type": media_type},
                          "source_asset_id": source_id, "source_kind": "asset",
                          "original_encoding": "base64"}
                cache[cache_key] = result
                return result
            if asset.user_id != context.user_id and not (
                    context.workspace_id and asset.workspace_id == context.workspace_id):
                raise OwnershipError("Request media source does not belong to the execution scope")
            reference = next((payload_reference(row) for row in candidates
                              if row.source_asset_id == source_id), None)
        else:
            reference = None
        if reference is None:
            reference = await store_bytes(db, trajectory.id, content, first_seq=first_seq,
                                          media_type=media_type, source_asset_id=source_id)
        result = {"$media": reference, "source_kind": "asset" if source_id else "inline_non_asset",
                  "original_encoding": "base64", "declared_media_type": media_type}
        if source_id:
            result["source_asset_id"] = source_id
        cache[cache_key] = result
        return result

    async def walk(value, parent_key=None):
        if isinstance(value, str):
            match = re.fullmatch(r"data:([^;,]+)(?:;[^,;]+)*;base64,(.*)", value, re.DOTALL)
            if match:
                return await retain(match[2], match[1])
            return value
        if isinstance(value, list):
            return [await walk(child, parent_key) for child in value]
        if isinstance(value, dict):
            if value.get("type") == "base64" and isinstance(value.get("data"), str):
                return await retain(value["data"], value.get("media_type") or "application/octet-stream")
            if parent_key == "input_audio" and isinstance(value.get("data"), str):
                return await retain(value["data"], f"audio/{value.get('format') or 'wav'}")
            return {key: await walk(child, key) for key, child in value.items()}
        return value

    return await walk(snapshot)


async def revoke_asset_in_tx(db, asset_id: str) -> None:
    from db.models.trajectory import SessionTrajectory, TrajectoryPayload
    from db.models.session import Session
    rows = (await db.scalars(select(SessionTrajectory).join(
        TrajectoryPayload, TrajectoryPayload.trajectory_id == SessionTrajectory.id).where(
            TrajectoryPayload.source_asset_id == asset_id).distinct())).all()
    await delete_for_asset(db, asset_id)
    for trajectory in rows:
        session = await db.get(Session, trajectory.session_id)
        if session is None or session.is_deleted:
            continue
        identity = hashlib.sha256(f"{trajectory.id}:{asset_id}:deleted".encode()).hexdigest()
        await record("artifact.removed", {"artifact_id": asset_id, "availability": "deleted",
                                          "reason": "explicitly_deleted"},
                     context=TraceContext(user_id=trajectory.user_id, session_id=trajectory.session_id),
                     db=db, event_id=f"asset:{identity}")
