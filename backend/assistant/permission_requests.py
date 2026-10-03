"""Durable approval decisions for assistant-linked execution calls.

The existing AgentEvent is the immutable request; AssistantCommand owns the
decision/outbox. Redis carries only wake hints. Only the original waiting call
can apply a decision, under its current SQL run fence, with any Always rules in
the same transaction as the applied receipt. Recovery never executes a tool.
"""
from datetime import datetime, timedelta
from hashlib import sha256

from sqlalchemy import String, cast, func, select

from assistant.commands import _authority, command_digest, task_locked
from assistant.policy import AssistantError, lock_actor
from assistant.requests import finish_decision
from assistant.transactions import begin_snapshot
from core.identifier import generate_id
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from db.models.permission import PermissionRule
from db.models.project import Project
from db.models.question import SessionExecution
from db.models.user import User
from question import runtime
from session.agent_event_log import append_agent_event_locked, ensure_surface_seed_locked
from session.internal_parts import begin_session_write

ASKED = "assistant.permission.asked"
CLOSED = "assistant.permission.closed"
REQUEST_TTL_SECONDS = 300
log = create_logger("assistant.permission_requests")


def json_text(db, column, key):
    return (func.jsonb_extract_path_text(column, key) if db.get_bind().dialect.name == "postgresql"
            else cast(func.json_extract(column, f"$.{key}"), String))


def gone(reason="expired"):
    return AssistantError(410, "PERMISSION_GONE", f"The original permission request is {reason}")


async def event_for(db, request_id, user_id):
    event = await db.get(AgentEvent, request_id)
    if event is None or event.kind != ASKED:
        return None
    if event.user_id != user_id:
        raise AssistantError(404, "PERMISSION_UNAVAILABLE", "Permission request is unavailable")
    return event


def request_for(event):
    from permission.permission import PermissionRequest
    payload = event.payload
    binding = {**payload["binding"], "kind": "permission", "run_id": event.run_id,
        "generation": event.generation, "request_revision": command_digest({
            "id": event.id, "session_id": event.session_id, "actor": event.user_id,
            "run_id": event.run_id, "generation": event.generation, "payload": payload})}
    return PermissionRequest(id=event.id, **payload["request"], assistant=binding)


async def scope_for(db, event, *, lock=False):
    binding = event.payload["binding"]
    await _authority(db, user_id=event.user_id, workspace_id=binding["workspace_id"],
        main_id=binding["assistant_session_id"])
    task, session = await task_locked(db, user_id=event.user_id, workspace_id=binding["workspace_id"],
        main_id=binding["assistant_session_id"], task_id=binding["task_id"], lock=lock)
    if task.execution_session_id != event.session_id or task.project_id != binding["project_id"]:
        raise AssistantError(404, "PERMISSION_UNAVAILABLE", "Permission request is unavailable")
    return task, session


async def decision_for(db, request_id):
    return await db.scalar(select(AssistantCommand).where(AssistantCommand.action == "request_reply",
        AssistantCommand.target_type == "permission", AssistantCommand.target_id == request_id))


async def closed_for(db, event):
    return await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == event.session_id,
        AgentEvent.kind == CLOSED, AgentEvent.run_id == event.run_id, AgentEvent.generation == event.generation,
        json_text(db, AgentEvent.payload, "request_id") == event.id).limit(1))


async def fresh(db, event, task):
    request = request_for(event)
    if runtime.utc(datetime.fromisoformat(request.expires_at)) <= runtime.now() or await closed_for(db, event):
        raise gone()
    if not await db.scalar(select(User.id).where(User.id == event.user_id,
            User.is_active.is_(True), User.is_deleted.is_(False))):
        raise AssistantError(403, "ASSISTANT_ACTOR_UNAVAILABLE", "Actor is unavailable")
    driver = await db.get(AgentDriverState, event.session_id)
    execution = await db.get(SessionExecution, event.session_id)
    if (driver is None or driver.user_id != event.user_id or driver.run_id != event.run_id
            or driver.generation != event.generation or driver.phase not in {"running", "reserved"}
            or driver.abort_requested_at is not None or not driver.lease_expires_at
            or runtime.utc(driver.lease_expires_at) <= runtime.now()
            or execution is None or execution.run_id != event.run_id
            or execution.generation != event.payload["binding"]["turn_generation"]
            or not runtime.is_live(execution) or task.desired_state != "running"):
        raise gone("superseded")
    part = await db.get(Part, event.part_id)
    if (part is None or part.user_id != event.user_id or part.session_id != event.session_id
            or part.message_id != event.message_id or part.type != "tool"
            or part.data.get("status") not in {"pending", "running"}):
        raise gone("no longer waiting")
    return request


