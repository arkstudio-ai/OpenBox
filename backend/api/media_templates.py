"""The shared app catalogue, owner-scoped trials and the administrator's review queue."""
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api.light import _raise, _scope
from auth.middleware import get_current_user
from auth.workspace import get_workspace
from light import media_templates as library, taxonomy
from light.mode import LightError
from light.production_form import EntryId
from notifications.testing import live_admin

BASE = "/api/media-templates"
router = APIRouter(tags=["media-templates"])
author = APIRouter(prefix=BASE, dependencies=[Depends(get_workspace)])
admin = APIRouter(prefix=BASE + "/admin", dependencies=[Depends(live_admin)])


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    output_asset_id: str = Field(min_length=1, max_length=64)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=64)
    scene: str = Field(min_length=1, max_length=32)
    share_finished_preview: Literal[True]


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["listed", "rejected", "withdrawn"]
    reason: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def require_reason(self):
        if self.decision != "listed" and not self.reason:
            raise ValueError("请填写原因")
        return self


async def checked(operation):
    try:
        return await operation
    except LightError as exc:
        _raise(exc)


@router.get(BASE + "/sample/{key}/{kind}")
async def builtin_sample(key: str, kind: Literal["video", "cover"]):
    root = Path(__file__).resolve().parents[1]
    if key == "gallery_wishes" and kind == "cover":
        path = root / "assets/video_templates/gallery-wishes/frames/gallery-open.jpg"
    else:
        name = (library.BUILTIN_VIDEO.get(key) or ("", None))[1]
        if not name:
            raise HTTPException(404)
        filename = name + (".mp4" if kind == "video" else ".jpg")
        path = root / "assets/media-template-samples" / filename
        if not path.is_file():
            path = root.parent / "mobile/assets/light_production" / filename
    if not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


@author.get("/categories")
async def categories(user=Depends(get_current_user)):
    _scope(user)
    return {"entries": [entry.model_dump() for entry in (await taxonomy.refresh()).entries]}


@author.get("")
async def catalog(entry_id: EntryId, scene: str | None = Query(None, max_length=32), user=Depends(get_current_user)):
    _scope(user)
    return {"items": await library.catalog(entry_id, scene)}


@author.get("/mine")
async def mine(user=Depends(get_current_user)):
    return {"items": await library.mine(*_scope(user))}


@author.post("/{edition_id}/trial", status_code=201)
async def start_trial(edition_id: str, user=Depends(get_current_user)):
    return await checked(library.start_trial(*_scope(user), edition_id, user.get("ui_language")))


@author.post("/{edition_id}/confirm")
async def confirm(edition_id: str, body: Confirmation, user=Depends(get_current_user)):
    return await checked(library.confirm(*_scope(user), edition_id, body.session_id, body.output_asset_id))


@author.post("/{edition_id}/submit")
async def submit(edition_id: str, body: Submission, user=Depends(get_current_user)):
    return await checked(library.submit(*_scope(user), edition_id, body.session_id, body.scene))


@admin.get("")
async def queue(entry_id: EntryId | None = None, scene: str | None = Query(None, max_length=32),
                status: Literal["pending", "listed", "rejected", "superseded", "withdrawn"] | None = None,
                q: str = Query("", max_length=80), limit: int = Query(30, ge=1, le=50),
                offset: int = Query(0, ge=0, le=10000)):
    return await library.admin_list(entry_id, scene, status, q.strip(), limit, offset)


@admin.get("/{edition_id}")
async def detail(edition_id: str):
    operation = library.legacy_detail if edition_id.startswith(library.video_templates.PREFIX) else library.review_detail
    return await checked(operation(edition_id))


@admin.post("/{edition_id}/decision")
async def decision(edition_id: str, body: Decision, request: Request, user=Depends(live_admin)):
    if edition_id.startswith(library.video_templates.PREFIX):
        from api.admin_skills import _settle
        from skill import user_library
        saved = await checked(library.legacy_detail(edition_id))
        if body.decision == "listed":
            _raise(library.conflict("历史待审模板需要创建新版本并完成精简版试做。"))
        expected = "listed" if body.decision == "withdrawn" else "pending"
        if saved["status"] != expected:
            _raise(library.conflict("模板审核状态已变化，请刷新。"))
        listing = user_library.LISTING_DELISTED if body.decision == "withdrawn" else body.decision
        result = await user_library.set_listing(saved["catalog_id"], listing, note=body.reason or None,
                                                actor_user_id=user["user_id"])
        return await _settle(result, action="admin.media_template.legacy_decision", admin=user,
                             request=request, detail={"note": body.reason})
    return await checked(library.decide(edition_id, user["user_id"], body.decision, body.reason))


router.include_router(author)
router.include_router(admin)
