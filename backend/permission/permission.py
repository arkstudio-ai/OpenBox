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


# State
_pending: dict[str, PendingPermission] = {}
_approved: dict[str, Ruleset] = {}  # user_id -> rules (per-user isolation)
_loaded_users: set[str] = set()


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


async def _persist_rule(
    user_id: str,
    rule: Rule,
    *,
    raise_on_error: bool = False,
) -> bool:
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
        return True
    except Exception as e:
        log.warning(f"Failed to persist permission rule: {e}")
        if raise_on_error:
            raise
        return False


def _cache_rule(user_id: str, rule: Rule) -> None:
    """Merge one persisted grant into this worker without duplicating it."""
    user_rules = _get_user_approved(user_id)
    if not any(existing == rule for existing in user_rules):
        user_rules.append(rule)


def _rules_from_reply(data: dict[str, Any]) -> list[Rule]:
    """Validate the bounded rule projection carried between workers."""
    raw_rules = data.get("granted_rules") or []
    if not isinstance(raw_rules, list):
        return []
    rules: list[Rule] = []
    for raw in raw_rules:
        if not isinstance(raw, dict):
            continue
        try:
            rule = Rule.model_validate(raw)
        except Exception:
            continue
        if rule.action == "allow":
            rules.append(rule)
    return rules


def _apply_reply_data(pending: PendingPermission, data: dict[str, Any]) -> None:
    """Apply a local or Redis reply and synchronize this worker's grant cache."""
    action = data.get("action")
    pending.result = action if action in {"once", "always", "reject"} else None
    pending.error_message = data.get("message")
    if pending.result == "always":
        for rule in _rules_from_reply(data):
            _cache_rule(pending.request.user_id, rule)


def _resolve_related_pending(pending: PendingPermission) -> None:
    """Apply session-scoped always/reject behavior on the waiting worker."""
    action = pending.result
    if action not in {"always", "reject"}:
        return
    user_rules = _get_user_approved(pending.request.user_id)
    for request_id, other in list(_pending.items()):
        if request_id == pending.request.id:
            continue
        if (
            other.request.user_id != pending.request.user_id
            or other.request.session_id != pending.request.session_id
        ):
            continue
        if action == "always":
            all_ok = all(
                evaluate(other.request.tool, pattern, user_rules).action == "allow"
                for pattern in other.request.patterns
            )
            if not all_ok:
                continue
            other.result = "always"
        else:
            other.result = "reject"
        _pending.pop(request_id, None)
        other.event.set()


async def _record_always_grant(
    user_id: str,
    request: PermissionRequest,
) -> list[Rule]:
    """Durably record an always reply before acknowledging it to any worker."""
    await load_persisted_rules(user_id)
    rules = [
        Rule(permission=request.tool, pattern=pattern, action="allow")
        for pattern in (request.always or request.patterns)
    ]
    existing = _get_user_approved(user_id)
    for rule in rules:
        if any(current == rule for current in existing):
            continue
        await _persist_rule(user_id, rule, raise_on_error=True)
        _cache_rule(user_id, rule)
    return rules


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


def evaluate_guard(permission: str, pattern: str, ruleset: Ruleset) -> Rule:
    """Evaluate a deployment guard without treating no-match as an ask.

    Ordinary permission evaluation intentionally defaults to ``ask``. A guard
    is instead a restriction floor layered over the ordinary policy, so an
    unmatched guard must be neutral while an explicitly matched ``ask`` must
    remain visible even when a later Agent rule says ``allow``.
    """
    return evaluate(
        permission,
        pattern,
        [Rule(permission="*", pattern="*", action="allow"), *ruleset],
    )


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
    """Wait for a permission reply via Pub/Sub plus a durable response key."""
    redis_client = _get_redis_client()
    channel_name = f"perm_reply:{request_id}"
    response_key = f"perm_resp:{request_id}"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_name)
    try:
        while True:
            if pending.event.is_set():
                return
            try:
                durable = await redis_client.get(response_key)
            except Exception:
                durable = None
            if durable:
                try:
                    reply_data = json.loads(durable)
                    _apply_reply_data(pending, reply_data)
                    _resolve_related_pending(pending)
                    return
                except (json.JSONDecodeError, TypeError) as exc:
                    log.warning(f"Invalid durable permission reply: {exc}")
            message = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0
            )
            if message is not None and message["type"] == "message":
                try:
                    reply_data = json.loads(message["data"])
                    _apply_reply_data(pending, reply_data)
                    _resolve_related_pending(pending)
                    return
                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    log.warning(f"Invalid permission reply message: {e}")
            else:
                await asyncio.sleep(0.01)
    finally:
        try:
            await pubsub.unsubscribe(channel_name)
            await pubsub.aclose()
        except Exception:
            pass


