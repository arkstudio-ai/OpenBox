"""The first video a new user gets without talking to the agent (docs/LIGHT_MODE_PLAN.md §3).

Three calls, all workspace-scoped like the rest of ``/api``:

* ``GET  /api/first-video``            → where this workspace's first video stands
* ``POST /api/first-video``            → the form: industry, shop name, headline, template, photo
* ``GET  /api/first-video/templates``  → the options the form shows

The flow runs once per workspace; a second POST returns the first job.
"""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from light import first_video
from light.templates import first_video_templates

router = APIRouter(prefix="/api/first-video", tags=["first-video"], dependencies=[Depends(get_workspace)])

Industry = Literal["food", "beauty", "retail", "other"]
Template = Literal["signature", "ambience", "promo"]


class SubmitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    industry: Industry = "other"
    shop_name: str | None = Field(default=None, max_length=40)
    headline: str | None = Field(default=None, max_length=40)
    template: Template
    asset_id: str | None = Field(default=None, max_length=128)


def _scope(current_user: dict) -> tuple[str, str]:
    workspace_id = current_user.get("workspace_id")
    if not workspace_id:
        raise HTTPException(400, detail={"code": "WORKSPACE_REQUIRED", "message": "A workspace is required"})
    return current_user["user_id"], workspace_id


@router.get("")
async def get_state(current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    return await first_video.state(user_id, workspace_id)


@router.get("/templates")
async def get_templates(current_user: dict = Depends(get_current_user)):
    return first_video_templates().public()


@router.post("")
async def submit(body: SubmitBody, current_user: dict = Depends(get_current_user)):
    user_id, workspace_id = _scope(current_user)
    try:
        return await first_video.submit(
            user_id=user_id, workspace_id=workspace_id, industry=body.industry,
            shop_name=body.shop_name, headline=body.headline, template=body.template,
            asset_id=body.asset_id,
        )
    except first_video.FirstVideoError as exc:
        raise HTTPException(400, detail={"code": exc.code, "message": str(exc)})
