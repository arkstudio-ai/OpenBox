"""Durable resource origin for Driver preparation before a provider/tool call."""
import asyncio
import re
from contextlib import suppress
from dataclasses import asdict

from sqlalchemy import select

from agent import effect_ledger as effects
from assistant import resource_control as controls
from sandbox import resource_operation as operations


class RuntimePreparationUncertain(effects.EffectNotDispatchableError):
    pass


async def _runtime_session_locked(db, sandbox, lease, run):
    from db.models.session import Session
    from assistant.scheduling import require_runnable_locked

    # Keep the global Session -> Driver -> private binding -> resource order.
    # This check belongs to this transaction, not to a reusable authorization.
    await effects._assert_agent_fence_locked(db, run)
    session = await db.get(Session, lease.session_id)
    if (session is None or session.is_deleted or session.kind == "assistant"
            or session.workspace_id != sandbox.workspace_id):
        raise controls.unavailable()
    await require_runnable_locked(db, session)
    return session


async def _private_control_locked(db, session, route):
    row = await controls.enroll_private_runtime_locked(db, session, route)
    if (row.epoch != 1 or row.owner_kind != "automation" or row.owner_id != session.workspace_id
            or row.status != "active" or row.admission_state != "open"):
        raise controls.unavailable()
    return row


async def ensure_private_runtime_control(sandbox, lease):
    """Bind a guest's independent journal before its first preparation IO.

    The expected journal commits before bind. Its deterministic command and
    exact immutable receipt survive a lost response; neither a matching owner
    nor a replacement journal can be adopted as success. No SQL transaction
    spans the network. This grants no human control or shell drainage proof.
    """
    route = getattr(sandbox, "private_runtime_route", None)
    if route is None:
        return
    from db.base import get_db_session
    from session.internal_parts import begin_session_write
    run = effects.EffectRunFence(lease.session_id, lease.user_id, lease.run_id, lease.generation)
    async with get_db_session() as db:
        await begin_session_write(db)
        session = await _runtime_session_locked(db, sandbox, lease, run)
        row = await _private_control_locked(db, session, route)
        fence, pinned = controls.fence_for(row), row.remote_journal_id
        if row.remote_status is not None:
            await controls.validate_locked(db, fence, user_id=lease.user_id, session_id=lease.session_id)
            return
    await _bind_private_runtime_control(sandbox, lease, run, route, fence, pinned)


async def _bind_private_runtime_control(sandbox, lease, run, route, fence, pinned):
    """Finish the original cold binding after its initial transaction commits."""
    from db.base import get_db_session
    from session.internal_parts import begin_session_write
    from assistant.resource_commands import verified_status

    async def original(db):
        session = await _runtime_session_locked(db, sandbox, lease, run)
        return await _private_control_locked(db, session, route)

    status = verified_status(await sandbox.resource_status(), asdict(fence), pinned)
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await original(db)
        if controls.fence_for(row) != fence or row.remote_journal_id not in {None, status.journal_id}:
            raise controls.unavailable()
        row.remote_journal_id = status.journal_id
    payload = {**asdict(fence), "journal_id": status.journal_id,
               "command_id": "actorbind_" + fence.resource_id}
    result = await sandbox.resource_command("bind", payload)
    observed = verified_status(result, asdict(fence), status.journal_id)
    receipt = result.get("command_receipt")
    if (receipt != {**payload, "action": "bind", "admission": "open"}
            or observed.control is None or observed.control.admission != "open"):
        raise controls.unavailable()
    async with get_db_session() as db:
        await begin_session_write(db)
        row = await original(db)
        if controls.fence_for(row) != fence or row.remote_journal_id != status.journal_id:
            raise controls.unavailable()
        row.remote_status = {**observed.model_dump(), "binding_receipt": receipt,
                             "observed_at": (await controls.clock(db)).isoformat()}


def runtime_read_lease(sandbox):
    """A pre-tool Driver read must not join another caller's background IO."""
    from agent.driver import _current_lease
    lease = _current_lease.get()
    desktop = getattr(sandbox, "desktop_id", None)
    if (lease is not None and isinstance(desktop, str) and desktop
            and operations._bound_operation() is None and operations._tool_scope() is None):
        return lease
    return None


