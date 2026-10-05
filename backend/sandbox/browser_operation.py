"""Finite browser automation bound to its original provider request and effect."""
from contextlib import suppress
from dataclasses import asdict
import hashlib
import json
import re

from sqlalchemy import select

from agent import effect_ledger as effects
from assistant import browser_resources as browsers, resource_control as controls
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask
from db.models.browser_resource import BrowserResourceBinding, BrowserResourceSession
from db.models.part import Part
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session


def enabled(session, config, agent_name):
    option = getattr(config, "private_runtime", None)
    return bool(option and option.enabled and option.browser_image and session.user_id in option.allowed_user_ids
        and session.kind != "assistant" and session.visibility == "private"
        and session.memory_policy == "assistant_isolated" and agent_name in {"build", "general"})


async def execution_main_locked(db, session):
    """Use a real Task link or its private descendants, never caller metadata."""
    from assistant.scheduling import require_runnable_locked
    if (session is None or session.is_deleted or session.kind == "assistant"
            or session.visibility != "private" or session.memory_policy != "assistant_isolated"):
        raise browsers.held("BROWSER_EXECUTION_REQUIRED")
    await require_runnable_locked(db, session)
    current, seen = session, set()
    while current is not None and current.id not in seen and len(seen) < 64:
        seen.add(current.id)
        if (current.is_deleted or current.user_id != session.user_id or current.workspace_id != session.workspace_id
                or current.visibility != "private" or current.memory_policy != "assistant_isolated"):
            break
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == current.id))
        if task is not None:
            if (task.user_id, task.workspace_id) != (session.user_id, session.workspace_id) or task.archived_at:
                break
            return task.assistant_session_id
        current = await db.get(Session, current.parent_id) if current.parent_id else None
    raise browsers.held("BROWSER_EXECUTION_REQUIRED")


async def validate_binding_locked(db, row, session, user_id):
    main_id = await execution_main_locked(db, session)
    binding = await db.get(BrowserResourceBinding, row.id)
    if (binding is None or row.resource_type != "browser_profile" or row.provider != browsers.PROVIDER
            or (binding.actor_user_id, binding.workspace_id, binding.assistant_session_id)
                != (user_id, session.workspace_id, main_id)):
        raise controls.unavailable()
    identity = browsers._identity(binding.identity)
    runtime = await db.get(PrivateRuntimeBinding, binding.private_runtime_id)
    if (runtime is None or runtime.status != "ready" or runtime.kind != "browser_profile"
            or (runtime.actor_user_id, runtime.workspace_id, runtime.revision)
                != (user_id, session.workspace_id, binding.runtime_revision)
            or row.remote_journal_id != identity["journal_id"]
            or row.physical_id != identity["runtime_id"] + ":" + identity["profile_id"]):
        raise controls.unavailable()
    from sandbox.private_runtime import _config, _mode_enabled, _snapshot
    _mode_enabled(_snapshot(runtime), _config(user_id, "browser_profile"))
    return binding


async def prepare_provider(lease):
    """Provision/inspect outside the provider checkpoint's SQL transaction."""
    async with get_db_session() as db:
        session = await db.get(Session, lease.session_id)
        main_id = await execution_main_locked(db, session)
        workspace_id = session.workspace_id
    await lease.assert_current()
    result = await browsers.ensure_browser(user_id=lease.user_id, workspace_id=workspace_id, main_id=main_id)
    await lease.assert_current()
    return result["resource_id"]


async def provider_context_locked(db, session, resource_id, images, run_fence):
    row = await controls.locked(db, resource_id)
    if row is None:
        raise controls.unavailable()
    binding = await validate_binding_locked(db, row, session, session.user_id)
    await controls.actor(db, session.user_id, session.workspace_id)
    from assistant.resource_observations import for_request_locked
    observation = await for_request_locked(db, row, session, images or [], run_fence)
    if await db.get(BrowserResourceSession, (row.id, session.id)) is None:
        db.add(BrowserResourceSession(resource_id=row.id, session_id=session.id, created_at=await controls.clock(db)))
    return {"version": 1, "binding_id": binding.private_runtime_id, "runtime_revision": binding.runtime_revision,
        "identity": dict(binding.identity), "fence": asdict(controls.fence_for(row)), "observation": observation}


