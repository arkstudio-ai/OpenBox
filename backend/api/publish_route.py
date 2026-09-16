"""设置 → 视频发布: which route a person's videos take to Douyin.

`GET /api/publish/preference` reports the stored choice, the deployment
default and what would apply right now; `PUT` stores a choice (or clears it).
The agent reads the same preference inside `publish.desktop_service.precheck`.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from publish.route_pref import ROUTES, InvalidPublishRoute, get_publish_route, mode_to_route, set_publish_route

router = APIRouter(
    prefix="/api/publish",
    tags=["publish"],
    dependencies=[Depends(get_workspace)],
)


class PreferenceUpdate(BaseModel):
    #: "desktop" | "api" | null (null clears the choice → deployment default)
    route: str | None = None


async def _build_status(user_id: str) -> dict:
    from core.config import get_config

    preference = await get_publish_route(user_id)
    deployment_default = mode_to_route(get_config().desktop_publish.default_mode)
    return {
        "preference": preference,
        "deploymentDefault": deployment_default,
        "effective": preference or deployment_default,
        "routes": list(ROUTES),
    }


@router.get("/preference")
async def get_preference(current_user: dict = Depends(get_current_user)):
    return await _build_status(current_user["user_id"])


@router.put("/preference")
async def set_preference(body: PreferenceUpdate, current_user: dict = Depends(get_current_user)):
    try:
        await set_publish_route(current_user["user_id"], (body.route or "").strip() or None)
    except InvalidPublishRoute:
        return JSONResponse({"error": "invalid_route", "allowed": list(ROUTES)}, status_code=400)
    return await _build_status(current_user["user_id"])
