"""Light mode: the home entries, starting a guided topic chat, recent results, the mode switch.

* ``GET  /api/light/entries`` → the buttons with availability computed for this workspace
* ``POST /api/light/start``   → a real session with the entry's opening card already in it
* ``GET  /api/light/recent``  → latest image/video results grouped by conversation
* ``GET/PUT /api/light/mode`` → ``ui_mode`` (light | pro)
* ``/api/light/autopilot``     → 省心托管: create from a result, list, pause/resume/change, cancel
"""
from decimal import Decimal
from typing import Literal

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


class AutopilotBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=96)
    asset_id: str | None = Field(default=None, max_length=96)
    task: Literal["video", "image", "listing", "reviews", "report"] | None = None
    repeat: Literal["daily", "weekdays", "weekly"] = "daily"
    time: str = Field(default="10:00", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    mode: Literal["make_only", "make_and_publish"] = "make_only"
    credits_cap: Decimal = Field(default=Decimal("10"), gt=0, le=Decimal("500"))
    note: str = Field(default="", max_length=200)


class AutopilotPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    repeat: Literal["daily", "weekdays", "weekly"] | None = None
    time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    credits_cap: Decimal | None = Field(default=None, gt=0, le=Decimal("500"))


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


@router.get("/autopilot")
async def autopilot_list(current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    return await autopilot.list_view(user_id, workspace_id)


@router.post("/autopilot")
async def autopilot_create(body: AutopilotBody, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        return await autopilot.create(user_id=user_id, workspace_id=workspace_id, session_id=body.session_id,
                                      asset_id=body.asset_id, task=body.task, repeat=body.repeat, time=body.time,
                                      mode=body.mode, cap=body.credits_cap, note=body.note)
    except light_mode.LightError as exc:
        _raise(exc)
    except ValueError as exc:  # cron quota / validation
        raise HTTPException(400, detail={"code": "LIGHT_AUTOPILOT_REFUSED", "message": str(exc)[:300]})


@router.patch("/autopilot/{job_id}")
async def autopilot_patch(job_id: str, body: AutopilotPatch, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        view = None
        if body.repeat or body.time or body.credits_cap:
            view = await autopilot.update(user_id, workspace_id, job_id, repeat=body.repeat, time=body.time,
                                          cap=body.credits_cap)
        if body.enabled is not None:
            view = await autopilot.set_enabled(user_id, workspace_id, job_id, body.enabled)
        return view or autopilot.view(await autopilot._job(user_id, workspace_id, job_id))
    except light_mode.LightError as exc:
        _raise(exc)


@router.delete("/autopilot/{job_id}")
async def autopilot_cancel(job_id: str, current_user: dict = Depends(get_current_user)):
    from light import autopilot
    user_id, workspace_id = _scope(current_user)
    try:
        return await autopilot.cancel(user_id, workspace_id, job_id)
    except light_mode.LightError as exc:
        _raise(exc)