async def changed(db, session, event, stage):
    await append_agent_event_locked(db, session, kind="assistant.request.changed",
        payload={"task_id": event.payload["binding"]["task_id"], "request_id": event.id,
                 "request_kind": "permission"}, idempotency_key=f"permission-change:{event.id}:{stage}")


async def register(request):
    """Freeze a linked request before exposing either its card or Redis key."""
    from permission.permission import _use_db
    if not _use_db():
        return request
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == request.session_id))
    if task is None:
        return request
    from agent.hooks import current_tool_context
    ctx, ticket = current_tool_context(), runtime.current_run.get()
    part_id = (getattr(ctx, "_trajectory_requested_part", None) or getattr(ctx, "part_id", None)) if ctx else None
    if (ticket is None or ticket.session_id != request.session_id or ticket.user_id != request.user_id
            or ctx is None or not part_id):
        raise AssistantError(409, "PERMISSION_RUN_REQUIRED", "A current tool call must request permission")
    async with runtime.transaction(request.session_id, request.user_id) as (db, session, execution):
        await _authority(db, user_id=request.user_id, workspace_id=task.workspace_id, main_id=task.assistant_session_id)
        task, session = await task_locked(db, user_id=request.user_id, workspace_id=task.workspace_id,
            main_id=task.assistant_session_id, task_id=task.id, lock=True)
        from agent.driver import assert_run_fence_locked
        driver = await db.get(AgentDriverState, request.session_id)
        if driver is None or driver.run_id != ticket.run_id:
            raise gone("superseded")
        await assert_run_fence_locked(db, session_id=session.id, user_id=request.user_id,
            run_id=driver.run_id, generation=driver.generation)
        from assistant.scheduling import require_runnable_locked
        await require_runnable_locked(db, session)
        request.expires_at = (runtime.now() + timedelta(seconds=REQUEST_TTL_SECONDS)).isoformat()
        body = request.model_dump(exclude={"id", "assistant"})
        binding = {"task_id": task.id, "assistant_session_id": task.assistant_session_id,
            "workspace_id": task.workspace_id, "project_id": task.project_id,
            "turn_generation": ticket.generation, "options_hash": command_digest({
                "tool": request.tool, "input": request.input, "patterns": request.patterns,
                "always": request.always, "always_scope": "user", "metadata": request.metadata})}
        await ensure_surface_seed_locked(db, session)
        event = await append_agent_event_locked(db, session, kind=ASKED,
            payload={"request": body, "binding": binding},
            run_fence=(session.id, driver.run_id, driver.generation),
            message_id=ctx.message_id, part_id=part_id)
        await fresh(db, event, task)
        await changed(db, session, event, "asked")
        return request_for(event)