def remote_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def prepare(ctx, arguments):
    """The earliest canonical call pins the request even after approval/restart."""
    await ctx.assert_dispatch_allowed()
    await ctx.assert_run_current()
    from tool.private_browser import BrowserArgs
    args = BrowserArgs.model_validate(arguments)
    payload = args.model_dump(mode="json", exclude_none=True)
    async with get_db_session() as db:
        part = await db.get(Part, ctx.part_id)
        if (part is None or part.user_id != ctx.user_id or part.session_id != ctx.session_id
                or part.message_id != ctx.message_id or part.canonical_tool_id != "private_browser"):
            raise controls.unavailable()
        called = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.part_id == ctx.part_id,
            AgentEvent.message_id == ctx.message_id, AgentEvent.kind == "tool.called")
            .order_by(AgentEvent.sequence).limit(1))
        request = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested", AgentEvent.run_id == called.run_id,
            AgentEvent.generation == called.generation, AgentEvent.sequence < called.sequence)
            .order_by(AgentEvent.sequence.desc()).limit(1)) if called else None
        context = request.payload.get("browser_context") if request else None
        if (not isinstance(context, dict) or set(context) != {"version", "binding_id", "runtime_revision", "identity", "fence", "observation"}
                or type(context.get("version")) is not int or context["version"] != 1):
            raise controls.unavailable()
        identity = browsers._identity(context["identity"])
        try:
            fence = controls.ResourceFence(**context["fence"])
        except (ValueError, TypeError):
            raise controls.unavailable() from None
        row = await controls.validate_locked(db, fence, user_id=ctx.user_id, session_id=ctx.session_id)
        binding = await db.get(BrowserResourceBinding, row.id)
        if ((binding.private_runtime_id, binding.runtime_revision, binding.identity)
                != (context["binding_id"], context["runtime_revision"], identity)):
            raise controls.unavailable()
        original = BrowserArgs.model_validate(part.data.get("input")).model_dump(mode="json", exclude_none=True)
        if effects.request_hash(original) != effects.request_hash(payload):
            raise effects.EffectConflictError("Browser input differs from its persisted tool arguments")
        observation_id = None
        if args.action != "capture":
            from assistant.browser_observations import for_call_locked
            observation_id = await for_call_locked(db, context["observation"], ctx=ctx, fence=fence, identity=identity)
            pointer = row.last_observation_ref or {}
            if {key: pointer.get(key) for key in ("event_id", "digest")} != context["observation"]:
                raise controls.unavailable()
        request_sequence, request_id = request.sequence, request.payload["request_id"]
    effect_id = effects.stable_effect_id(tenant_id=ctx.user_id, session_id=ctx.session_id,
        adapter="private_browser", operation=args.action, logical_key=ctx.part_id)
    remote = {"identity": identity, "fence": asdict(fence), "operation_id": effect_id,
        "kind": args.action, "args": args.operation_args()}
    if observation_id is not None:
        remote["observation_id"] = observation_id
    safe = {"tool_part_id": ctx.part_id, "resource_request_sequence": request_sequence,
        "resource_request_id": request_id, "resource_journal_id": identity["journal_id"],
        "browser_identity": identity, "browser_binding_id": context["binding_id"],
        "browser_runtime_revision": context["runtime_revision"], "browser_request_hash": remote_hash(remote)}
    if observation_id is not None:
        safe["resource_observation"] = context["observation"]
    prepared = await effects.prepare_effect(effects.EffectRunFence.from_tool_context(ctx),
        adapter="private_browser", provider=browsers.PROVIDER, operation=args.action, logical_key=ctx.part_id,
        request_payload=payload, project_id=ctx.project_id or None, resource_fence=fence, safe_context=safe)
    if prepared.snapshot.safe_context != safe:
        raise effects.EffectConflictError("The original browser request proof changed")
    return prepared.snapshot, binding, fence, remote


def checked_receipt(payload, effect):
    identity = effect.safe_context["browser_identity"]
    receipt = payload.get("receipt") if isinstance(payload, dict) else None
    if (not isinstance(receipt, dict) or payload.get("protocol") != "browser_resource_v1"
            or receipt.get("identity") != identity or receipt.get("fence") != asdict(effect.resource_fence)
            or receipt.get("operation_id") != effect.effect_id or receipt.get("kind") != effect.operation
            or receipt.get("request_hash") != effect.safe_context["browser_request_hash"]
            or receipt.get("state") not in {"completed", "canceled", "unknown"}):
        raise effects.EffectLedgerError("The original browser operation receipt is unavailable")
    return receipt


def receipt_summary(receipt):
    result = {key: receipt[key] for key in ("operation_id", "identity", "fence", "kind", "request_hash", "state")}
    failure = navigation_failure(receipt)
    if failure:
        result["navigation_error"] = failure
    return result