async def read_runtime_catalogue(sandbox, lease, operation, *, on_context_failure=None):
    """Keep directory bytes ephemeral; persist only IO identity and completion.

    A fresh read has a fresh operation identity. An unresolved earlier read
    blocks another automatic request, including after Driver/process recovery.
    This is deliberately limited to the fixed Skill/MCP catalogue operation.
    """
    from core.identifier import ascending
    from db.base import get_db_session
    from db.models.external_effect import ExternalEffect
    try:
        _, resource, _, _, _ = await runtime_context(sandbox, lease)
    except BaseException:
        # Cleanup only: the caller cannot supply or bypass resource authority.
        # Keep this separate from catalogue transport/unavailable handling.
        if on_context_failure is not None:
            on_context_failure()
        raise
    async with get_db_session() as db:
        uncertain = await db.scalar(select(ExternalEffect.id).where(
            ExternalEffect.resource_id == resource.resource_id,
            ExternalEffect.adapter == "sandbox_runtime", ExternalEffect.operation == "catalogue_read",
            ExternalEffect.submitting_at.is_not(None),
            ExternalEffect.state.not_in(("succeeded", "failed"))).limit(1))
    if uncertain:
        raise RuntimePreparationUncertain("An earlier catalogue read has an unresolved outcome")
    result = None

    async def read():
        nonlocal result
        result = await operation()
        return {"observed": True}

    async def current():
        await runtime_context(sandbox, lease)

    await run_runtime_operation(sandbox, session_id=lease.session_id, user_id=lease.user_id,
        stage="catalogue_read", key=ascending("catalogue_read"),
        payload={"surface": "skill_mcp_catalogue", "method": "GET"}, operation=read,
        before_request=current)
    return result


async def runtime_context(sandbox, lease):
    """Freeze one physical control snapshot for every preparation in this run."""
    from db.base import get_db_session
    from db.models.agent_event import AgentEvent
    from db.models.session import Session
    from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
    from session.internal_parts import begin_session_write

    run = effects.EffectRunFence(lease.session_id, lease.user_id, lease.run_id, lease.generation)
    route = getattr(sandbox, "private_runtime_route", None)

    async def origin_locked(db, session):
        if getattr(sandbox, "private_runtime_route", None) != route:
            raise controls.unavailable()
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.user_id == lease.user_id, AgentEvent.kind == "resource.runtime_requested",
            AgentEvent.run_id == lease.run_id, AgentEvent.generation == lease.generation))
        if event is None:
            context = await controls.capture_desktop_context_locked(db, session, sandbox.desktop_id,
                runtime_route=route)
            await ensure_surface_seed_locked(db, session)
            event = await append_agent_event_locked(db, session, kind="resource.runtime_requested",
                payload={"resource_context": context},
                run_fence=(lease.session_id, lease.run_id, lease.generation),
                idempotency_key=f"runtime-resource:{lease.run_id}:{lease.generation}")
        return event.payload.get("resource_context"), session.project_id, event.id

    cold = None
    async with get_db_session() as db:
        await begin_session_write(db)
        session = await _runtime_session_locked(db, sandbox, lease, run)
        if route is not None:
            row = await _private_control_locked(db, session, route)
            if row.remote_status is None:
                cold = controls.fence_for(row), row.remote_journal_id
            else:
                await controls.validate_locked(db, controls.fence_for(row),
                    user_id=lease.user_id, session_id=lease.session_id)
        if cold is None:
            # Ready control and its origin share the locked Session/Driver and
            # one source replay. No authorization survives this transaction.
            context, project_id, event_id = await origin_locked(db, session)
    if cold is not None:
        # Enrollment commits before network IO; bind retains the exact original
        # fence/journal and rechecks each SQL phase. Never hold these locks while
        # waiting for a guest, or retry by following a replacement binding.
        await _bind_private_runtime_control(sandbox, lease, run, route, *cold)
        async with get_db_session() as db:
            await begin_session_write(db)
            session = await _runtime_session_locked(db, sandbox, lease, run)
            row = await _private_control_locked(db, session, route)
            await controls.validate_locked(db, controls.fence_for(row),
                user_id=lease.user_id, session_id=lease.session_id)
            context, project_id, event_id = await origin_locked(db, session)
    # Commit the original identity even when admission is currently closed.
    # A retry cannot adopt a new owner/epoch/journal in the same Driver run.
    if (not isinstance(context, dict) or set(context) != {"version", "desktop_id", "fence", "journal_id"}
            or type(context.get("version")) is not int or context["version"] != 1
            or context["desktop_id"] != sandbox.desktop_id
            or context["journal_id"] is not None and (not isinstance(context["journal_id"], str)
                or not re.fullmatch(r"[0-9a-f]{32}", context["journal_id"]))):
        raise controls.unavailable()
    try:
        resource = controls.ResourceFence(**context["fence"])
    except (TypeError, ValueError) as exc:
        raise controls.unavailable() from exc
    async with get_db_session() as db:
        row = await controls.validate_locked(db, resource, user_id=lease.user_id, session_id=lease.session_id)
        if route is not None:
            session = await db.get(Session, lease.session_id)
            await controls.private_runtime_binding_locked(db, session,
                runtime_route=route, resource=row)
        if row.remote_journal_id != context["journal_id"]:
            raise controls.unavailable()
    if getattr(sandbox, "private_runtime_route", None) != route:
        raise controls.unavailable()
    return run, resource, context["journal_id"], project_id, event_id


