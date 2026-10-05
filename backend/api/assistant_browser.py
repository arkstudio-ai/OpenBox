"""Authenticated finite browser controls; never a raw proxy or CDP endpoint."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
import httpx

from api.assistant import AssistantRoute, _scope
from assistant import browser_resources as service
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from sandbox.browser_resource_client import BrowserResourceError


router = APIRouter(prefix="/api/assistant/browser-resources", tags=["assistant"],
    route_class=AssistantRoute, dependencies=[Depends(get_workspace)])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Fence(Body):
    resource_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: int = Field(ge=1, strict=True)
    owner_kind: Literal["human"]
    owner_id: str = Field(min_length=1, max_length=64)


class ControlBody(Body):
    action: Literal["takeover", "giveback", "close"]
    expected_epoch: int = Field(ge=1, strict=True)
    idempotency_key: str = Field(min_length=1, max_length=64)


class HumanBody(Body):
    fence: Fence
    human_token: str = Field(min_length=1, max_length=128)


class OperationBody(HumanBody):
    operation_id: str = Field(pattern=r"^[A-Za-z0-9_:-]{1,64}$")
    kind: Literal["capture", "navigate", "back", "reload", "mouse", "key", "text", "wheel"]
    args: dict = Field(default_factory=dict)


class HeartbeatBody(HumanBody):
    command_id: str = Field(pattern=r"^[A-Za-z0-9_:-]{1,64}$")


async def _remote(operation):
    try:
        return await operation
    except BrowserResourceError as exc:
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.detail}) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(503, {"code": "BROWSER_TRANSPORT_UNCONFIRMED",
            "message": "尚未确认浏览器的操作结果，请使用原交接命令继续核对。"}) from exc


@router.get("/current")
async def current(user=Depends(get_current_user)):
    return await _remote(service.current_browser(**await _scope(user)))


@router.post("/ensure")
async def ensure(user=Depends(get_current_user)):
    return await _remote(service.ensure_browser(**await _scope(user)))


@router.get("/{resource_id}")
async def read(resource_id: str, user=Depends(get_current_user)):
    return await _remote(service.read_browser(**await _scope(user), resource_id=resource_id))


@router.post("/{resource_id}/control")
async def control(resource_id: str, body: ControlBody, user=Depends(get_current_user)):
    command_id = await service.accept_control(**await _scope(user), resource_id=resource_id, **body.model_dump())
    return await _remote(service.dispatch_control(command_id))


@router.post("/{resource_id}/operations")
async def operation(resource_id: str, body: OperationBody, user=Depends(get_current_user)):
    return await _remote(service.human_operation(**await _scope(user), resource_id=resource_id, **body.model_dump()))


@router.post("/{resource_id}/heartbeat")
async def heartbeat(resource_id: str, body: HeartbeatBody, user=Depends(get_current_user)):
    return await _remote(service.heartbeat(**await _scope(user), resource_id=resource_id, **body.model_dump()))
