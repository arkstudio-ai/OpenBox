"""Authenticated push setup. Test sends target only the current mobile login."""
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select

from auth.middleware import get_current_user
from auth.mobile import mobile_transaction, now, require_mobile, session_error, utc
from db.base import get_db_session
from db.models.push import MobilePresence, PushDelivery, PushDevice, PushMessage
from notifications.providers import PushProviders
from notifications.schema import DeviceRegistration, PresenceReport
from notifications import presence
from notifications.store import device_status, disable_device, enqueue_notification, register_device

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
async def test_push(request: Request, user=Depends(mobile_user)):
    async with mobile_transaction() as db:
        await require_mobile(db, user["user_id"], user["mobile_session_id"])
        device = await db.get(PushDevice, user["user_id"])
        if not device or not device.enabled or device.mobile_session_id != user["mobile_session_id"]:
            raise HTTPException(409, detail={"code": "PUSH_DEVICE_NOT_READY"})
        if device.provider not in providers(request).enabled:
            raise HTTPException(409, detail={"code": "PUSH_NOT_CONFIGURED"})
        latest = await db.scalar(select(PushMessage).where(
            PushMessage.user_id == user["user_id"], PushMessage.event_key.like("system_test:%"),
        ).order_by(PushMessage.created_at.desc()).limit(1))
        if latest and (now() - utc(latest.created_at)).total_seconds() < 30:
            raise HTTPException(429, detail={"code": "PUSH_TEST_RATE_LIMITED"})
        zh = device.locale.startswith("zh")
        message = await enqueue_notification(db, user_id=user["user_id"], event_key="system_test:" + uuid4().hex,
            kind="system_test", title="BossIP 通知测试" if zh else "BossIP notification test",
            body="这条通知由服务器发送到你当前登录的手机。" if zh else "This notification was sent by the server to your signed-in phone.",
            ttl_seconds=300, delay_seconds=10)
        return {"id": message.id, "status": "pending"}


@router.get("/messages/{message_id}")
async def delivery_status(message_id: str, user=Depends(mobile_user)):
    async with get_db_session() as db:
        message = await db.get(PushMessage, message_id)
        if not message or message.user_id != user["user_id"]:
            raise HTTPException(404)
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message_id))
        return {"id": message_id, "status": delivery.status if delivery else "unbound",
                "error": delivery.error if delivery else None}