async def run_runtime_operation(sandbox, *, session_id, user_id, stage, key=None, payload, operation,
                                before_request=None):
    """Run fixed preparation once, retaining success or uncertainty durably.

    Non-Driver management and Docker paths still need separate physical
    adapters. This helper does not certify those paths or human exclusivity.
    """
    from agent.driver import _current_lease
    lease = _current_lease.get()
    desktop_id = getattr(sandbox, "desktop_id", None)
    if lease is None or not isinstance(desktop_id, str) or not desktop_id:
        if before_request is not None:
            await before_request()
        result = await operation()
        if before_request is not None:
            await before_request()
        return result
    if (lease.session_id, lease.user_id) != (session_id, user_id):
        raise controls.unavailable()
    # A tool already carries the earlier provider request's control. Do not
    # let a tool's helper replace it with a newer preparation origin.
    if operations._bound_operation() is not None or operations._tool_scope() is not None:
        return await operation()
    await lease.assert_current()
    if before_request is not None:
        await before_request()
    run, resource, journal, project_id, origin = await runtime_context(sandbox, lease)
    logical_key = key or f"{run.run_id}:{run.generation}"
    previous = await effects.get_effect(effects.stable_effect_id(tenant_id=user_id, session_id=session_id,
        adapter="sandbox_runtime", operation=stage, logical_key=logical_key), tenant_id=user_id)
    # A proven-unsent effect may move to a new Driver generation, but the
    # general ledger refreshes its safe context during that move. Check the
    # original journal first so a continuation cannot adopt a replacement.
    if previous is not None and previous.safe_context.get("resource_journal_id") != journal:
        raise controls.unavailable()
    prepared = await effects.prepare_effect(run, adapter="sandbox_runtime", provider="wuying",
        operation=stage, logical_key=logical_key, request_payload=payload,
        project_id=project_id, resource_fence=resource,
        safe_context={"runtime_origin_id": origin, "resource_journal_id": journal})
    snapshot = prepared.snapshot
    if snapshot.safe_context.get("resource_journal_id") != journal:
        raise controls.unavailable()
    if snapshot.state == "succeeded":
        receipt = snapshot.provider_receipt or {}
        if set(receipt) != {"result", "remote_exclusivity_verified"} or receipt["remote_exclusivity_verified"] is not False:
            raise effects.EffectNotDispatchableError("Runtime preparation receipt is unavailable")
        return receipt["result"]
    if snapshot.state != "prepared":
        raise RuntimePreparationUncertain("Runtime preparation already crossed its send boundary")
    claim = await effects.claim_effect_for_dispatch(snapshot.effect_id, run)
    if claim is None:
        raise effects.EffectNotDispatchableError("Runtime preparation is already being processed")
    # Claim first, but cross the send boundary only inside the actual HTTP
    # hook, after the Driver, Task and subscription guards. A pre-send pause
    # can then release this claim without manufacturing an unknown outcome.
    bound = operations.BoundOperation(sandbox, claim, resource, journal,
        before_request=before_request, submitted=False)
    token = operations._current_operation.set(bound)
    try:
        result = await effects.run_with_effect_claim_heartbeat(claim, operation())
        bound.closed = True
        if bound.request_failed or bound.active_clients:
            raise effects.EffectNotDispatchableError("Runtime preparation has an unknown outcome")
        if before_request is not None:
            await before_request()
        receipt = {"result": result, "remote_exclusivity_verified": False}
        if effects.sanitize_public_evidence(receipt) != receipt:
            raise effects.EffectLedgerError("Runtime preparation receipt cannot be saved exactly")
        await effects.settle_effect(claim, state="succeeded", receipt=receipt)
        return result
    except BaseException as exc:
        bound.closed = True
        unsent = False
        try:
            await effects.abandon_effect_before_dispatch(claim, reason="runtime_preparation_not_sent")
            unsent = True
        except Exception:
            with suppress(Exception):
                await effects.record_effect_outcome_unknown(claim, error={"code": "runtime_preparation_interrupted"})
        from assistant.scheduling import TaskSchedulingHeld
        if unsent or isinstance(exc, (asyncio.CancelledError, TaskSchedulingHeld)):
            raise
        raise RuntimePreparationUncertain("Runtime preparation may have partially completed") from exc
    finally:
        bound.closed = True
        operations._current_operation.reset(token)
