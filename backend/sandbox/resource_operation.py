"""Bind tool sandbox requests to their original resource control and effects.

Bound v2 Action Servers also fence HTTP requests against the pinned journal.
Legacy servers and direct CDP/native clients remain outside that coverage, so
resource_control deliberately never grants exclusive human control yet.
"""
import asyncio
import copy
from contextlib import suppress
from contextvars import ContextVar
from dataclasses import dataclass
import re
import secrets
from typing import Any, Awaitable, Callable

from agent import effect_ledger as effects
from assistant import resource_control as controls
from tool.tool import ToolContext, ToolResult


@dataclass
class BoundOperation:
    sandbox: Any
    claim: effects.EffectClaim
    fence: controls.ResourceFence
    journal_id: str | None = None
    request_failed: bool = False
    active_clients: int = 0
    closed: bool = False


_current_operation: ContextVar[BoundOperation | None] = ContextVar("resource_operation", default=None)
_current_tool_scope: ContextVar["ToolResourceScope | None"] = ContextVar("tool_resource_scope", default=None)


def _physical_driver(ctx):
    desktop_id = getattr(ctx.sandbox, "desktop_id", None)
    return isinstance(desktop_id, str) and bool(desktop_id) and ctx.run_fence is not None


def _tool_scope():
    scope = _current_tool_scope.get()
    if scope is not None:
        from agent.driver import _current_lease
        lease = _current_lease.get()
        if lease is not None and (lease.session_id, lease.run_id, lease.generation) != scope.ctx.run_fence:
            # A child Driver has its own provider checkpoint and tool scopes.
            # Do not attribute its startup work to an inherited parent call.
            return None
    return scope


def _bound_operation():
    explicit = _current_operation.get()
    scope = _tool_scope()
    return explicit or (scope.bound if scope is not None else None)


async def authorize_request(sandbox, request) -> None:
    operation = _current_operation.get()
    if operation is None:
        scope = _tool_scope()
        if scope is not None:
            operation = await scope.admit(sandbox)
    if operation is None:
        return
    if operation.sandbox is not sandbox:
        raise controls.unavailable()
    # Release can only relinquish the exact temporary token; it cannot send
    # input. A close during a compound action must not prevent that cleanup.
    if request.method == "POST" and request.url.path == "/desktop/lease/release":
        return
    if operation.closed:
        raise controls.unavailable()
    if operation.request_failed:
        raise effects.EffectNotDispatchableError("A previous sandbox request has an unknown outcome")
    await effects.assert_effect_dispatchable(operation.claim)
    if operation.closed or operation.request_failed:
        raise controls.unavailable()
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
    operation = _bound_operation()
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


async def request_failed(sandbox) -> None:
    """Latch uncertainty before a tool can swallow the error and send again."""
    operation = _bound_operation()
    if operation is None:
        scope = _tool_scope()
        if scope is not None:
            scope.request_failed = True
        return
    # Submitting was committed before sending. Keep its claim alive until the
    # enclosing tool settles unknown; even a process crash cannot make it
    # eligible to resend. The local latch also blocks caught-error fallbacks.
    operation.request_failed = True


def client_started():
    scope = _current_operation.get() or _tool_scope()
    if scope is not None:
        scope.active_clients += 1
    return scope


def client_finished(scope):
    if scope is not None:
        scope.active_clients -= 1


async def prepare_desktop_tool(ctx: ToolContext, args, *, part_id=None):
    """Persist the original model request's resource fence before approval/queueing."""
    # Docker and non-Driver legacy tools have no physical adapter yet. They
    # remain outside the takeover coverage inventory, never silently certified.
    if not _physical_driver(ctx):
        return None
    from tool.computer import ComputerArgs
    args = ComputerArgs.model_validate(args) if isinstance(args, dict) else args
    if args.action == "wait":
        return None
    part_id = part_id or ctx.part_id
    payload = args.model_dump(mode="json")
    resource, journal_id, request = await _original_call_resource(ctx, "computer", part_id,
        payload, normalize=lambda value: ComputerArgs.model_validate(value).model_dump(mode="json"))
    run = effects.EffectRunFence.from_tool_context(ctx)
    prepared = await effects.prepare_effect(run, adapter="computer", provider="wuying",
        operation="desktop_tool", logical_key=part_id, request_payload=payload,
        resource_fence=resource, project_id=ctx.project_id or None,
        safe_context={"tool_part_id": part_id, "action": args.action,
            **_request_context(request, journal_id)})
    return prepared, resource, journal_id


def _request_context(request, journal_id):
    return {"resource_request_sequence": request.sequence,
            "resource_request_id": request.payload["request_id"], "resource_journal_id": journal_id}


async def _original_call_resource(ctx, tool_id, part_id, args, *, normalize=lambda value: value):
    desktop_id = ctx.sandbox.desktop_id
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
                or part.message_id != ctx.message_id or part.canonical_tool_id != tool_id):
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
        if effects.request_hash(normalize(part.data.get("input"))) != effects.request_hash(args):
            raise effects.EffectConflictError("Sandbox call input differs from its persisted tool arguments")
    return resource, journal_id, request


