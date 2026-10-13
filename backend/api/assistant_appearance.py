"""The PC companion's settings and small, private custom portrait upload."""
import hashlib

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from typing import Literal

from assistant import appearance
from auth.middleware import get_current_user
from core.oss import OssError, OssNotConfigured, get_oss

router = APIRouter(prefix="/api/assistant/appearance", tags=["assistant"])


class AppearanceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    preset: Literal["orb", "sprout", "cat", "custom"] | None = None


@router.get("")
async def get_appearance(current_user: dict = Depends(get_current_user)):
    return await appearance.view(current_user["user_id"])


@router.put("")
async def set_appearance(body: AppearanceBody, current_user: dict = Depends(get_current_user)):
    try:
        await appearance.save(current_user["user_id"], body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(422, {"code": str(exc), "message": "Upload a portrait first"}) from exc
    return await appearance.view(current_user["user_id"])


@router.post("/image")
async def upload_image(file: UploadFile = File(...), current_user: dict = Depends(get_current_user)):
    try:
        data = await file.read(appearance.MAX_IMAGE_BYTES + 1)
    finally:
        await file.close()
    try:
        appearance.png_dimensions(data)
    except ValueError as exc:
        raise HTTPException(422, {"code": str(exc), "message": "Use a PNG up to 1 MB and 1024 pixels"}) from exc
    user_id = current_user["user_id"]
    key = f"assistant-avatars/{user_id}/{hashlib.sha256(data).hexdigest()}.png"
    try:
        # Immutable, content-addressed bytes: a retried upload cannot overwrite
        # a different portrait or break a preview already open on another device.
        await get_oss().put_object(key, data, content_type="image/png", forbid_overwrite=True, timeout=30)
    except (OssError, OssNotConfigured) as exc:
        raise HTTPException(503, {"code": "ASSISTANT_AVATAR_UNAVAILABLE", "message": "Portrait storage is unavailable"}) from exc
    await appearance.save(user_id, {"preset": "custom", "image_key": key})
    return await appearance.view(user_id)
