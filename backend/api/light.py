"""Light mode: the home entries, starting a guided topic chat, recent results, the mode switch.

* ``GET  /api/light/entries`` → the buttons with availability computed for this workspace
* ``POST /api/light/start``   → a real session with the entry's opening card already in it
* ``GET  /api/light/recent``  → latest image/video results grouped by conversation
* ``GET/PUT /api/light/mode`` → ``ui_mode`` (light | pro)
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from light import mode as light_mode

router = APIRouter(prefix="/api/light", tags=["light"], dependencies=[Depends(get_workspace)])


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_id: str = Field(min_length=1, max_length=32)
    project_id: str | None = Field(default=None, max_length=64)
    subtype: str | None = Field(default=None, max_length=32)
    sample: str | None = Field(default=None, max_length=40)


class ModeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ui_mode: str = Field(min_length=1, max_length=8)


def _scope(current_user: dict) -> tuple[str, str]:
    workspace_id = current_user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(400, detail={"code": "WORKSPACE_REQUIRED", "message": "A workspace is required"})
    return current_user["user_id"], workspace_id


def _raise(exc: light_mode.LightError):
    raise HTTPException(exc.status, detail={"code": exc.code, "message": str(exc), **exc.extra})


@router.get("/entries")
async def entries(current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    return await light_mode.entries_view(user_id, workspace_id)


@router.post("/start")
async def start(body: StartBody, current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    try:
        return await light_mode.start(user_id=user_id, workspace_id=workspace_id, entry_id=body.entry_id,
                                      project_id=body.project_id, subtype=body.subtype, sample=body.sample)
    except light_mode.LightError as exc:
        _raise(exc)


@router.get("/recent")
async def recent(limit: int = Query(8, ge=1, le=30), current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    return await light_mode.recent(user_id, workspace_id, limit=limit)


@router.get("/mode")
async def get_mode(current_user: dict = Depends(get_current_user)):
    return {"ui_mode": await light_mode.ui_mode(current_user["user_id"])}


@router.put("/mode")
async def put_mode(body: ModeBody, current_user: dict = Depends(get_current_user)):
    try:
        return {"ui_mode": await light_mode.set_ui_mode(current_user["user_id"], body.ui_mode)}
    except light_mode.LightError as exc:
        _raise(exc)