async def maybe_reply(request_id, action, message, user_id, *, reply_id=None,
                      expected_request_revision=None, options_hash=None, source_ref=None):
    from permission.permission import _use_db
    if not _use_db():
        return None
    async with get_db_session() as db:
        event = await event_for(db, request_id, user_id)
    if event is None:
        return None
    if not reply_id or not expected_request_revision or not options_hash:
        raise AssistantError(409, "ASSISTANT_REQUEST_VERSION_REQUIRED", "Reload the original permission card")
    if (not isinstance(reply_id, str) or len(reply_id) > 64 or not isinstance(options_hash, str)
            or len(options_hash) != 64 or not isinstance(expected_request_revision, str)
            or len(expected_request_revision) != 64 or message is not None and len(message) > 4000):
        raise ValueError("Invalid permission reply")
    if source_ref != {"kind": "card"}:
        raise AssistantError(403, "ASSISTANT_REPLY_SOURCE_REQUIRED", "An authenticated card reply is required")
    digest = command_digest({"request_id": request_id, "action": action, "message": message,
        "request_revision": expected_request_revision, "options_hash": options_hash, "source_ref": source_ref})
    async with get_db_session() as db:
        await begin_session_write(db)
        await lock_actor(db, user_id)
        task, session = await scope_for(db, event, lock=True)
        command = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == task.workspace_id, AssistantCommand.assistant_session_id == task.assistant_session_id,
            AssistantCommand.idempotency_key == reply_id))
        if command is not None:
            if (command.payload_digest != digest or command.action != "request_reply"
                    or command.target_type != "permission" or command.target_id != request_id):
                raise AssistantError(409, "PERMISSION_CONFLICT", "Reply ID was used for different input")
            return dict(command.receipt)
        if await decision_for(db, request_id):
            raise AssistantError(409, "PERMISSION_CONFLICT", "Another decision was already accepted")
        request = await fresh(db, event, task)
        if (expected_request_revision != request.assistant["request_revision"]
                or options_hash != request.assistant["options_hash"]):
            raise gone("changed")
        stamp, command_id = runtime.now(), generate_id()
        receipt = {"ok": True, "command_id": command_id, "reply_id": reply_id, "request_id": request_id,
            "request_kind": "permission", "task_id": task.id, "session_id": session.id,
            "assistant_session_id": task.assistant_session_id, "request_revision": expected_request_revision,
            "options_hash": options_hash, "action": action, "state": "accepted", "accepted_at": stamp.isoformat()}
        db.add(AssistantCommand(id=command_id, actor_user_id=user_id, workspace_id=task.workspace_id,
            assistant_session_id=task.assistant_session_id, idempotency_key=reply_id, action="request_reply",
            target_type="permission", target_id=request_id, payload_digest=digest, state="accepted", receipt=receipt,
            source_ref={"kind": "human_card", "actor_user_id": user_id, "request_id": request_id,
                "request_revision": expected_request_revision, "options_hash": options_hash,
                "decision": {"action": action, "message": message}}, created_at=stamp, updated_at=stamp))
        await changed(db, session, event, "accepted")
    # Committed decisions survive a crash or failure before this wake hint.
    await stage_delivery(request_id, user_id)
    async with get_db_session() as db:
        return dict((await decision_for(db, request_id)).receipt)


async def close_locked(db, session, event, reason):
    if not await closed_for(db, event):
        await append_agent_event_locked(db, session, kind=CLOSED,
            payload={"request_id": event.id, "reason": reason},
            run_fence=(event.session_id, event.run_id, event.generation),
            idempotency_key=f"permission-closed:{event.id}")
        await changed(db, session, event, "closed")
    command = await decision_for(db, event.id)
    if command is not None and command.state != "applied":
        finish_decision(command, "failed", error_code="PERMISSION_GONE")
        command.receipt = {**command.receipt, "retryable": False}


async def close(request, reason="wait_ended"):
    async with runtime.transaction(request.session_id, request.user_id, fence=False) as (db, session, _):
        event = await event_for(db, request.id, request.user_id)
        if event is not None:
            await close_locked(db, session, event, reason)


async def stage_delivery(request_id, user_id):
    """Recoverable application intent; Redis is never an authorization source."""
    async with get_db_session() as db:
        event = await event_for(db, request_id, user_id)
    if event is None:
        raise LookupError("The original permission request is unavailable")
    async with runtime.transaction(event.session_id, user_id, fence=False) as (db, session, _):
        command = await decision_for(db, request_id)
        if command is None or command.state == "applied" or command.state == "failed" and not command.receipt.get("retryable"):
            return
        try:
            task, _ = await scope_for(db, event)
            await fresh(db, event, task)
        except AssistantError:
            await close_locked(db, session, event, "unavailable")
            return
        command.state, command.updated_at = "applying", runtime.now()
        command.receipt = {**command.receipt, "state": "applying", "retryable": True}
        await changed(db, session, event, "applying")
        reply_id = command.idempotency_key
    from permission.permission import _get_redis_client, _pending
    pending = _pending.get(request_id)
    if pending is not None:
        pending.event.set()
    redis = _get_redis_client()
    if redis is not None:
        import json
        try:
            hint = json.dumps({"request_id": request_id, "reply_id": reply_id})
            await redis.setex(f"perm_reply:{request_id}", 86400, hint)
            await redis.publish(f"perm_reply:{request_id}", hint)
        except Exception:
            # The original waiter polls SQL even when all Redis state is lost.
            pass


