"""Permission system: rule evaluation, ask/reply for user authorization."""
import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel

from bus import bus
from bus.events import PERMISSION_ASKED, PERMISSION_REPLIED
from core.identifier import generate_id
from core.log import create_logger
from core.wildcard import match as wildcard_match

log = create_logger("permission")

PermissionAction = Literal["once", "always", "reject"]


class Rule(BaseModel):
    permission: str
    pattern: str
    action: Literal["allow", "deny", "ask"]


class PermissionRequest(BaseModel):
    id: str
    user_id: str = "default"
    session_id: str
    tool: str
    input: dict[str, Any] = {}
    patterns: list[str] = []
    always: list[str] = []  # Broader patterns stored when user clicks "always allow"
    metadata: dict[str, Any] = {}
    is_doom_loop: bool = False
    created_at: str = ""


Ruleset = list[Rule]


@dataclass
class PendingPermission:
    """A permission request waiting for user response."""
    request: PermissionRequest
    event: asyncio.Event = field(default_factory=asyncio.Event)
    result: PermissionAction | None = None
    error_message: str | None = None
    trace_context: dict | None = None


# State
_pending: dict[str, PendingPermission] = {}
_approved: dict[str, Ruleset] = {}  # user_id -> rules (per-user isolation)
_loaded_users: set[str] = set()


#: A stored reply outlives any wait for it; its wake-up message may be lost.
REPLY_TTL_SECONDS = 24 * 60 * 60


def _trace_permission(request: PermissionRequest, event_type: str, data: dict | None = None,
                      *, saved: dict | None = None) -> dict | None:
    """Enqueue a permission fact under the call that asked; no transaction, never raises."""
    from trajectory import TraceContext, current, emit, enabled
    from trajectory.producers import identity
    if not enabled(request.user_id):
        return None
    base = identity(saved)
    try:
        context = TraceContext.from_dict(base) if base else current()
    except (TypeError, ValueError):
        return None
    if (context is None or context.user_id != request.user_id
            or (context.source_session_id or context.session_id) != request.session_id):
        return None
    emit(event_type, {
        "permission_id": request.id, "tool": request.tool,
        "requested_arguments": request.input, "patterns": request.patterns,
        "requested_at": request.created_at, "timing_source": "session_timestamps",
        **(data or {}),
    }, context=context, event_id=f"{event_type}:{request.id}")
    return context.to_dict()


def _trace_rule(session_id: str, user_id: str, permission: str, pattern: str,
                input_data: dict, action: str):
    from trajectory import enabled
    if not enabled(user_id):
        return
    request = PermissionRequest(id=generate_id(), user_id=user_id, session_id=session_id,
                                tool=permission, input=input_data, patterns=[pattern],
                                created_at=datetime.now(timezone.utc).isoformat())
    context = _trace_permission(request, "permission.requested", {"source_kind": "policy"})
    _trace_permission(request, "permission.resolved", {
        "decision": action, "source_kind": "policy",
        "status": "completed" if action == "allow" else "denied",
    }, saved=context)


def _get_user_approved(user_id: str) -> Ruleset:
    """Get approved rules for a specific user (creates empty list if needed)."""
    if user_id not in _approved:
        _approved[user_id] = []
    return _approved[user_id]


def _use_db() -> bool:
    try:
        from db.base import _engine
        return _engine is not None
    except ImportError:
        return False


async def load_persisted_rules(user_id: str = "default") -> None:
    """Load persisted permission rules from DB/FS into _approved[user_id]."""
    if user_id in _loaded_users:
        return
    _loaded_users.add(user_id)
    user_rules = _get_user_approved(user_id)
    try:
        if _use_db():
            from db.repository.permission_repo import PgPermissionRepo
            repo = PgPermissionRepo()
            rows = await repo.list_rules(user_id)
            for row in rows:
                user_rules.append(Rule(
                    permission=row["permission"],
                    pattern=row.get("pattern") or "*",
                    action=row.get("action") or "allow",
                ))
            if rows:
                log.info(f"Loaded {len(rows)} persisted permission rules for user {user_id}")
        else:
            from storage import storage
            data = await storage.read(["permissions", user_id])
            if data and isinstance(data, list):
                for item in data:
                    user_rules.append(Rule(**item))
                log.info(f"Loaded {len(data)} persisted permission rules from FS")
    except Exception as e:
        log.warning(f"Failed to load persisted permissions: {e}")


