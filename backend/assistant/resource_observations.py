"""Bind graphical input to a captured frame present in the original request.

An observation is provenance, not a grant of exclusive physical access. The
remote/native channels still need their own fencing before human takeover.
"""
from dataclasses import asdict
import re

from sqlalchemy import select

from assistant import resource_control as controls
from assistant.policy import AssistantError
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from db.models.file_asset import FileAsset
from db.models.part import Part


def unavailable():
    return AssistantError(423, "RESOURCE_OBSERVATION_REQUIRED",
        "Take a new screenshot before desktop input; the original request has no current verified frame")


def _hash(value):
    from agent.effect_ledger import request_hash
    return request_hash(value)


def _asset_source(asset):
    return {key: getattr(asset, key) for key in (
        "id", "user_id", "workspace_id", "session_id", "oss_key", "mime", "size", "source", "transient")}


def _geometry(value):
    result = {}
    for name in ("native", "scaled"):
        pair = value.get(name) if isinstance(value, dict) else None
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or any(type(v) is not int or not 1 <= v <= 32768 for v in pair)):
            raise unavailable()
        result[name] = list(pair)
    return result


def reference(event):
    return {"event_id": event.id, "digest": _hash(event.payload)}


async def load_locked(db, ref, *, user_id, session_id, fence, journal_id):
    if (not isinstance(ref, dict) or set(ref) != {"event_id", "digest"}
            or not isinstance(ref["event_id"], str) or not ref["event_id"]
            or not isinstance(ref["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["digest"])):
        raise unavailable()
    event = await db.get(AgentEvent, ref["event_id"])
    if (event is None or event.kind != "resource.observed" or event.user_id != user_id
            or event.session_id != session_id or reference(event) != ref):
        raise unavailable()
    p = event.payload
    if (p.get("fence") != asdict(fence) or p.get("journal_id") != journal_id
            or p.get("eligible") is not True):
        raise unavailable()
    asset = await db.get(FileAsset, p.get("asset_id"))
    part = await db.get(Part, p.get("file_part_id"))
    if (asset is None or asset.is_deleted or asset.status != "ready"
            or asset.user_id != user_id or asset.session_id != session_id
            or _hash(_asset_source(asset)) != p.get("asset_digest")
            or part is None or part.user_id != user_id or part.session_id != session_id
            or part.message_id != event.message_id or _hash(part.data) != p.get("file_digest")):
        raise unavailable()
    _geometry(p)
    return event


async def capture(ctx, asset_id, geometry):
    """Record only inside the actual computer operation, after its image upload."""
    from sandbox.resource_operation import _explicit_operation, _physical_driver
    if not _physical_driver(ctx):
        return
    operation = _explicit_operation()
    if (operation is None or operation.sandbox is not ctx.sandbox or operation.closed
            or operation.request_failed or operation.claim.session_id != ctx.session_id):
        raise unavailable()
    digest = geometry.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise unavailable()
    dimensions = _geometry(geometry)
    from agent import effect_ledger as effects
    from db.base import get_db_session
    from db.models.session import Session
    from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
    async with get_db_session() as db:
        await effects._assert_agent_fence_locked(db, effects.EffectRunFence.from_tool_context(ctx))
        resource = await controls.validate_locked(db, operation.fence,
            user_id=ctx.user_id, session_id=ctx.session_id)
        if resource.remote_journal_id != operation.journal_id:
            raise unavailable()
        effect = await db.get(ExternalEffect, operation.claim.effect_id)
        if (effect is None or effect.state != "submitting" or effect.adapter != "computer"
                or effect.claim_token != operation.claim.token
                or effect.claim_generation != operation.claim.generation
                or effect.claim_expires_at is None
                or controls.aware(effect.claim_expires_at) <= await controls.clock(db)
                or effect.safe_context.get("tool_part_id") != ctx.part_id):
            raise unavailable()
        asset = await db.get(FileAsset, asset_id)
        parts = list((await db.scalars(select(Part).where(Part.session_id == ctx.session_id,
            Part.message_id == ctx.message_id, Part.user_id == ctx.user_id, Part.type == "file"))).all())
        matches = [p for p in parts if p.data.get("asset_id") == asset_id
            and (p.data.get("relation") or {}).get("kind") == "computer_screenshot"
            and (p.data.get("relation") or {}).get("source_part_id") == ctx.part_id]
        if (len(matches) != 1 or asset is None or asset.is_deleted or asset.status != "ready"
                or asset.user_id != ctx.user_id or asset.session_id != ctx.session_id
                or asset.workspace_id != ctx.workspace_id or asset.source != "agent"
                or not asset.transient or asset.mime != "image/png" or asset.size != geometry.get("bytes")):
            raise unavailable()
        # A screenshot may still be shown while another operation is unknown,
        # but it must not silently authorize more input while that work runs.
        competing = await db.scalar(select(ExternalEffect.id).where(
            ExternalEffect.resource_id == resource.id, ExternalEffect.id != effect.id,
            ExternalEffect.submitting_at.is_not(None),
            ExternalEffect.state.not_in(("succeeded", "failed"))).limit(1))
        eligible = (resource.last_observation_ref or {}).get("effect_id") == effect.id and not competing
        session = await db.get(Session, ctx.session_id)
        await ensure_surface_seed_locked(db, session)
        event = await append_agent_event_locked(db, session, kind="resource.observed",
            payload={"version": 1, "kind": "screen", "fence": asdict(operation.fence),
                "journal_id": operation.journal_id, "effect_id": effect.id, "asset_id": asset_id,
                "asset_digest": _hash(_asset_source(asset)), "file_part_id": matches[0].id,
                "file_digest": _hash(matches[0].data), "sha256": digest,
                "size_bytes": asset.size, **dimensions, "eligible": bool(eligible)},
            message_id=ctx.message_id, part_id=ctx.part_id, run_fence=ctx.run_fence,
            idempotency_key=f"resource-observed:{effect.id}:{asset_id}")
        if eligible:
            resource.last_observation_ref = {"effect_id": effect.id, **reference(event)}
        operation.observation_recorded = bool(eligible)


async def for_request_locked(db, resource, session, images, run_fence):
    """Only actual resolved bytes in this request can establish an observation."""
    pointer = resource.last_observation_ref or {}
    ref = {key: pointer.get(key) for key in ("event_id", "digest")}
    try:
        event = await load_locked(db, ref, user_id=session.user_id, session_id=session.id,
            fence=controls.fence_for(resource), journal_id=resource.remote_journal_id)
    except AssistantError:
        return None
    if run_fence != (event.session_id, event.run_id, event.generation):
        return None  # A new model turn after pause/restart must observe again.
    p = event.payload
    source_effect = await db.get(ExternalEffect, p["effect_id"])
    if source_effect is None or source_effect.state != "succeeded":
        return None
    if any(image.get("asset_id") == p["asset_id"] and image.get("sha256") == p["sha256"]
            and image.get("size_bytes") == p["size_bytes"] for image in images):
        return ref
    return None


async def for_call(ctx, request, resource, journal_id):
    from db.base import get_db_session
    ref = request.payload["resource_context"].get("observation")
    async with get_db_session() as db:
        event = await load_locked(db, ref, user_id=ctx.user_id, session_id=ctx.session_id,
            fence=resource, journal_id=journal_id)
        row = await controls.validate_locked(db, resource, user_id=ctx.user_id, session_id=ctx.session_id)
        pointer = row.last_observation_ref or {}
        if {key: pointer.get(key) for key in ("event_id", "digest")} != ref:
            raise unavailable()
        source_effect = await db.get(ExternalEffect, event.payload["effect_id"])
        if source_effect is None or source_effect.state != "succeeded":
            raise unavailable()
    return ref


async def guard_effect_locked(db, effect, resource, *, consume):
    ref = effect.safe_context.get("resource_observation")
    if ref is not None:
        await load_locked(db, ref, user_id=effect.tenant_id, session_id=effect.session_id,
            fence=controls.fence_for(resource), journal_id=resource.remote_journal_id)
        pointer = resource.last_observation_ref or {}
        if consume:
            if {key: pointer.get(key) for key in ("event_id", "digest")} != ref:
                raise unavailable()
        elif pointer.get("effect_id") != effect.id:
            raise unavailable()
    if consume:
        # Every tracked physical operation, including generic shell/file
        # tools and preparation, invalidates the previous actionable frame.
        resource.last_observation_ref = {"effect_id": effect.id}


async def current_geometry(ctx):
    from sandbox.resource_operation import _explicit_operation, _physical_driver
    if not _physical_driver(ctx):
        return None
    operation = _explicit_operation()
    if operation is None or operation.observation is None:
        raise unavailable()
    from db.base import get_db_session
    async with get_db_session() as db:
        event = await load_locked(db, operation.observation, user_id=ctx.user_id, session_id=ctx.session_id,
            fence=operation.fence, journal_id=operation.journal_id)
        return _geometry(event.payload)
