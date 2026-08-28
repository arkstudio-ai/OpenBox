"""Authenticated REST facade for verified-person video materials."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth.middleware import get_current_user
from video.materials import (
    MaterialProviderError,
    create_liveness_session,
    ensure_material_asset,
    get_identity,
    list_identities,
    list_identity_assets,
    refresh_liveness_session,
)

router = APIRouter(
    prefix="/api/video/identities",
    tags=["video-identities"],
    dependencies=[Depends(get_current_user)],
)


class CreateIdentityBody(BaseModel):
    label: str = Field(default="真人主持人", min_length=1, max_length=120)


class AddIdentityAssetBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=96)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MaterialProviderError):
        status = exc.status if 400 <= exc.status < 500 else 502
        return HTTPException(status, detail=str(exc))
    message = str(exc) or exc.__class__.__name__
    if "不属于当前用户" in message or "不存在" in message:
        return HTTPException(404, detail=message)
    if "尚未完成" in message or "超过" in message or "必须" in message:
        return HTTPException(409, detail=message)
    return HTTPException(502, detail=message[:1000])


@router.get("")
async def identities(current_user: dict = Depends(get_current_user)):
    return {"items": await list_identities(current_user["user_id"])}


@router.post("")
async def create_identity(
    body: CreateIdentityBody,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await create_liveness_session(current_user["user_id"], body.label)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{identity_id}")
async def identity(identity_id: str, current_user: dict = Depends(get_current_user)):
    result = await get_identity(current_user["user_id"], identity_id)
    if not result:
        raise HTTPException(404, detail="真人身份不存在")
    return result


@router.post("/{identity_id}/refresh")
async def refresh_identity(identity_id: str, current_user: dict = Depends(get_current_user)):
    try:
        return await refresh_liveness_session(current_user["user_id"], identity_id)
    except Exception as exc:
        raise _http_error(exc) from exc


@router.get("/{identity_id}/assets")
async def identity_assets(identity_id: str, current_user: dict = Depends(get_current_user)):
    try:
        return {"items": await list_identity_assets(current_user["user_id"], identity_id)}
    except Exception as exc:
        raise _http_error(exc) from exc


@router.post("/{identity_id}/assets")
async def add_identity_asset(
    identity_id: str,
    body: AddIdentityAssetBody,
    current_user: dict = Depends(get_current_user),
):
    try:
        return await ensure_material_asset(
            current_user["user_id"],
            body.asset_id,
            identity_id=identity_id,
        )
    except Exception as exc:
        raise _http_error(exc) from exc
