"""Bind a computer call and its actual HTTP requests to one durable effect.

Bound v2 Action Servers also fence HTTP requests against the pinned journal.
Legacy servers and direct CDP/native clients remain outside that coverage, so
resource_control deliberately never grants exclusive human control yet.
"""
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
import re
import secrets
from typing import Any, Awaitable, Callable

from agent import effect_ledger as effects
from assistant import resource_control as controls
from tool.tool import ToolContext, ToolResult


@dataclass(frozen=True)
class BoundOperation:
    sandbox: Any
    claim: effects.EffectClaim
    fence: controls.ResourceFence
    journal_id: str | None = None


_current_operation: ContextVar[BoundOperation | None] = ContextVar("resource_operation", default=None)


async def authorize_request(sandbox, request) -> None:
    operation = _current_operation.get()
    if operation is None:
        return
    if operation.sandbox is not sandbox:
        raise controls.unavailable()
    # Release can only relinquish the exact temporary token; it cannot send
    # input. A close during a compound action must not prevent that cleanup.
    if request.method == "POST" and request.url.path == "/desktop/lease/release":
        return
    await effects.assert_effect_dispatchable(operation.claim)
    request.headers.update({
        "X-OpenBox-Resource": operation.fence.resource_id,
        "X-OpenBox-Resource-Epoch": str(operation.fence.epoch),
        "X-OpenBox-Resource-Owner": operation.fence.owner_kind,
        "X-OpenBox-Resource-Owner-Id": operation.fence.owner_id,
        "X-OpenBox-Resource-Operation": operation.claim.effect_id,
    })
    # Re-sending this HTTP request must carry the same child operation ID.
    # Separate requests in one compound tool share the durable parent effect,
    # but cannot accidentally deduplicate two legitimate sequential inputs.
    request.headers.setdefault("X-OpenBox-Resource-Step", "rop_" + secrets.token_hex(24))
    if operation.journal_id is not None:
        request.headers["X-OpenBox-Resource-Journal"] = operation.journal_id


async def observe_response(sandbox, response) -> None:
    operation = _current_operation.get()
    if operation is None:
        return
    if response.request.method == "POST" and response.request.url.path == "/desktop/lease/release":
        return
    remote_id = response.headers.get("X-OpenBox-Remote-Operation")
    journal_id = response.headers.get("X-OpenBox-Resource-Journal")
    if remote_id is None and journal_id is None:
        if operation.journal_id is not None:
            raise effects.EffectLedgerError("Pinned resource admission receipt is missing")
        return  # Legacy servers remain explicitly uncertified for takeover.
    if (operation.sandbox is not sandbox or not remote_id
            or remote_id != response.request.headers.get("X-OpenBox-Resource-Step")
            or not journal_id or not re.fullmatch(r"[0-9a-f]{32}", journal_id)):
        raise effects.EffectLedgerError("Invalid remote resource admission receipt")
    if operation.journal_id is not None and journal_id != operation.journal_id:
        raise effects.EffectLedgerError("Remote resource journal changed")
    await effects.record_effect_dispatch_progress(operation.claim, phase="resource.admitted",
        evidence={"remote_operation_id": remote_id, "remote_journal_id": journal_id,
                  "remote_exclusivity_verified": False})


