"""Versioned SQL observations, distinct from current authority and progress.

Each provider checkpoint retains the exact observation it consumed. Later
progress does not rewrite that history, but every referenced resource must
remain accessible. Current tool projections are refreshed before dispatch.
Legacy digest-only reads cannot be upgraded without their original bodies.
"""
from copy import deepcopy
import json

from sqlalchemy import select

from assistant.commands import _authority, _project, command_digest, task_locked
from assistant.policy import AssistantError
from assistant.reads import get_task, list_projects, list_sessions, list_tasks
from assistant.request_reads import get_request, list_requests
from assistant.assets import list_assets
from assistant.task_context import _scope, validate_task_snapshots
from assistant.transactions import begin_snapshot
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.session import Session
from memory.redaction import redact_credentials

OPERATIONS = {"projects.list": list_projects, "sessions.list": list_sessions,
              "tasks.get": get_task, "tasks.list": list_tasks,
              "requests.get": get_request, "requests.list": list_requests, "assets.list": list_assets}
VERSION = 2
MAX_OBSERVATION_BYTES = 60000


def _safe(value):
    if isinstance(value, str):
        return redact_credentials(value)
    if isinstance(value, list):
        return [_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _safe(item) for key, item in value.items()}
    return value


def _unverified():
    return AssistantError(410, "ASSISTANT_BUSINESS_UNVERIFIED", "The original business observation is unavailable")


async def _sources(db, main, operation, arguments, value):
    """Recompute scope from the exact returned identities, never today's list."""
    if operation in {"requests.list", "requests.get"}:
        from assistant.request_reads import sources
        return await sources(db, main, operation, arguments, value)
    if operation == "assets.list":
        from assistant.assets import sources
        return await sources(db, main, arguments, value)
    resources, tasks = [], []
    if operation == "sessions.list" and arguments.get("project_id"):
        project = await _project(db, arguments["project_id"], main.user_id, main.workspace_id)
        resources.append({"kind": "project_filter", "id": project.id})
    if operation == "tasks.get":
        items = [value.get("task")]
        if not isinstance(items[0], dict) or items[0].get("id") != arguments.get("task_id"):
            raise _unverified()
    else:
        items = value.get("items")
    if (not isinstance(items, list) or len(items) > 50 or any(not isinstance(item, dict) for item in items)
            or len({item.get("id") for item in items}) != len(items)):
        raise _unverified()
    for item in items:
        if operation == "projects.list":
            row = await _project(db, item.get("id"), main.user_id, main.workspace_id)
            scope = {key: getattr(row, key) for key in ("id", "user_id", "workspace_id", "name")}
        elif operation == "sessions.list":
            row = await db.scalar(select(Session).where(Session.id == item.get("id"),
                Session.user_id == main.user_id, Session.workspace_id == main.workspace_id,
                Session.kind == "normal", Session.is_deleted.is_(False)))
            if row is None or row.project_id != item.get("project_id"):
                raise _unverified()
            await _project(db, row.project_id, main.user_id, main.workspace_id)
            scope = {key: getattr(row, key) for key in (
                "id", "user_id", "workspace_id", "project_id", "kind", "visibility", "memory_policy", "title")}
        else:
            row, _ = await task_locked(db, user_id=main.user_id, workspace_id=main.workspace_id,
                main_id=main.id, task_id=item.get("id"))
            snapshot = value if operation == "tasks.get" else {"task": item}
            tasks.append({"task_id": row.id, "scope_digest": _scope(row), "snapshot": snapshot,
                          "snapshot_digest": command_digest(snapshot)})
            continue
        resources.append({"kind": operation.split(".")[0], "id": row.id, "scope_digest": command_digest(scope)})
    return {"resources": resources, "tasks": tasks}