async def consume(request):
    """Called only by the exact suspended authorization, never by an API worker."""
    ticket = runtime.current_run.get()
    if (ticket is None or ticket.session_id != request.session_id or ticket.user_id != request.user_id
            or ticket.run_id != request.assistant["run_id"]
            or ticket.generation != request.assistant["turn_generation"]):
        raise gone("superseded")
    async with runtime.transaction(request.session_id, request.user_id, fence=False) as (db, session, _):
        event = await event_for(db, request.id, request.user_id)
        if event is None:
            raise gone("unavailable")
        task, _ = await scope_for(db, event)
        current = await fresh(db, event, task)
        if current.model_dump() != request.model_dump():
            raise gone("changed")
        command = await decision_for(db, request.id)
        if command is None:
            return None
        if command.state == "failed" and not command.receipt.get("retryable"):
            raise gone()
        source = command.source_ref
        if (command.actor_user_id != request.user_id or command.workspace_id != task.workspace_id
                or command.assistant_session_id != task.assistant_session_id or source.get("kind") != "human_card"
                or source.get("actor_user_id") != request.user_id or source.get("request_id") != request.id
                or source.get("request_revision") != current.assistant["request_revision"]
                or source.get("options_hash") != current.assistant["options_hash"]):
            raise gone("unverified")
        decision = source["decision"]
        action, message = decision["action"], decision["message"]
        digest = command_digest({"request_id": request.id, "action": action, "message": message,
            "request_revision": current.assistant["request_revision"], "options_hash": current.assistant["options_hash"],
            "source_ref": {"kind": "card"}})
        if (action not in {"once", "always", "reject"} or command.payload_digest != digest
                or command.receipt.get("action") != action or command.receipt.get("reply_id") != command.idempotency_key):
            raise gone("unverified")
        rules, rule_ids = [], []
        if action == "always":
            for pattern in dict.fromkeys(request.always or request.patterns):
                rule_id = sha256(f"permission-grant:{command.id}:{request.tool}:{pattern}".encode()).hexdigest()
                rule = await db.get(PermissionRule, rule_id)
                if rule is None:
                    if command.state == "applied":
                        raise gone("revoked")
                    rule = PermissionRule(id=rule_id, user_id=request.user_id, project_id=None,
                        permission=request.tool, pattern=pattern, action="allow", created_at=runtime.now())
                    db.add(rule)
                if (rule.user_id != request.user_id or rule.project_id is not None or rule.permission != request.tool
                        or rule.pattern != pattern or rule.action != "allow"):
                    raise gone("unverified")
                rule_ids.append(rule_id)
                rules.append({"permission": rule.permission, "pattern": rule.pattern, "action": "allow"})
        if command.state != "applied":
            command.receipt = {key: value for key, value in command.receipt.items() if key != "error_code"}
            finish_decision(command, "applied")
            command.receipt = {**command.receipt, "rule_ids": rule_ids, "retryable": False}
            await changed(db, session, event, "applied")
        return {"action": action, "message": message, "user_id": request.user_id,
            "session_id": request.session_id, "request_id": request.id, "reply_id": command.idempotency_key,
            "command_id": command.id, "granted_rules": rules}


async def application_failed(request):
    async with runtime.transaction(request.session_id, request.user_id, fence=False) as (db, session, _):
        command = await decision_for(db, request.id)
        if command and command.state in {"accepted", "applying"}:
            finish_decision(command, "failed", error_code="PERMISSION_APPLY_RETRY")
            command.receipt = {**command.receipt, "retryable": True}
            await changed(db, session, await event_for(db, request.id, request.user_id), "apply_retry")


