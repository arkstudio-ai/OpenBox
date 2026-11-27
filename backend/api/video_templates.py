"""Professional author's studio and the catalogue used by both OpenBox clients."""
from fastapi import APIRouter, Depends, Request
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
    return await _result(studio.save(*_scope(user), body))


@router.post("/{edition_id}/publish")
async def publish(edition_id: str, body: Submission, request: Request, user=Depends(get_current_user)):
    published = await _result(studio.publish(*_scope(user), edition_id, body.sha256,
                                admin=user.get("role") == "admin", share_cast=body.share_cast_images))
    from audit import record
    await record(*_scope(user), "video_template.publish", "video_template", edition_id, request=request, detail={
        "edition_id": edition_id, "sha256": body.sha256, "listing": published["status"],
    })
    if published["status"] == "pending":
        from api.metadata import _notify_admins_of_submission
        await _notify_admins_of_submission({"id": published["catalog_id"].removeprefix("community:"),
                                           "name": published["title"], "published_version": published["template_ref"]["version"]}, user["user_id"])
    return published