async def capture_locked(db, main, operation, arguments):
    if operation not in OPERATIONS or not isinstance(arguments, dict):
        raise _unverified()
    value = await OPERATIONS[operation](db=db, user_id=main.user_id, workspace_id=main.workspace_id,
                                       main_id=main.id, **arguments)
    value = _safe(json.loads(json.dumps(value, default=str)))
    if len(json.dumps(value, ensure_ascii=False).encode()) > MAX_OBSERVATION_BYTES:
        raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "Narrow this business read before continuing")
    sources = await _sources(db, main, operation, arguments, value)
    return value, {"version": VERSION, "operation": operation, "arguments": arguments,
                   "digest": command_digest(value), "projection": value, "sources": sources}


async def capture(ctx, operation, arguments):
    async with get_db_session() as db:
        await begin_snapshot(db)
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        return await capture_locked(db, main, operation, arguments)


async def validate(db, main, snapshot, *, fresh=False):
    value = snapshot.get("projection")
    operation, arguments = snapshot.get("operation"), snapshot.get("arguments")
    if (snapshot.get("version") != VERSION or operation not in OPERATIONS or not isinstance(arguments, dict)
            or not isinstance(value, dict) or command_digest(value) != snapshot.get("digest")
            or len(json.dumps(value, ensure_ascii=False).encode()) > MAX_OBSERVATION_BYTES):
        raise _unverified()
    sources = await _sources(db, main, operation, arguments, value)
    if sources != snapshot.get("sources"):
        raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Business source scope changed")
    await validate_task_snapshots(db, main, sources["tasks"])
    if fresh:
        _, current = await capture_locked(db, main, operation, arguments)
        if current != snapshot:
            raise AssistantError(409, "ASSISTANT_BUSINESS_SNAPSHOT_CHANGED", "Business state changed before dispatch; refresh its observation")


async def record(ctx, operation, arguments):
    from assistant.reporting import _read_call
    from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write
    value, snapshot = await capture(ctx, operation, arguments)
    async with get_db_session() as db:
        main = await prepare_agent_event_write(db, session_id=ctx.session_id,
            user_id=ctx.user_id, run_fence=ctx.run_fence)
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await _read_call(db, main, ctx, operation)
        await validate(db, main, snapshot)
        prior = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == main.user_id, AgentEvent.part_id == ctx.part_id,
            AgentEvent.kind == "assistant.business.read"))
        if prior:
            if prior.payload.get("operation") != operation or prior.payload.get("arguments") != arguments:
                raise _unverified()
            snapshot = prior.payload.get("observation")
            if not isinstance(snapshot, dict):
                raise _unverified()
            await validate(db, main, snapshot)
            value = snapshot["projection"]
            event = prior
        else:
            event = await append_agent_event_locked(db, main, kind="assistant.business.read", payload={
                **{key: snapshot[key] for key in ("operation", "arguments", "digest")}, "observation": snapshot},
                run_fence=ctx.run_fence, message_id=ctx.message_id, part_id=ctx.part_id,
                idempotency_key=f"assistant-business-read:{ctx.part_id}")
        descriptor = {"version": VERSION, "operation": operation, "arguments": arguments,
            "session_id": main.id, "run_id": ctx.run_id, "generation": ctx.run_generation,
            "part_id": ctx.part_id, "observation_sequence": event.sequence,
            "observation_hash": command_digest(event.payload)}
    return deepcopy(value), descriptor


async def refresh(ctx, part, descriptor):
    """Only the original persisted call can select a refreshed read projection."""
    async with get_db_session() as db:
        await begin_snapshot(db)
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.user_id == main.user_id, AgentEvent.run_id == ctx.run_id,
            AgentEvent.generation == ctx.run_generation, AgentEvent.part_id == part.get("id"),
            AgentEvent.sequence == descriptor.get("observation_sequence"), AgentEvent.kind == "assistant.business.read"))
        observed = event.payload.get("observation") if event else None
        if (event is None or not isinstance(observed, dict) or descriptor.get("part_id") != part.get("id")
                or command_digest(event.payload) != descriptor.get("observation_hash")
                or any(observed.get(key) != descriptor.get(key) for key in ("version", "operation", "arguments"))):
            raise _unverified()
        await validate(db, main, observed)
        return await capture_locked(db, main, descriptor["operation"], descriptor["arguments"])