async def recover_decisions(limit=100):
    async with get_db_session() as db:
        commands = (await db.scalars(select(AssistantCommand).where(AssistantCommand.target_type == "permission",
            AssistantCommand.action == "request_reply", AssistantCommand.state.in_(("accepted", "applying", "failed")),
            (AssistantCommand.state != "failed") | json_text(db, AssistantCommand.receipt, "retryable").in_(("true", "1")))
            .order_by(AssistantCommand.updated_at, AssistantCommand.id).limit(limit))).all()
    for command in commands:
        try:
            await stage_delivery(command.target_id, command.actor_user_id)
        except (LookupError, AssistantError):
            async with get_db_session() as db:
                await begin_session_write(db)
                row = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command.id).with_for_update())
                if row is not None and row.state != "applied":
                    finish_decision(row, "failed", error_code="PERMISSION_GONE")
                    row.receipt = {**row.receipt, "retryable": False}
        except Exception as error:
            # One damaged request must not prevent independent decisions from
            # recovering. Rotate its retry position without granting authority.
            log.warning("Permission delivery deferred: %s", type(error).__name__)
            async with get_db_session() as db:
                await begin_session_write(db)
                row = await db.scalar(select(AssistantCommand).where(AssistantCommand.id == command.id).with_for_update())
                if row is not None and row.state != "applied":
                    row.updated_at = runtime.now()
    return len(commands)


async def list_requests(*, user_id, workspace_id, main_id, cursor=None, limit=20):
    if not 1 <= limit <= 50:
        raise ValueError("Invalid page size")
    async with get_db_session() as db:
        await begin_snapshot(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        query = select(AgentEvent).join(AssistantTask, AssistantTask.execution_session_id == AgentEvent.session_id).join(
            AgentDriverState, AgentDriverState.session_id == AgentEvent.session_id).where(
            AgentEvent.kind == ASKED, AgentEvent.user_id == user_id, AgentEvent.id > (cursor or ""),
            AssistantTask.user_id == user_id, AssistantTask.workspace_id == workspace_id,
            AssistantTask.assistant_session_id == main_id, AgentEvent.run_id == AgentDriverState.run_id,
            AgentEvent.generation == AgentDriverState.generation,
            ~select(AssistantCommand.id).where(AssistantCommand.target_type == "permission",
                AssistantCommand.target_id == AgentEvent.id, AssistantCommand.action == "request_reply").exists())
        rows = list((await db.scalars(query.order_by(AgentEvent.id).limit(limit + 1))).all())
        items = []
        for event in rows[:limit]:
            try:
                task, _ = await scope_for(db, event)
                request = await fresh(db, event, task)
            except AssistantError:
                continue
            if await decision_for(db, event.id) is None:
                project = await db.get(Project, task.project_id)
                items.append({**request.model_dump(), "task_title": task.title, "project_name": project.name})
        commands = (await db.scalars(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
            AssistantCommand.workspace_id == workspace_id, AssistantCommand.assistant_session_id == main_id,
            AssistantCommand.target_type == "permission", AssistantCommand.action == "request_reply")
            .order_by(AssistantCommand.created_at.desc()).limit(10))).all()
        receipts = []
        for command in commands:
            event = await event_for(db, command.target_id, user_id)
            try:
                if event:
                    await scope_for(db, event)
                    receipts.append(dict(command.receipt))
            except AssistantError:
                continue
        return {"items": items, "receipts": receipts,
            "next_cursor": rows[limit - 1].id if len(rows) > limit else None}


async def pending_for_user(user_id):
    """Original execution page reads the same SQL requests across API workers."""
    from permission.permission import _use_db
    if not _use_db():
        return []
    async with get_db_session() as db:
        mains = (await db.execute(select(AssistantTask.workspace_id, AssistantTask.assistant_session_id)
            .where(AssistantTask.user_id == user_id).distinct())).all()
    items = []
    for workspace_id, main_id in mains:
        try:
            page = await list_requests(user_id=user_id, workspace_id=workspace_id, main_id=main_id, limit=50)
            items.extend(page["items"])
        except AssistantError:
            continue
    return items


async def legacy_pending_for_user(user_id):
    from permission.permission import _use_db, list_pending
    pending = [request for request in list_pending(user_id) if not request.assistant]
    if not pending or not _use_db():
        return pending
    async with get_db_session() as db:
        linked = set((await db.scalars(select(AssistantTask.execution_session_id).where(
            AssistantTask.execution_session_id.in_([r.session_id for r in pending])))).all())
    return [request for request in pending if request.session_id not in linked]
