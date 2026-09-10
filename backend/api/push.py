"""Authenticated push setup. Test sends target only the current mobile login."""
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from auth.middleware import get_current_user
from auth.mobile import require_mobile, session_error
from db.base import get_db_session
from db.models.push import MobilePresence, PushDelivery, PushDevice, PushMessage
from notifications.providers import PushProviders
from notifications.schema import DeviceRegistration, PresenceReport
from notifications import presence
from notifications.store import device_status, disable_device, register_device

from notifications.testing import live_admin, send_test

router = APIRouter(prefix="/api/push", tags=["Mobile push"])


def providers(request):
    # Lifespan initializes this in production; lazy construction also supports
    # API-only mounts/tests without starting any background task.
    if not hasattr(request.app.state, "push_providers"):
        request.app.state.push_providers = PushProviders.from_env()
    return request.app.state.push_providers


async def mobile_user(user=Depends(get_current_user)):
    if user.get("client") != "mobile" or not user.get("mobile_session_id"):
        raise session_error("AUTH_MOBILE_LOGIN_REQUIRED")
    return user


@router.get("/status")
async def status(request: Request, user=Depends(mobile_user)):
    async with get_db_session() as db:
        await require_mobile(db, user["user_id"], user["mobile_session_id"])
        device = await db.get(PushDevice, user["user_id"])
        data = device_status(device if device and device.mobile_session_id == user["mobile_session_id"] else None)
        app_state = presence.public_status(await db.get(MobilePresence, user["user_id"]))
    enabled = providers(request).enabled
    configured = data["provider"] in enabled
    return {**data, "presence": app_state, "enabledProviders": sorted(enabled), "providerConfigured": configured,
            "deliveryEnabled": configured and data["notificationsEnabled"]}


@router.put("/presence")
async def report_presence(body: PresenceReport, user=Depends(mobile_user)):
    return await presence.report(user["user_id"], user["mobile_session_id"], body)


@router.post("/devices")
async def register(body: DeviceRegistration, request: Request, user=Depends(mobile_user)):
    data = await register_device(user["user_id"], user["mobile_session_id"], body)
    configured = body.provider in providers(request).enabled
    return {**data, "providerConfigured": configured,
            "deliveryEnabled": configured and data["notificationsEnabled"]}


@router.delete("/devices/{binding_id}")
async def disable(binding_id: str, user=Depends(mobile_user)):
    await disable_device(user["user_id"], user["mobile_session_id"], binding_id)
    return {"ok": True}


@router.post("/test", status_code=202)
async def test_push(request: Request, user=Depends(mobile_user), admin=Depends(live_admin)):
    # Compatibility for older admin clients; regular users cannot invoke it.
    return await send_test(user, providers(request).enabled)


@router.get("/messages/{message_id}")
async def delivery_status(message_id: str, user=Depends(mobile_user)):
    async with get_db_session() as db:
        message = await db.get(PushMessage, message_id)
        if not message or message.user_id != user["user_id"]:
            raise HTTPException(404)
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message_id))
        return {"id": message_id, "status": delivery.status if delivery else "unbound",
                "error": delivery.error if delivery else None}
