"""Bind graphical input to a captured frame present in the original request.

An observation is provenance, not a grant of exclusive physical access. The
remote/native channels still need their own fencing before human takeover.

Every refusal keeps one user-facing message but carries a machine-readable
``reason`` (``error.detail``) so logs and tool metadata say which check failed.
"""
from dataclasses import asdict
import re

from sqlalchemy import and_, not_, or_, select

from assistant import resource_control as controls
from assistant.policy import AssistantError
from core.log import create_logger
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from db.models.file_asset import FileAsset
from db.models.part import Part

log = create_logger("assistant.resource_observations")

#: Fixed, backend-authored reads that run between model steps. They cannot
#: change what a captured frame shows, so they neither replace the actionable
#: frame nor make a concurrent capture ineligible. Every other tracked physical
#: operation, including generic shell/file tools and preparation that writes,
#: still invalidates the frame.
READ_ONLY_OPERATIONS = frozenset({
    ("sandbox_runtime", "catalogue_read"),   # Skill/MCP directory GET, every step
    ("sandbox_runtime", "plan_transition"),  # plan -> build `test -f` probe
})

_MESSAGE = "Take a new screenshot before desktop input; the original request has no current verified frame"


def unavailable(reason="unspecified", **detail):
    message = (_MESSAGE if not reason.startswith("competing:") else
        "A previous desktop operation is unresolved. Inspect its outcome before sending more input; "
        "taking repeated screenshots cannot settle that operation. Once resolved, capture a new frame.")
    error = AssistantError(423, "RESOURCE_OBSERVATION_REQUIRED", message)
    error.reason = reason
    error.detail = {"reason": reason, **detail}
    return error


def failure_metadata(exc) -> dict:
    """Tool-result metadata naming the failed check; the message stays fixed."""
    detail = getattr(exc, "detail", None)
    if getattr(exc, "code", None) != "RESOURCE_OBSERVATION_REQUIRED" or not isinstance(detail, dict):
        return {}
    return {"observation_reason": detail.get("reason"), "observation_detail": dict(detail)}


def read_only(effect) -> bool:
    return (effect.adapter, effect.operation) in READ_ONLY_OPERATIONS


def _mutating():
    return not_(or_(*(and_(ExternalEffect.adapter == adapter, ExternalEffect.operation == operation)
                      for adapter, operation in sorted(READ_ONLY_OPERATIONS))))


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
            raise unavailable("geometry_invalid")
        result[name] = list(pair)
    return result


def reference(event):
    return {"event_id": event.id, "digest": _hash(event.payload)}


def _ineligible(payload):
    competing = payload.get("competing_effect_id")
    if competing:
        return unavailable(f"competing:{competing}/{payload.get('competing_state')}",
            competing_effect_id=competing)
    return unavailable("frame_ineligible")


async def _pointer_reason(db, pointer):
    """Why the desktop's current pointer is not the frame a caller expected."""
    effect_id = (pointer or {}).get("effect_id")
    if not effect_id:
        return unavailable("observation_missing")
    effect = await db.get(ExternalEffect, effect_id)
    if effect is None:
        return unavailable("superseded_by:unknown", effect_id=effect_id)
    context = effect.safe_context or {}
    if not pointer.get("event_id") and effect.adapter == "computer":
        # The newest operation is desktop input whose own frame (screenshot or
        # post-action capture) was never recorded as actionable: say why.
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == effect.session_id,
            AgentEvent.part_id == context.get("tool_part_id"),
            AgentEvent.kind == "resource.observed").order_by(AgentEvent.sequence.desc()).limit(1))
        if event is None:
            return unavailable("frame_not_recorded", effect_id=effect.id, effect_state=effect.state,
                action=context.get("action"))
        return _ineligible(event.payload)
    return unavailable(f"superseded_by:{effect.adapter}/{effect.operation}",
        effect_id=effect.id, effect_state=effect.state)