async def prepare_desktop_tool(ctx: ToolContext, args, *, part_id=None):
    """Persist the original model request's resource fence before approval/queueing."""
    desktop_id = getattr(ctx.sandbox, "desktop_id", None)
    # Docker and non-Driver legacy tools have no physical adapter yet. They
    # remain outside the takeover coverage inventory, never silently certified.
    if not isinstance(desktop_id, str) or not desktop_id or getattr(ctx, "run_fence", None) is None:
        return None
    from tool.computer import ComputerArgs
    args = ComputerArgs.model_validate(args) if isinstance(args, dict) else args
    if args.action == "wait":
        return None
    part_id = part_id or ctx.part_id
    if (not part_id or not ctx.message_id or not ctx.workspace_id
            or getattr(ctx.sandbox, "workspace_id", None) != ctx.workspace_id):
        raise controls.unavailable()
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.agent_event import AgentEvent
    from db.models.part import Part
    async with get_db_session() as db:
        part = await db.get(Part, part_id)
        if (part is None or part.user_id != ctx.user_id or part.session_id != ctx.session_id
                or part.message_id != ctx.message_id or part.canonical_tool_id != "computer"):
            raise controls.unavailable()
        # Recovery may use a new Driver. The first canonical call, not its
        # newest update or the latest provider step, owns the original request.
        called = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.part_id == part_id,
            AgentEvent.message_id == ctx.message_id, AgentEvent.kind == "tool.called")
            .order_by(AgentEvent.sequence).limit(1))
        request = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.user_id == ctx.user_id, AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested", AgentEvent.run_id == called.run_id,
            AgentEvent.generation == called.generation, AgentEvent.sequence < called.sequence)
            .order_by(AgentEvent.sequence.desc()).limit(1)) if called else None
        context = request.payload.get("resource_context") if request else None
        if (not isinstance(context, dict) or set(context) != {"version", "desktop_id", "fence", "journal_id"}
                or type(context.get("version")) is not int or context["version"] != 1
                or context.get("desktop_id") != desktop_id or not isinstance(context.get("fence"), dict)):
            raise controls.unavailable()
        try:
            resource = controls.ResourceFence(**context["fence"])
        except (TypeError, ValueError) as exc:
            raise controls.unavailable() from exc
        journal_id = context.get("journal_id")
        if journal_id is not None and (not isinstance(journal_id, str) or not re.fullmatch(r"[0-9a-f]{32}", journal_id)):
            raise controls.unavailable()
        row = await controls.validate_locked(db, resource, user_id=ctx.user_id, session_id=ctx.session_id)
        if row.remote_journal_id != journal_id:
            raise controls.unavailable()
        original = ComputerArgs.model_validate(part.data.get("input"))
        if original.model_dump(mode="json") != args.model_dump(mode="json"):
            raise effects.EffectConflictError("Desktop call input differs from its persisted tool arguments")
    run = effects.EffectRunFence.from_tool_context(ctx)
    prepared = await effects.prepare_effect(run, adapter="computer", provider="wuying",
        operation="desktop_tool", logical_key=part_id, request_payload=args.model_dump(mode="json"),
        resource_fence=resource, project_id=ctx.project_id or None,
        safe_context={"tool_part_id": part_id, "action": args.action,
            "resource_request_sequence": request.sequence, "resource_request_id": request.payload["request_id"],
            "resource_journal_id": journal_id})
    return prepared, resource, journal_id


async def run_desktop_tool(ctx: ToolContext, args, operation: Callable[[], Awaitable[ToolResult]]) -> ToolResult:
    binding = await prepare_desktop_tool(ctx, args)
    if binding is None:
        return await operation()
    prepared, resource, journal_id = binding
    run = effects.EffectRunFence.from_tool_context(ctx)
    if prepared.snapshot.state != "prepared":
        raise effects.EffectNotDispatchableError(
            "This desktop operation already crossed the send boundary. Do not repeat it; inspect its outcome first.")
    claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, run)
    if claim is None:
        raise effects.EffectNotDispatchableError("This desktop operation is already being processed")
    await effects.mark_effect_submitting(claim)
    token = _current_operation.set(BoundOperation(ctx.sandbox, claim, resource, journal_id))
    try:
        result = await effects.run_with_effect_claim_heartbeat(claim, operation())
        if result.metadata.get("error") or result.metadata.get("observation_error"):
            await effects.record_effect_outcome_unknown(claim, error={"code": "desktop_result_uncertain"})
            result = result.model_copy(update={
                "output": result.output + "\nThis desktop operation may have partially completed. Inspect current state before any retry.",
                "metadata": {**result.metadata, "resource_outcome": "outcome_unknown",
                             "resource_effect_id": claim.effect_id},
            })
        else:
            # A successful tool response settles this tracked invocation. It
            # does not certify that all other physical channels have drained.
            await effects.settle_effect(claim, state="succeeded",
                receipt={"tool_part_id": ctx.part_id, "response_received": True,
                         "remote_exclusivity_verified": False})
        return result
    except BaseException:
        # Cancellation, transport loss and a dead Driver cannot prove that a
        # remote command stopped. If even this write fails, submitting remains
        # durable and also blocks drainage; recovery must never resend it.
        with suppress(Exception):
            await effects.record_effect_outcome_unknown(claim, error={"code": "desktop_response_lost"})
        raise
    finally:
        _current_operation.reset(token)