async def _persist_rule(user_id: str, rule: Rule) -> None:
    """Persist a single permission rule to DB/FS."""
    try:
        if _use_db():
            from db.repository.permission_repo import PgPermissionRepo
            repo = PgPermissionRepo()
            await repo.create_rule(
                user_id=user_id, id=generate_id(),
                permission=rule.permission, pattern=rule.pattern, action=rule.action,
            )
        else:
            from storage import storage
            existing = await storage.read(["permissions", user_id]) or []
            existing.append({"permission": rule.permission, "pattern": rule.pattern, "action": rule.action})
            await storage.write(["permissions", user_id], existing)
    except Exception as e:
        log.warning(f"Failed to persist permission rule: {e}")


def _get_redis_client():
    """Get the Redis client from the bus module, if available."""
    return bus._redis_client


def evaluate(permission: str, pattern: str, *rulesets: Ruleset) -> Rule:
    """Evaluate permission rules. Last-match-wins."""
    merged = []
    for rs in rulesets:
        merged.extend(rs)

    match = None
    for rule in merged:
        if wildcard_match(permission, rule.permission) and wildcard_match(pattern, rule.pattern):
            match = rule

    return match or Rule(permission=permission, pattern="*", action="ask")


EDIT_TOOLS = ["edit", "write", "patch", "multiedit", "apply_patch"]


def disabled_tools(tools: list[str], ruleset: Ruleset) -> set[str]:
    """Find tools that are denied by rules."""
    result = set()
    for tool in tools:
        permission = "edit" if tool in EDIT_TOOLS else tool
        for rule in reversed(ruleset):
            if wildcard_match(permission, rule.permission):
                if rule.pattern == "*" and rule.action == "deny":
                    result.add(tool)
                break
    return result


async def _wait_via_redis(request_id: str, pending: PendingPermission) -> None:
    """Wait for a permission reply via Redis Pub/Sub channel."""
    redis_client = _get_redis_client()
    channel_name = f"perm_reply:{request_id}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_name)
    try:
        while True:
            if pending.event.is_set():
                return
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is not None and message["type"] == "message":
                try:
                    reply_data = json.loads(message["data"])
                    pending.result = reply_data.get("action")
                    pending.error_message = reply_data.get("message")
                    return
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    log.warning(f"Invalid permission reply message: {e}")
            else:
                if await _read_recorded_reply(request_id, pending):
                    return
                await asyncio.sleep(0.01)
    finally:
        try:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()
        except Exception:
            pass


async def _read_recorded_reply(request_id: str, pending: PendingPermission) -> bool:
    """A stored reply survives loss of its Redis wake-up notification."""
    redis_client = _get_redis_client()
    if redis_client is None:
        return False
    try:
        raw = await redis_client.get(f"perm_reply:{request_id}")
        reply_data = json.loads(raw) if raw else None
    except Exception as e:
        log.debug(f"Stored permission reply unavailable: {type(e).__name__}")
        return False
    if not isinstance(reply_data, dict):
        return False
    # The stored reply names its request's owner and session, so no other
    # request can hand this waiter a decision.
    if (reply_data.get("user_id") != pending.request.user_id
            or reply_data.get("session_id") != pending.request.session_id):
        return False
    action = reply_data.get("action")
    if action not in {"once", "always", "reject"}:
        return False
    pending.result, pending.error_message = action, reply_data.get("message")
    return True


async def _push_waiting(request):
    try:
        from notifications.events import permission_waiting
        from question.runtime import current_run
        await permission_waiting(request, current_run.get())
    except Exception as error:
        log.warning("Permission notification deferred: %s", type(error).__name__)


