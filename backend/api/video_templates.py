"""Professional author's studio and the catalogue used by both OpenBox clients."""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

from api.light import _raise, _scope
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from light import video_templates as studio
from light.mode import LightError

router = APIRouter(prefix="/api/video-templates", tags=["video-templates"], dependencies=[Depends(get_workspace)])


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    share_finished_preview: Literal[True]
    share_cast_images: bool = False


async def _result(operation):
    try:
        return await operation
    except LightError as exc:
        _raise(exc)


@router.get("")
async def catalogue(user=Depends(get_current_user)):
    _scope(user)
    return {"items": await studio.catalogue()}


@router.get("/sources")
async def sources(user=Depends(get_current_user)):
    return {"items": await studio.sources(*_scope(user))}


@router.get("/sources/{asset_id}")
async def author_preview(asset_id: str, user=Depends(get_current_user)):
    return await _result(studio.author_preview(*_scope(user), asset_id))


@router.get("/mine")
async def mine(user=Depends(get_current_user)):
    return {"items": await studio.owned(*_scope(user))}


@router.post("/sources/{asset_id}/analyze")
async def analyze(asset_id: str, user=Depends(get_current_user)):
    return await _result(studio.analyze(*_scope(user), asset_id))


@router.post("/drafts", status_code=201)
async def save(body: studio.DraftInput, user=Depends(get_current_user)):
    _scope(user)
    raise HTTPException(410, detail={"code": "MEDIA_TEMPLATE_PRO_REQUIRED",
                                    "message": "请从专业版对话把成品做成模板，再完成精简版试做。"})


@router.post("/{edition_id}/publish")
async def publish(edition_id: str, body: Submission, request: Request, user=Depends(get_current_user)):
    _scope(user)
    raise HTTPException(410, detail={"code": "MEDIA_TEMPLATE_TRIAL_REQUIRED",
                                    "message": "请从专业版对话制作新版本，并完成精简版试做后提交。"})
