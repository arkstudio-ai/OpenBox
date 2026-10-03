"""Thin actor-bound HTTP adapters for the durable personal assistant services."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.inbox import InboxAttachmentError, InboxIdempotencyConflict, schedule_inbox_wake
from assistant import commands, history, inputs, reads, reporting, retry, service, snapshot
from assistant.policy import AssistantError
from auth.middleware import get_current_user
from auth.workspace import get_workspace


class AssistantRoute(APIRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def handle(request):
            try:
                return await handler(request)
            except AssistantError as exc:
                raise HTTPException(exc.status, {"code": exc.code, "message": str(exc)}) from exc
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

    @field_validator("attachment_ids")
    @classmethod
    def unique_attachments(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("Attachment IDs must be unique")
        return value


class TurnBody(InputBody):
    client_id: Identity


class CreateTaskBody(Body):
    idempotency_key: Identity
    project_id: Identity
    title: str = Field(default="", max_length=128)
    input: InputBody


class TaskCommandBody(Body):
    idempotency_key: Identity
    action: Literal["input"]
    expected_revision: int = Field(ge=1, strict=True)
    input: InputBody


class RetryBody(Body):
    idempotency_key: Identity
    expected_report_attempt: int = Field(ge=1, strict=True)


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
            "variant_explicit": "variant" in body.model_fields_set}


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


@router.post("/turns", status_code=202)
async def turn(body: TurnBody, current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    receipt = await inputs.accept_turn(**scope, client_id=body.client_id, text=body.text,
        attachments=body.attachment_ids, model=body.model, variant=body.variant,
        variant_explicit="variant" in body.model_fields_set)
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
async def task_command(task_id: str, body: TaskCommandBody, current_user: dict = Depends(get_current_user)):
    scope = await _scope(current_user)
    receipt = await commands.accept_task_command(**scope, idempotency_key=body.idempotency_key,
        task_id=task_id, expected_revision=body.expected_revision, **_input(body.input))
    schedule_inbox_wake(receipt["execution_session_id"], scope["user_id"])
    return receipt


@router.get("/commands/{command_id}")
async def get_command(command_id: str, current_user: dict = Depends(get_current_user)):
    return await retry.read_command(**await _scope(current_user), command_id=command_id)


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