async def _push_resolved(request_id, user_id):
    try:
        from notifications.events import permission_resolved
        await permission_resolved(request_id, user_id)
    except Exception as error:
        log.warning("Permission notification cancellation deferred: %s", type(error).__name__)


async def ask(
    session_id: str,
    permission: str,
    patterns: list[str],
    input_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    config_rules: Ruleset | None = None,
    is_doom_loop: bool = False,
    always: list[str] | None = None,
    user_id: str = "default",
) -> None:
    """Check permission and block until authorized if needed.

    Raises:
        PermissionDeniedError: If denied by rule
        PermissionRejectedError: If rejected by user
    """
    if input_data is None:
        input_data = {}
    if metadata is None:
        metadata = {}

    user_rules = _get_user_approved(user_id)
    rulesets = []
    if config_rules:
        rulesets.append(config_rules)
    rulesets.append(user_rules)

    # Check each pattern
    for pattern in patterns:
        rule = evaluate(permission, pattern, *rulesets)

        if rule.action == "allow":
            _trace_rule(session_id, user_id, permission, pattern, input_data, "allow")
            continue
        elif rule.action == "deny":
            _trace_rule(session_id, user_id, permission, pattern, input_data, "deny")
            raise PermissionDeniedError(permission, pattern)
        else:
            # Need to ask user
            request_id = generate_id()
            request = PermissionRequest(
                id=request_id,
                user_id=user_id,
                session_id=session_id,
                tool=permission,
                input=input_data,
                patterns=patterns,
                always=always or patterns,
                metadata=metadata,
                is_doom_loop=is_doom_loop,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

            trace = _trace_permission(request, "permission.requested", {"source_kind": "user"})
            pending = PendingPermission(request=request, trace_context=trace)
            _pending[request_id] = pending

            redis_client = _get_redis_client()

            if redis_client is not None:
                # Store request data in Redis for cross-worker access
                try:
                    await redis_client.setex(
                        f"perm_req:{request_id}",
                        300,  # TTL 300s
                        json.dumps({**request.model_dump(), "trace_context": trace}),
                    )
                except Exception as e:
                    log.warning(f"Failed to store permission request in Redis: {e}")

            try:
                await _push_waiting(request)
                # Publish only after the outbox exists, so an immediate reply
                # cannot cancel before the waiting notification is enqueued.
                bus.publish(PERMISSION_ASKED, {**request.model_dump(), "userId": user_id})
                if redis_client is not None:
                    try:
                        await _wait_via_redis(request_id, pending)
                    except Exception as e:
                        log.warning(f"Redis wait failed, falling back to local: {e}")
                        await pending.event.wait()
                else:
                    await pending.event.wait()
            except asyncio.CancelledError:
                _trace_permission(request, "permission.expired", {
                    "status": "cancelled", "reason": "run_cancelled",
                }, saved=trace)
                raise
            finally:
                _pending.pop(request_id, None)
                await _push_resolved(request_id, user_id)

            if pending.result == "reject":
                if pending.error_message:
                    raise PermissionCorrectedError(pending.error_message)
                raise PermissionRejectedError()

            # "once" or "always" — continue
            return


async def reply(request_id: str, action: PermissionAction, message: str | None = None, user_id: str = "default") -> None:
    """Handle user reply to a permission request."""
    redis_client = _get_redis_client()

    # Try to load request from Redis first (cross-worker scenario)
    request_data = None
    if redis_client is not None:
        try:
            raw = await redis_client.get(f"perm_req:{request_id}")
            if raw:
                request_data = json.loads(raw)
        except Exception as e:
            log.warning(f"Failed to read permission request from Redis: {e}")

    # Check local pending dict
    pending = _pending.get(request_id)

    # Enforce per-user ownership (works for local + cross-worker request data).
    owner_id = pending.request.user_id if pending is not None else (request_data or {}).get("user_id")
    if owner_id and owner_id != user_id:
        # Put back local pending if we popped someone else's request by accident.
        if pending is not None:
            _pending[request_id] = pending
        raise PermissionError("Permission request does not belong to current user")
    if pending is None and request_data is None:
        raise KeyError("Permission request not found")

    request = pending.request if pending else PermissionRequest.model_validate(request_data)
    trace = pending.trace_context if pending else request_data.get("trace_context")
    _trace_permission(request, "permission.resolved", {
        "decision": action, "message": message, "source_kind": "user",
        "status": "denied" if action == "reject" else "completed",
        "correction": message if action == "reject" and message else None,
    }, saved=trace)
    _pending.pop(request_id, None)
    if redis_client is not None and request_data is not None:
        await redis_client.delete(f"perm_req:{request_id}")

    await _push_resolved(request_id, user_id)

    if pending is not None:
        # Local worker owns this request
        pending.result = action
        pending.error_message = message

        if action == "always":
            # Add broader "always" patterns to this user's approved rules
            user_rules = _get_user_approved(user_id)
            always_patterns = pending.request.always or pending.request.patterns
            for pattern in always_patterns:
                new_rule = Rule(
                    permission=pending.request.tool,
                    pattern=pattern,
                    action="allow",
                )
                user_rules.append(new_rule)
                asyncio.create_task(_persist_rule(user_id, new_rule))

            # Auto-resolve other pending permissions for the same session that now pass
            session_id = pending.request.session_id
            for rid, p in list(_pending.items()):
                if p.request.session_id != session_id:
                    continue
                all_ok = all(
                    evaluate(p.request.tool, pat, user_rules).action == "allow"
                    for pat in p.request.patterns
                )
                if all_ok:
                    _trace_permission(p.request, "permission.resolved", {
                        "decision": "always", "source_kind": "policy",
                        "status": "completed", "caused_by_permission_id": request_id,
                    }, saved=p.trace_context)
                    p.result = "always"
                    _pending.pop(rid, None)
                    p.event.set()

        elif action == "reject":
            # Reject all pending permissions for this session
            session_id = pending.request.session_id
            for rid, p in list(_pending.items()):
                if p.request.session_id == session_id:
                    _trace_permission(p.request, "permission.resolved", {
                        "decision": "reject", "source_kind": "user",
                        "status": "denied", "caused_by_permission_id": request_id,
                    }, saved=p.trace_context)
                    p.result = "reject"
                    _pending.pop(rid, None)
                    p.event.set()

        pending.event.set()

    # Publish reply via Redis channel for cross-worker delivery. The reply is
    # stored first, so a waiter that misses the message still reads it.
    if redis_client is not None:
        reply_payload = json.dumps({
            "action": action,
            "message": message,
            "user_id": request.user_id,
            "session_id": request.session_id,
        })
        try:
            await redis_client.setex(f"perm_reply:{request_id}", REPLY_TTL_SECONDS, reply_payload)
        except Exception as e:
            log.warning(f"Failed to store permission reply in Redis: {e}")
        try:
            await redis_client.publish(f"perm_reply:{request_id}", reply_payload)
        except Exception as e:
            log.warning(f"Failed to publish permission reply to Redis: {e}")

    # Determine session_id for the bus event
    session_id = None
    if pending is not None:
        session_id = pending.request.session_id
    elif request_data is not None:
        session_id = request_data.get("session_id")

    # Publish replied event so all connected clients can remove the dialog
    bus.publish(PERMISSION_REPLIED, {
        "userId": user_id,
        "id": request_id,
        "session_id": session_id or "",
        "action": action,
    })


def list_pending(user_id: str | None = None) -> list[PermissionRequest]:
    """List all pending permission requests."""
    requests = [p.request for p in _pending.values()]
    if user_id is None:
        return requests
    return [r for r in requests if r.user_id == user_id]


class PermissionDeniedError(Exception):
    def __init__(self, permission: str, pattern: str):
        super().__init__(f"Permission denied: {permission} for {pattern}")
        self.permission = permission
        self.pattern = pattern


class PermissionRejectedError(Exception):
    def __init__(self):
        super().__init__("The user rejected permission to use this tool call.")


class PermissionCorrectedError(Exception):
    def __init__(self, message: str):
        super().__init__(f"The user rejected with feedback: {message}")
        self.feedback = message
