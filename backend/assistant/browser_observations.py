"""Private browser frames become input authority only after provider delivery."""
import base64
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import re
import struct

from sqlalchemy import cast, func, select
from sqlalchemy.dialects.postgresql import JSONB

from agent import effect_ledger as effects
from assistant import resource_control as controls, resource_observations as observations
from assistant.browser_resources import _identity
from core.identifier import ascending
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.agent_event import AgentEvent
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.session import Session


def verified_frame(receipt, identity, fence):
    result = receipt.get("result") or {}
    frame = result.get("observation") or {}
    if (not isinstance(frame, dict) or _identity(frame.get("identity")) != identity
            or frame.get("fence") != asdict(fence) or frame.get("eligible") is not True
            or not isinstance(frame.get("observation_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", frame["observation_id"])):
        raise observations.unavailable()
    encoded = result.get("png_base64")
    if not isinstance(encoded, str) or len(encoded) > 12 * 1024 * 1024:
        raise observations.unavailable()
    try:
        raw = base64.b64decode(encoded, validate=True)
        width, height = struct.unpack(">II", raw[16:24])
    except (ValueError, struct.error):
        raise observations.unavailable() from None
    if (not 24 <= len(raw) <= 8 * 1024 * 1024 or raw[:8] != b"\x89PNG\r\n\x1a\n"
            or (width, height) != (1024, 768) or frame.get("width") != width or frame.get("height") != height
            or hashlib.sha256(raw).hexdigest() != frame.get("sha256")):
        raise observations.unavailable()
    return raw, frame


async def attach(ctx, claim, fence, identity, receipt):
    """Only bytes from this admitted remote capture are attached to the Part."""
    raw, frame = verified_frame(receipt, identity, fence)
    await ctx.assert_dispatch_allowed()
    await ctx.assert_run_current()
    from core.oss import get_oss
    from models.message import FilePart, FileRelation
    from session.session import save_part
    asset_id = ascending("asset")
    name = "private-browser-" + ctx.part_id + ".png"
    key = f"assets/{ctx.user_id}/{asset_id}/{name}"
    await get_oss().put_object(key, raw, content_type="image/png", forbid_overwrite=True)
    async with get_db_session() as db:
        await effects._assert_agent_fence_locked(db, effects.EffectRunFence.from_tool_context(ctx))
        await controls.validate_locked(db, fence, user_id=ctx.user_id, session_id=ctx.session_id)
        asset = FileAsset(id=asset_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            session_id=ctx.session_id, project_id=ctx.project_id, name=name, oss_key=key,
            mime="image/png", size=len(raw), status="ready", source="agent", transient=True,
            created_at=datetime.now(timezone.utc))
        db.add(asset)
    part = FilePart(id=ascending("part"), session_id=ctx.session_id, message_id=ctx.message_id,
        path=name, asset_id=asset_id, oss_key=key, mime_type="image/png", size=len(raw), transient=True,
        relation=FileRelation(source_part_id=ctx.part_id, group_id="tool:" + ctx.part_id,
            kind="private_browser_screenshot", role="evidence", label="Private browser capture"))
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
    async with get_db_session() as db:
        await effects._assert_agent_fence_locked(db, effects.EffectRunFence.from_tool_context(ctx))
        resource = await controls.validate_locked(db, fence, user_id=ctx.user_id, session_id=ctx.session_id)
        effect = await db.get(ExternalEffect, claim.effect_id)
        if (effect is None or effect.state != "submitting" or effect.adapter != "private_browser"
                or effect.claim_token != claim.token or effect.claim_generation != claim.generation
                or effect.claim_expires_at is None or controls.aware(effect.claim_expires_at) <= await controls.clock(db)
                or effect.safe_context.get("tool_part_id") != ctx.part_id
                or effect.safe_context.get("browser_identity") != identity):
            raise observations.unavailable()
        competing = await db.scalar(select(ExternalEffect.id).where(ExternalEffect.resource_id == resource.id,
            ExternalEffect.id != effect.id, ExternalEffect.submitting_at.is_not(None),
            ExternalEffect.state.not_in(("succeeded", "failed"))).limit(1))
        eligible = (resource.last_observation_ref or {}).get("effect_id") == effect.id and not competing
        stored_asset, stored_part = await db.get(FileAsset, asset_id), await db.get(Part, part.id)
        session = await db.get(Session, ctx.session_id)
        await ensure_surface_seed_locked(db, session)
        event = await append_agent_event_locked(db, session, kind="resource.observed",
            payload={"version": 1, "kind": "private_browser", "fence": asdict(fence),
                "journal_id": identity["journal_id"], "effect_id": effect.id, "asset_id": asset_id,
                "asset_digest": effects.request_hash(observations._asset_source(stored_asset)),
                "file_part_id": part.id, "file_digest": effects.request_hash(stored_part.data),
                "sha256": frame["sha256"], "size_bytes": len(raw), "native": [1024, 768],
                "scaled": [1024, 768], "eligible": bool(eligible), "browser_identity": identity,
                "remote_observation_id": frame["observation_id"]},
            message_id=ctx.message_id, part_id=ctx.part_id, run_fence=ctx.run_fence,
            idempotency_key=f"browser-observed:{effect.id}:{asset_id}")
        if eligible:
            resource.last_observation_ref = {"effect_id": effect.id, **observations.reference(event)}
    return {"asset_id": asset_id, "sha256": frame["sha256"], "size_bytes": len(raw)}


async def for_call_locked(db, ref, *, ctx, fence, identity):
    event = await observations.load_locked(db, ref, user_id=ctx.user_id, session_id=ctx.session_id,
        fence=fence, journal_id=identity["journal_id"])
    effect = await db.get(ExternalEffect, event.payload["effect_id"])
    remote_id = event.payload.get("remote_observation_id")
    if (event.payload.get("kind") != "private_browser" or event.payload.get("browser_identity") != identity
            or effect is None or effect.state != "succeeded" or effect.adapter != "private_browser"
            or not isinstance(remote_id, str) or not re.fullmatch(r"[0-9a-f]{32}", remote_id)):
        raise observations.unavailable()
    return remote_id


async def validate_provider_images_locked(db, session, images):
    """A cached frame may be historical, but its original source must survive.

    The current epoch decides input authority separately. This check prevents
    deleted/rebound screenshot assets or changed downloaded bytes from being
    sent merely because an earlier step populated the process image cache.
    """
    if len(images) > 256:
        raise observations.unavailable()
    indexed = {image["asset_id"]: image for image in images}
    if not indexed:
        return
    def field(name):
        return (cast(AgentEvent.payload, JSONB)[name].astext if db.get_bind().dialect.name == "postgresql"
            else func.json_extract(AgentEvent.payload, "$." + name))
    events = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session.id,
        AgentEvent.user_id == session.user_id, AgentEvent.kind == "resource.observed",
        field("kind") == "private_browser", field("asset_id").in_(tuple(indexed))).limit(257))).all())
    if len(events) > 256:
        raise observations.unavailable()
    seen = set()
    for event in events:
        p = event.payload
        if p["asset_id"] in seen:
            raise observations.unavailable()
        seen.add(p["asset_id"])
        try:
            fence = controls.ResourceFence(**p["fence"])
        except (KeyError, ValueError, TypeError):
            raise observations.unavailable() from None
        await observations.load_locked(db, observations.reference(event), user_id=session.user_id,
            session_id=session.id, fence=fence, journal_id=p["journal_id"])
        image = indexed[p["asset_id"]]
        effect = await db.get(ExternalEffect, p["effect_id"])
        if (image.get("sha256") != p["sha256"] or image.get("size_bytes") != p["size_bytes"]
                or effect is None or effect.state != "succeeded" or effect.adapter != "private_browser"
                or effect.safe_context.get("browser_identity") != p.get("browser_identity")):
            raise observations.unavailable()