class ToolResourceScope:
    """Call-local scope; actual HTTP use, not tool metadata, admits its effect."""

    def __init__(self, ctx, tool_id, args):
        self.ctx = copy.copy(ctx)
        self.tool_id, self.args = tool_id, copy.deepcopy(args)
        self.bound = None
        self.closed = False
        self.active_clients = 0
        self.request_failed = False
        self.admitted = asyncio.Event()
        self.lock = asyncio.Lock()

    async def admit(self, sandbox):
        async with self.lock:
            if self.closed or sandbox is not self.ctx.sandbox:
                raise controls.unavailable()
            if self.bound is None:
                ctx = self.ctx
                resource, journal, request = await _original_call_resource(
                    ctx, self.tool_id, ctx.part_id, self.args)
                if self.closed:
                    raise controls.unavailable()
                run = effects.EffectRunFence.from_tool_context(ctx)
                prepared = await effects.prepare_effect(run, adapter="sandbox_tool", provider="wuying",
                    operation="tool_execute", logical_key=ctx.part_id,
                    request_payload={"tool": self.tool_id, "arguments": self.args},
                    resource_fence=resource, project_id=ctx.project_id or None,
                    safe_context={"tool_part_id": ctx.part_id, "tool_id": self.tool_id,
                        **_request_context(request, journal)})
                if prepared.snapshot.state != "prepared":
                    raise effects.EffectNotDispatchableError("This sandbox call already crossed its send boundary")
                if self.closed:
                    raise controls.unavailable()
                claim = await effects.claim_effect_for_dispatch(prepared.snapshot.effect_id, run)
                if claim is None:
                    raise effects.EffectNotDispatchableError("This sandbox call is already being processed")
                if self.closed:
                    raise controls.unavailable()
                await effects.mark_effect_submitting(claim)
                self.bound = BoundOperation(sandbox, claim, resource, journal)
                if self.closed:
                    self.bound.closed = True
                    with suppress(Exception):
                        await effects.record_effect_outcome_unknown(claim,
                            error={"code": "scope_closed_during_admission"})
                    raise controls.unavailable()
                self.admitted.set()
            return self.bound


async def run_tool_resource_scope(ctx, tool_id, args, operation):
    """Fence every sandbox request from a tool, including dynamic MCP/composites.

    Tools with no physical HTTP use need no resource claim. Detached work keeps
    the closed scope and cannot send after the owning call has finished.
    """
    if not _physical_driver(ctx):
        return await operation()
    parent = _tool_scope()
    if (parent is not None and parent.ctx.run_fence == ctx.run_fence
            and parent.ctx.part_id == ctx.part_id and parent.tool_id == tool_id):
        if effects.request_hash(parent.args) != effects.request_hash(args):
            raise effects.EffectConflictError("Nested tool arguments changed inside their resource scope")
        return await operation()
    scope = ToolResourceScope(ctx, tool_id, args)
    token = _current_tool_scope.set(scope)
    body = asyncio.create_task(operation())
    admitted = asyncio.create_task(scope.admitted.wait())
    try:
        await asyncio.wait({body, admitted}, return_when=asyncio.FIRST_COMPLETED)
        result = (await effects.run_with_effect_claim_heartbeat(scope.bound.claim, body)
                  if scope.bound is not None else await body)
        scope.closed = True
        if scope.bound is not None:
            scope.bound.closed = True
            failed = scope.active_clients > 0 or scope.bound.request_failed or bool(result.metadata.get("error")
                or result.metadata.get("observation_error") or result.metadata.get("outcome_unknown"))
            if failed:
                await effects.record_effect_outcome_unknown(scope.bound.claim,
                    error={"code": "sandbox_result_uncertain"})
                result = result.model_copy(update={
                    "output": result.output + "\nThis sandbox operation may have partially completed. Inspect current state before any retry.",
                    "metadata": {**result.metadata, "error": True, "resource_outcome": "outcome_unknown",
                        "resource_effect_id": scope.bound.claim.effect_id}})
            else:
                await effects.settle_effect(scope.bound.claim, state="succeeded",
                    receipt={"tool_part_id": ctx.part_id, "response_received": True,
                        "remote_exclusivity_verified": False})
        elif scope.request_failed:
            result = result.model_copy(update={
                "output": result.output + "\nThe sandbox request was not admitted.",
                "metadata": {**result.metadata, "error": True, "resource_outcome": "not_dispatched"}})
        return result
    except BaseException:
        body.cancel()
        with suppress(BaseException):
            await body
        if scope.bound is not None:
            with suppress(Exception):
                await effects.record_effect_outcome_unknown(scope.bound.claim,
                    error={"code": "sandbox_tool_interrupted"})
        raise
    finally:
        scope.closed = True
        if scope.bound is not None:
            scope.bound.closed = True
        admitted.cancel()
        with suppress(asyncio.CancelledError):
            await admitted
        _current_tool_scope.reset(token)


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
    bound = BoundOperation(ctx.sandbox, claim, resource, journal_id)
    token = _current_operation.set(bound)
    try:
        result = await effects.run_with_effect_claim_heartbeat(claim, operation())
        bound.closed = True
        if (bound.active_clients > 0 or bound.request_failed or result.metadata.get("error")
                or result.metadata.get("observation_error")):
            await effects.record_effect_outcome_unknown(claim, error={"code": "desktop_result_uncertain"})
            result = result.model_copy(update={
                "output": result.output + "\nThis desktop operation may have partially completed. Inspect current state before any retry.",
                "metadata": {**result.metadata, "resource_outcome": "outcome_unknown",
                             "resource_effect_id": claim.effect_id, "error": True},
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
        bound.closed = True
        _current_operation.reset(token)
