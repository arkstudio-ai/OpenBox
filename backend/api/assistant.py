"""Thin actor-bound HTTP adapters for the durable personal assistant services."""
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent.inbox import InboxAttachmentError, InboxIdempotencyConflict, schedule_inbox_wake
from assistant import commands, events, history, inputs, reads, reporting, retry, service, snapshot
from assistant.policy import AssistantError
from assistant.steering import ExpectedRun
from auth.middleware import get_current_user
from auth.workspace import get_workspace


class AssistantRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except AssistantError as exc:
                detail = {"code": exc.code, "message": str(exc)}
                if getattr(exc, "current_task", None) is not None:
                    detail["current_task"] = exc.current_task
                raise HTTPException(exc.status, detail) from exc
            except InboxIdempotencyConflict as exc:
                raise HTTPException(409, {"code": "ASSISTANT_INPUT_CONFLICT", "message": str(exc)}) from exc
            except (InboxAttachmentError, ValueError) as exc:
                raise HTTPException(400, {"code": "ASSISTANT_INVALID_INPUT", "message": str(exc)}) from exc
        return handle


router = APIRouter(prefix="/api/assistant", tags=["assistant"], route_class=AssistantRoute,
                   dependencies=[Depends(get_workspace)])
Identity = Annotated[str, Field(min_length=1, max_length=64)]


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EnsureBody(Body):
    model: str = Field(default="", max_length=128)
    variant: str | None = Field(default=None, max_length=32)


class InputBody(Body):
    text: str = Field(min_length=1, max_length=65536)
    attachment_ids: list[Identity] = Field(default_factory=list, max_length=32)
    delivery: Literal["followup"]
    model: str | None = Field(default=None, min_length=1, max_length=128)
    variant: str | None = Field(default=None, max_length=32)
    video_model: str | None = Field(default=None, max_length=160)
    video_resolution: str | None = Field(default=None, max_length=16)

    @field_validator("attachment_ids")
    @classmethod
    def unique_attachments(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Attachment IDs must be unique")
        return value


class TurnBody(InputBody):
    client_id: Identity
    assistant_session_id: Identity | None = None


class RequestDisplayBody(Body):
    display_token: str = Field(min_length=1, max_length=4096)


class CreateTaskBody(Body):
    idempotency_key: Identity
    project_id: Identity
    title: str = Field(default="", max_length=128)
    input: InputBody


class LinkTaskBody(Body):
    idempotency_key: Identity
    session_id: Identity
    expected_version: str = Field(pattern=r"^[0-9a-f]{64}$")


class AttachAssetsBody(Body):
    idempotency_key: Identity
    text: str = Field(min_length=1, max_length=8000)
    attachment_ids: list[Identity] = Field(min_length=1, max_length=20)
    expected_revision: int = Field(ge=1, strict=True)
    delivery: Literal["followup", "steer"] = "followup"
    expected_run: ExpectedRun | None = None


class TaskInputBody(InputBody):
    delivery: Literal["followup", "steer"]
    expected_run: ExpectedRun | None = None


class TaskCommandBody(Body):
    idempotency_key: Identity
    action: Literal["input", "pause", "resume", "cancel"]
    expected_revision: int = Field(ge=1, strict=True)
    input: TaskInputBody | None = None
    expected_run: ExpectedRun | None = None

    @model_validator(mode="after")
    def control_shape(self):
        if self.action == "input":
            if self.input is None or "expected_run" in self.model_fields_set:
                raise ValueError("Input commands require input; their run belongs inside input")
        elif "input" in self.model_fields_set:
            raise ValueError("Controls do not carry input")
        return self


class RetryBody(Body):
    idempotency_key: Identity
    expected_report_attempt: int = Field(ge=1, strict=True)


class ResourceCommandBody(Body):
    idempotency_key: Identity
    action: Literal["bind", "close"]
    expected_epoch: int = Field(ge=1, strict=True)


class ReadBody(Body):
    last_seen_sequence: int = Field(ge=1, strict=True)
    display_token: str = Field(min_length=1, max_length=4096)


def _actor(user):
    return {"user_id": user["user_id"], "workspace_id": user["workspace_id"]}


async def _scope(user):
    actor = _actor(user)
    main = await service.get_main_session(**actor)
    if main is None:
        raise AssistantError(404, "ASSISTANT_UNAVAILABLE", "Open the personal assistant first")
    return {**actor, "main_id": main.id}


def _input(body):
    return {"prompt": body.text, "attachments": body.attachment_ids, "model": body.model, "variant": body.variant,
            "variant_explicit": "variant" in body.model_fields_set,
            "video_model": body.video_model, "video_resolution": body.video_resolution}


@router.get("")
async def get_snapshot(current_user: dict = Depends(get_current_user),
                       task_cursor: str | None = Query(None, max_length=64),
                       before_sequence: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=50)):
    return await snapshot.get_snapshot(**_actor(current_user), task_cursor=task_cursor,
                                       before_sequence=before_sequence, limit=limit)