def navigation_failure(receipt):
    value = receipt.get("result") or {}
    error = value.get("navigation_error") if isinstance(value, dict) else None
    if receipt.get("kind") != "navigate" or not error:
        return None
    # Chromium normally returns a fixed net::ERR_* code. Do not persist or
    # relay arbitrary untrusted response text as durable public evidence.
    return (error if isinstance(error, str) and re.fullmatch(r"net::ERR_[A-Z0-9_]{1,100}", error)
        else "Browser navigation failed")


async def execute(ctx, arguments):
    snapshot, binding, fence, remote = await prepare(ctx, arguments)
    if snapshot.state != "prepared":
        raise effects.EffectNotDispatchableError("This browser call already crossed its send boundary; request a fresh capture")
    claim = await effects.claim_effect_for_dispatch(snapshot.effect_id, effects.EffectRunFence.from_tool_context(ctx))
    if claim is None:
        raise effects.EffectNotDispatchableError("The browser call is already being processed")
    try:
        client = await browsers.client_for(binding)
        await ctx.assert_dispatch_allowed()
        await ctx.assert_run_current()
        await effects.mark_effect_submitting(claim)
        await effects.assert_effect_dispatchable(claim)
        payload = await effects.run_with_effect_claim_heartbeat(claim, client.operate(
            fence=remote["fence"], operation_id=remote["operation_id"], kind=remote["kind"],
            args=remote["args"], observation_id=remote.get("observation_id")))
        receipt = checked_receipt(payload, snapshot)
        # Re-resolving validates the original physical ID and current actor;
        # it never follows a new browser binding after an in-flight response.
        await browsers.client_for(binding)
        await ctx.assert_dispatch_allowed()
        await ctx.assert_run_current()
        async with get_db_session() as db:
            await controls.validate_locked(db, fence, user_id=ctx.user_id, session_id=ctx.session_id)
        if receipt["state"] == "unknown":
            raise effects.EffectNotDispatchableError("The browser operation outcome is unknown")
        if receipt["state"] == "canceled":
            await effects.settle_effect(claim, state="failed", receipt=receipt_summary(receipt))
            raise controls.unavailable()
        result = {"action": snapshot.operation, "operation_id": snapshot.effect_id, "state": "completed"}
        if snapshot.operation == "capture":
            from assistant.browser_observations import attach
            result.update(await attach(ctx, claim, fence, binding.identity, receipt))
        failure = navigation_failure(receipt)
        if failure:
            result.update(state="navigation_failed", error=True, error_code="BROWSER_NAVIGATION_FAILED",
                navigation_error=failure, delivered=True)
            await effects.settle_effect(claim, state="failed", receipt=receipt_summary(receipt),
                error={"code": "BROWSER_NAVIGATION_FAILED"}, projection=result)
            return result
        await effects.settle_effect(claim, state="succeeded", receipt=receipt_summary(receipt), projection=result)
        return result
    except BaseException:
        try:
            await effects.abandon_effect_before_dispatch(claim, reason="browser_not_sent")
        except Exception:
            with suppress(Exception):
                await effects.record_effect_outcome_unknown(claim, error={"code": "browser_response_unconfirmed"})
        raise


class BrowserReconciler:
    can_reconcile_without_handle = True

    async def reconcile(self, effect):
        # Receipt recovery never requires a running Task or grants new input.
        async with get_db_session() as db:
            binding = await db.get(BrowserResourceBinding, effect.resource_fence.resource_id)
            if (binding is None or binding.identity != effect.safe_context.get("browser_identity")
                    or binding.private_runtime_id != effect.safe_context.get("browser_binding_id")
                    or binding.runtime_revision != effect.safe_context.get("browser_runtime_revision")):
                return effects.ReconcileDecision(state="manual_review")
        client = await browsers.client_for(binding)
        receipt = checked_receipt(await client.operation_receipt(effect.effect_id), effect)
        await browsers.client_for(binding)
        if receipt["state"] == "completed" and navigation_failure(receipt):
            return effects.ReconcileDecision(state="failed", receipt=receipt_summary(receipt),
                evidence={"code": "BROWSER_NAVIGATION_FAILED"})
        state = {"completed": "succeeded", "canceled": "failed", "unknown": "outcome_unknown"}[receipt["state"]]
        return effects.ReconcileDecision(state=state, receipt=receipt_summary(receipt))


_reconciler = BrowserReconciler()
effects.register_effect_reconciler("private_browser", _reconciler)