async def ask(
    session_id: str,
    permission: str,
    patterns: list[str],
    input_data: dict[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
    config_rules: Ruleset | None = None,
    guard_rules: Ruleset | None = None,
    authority_rulesets: list[Ruleset] | tuple[Ruleset, ...] | None = None,
    authority_guard_rulesets: list[Ruleset] | tuple[Ruleset, ...] | None = None,
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
    trusted_rules = config_rules or []
    deployment_guards = guard_rules or []
    inherited_rulesets = authority_rulesets or []
    inherited_guard_rulesets = authority_guard_rulesets or []

    # Resolve the complete call before publishing a prompt. Trusted
    # config/Agent policy is authoritative: persisted user approvals may only
    # resolve an ``ask`` and can never turn a current deny into allow. Checking
    # every target first also prevents an early ask from skipping a later deny
    # in multi-file tools.
    needs_confirmation = False
    for pattern in patterns:
        trusted_actions = [
            evaluate(permission, pattern, trusted_rules),
            *(
                evaluate(permission, pattern, ruleset)
                for ruleset in inherited_rulesets
            ),
        ]
        guard_actions = [
            evaluate_guard(permission, pattern, deployment_guards),
            *(
                evaluate_guard(permission, pattern, ruleset)
                for ruleset in inherited_guard_rulesets
            ),
        ]
        if any(rule.action == "deny" for rule in [*trusted_actions, *guard_actions]):
            raise PermissionDeniedError(permission, pattern)
        if (
            all(rule.action == "allow" for rule in trusted_actions)
            and all(rule.action != "ask" for rule in guard_actions)
        ):
            continue

        user_rule = evaluate(permission, pattern, user_rules)
        if user_rule.action == "deny":
            raise PermissionDeniedError(permission, pattern)
        if user_rule.action == "allow":
            continue
        needs_confirmation = True

    if not needs_confirmation:
        return

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

    pending = PendingPermission(request=request)
    _pending[request_id] = pending
    redis_client = _get_redis_client()

    if redis_client is not None:
        try:
            await redis_client.setex(
                f"perm_req:{request_id}",
                300,
                json.dumps(request.model_dump()),
            )
        except Exception as e:
            log.warning(f"Failed to store permission request in Redis: {e}")

    bus.publish(PERMISSION_ASKED, {**request.model_dump(), "userId": user_id})

    try:
        if redis_client is not None:
            try:
                await _wait_via_redis(request_id, pending)
            except Exception as e:
                log.warning(f"Redis wait failed, falling back to local: {e}")
                await pending.event.wait()
        else:
            await pending.event.wait()
    finally:
        _pending.pop(request_id, None)
        if redis_client is not None:
            try:
                await redis_client.delete(
                    f"perm_req:{request_id}",
                    f"perm_resp:{request_id}",
                )
            except Exception:
                pass

    if pending.result not in {"once", "always"}:
        if pending.error_message:
            raise PermissionCorrectedError(pending.error_message)
        raise PermissionRejectedError()


async def _consume_redis_request(redis_client, request_id: str, raw: str) -> str | None:
    """Claim one request after ownership validation, atomically when supported."""
    key = f"perm_req:{request_id}"
    getdel = getattr(redis_client, "getdel", None)
    if callable(getdel):
        return await getdel(key)
    # Compatibility for older/fake clients. Production Redis clients expose
    # GETDEL; this fallback preserves the legacy behavior without pretending it
    # is an atomic distributed claim.
    await redis_client.delete(key)
    return raw


async def reply(
    request_id: str,
    action: PermissionAction,
    message: str | None = None,
    user_id: str = "default",
) -> None:
    """Handle a reply, durably recording ``always`` before acknowledgement."""
    if action not in {"once", "always", "reject"}:
        raise ValueError("Invalid permission action")

    redis_client = _get_redis_client()
    request_data = None
    raw_request = None
    if redis_client is not None:
        try:
            raw_request = await redis_client.get(f"perm_req:{request_id}")
            if raw_request:
                request_data = json.loads(raw_request)
        except Exception as exc:
            log.warning(f"Failed to read permission request from Redis: {exc}")

    # Do not consume either representation until the authenticated owner has
    # been checked. A wrong-user request must not be able to destroy the real
    # user's pending prompt.
    pending = _pending.get(request_id)
    owner_id = (
        pending.request.user_id
        if pending is not None
        else (request_data or {}).get("user_id")
    )
    if owner_id and owner_id != user_id:
        raise PermissionError("Permission request does not belong to current user")
    if pending is None and request_data is None:
        raise KeyError("Permission request not found")

    request = (
        pending.request
        if pending is not None
        else PermissionRequest.model_validate(request_data)
    )

    consumed_request = None
    if redis_client is not None and raw_request is not None:
        consumed_request = await _consume_redis_request(
            redis_client, request_id, raw_request
        )
        if consumed_request is None:
            raise KeyError("Permission request already replied")

    # Claim the in-process representation before the first persistence await.
    # Without this pop, two concurrent local Always clicks can both observe the
    # same PendingPermission and create duplicate durable grants.
    if pending is not None:
        _pending.pop(request_id, None)

    try:
        granted_rules = (
            await _record_always_grant(user_id, request)
            if action == "always"
            else []
        )
    except Exception:
        # Persistence failed before acknowledgement. Restore the distributed
        # request so the user may retry rather than silently degrading Always to
        # a one-shot approval.
        if redis_client is not None and consumed_request is not None:
            try:
                await redis_client.setex(
                    f"perm_req:{request_id}", 300, consumed_request
                )
            except Exception:
                pass
        if pending is not None:
            _pending[request_id] = pending
        raise

    reply_data = {
        "action": action,
        "message": message,
        "user_id": user_id,
        "session_id": request.session_id,
        "granted_rules": [rule.model_dump() for rule in granted_rules],
    }
    reply_payload = json.dumps(reply_data)

    durable_delivery = False
    published_delivery = False
    if redis_client is not None:
        try:
            await redis_client.setex(
                f"perm_resp:{request_id}", 300, reply_payload
            )
            durable_delivery = True
        except Exception as exc:
            log.warning(f"Failed to store permission reply in Redis: {exc}")

    if pending is not None:
        _apply_reply_data(pending, reply_data)
        _resolve_related_pending(pending)
        pending.event.set()

    if redis_client is not None:
        try:
            await redis_client.publish(f"perm_reply:{request_id}", reply_payload)
            published_delivery = True
        except Exception as exc:
            log.warning(f"Failed to publish permission reply to Redis: {exc}")

    if pending is None and redis_client is not None and not (
        durable_delivery or published_delivery
    ):
        if consumed_request is not None:
            try:
                await redis_client.setex(
                    f"perm_req:{request_id}", 300, consumed_request
                )
            except Exception:
                pass
        raise RuntimeError("Permission reply could not reach its waiting worker")

    # This global event removes the UI card and synchronizes warmed workers'
    # in-memory grant caches. Workers that were offline reload the durable rule.
    bus.publish(PERMISSION_REPLIED, {
        "userId": user_id,
        "id": request_id,
        "request_id": request_id,
        "session_id": request.session_id,
        "action": action,
        "granted_rules": reply_data["granted_rules"],
    })


def _sync_grants_from_bus(event: dict[str, Any]) -> None:
    """Keep already-warm worker caches coherent with durable Always replies."""
    data = event.get("data") or {}
    user_id = data.get("userId") or data.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return
    for rule in _rules_from_reply(data):
        _cache_rule(user_id, rule)


bus.subscribe(PERMISSION_REPLIED, _sync_grants_from_bus)


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