@router.post("/ensure")
async def ensure(body: EnsureBody, current_user: dict = Depends(get_current_user)):
    main = await service.ensure_main_session(**_actor(current_user), model=body.model, variant=body.variant)
    return {"session_id": main.id, "kind": main.kind, "agent": main.agent}


@router.get("/events")
async def get_events(current_user: dict = Depends(get_current_user),
                     after: str = Query(..., min_length=1, max_length=2048), limit: int = Query(100, ge=1, le=200)):
    return await events.read_events(**_actor(current_user), after=after, limit=limit)


@router.get("/messages")
async def revalidate_messages(session_id: Identity, message_ids: Annotated[list[Identity], Query(min_length=1, max_length=100)],
                              current_user: dict = Depends(get_current_user)):
    from assistant.public_history import public_messages
    from models.message import MessageWithParts
    main = await service.get_main_session(**_actor(current_user))
    if main is None or main.id != session_id:
        raise AssistantError(404, "ASSISTANT_UNAVAILABLE", "The private assistant is unavailable")
    selected = [MessageWithParts(id=key, session_id=main.id, role="assistant") for key in dict.fromkeys(message_ids)]
    return {"messages": await public_messages(main, selected, actor_user_id=current_user["user_id"])}


@router.post("/turns", status_code=202)
async def turn(body: TurnBody, current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    if body.assistant_session_id is not None and body.assistant_session_id != scope["main_id"]:
        raise AssistantError(409, "ASSISTANT_ENTRY_CHANGED", "The selected assistant entry changed; reload before sending")
    receipt = await inputs.accept_turn(**scope, client_id=body.client_id, text=body.text,
        attachments=body.attachment_ids, model=body.model, variant=body.variant,
        variant_explicit="variant" in body.model_fields_set,
        video_model=body.video_model, video_resolution=body.video_resolution)
    if receipt["state"] == "accepted":
        schedule_inbox_wake(scope["main_id"], scope["user_id"])
    return receipt


@router.get("/history")
async def get_history(current_user: dict = Depends(get_current_user),
                      session_id: str | None = Query(None, max_length=64),
                      message_ids: list[str] | None = Query(None, max_length=20),
                      cursor: str | None = Query(None, max_length=12000),
                      limit: int = Query(20, ge=1, le=50), max_chars: int = Query(8000, ge=1, le=16000)):
    scope = await _scope(current_user)
    return await history.read_history(**scope, session_id=session_id or scope["main_id"], message_ids=message_ids,
                                     cursor=cursor, limit=limit, max_chars=max_chars, record=False)


@router.get("/projects")
async def get_projects(current_user: dict = Depends(get_current_user), limit: int = Query(50, ge=1, le=50),
                       cursor: str | None = Query(None, max_length=64)):
    return await reads.list_projects(**await _scope(current_user), limit=limit, cursor=cursor)


@router.get("/tasks")
async def get_tasks(current_user: dict = Depends(get_current_user), limit: int = Query(50, ge=1, le=50),
                    cursor: str | None = Query(None, max_length=64), status: str | None = Query(None, max_length=24)):
    return await reads.list_tasks(**await _scope(current_user), limit=limit, cursor=cursor, status=status)


@router.get("/sessions")
async def get_sessions(current_user: dict = Depends(get_current_user), limit: int = Query(50, ge=1, le=50),
                       cursor: str | None = Query(None, max_length=64),
                       project_id: str | None = Query(None, max_length=64)):
    return await reads.list_sessions(**await _scope(current_user), limit=limit, cursor=cursor, project_id=project_id, include_link=True)


@router.post("/tasks/link")
async def link_task(body: LinkTaskBody, current_user: dict = Depends(get_current_user)):
    from assistant.linking import link_existing
    return await link_existing(**await _scope(current_user), **body.model_dump())


@router.get("/assets")
async def get_assets(current_user: dict = Depends(get_current_user), limit: int = Query(50, ge=1, le=50),
                     cursor: str | None = Query(None, max_length=64),
                     project_id: str | None = Query(None, max_length=64),
                     query: str = Query("", max_length=200), source: Literal["user", "agent"] | None = None):
    from assistant.assets import list_assets
    return await list_assets(**await _scope(current_user), limit=limit, cursor=cursor,
        project_id=project_id, query=query, source=source)


@router.post("/tasks/{task_id}/assets")
async def attach_task_assets(task_id: str, body: AttachAssetsBody, current_user: dict = Depends(get_current_user)):
    from assistant.assets import attach_assets
    receipt = await attach_assets(**await _scope(current_user), task_id=task_id, **body.model_dump())
    schedule_inbox_wake(receipt["execution_session_id"], current_user["user_id"])
    return receipt


@router.get("/schedules")
async def get_schedules(current_user: dict = Depends(get_current_user), limit: int = Query(50, ge=1, le=50),
                        cursor: str | None = Query(None, max_length=64),
                        project_id: str | None = Query(None, min_length=1, max_length=64),
                        query: str = Query("", max_length=200), enabled: bool | None = None):
    from assistant.schedules import list_schedules
    return await list_schedules(**await _scope(current_user), limit=limit, cursor=cursor,
        project_id=project_id, query=query, enabled=enabled)


@router.get("/requests")
async def get_requests(current_user: dict = Depends(get_current_user),
                       cursor: str | None = Query(None, max_length=64), limit: int = Query(20, ge=1, le=50),
                       kind: str = Query("question", pattern="^(question|permission)$")):
    if kind == "permission":
        from assistant.permission_requests import list_requests
        return await list_requests(**await _scope(current_user), cursor=cursor, limit=limit)
    from assistant.requests import list_requests
    return await list_requests(**await _scope(current_user), cursor=cursor, limit=limit)


@router.get("/requests/{kind}/{request_id}/review")
async def review_request(kind: Literal["question", "permission"], request_id: Identity,
                         current_user: dict = Depends(get_current_user)):
    from assistant.request_display import review
    return await review(**await _scope(current_user), kind=kind, request_id=request_id)


@router.post("/requests/displayed")
async def record_request_display(body: RequestDisplayBody, current_user: dict = Depends(get_current_user)):
    from assistant.request_display import displayed
    return await displayed(**await _scope(current_user), display_token=body.display_token)


@router.get("/tasks/{task_id}")
async def get_task(task_id: str, current_user: dict = Depends(get_current_user)):
    return await reads.get_task(**await _scope(current_user), task_id=task_id)


@router.post("/tasks", status_code=202)
async def create_task(body: CreateTaskBody, current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    receipt = await commands.accept_task_command(**scope, idempotency_key=body.idempotency_key,
        project_id=body.project_id, title=body.title, **_input(body.input))
    schedule_inbox_wake(receipt["execution_session_id"], scope["user_id"])
    return receipt


@router.post("/tasks/{task_id}/commands", status_code=202)
async def task_command(task_id: str, body: TaskCommandBody, background_tasks: BackgroundTasks,
                       current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    if body.action != "input":
        from assistant.control import accept_control_command, recover_controls
        receipt = await accept_control_command(**scope, task_id=task_id, action=body.action,
            idempotency_key=body.idempotency_key, expected_revision=body.expected_revision,
            expected_run=body.expected_run.model_dump() if body.expected_run else None)
        background_tasks.add_task(recover_controls, task_id=task_id)
        return receipt
    receipt = await commands.accept_task_command(**scope, idempotency_key=body.idempotency_key,
        task_id=task_id, expected_revision=body.expected_revision, delivery=body.input.delivery,
        expected_run=body.input.expected_run.model_dump() if body.input.expected_run else None, **_input(body.input))
    schedule_inbox_wake(receipt["execution_session_id"], scope["user_id"])
    return receipt


@router.get("/commands/{command_id}")
async def get_command(command_id: str, current_user: dict = Depends(get_current_user)):
    return await retry.read_command(**await _scope(current_user), command_id=command_id)


@router.get("/resources/{resource_id}")
async def get_resource(resource_id: str, current_user: dict = Depends(get_current_user)):
    from assistant.resource_commands import read_resource
    return await read_resource(**await _scope(current_user), resource_id=resource_id)


@router.post("/resources/{resource_id}/control", status_code=202)
async def resource_command(resource_id: str, body: ResourceCommandBody, background_tasks: BackgroundTasks,
                           current_user: dict = Depends(get_current_user)):
    from assistant.resource_commands import accept_resource_command, dispatch
    receipt = await accept_resource_command(**await _scope(current_user), resource_id=resource_id, **body.model_dump())
    background_tasks.add_task(dispatch, receipt["command_id"])
    return receipt


@router.get("/results/{result_id}")
async def get_result(result_id: str, current_user: dict = Depends(get_current_user),
                     view: Literal["summary", "full"] = Query("full"), offset: int = Query(0, ge=0),
                     max_chars: int = Query(8000, ge=1, le=16000), source_version: str | None = Query(None, max_length=64)):
    return await reporting.read_result_sources(**await _scope(current_user), result_id=result_id,
        offset=offset, max_chars=max_chars, source_version=source_version, record=False, summary=view == "summary")


@router.post("/results/{result_id}/retry", status_code=202)
async def retry_result(result_id: str, body: RetryBody, current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    receipt = await retry.retry_report(**scope, result_id=result_id, **body.model_dump())
    schedule_inbox_wake(scope["main_id"], scope["user_id"])
    return receipt


@router.post("/read-cursor")
async def read_cursor(body: ReadBody, current_user: dict = Depends(get_current_user)):
    return await snapshot.advance_read_cursor(**await _scope(current_user), **body.model_dump())
