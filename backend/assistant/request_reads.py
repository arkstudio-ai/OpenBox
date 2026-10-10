"""Current pending requests and reauthorizable historical observations."""
from copy import deepcopy

from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError
from assistant.transactions import begin_snapshot, read_session
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution


async def get_request(*, user_id, workspace_id, main_id, kind, request_id, db=None):
    from assistant import requests as questions, permission_requests as permissions
    from question import question
    owns_snapshot = db is None
    async with read_session(db) as db:
        if owns_snapshot:
            await begin_snapshot(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if kind == "question":
            row = await db.get(QuestionCheckpoint, request_id)
            if row is None or row.user_id != user_id:
                raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
            try:
                linked = await questions.validate_question_read(db, row)
            except question.QuestionGone:
                raise AssistantError(410, "ASSISTANT_REQUEST_UNAVAILABLE", "The original request is unavailable") from None
            if linked is None:
                raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is not linked to this assistant")
            task, _ = linked
            binding = row.continuation["assistant_request"]
            if binding["request_revision"] != questions._revision(row, binding):
                raise AssistantError(410, "ASSISTANT_REQUEST_CHANGED", "The original request changed")
            request = question._request(row)
            # Pydantic question items retain their original options and detail.
            body = {"questions": request.model_dump()["questions"]}
            command = await questions.decision_for(db, row.id)
            state = row.status
            if state == "pending":
                try:
                    execution = await db.get(SessionExecution, row.session_id)
                    await questions._fresh(db, row, execution, task)
                    question._check_pending(row, execution)
                except (AssistantError, question.QuestionGone):
                    state = "unavailable"
        elif kind == "permission":
            row = await permissions.event_for(db, request_id, user_id)
            if row is None:
                raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
            task, _ = await permissions.scope_for(db, row)
            request = permissions.request_for(row)
            binding = request.assistant
            body = request.model_dump(include={"tool", "input", "patterns", "always", "metadata", "is_doom_loop"})
            command = await permissions.decision_for(db, row.id)
            state = "pending"
            try:
                await permissions.fresh(db, row, task)
            except AssistantError:
                state = "unavailable"
        else:
            raise ValueError("Unknown request kind")
        if task.workspace_id != workspace_id or task.assistant_session_id != main_id:
            raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Request is unavailable")
        project = await db.get(Project, task.project_id)
        return {"kind": kind, "id": request.id, "task_id": task.id,
            "task_title": task.title, "project_id": task.project_id, "project_name": project.name,
            "session_id": task.execution_session_id, "assistant": deepcopy(binding),
            "expires_at": request.expires_at, "request": body,
            "state": command.state if command else state,
            "receipt": deepcopy(command.receipt) if command else None}


async def list_requests(*, user_id, workspace_id, main_id, kind, cursor=None, limit=20, db=None):
    from assistant import requests as questions, permission_requests as permissions
    service = {"question": questions, "permission": permissions}.get(kind)
    if service is None:
        raise ValueError("Unknown request kind")
    owns_snapshot = db is None
    async with read_session(db) as db:
        if owns_snapshot:
            await begin_snapshot(db)
        page = await service.list_requests(user_id=user_id, workspace_id=workspace_id,
            main_id=main_id, cursor=cursor, limit=limit, db=db)
        items = [await get_request(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
            kind=kind, request_id=item["id"], db=db) for item in page["items"]]
        return {"items": items, "next_cursor": page["next_cursor"]}


def original(value):
    """State and receipts advance; the original request/scope never does."""
    return {key: value[key] for key in ("kind", "id", "task_id", "task_title", "project_id",
        "project_name", "session_id", "assistant", "expires_at", "request")}


async def sources(db, main, operation, arguments, value):
    from assistant.business_context import _safe
    items = value.get("items") if operation == "requests.list" else [value]
    if (not isinstance(items, list) or len(items) > 50 or any(not isinstance(item, dict) for item in items)
            or len({(item.get("kind"), item.get("id")) for item in items}) != len(items)):
        raise AssistantError(410, "ASSISTANT_REQUEST_UNVERIFIED", "Request observation is incomplete")
    if operation == "requests.get" and (value.get("id") != arguments.get("request_id")
            or value.get("kind") != arguments.get("kind")):
        raise AssistantError(410, "ASSISTANT_REQUEST_UNVERIFIED", "Request observation changed")
    result = []
    for item in items:
        current = _safe(await get_request(db=db, user_id=main.user_id, workspace_id=main.workspace_id,
            main_id=main.id, kind=item.get("kind"), request_id=item.get("id")))
        if original(current) != original(item):
            raise AssistantError(410, "ASSISTANT_REQUEST_CHANGED", "The original request or its scope changed")
        result.append({"kind": "request", "request_kind": item["kind"], "id": item["id"],
            "digest": command_digest(original(item))})
    return {"resources": result, "tasks": []}