async def load_locked(db, ref, *, user_id, session_id, fence, journal_id):
    if not isinstance(ref, dict) or ref.get("event_id") is None:
        raise unavailable("observation_missing")
    if (set(ref) != {"event_id", "digest"}
            or not isinstance(ref["event_id"], str) or not ref["event_id"]
            or not isinstance(ref["digest"], str) or not re.fullmatch(r"[0-9a-f]{64}", ref["digest"])):
        raise unavailable("observation_malformed")
    event = await db.get(AgentEvent, ref["event_id"])
    if event is None:
        raise unavailable("event_missing", event_id=ref["event_id"])
    if event.kind != "resource.observed" or event.user_id != user_id:
        raise unavailable("event_foreign")
    if event.session_id != session_id:
        raise unavailable("event_other_session")
    if reference(event) != ref:
        raise unavailable("event_digest_changed", event_id=event.id)
    p = event.payload
    if p.get("fence") != asdict(fence):
        raise unavailable("fence_changed", event_id=event.id)
    if p.get("journal_id") != journal_id:
        raise unavailable("journal_changed", event_id=event.id)
    if p.get("eligible") is not True:
        raise _ineligible(p)
    asset = await db.get(FileAsset, p.get("asset_id"))
    if (asset is None or asset.is_deleted or asset.status != "ready"
            or asset.user_id != user_id or asset.session_id != session_id
            or _hash(_asset_source(asset)) != p.get("asset_digest")):
        raise unavailable("asset_changed", event_id=event.id, asset_id=p.get("asset_id"))
    part = await db.get(Part, p.get("file_part_id"))
    if (part is None or part.user_id != user_id or part.session_id != session_id
            or part.message_id != event.message_id or _hash(part.data) != p.get("file_digest")):
        raise unavailable("file_changed", event_id=event.id, file_part_id=p.get("file_part_id"))
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
        raise unavailable("capture_unbound")
    digest = geometry.get("sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise unavailable("capture_digest_invalid")
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
            raise unavailable("journal_changed")
        effect = await db.get(ExternalEffect, operation.claim.effect_id)
        if (effect is None or effect.state != "submitting" or effect.adapter != "computer"
                or effect.claim_token != operation.claim.token
                or effect.claim_generation != operation.claim.generation
                or effect.claim_expires_at is None
                or controls.aware(effect.claim_expires_at) <= await controls.clock(db)
                or effect.safe_context.get("tool_part_id") != ctx.part_id):
            raise unavailable("capture_claim_invalid")
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
            raise unavailable("capture_frame_invalid")
        # A screenshot may still be shown while another operation is unknown,
        # but it must not silently authorize more input while that work runs.
        # Terminal rows (manual_review included) are parked, not running, and
        # fixed backend reads cannot change the screen.
        competing = (await db.execute(select(ExternalEffect.id, ExternalEffect.state).where(
            ExternalEffect.resource_id == resource.id, ExternalEffect.id != effect.id,
            ExternalEffect.submitting_at.is_not(None),
            ExternalEffect.state.not_in(tuple(effects.TERMINAL_STATES)), _mutating())
            .order_by(ExternalEffect.created_at, ExternalEffect.id).limit(1))).first()
        pointer = resource.last_observation_ref or {}
        current = pointer.get("effect_id") == effect.id
        eligible = current and competing is None
        session = await db.get(Session, ctx.session_id)
        await ensure_surface_seed_locked(db, session)
        event = await append_agent_event_locked(db, session, kind="resource.observed",
            payload={"version": 1, "kind": "screen", "fence": asdict(operation.fence),
                "journal_id": operation.journal_id, "effect_id": effect.id, "asset_id": asset_id,
                "asset_digest": _hash(_asset_source(asset)), "file_part_id": matches[0].id,
                "file_digest": _hash(matches[0].data), "sha256": digest,
                "size_bytes": asset.size, **dimensions, "eligible": bool(eligible),
                **({"competing_effect_id": competing.id, "competing_state": competing.state}
                   if competing is not None else {})},
            message_id=ctx.message_id, part_id=ctx.part_id, run_fence=ctx.run_fence,
            idempotency_key=f"resource-observed:{effect.id}:{asset_id}")
        if eligible:
            resource.last_observation_ref = {"effect_id": effect.id, **reference(event)}
        else:
            reason = (_ineligible(event.payload) if competing is not None
                      else await _pointer_reason(db, pointer))
            log.info("Screen frame is not actionable reason=%s session_id=%s effect_id=%s detail=%s",
                reason.reason, ctx.session_id, effect.id, reason.detail)
        operation.observation_recorded = bool(eligible)


async def _request_observation_locked(db, resource, session, images, run_fence):
    pointer = resource.last_observation_ref or {}
    if not pointer.get("event_id"):
        raise await _pointer_reason(db, pointer)
    ref = {key: pointer.get(key) for key in ("event_id", "digest")}
    event = await load_locked(db, ref, user_id=session.user_id, session_id=session.id,
        fence=controls.fence_for(resource), journal_id=resource.remote_journal_id)
    if run_fence != (event.session_id, event.run_id, event.generation):
        # A new model turn after pause/restart must observe again.
        raise unavailable("run_fence_changed", event_id=event.id)
    p = event.payload
    source_effect = await db.get(ExternalEffect, p["effect_id"])
    if source_effect is None or source_effect.state != "succeeded":
        raise unavailable(f"source_effect:{source_effect.state if source_effect else 'missing'}",
            event_id=event.id, effect_id=p["effect_id"])
    if any(image.get("asset_id") == p["asset_id"] and image.get("sha256") == p["sha256"]
            and image.get("size_bytes") == p["size_bytes"] for image in images):
        return ref
    raise unavailable("image_not_in_request", event_id=event.id, asset_id=p["asset_id"])


async def for_request_locked(db, resource, session, images, run_fence):
    """Only actual resolved bytes in this request can establish an observation."""
    try:
        return await _request_observation_locked(db, resource, session, images, run_fence)
    except AssistantError as exc:
        # A request that carries a frame but cannot cite it is exactly what
        # later refuses desktop input: keep that at INFO, the rest at DEBUG.
        (log.info if images else log.debug)(
            "Model request has no verified frame reason=%s session_id=%s resource_id=%s images=%d detail=%s",
            getattr(exc, "reason", exc.code), session.id, resource.id, len(images),
            getattr(exc, "detail", None))
        return None


async def for_call(ctx, request, resource, journal_id):
    from db.base import get_db_session
    ref = request.payload["resource_context"].get("observation")
    if ref is None:
        raise unavailable("request_without_observation", request_event_id=request.id)
    async with get_db_session() as db:
        event = await load_locked(db, ref, user_id=ctx.user_id, session_id=ctx.session_id,
            fence=resource, journal_id=journal_id)
        row = await controls.validate_locked(db, resource, user_id=ctx.user_id, session_id=ctx.session_id)
        pointer = row.last_observation_ref or {}
        if {key: pointer.get(key) for key in ("event_id", "digest")} != ref:
            raise await _pointer_reason(db, pointer)
        source_effect = await db.get(ExternalEffect, event.payload["effect_id"])
        if source_effect is None or source_effect.state != "succeeded":
            raise unavailable(f"source_effect:{source_effect.state if source_effect else 'missing'}",
                event_id=event.id, effect_id=event.payload["effect_id"])
    return ref


async def guard_effect_locked(db, effect, resource, *, consume):
    ref = effect.safe_context.get("resource_observation")
    if ref is not None:
        await load_locked(db, ref, user_id=effect.tenant_id, session_id=effect.session_id,
            fence=controls.fence_for(resource), journal_id=resource.remote_journal_id)
        pointer = resource.last_observation_ref or {}
        if consume:
            if {key: pointer.get(key) for key in ("event_id", "digest")} != ref:
                raise await _pointer_reason(db, pointer)
        elif pointer.get("effect_id") != effect.id:
            raise await _pointer_reason(db, pointer)
    if consume and not read_only(effect):
        # Every tracked physical operation, including generic shell/file
        # tools and preparation, invalidates the previous actionable frame.
        # A fixed backend read (READ_ONLY_OPERATIONS) leaves it in place: the
        # Driver re-reads the Skill/MCP catalogue before every model request.
        resource.last_observation_ref = {"effect_id": effect.id}


async def current_geometry(ctx):
    from sandbox.resource_operation import _explicit_operation, _physical_driver
    if not _physical_driver(ctx):
        return None
    operation = _explicit_operation()
    if operation is None or operation.observation is None:
        raise unavailable("operation_without_observation")
    from db.base import get_db_session
    async with get_db_session() as db:
        event = await load_locked(db, operation.observation, user_id=ctx.user_id, session_id=ctx.session_id,
            fence=operation.fence, journal_id=operation.journal_id)
        return _geometry(event.payload)
