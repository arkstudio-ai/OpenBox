"""Transactional device binding and enqueue, scoped to one mobile login."""
import hashlib
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from auth.mobile import (cancel_deliveries, lock_mutation, mobile_transaction,
                         now, require_mobile, utc)
from db.models.push import MobileSession, PushDelivery, PushDevice, PushMessage
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from notifications.schema import DeviceRegistration


def device_status(device):
    return {"registered": device is not None, "bindingId": device.binding_id if device else None,
            "notificationsEnabled": bool(device and device.enabled),
            "platform": device.platform if device else None, "provider": device.provider if device else None,
            "apnsEnvironment": device.apns_environment if device else None}


async def register_device(user_id: str, sid: str, registration: DeviceRegistration):
    r = registration
    token_hash = hashlib.sha256(f"{r.provider}:{r.apns_environment}:{r.bundle_id}:{r.token}".encode()).hexdigest()
    async with mobile_transaction() as db:
        await require_mobile(db, user_id, sid)
        previous = await db.scalar(select(PushDevice).where(PushDevice.token_hash == token_hash))
        if previous and previous.user_id != user_id:
            await cancel_deliveries(db, previous.user_id, binding_id=previous.binding_id)
            await db.delete(previous)
            await db.flush()
        device = await db.get(PushDevice, user_id)
        changed = not device or device.mobile_session_id != sid or device.token_hash != token_hash
        if device and (changed or not r.notifications_enabled):
            await cancel_deliveries(db, user_id, binding_id=device.binding_id)
        if not device:
            device = PushDevice(user_id=user_id)
            db.add(device)
        # A repeated registration is idempotent. Re-enabling starts a new
        # binding so old queued deliveries cannot reappear after opt-out.
        if changed or (r.notifications_enabled and not device.enabled):
            device.binding_id = uuid4().hex
        device.mobile_session_id = sid
        device.platform, device.provider = r.platform, r.provider
        device.token, device.token_hash = r.token, token_hash
        device.apns_environment, device.bundle_id = r.apns_environment, r.bundle_id
        device.app_version, device.locale = r.app_version, r.locale
        device.enabled, device.updated_at = r.notifications_enabled, now()
        return device_status(device)


async def disable_device(user_id: str, sid: str, binding_id: str):
    async with mobile_transaction() as db:
        await require_mobile(db, user_id, sid)
        device = await db.get(PushDevice, user_id)
        if device and device.mobile_session_id == sid and device.binding_id == binding_id:
            device.enabled = False
            await cancel_deliveries(db, user_id, binding_id=binding_id)


async def can_receive(db, user_id, workspace_id, session_id=None):
    user = await db.get(User, user_id)
    if not user or not user.is_active or user.is_deleted:
        return False
    if workspace_id:
        workspace = await db.get(Workspace, workspace_id)
        if not workspace or workspace.is_deleted:
            return False
        member = await db.scalar(select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user_id,
            WorkspaceMember.status == "active",
        ))
        if member is None:
            return False
    if session_id:
        session = await db.get(Session, session_id)
        if not session or session.is_deleted or not workspace_id or session.workspace_id != workspace_id:
            return False
    return True


async def enqueue_notification(db, *, user_id: str, event_key: str, kind: str,
                               title: str, body: str, workspace_id: str | None = None,
                               session_id: str | None = None, action_id: str | None = None,
                               ttl_seconds: int = 86400, delay_seconds: int = 3,
                               guard: dict | None = None, notification_id: str | None = None) -> PushMessage:
    """Enqueue inside the caller's transaction; never commit or call a provider.

    Only explicit business signals should call this, not every session idle or
    WS frame. No delivery is backfilled to a device that logs in later.
    """
    if kind not in {"system_test", "task_completed", "task_failed", "approval_required", "input_required",
                    "cron_completed", "cron_failed", "platform_auth_expired", "publish_done", "publish_failed",
                    "notice"}:
        raise ValueError("Unsupported notification kind")
    if not event_key or len(event_key) > 255 or not 1 <= ttl_seconds <= 86400:
        raise ValueError("Invalid notification event key or TTL")
    await lock_mutation(db)
    existing = await db.scalar(select(PushMessage).where(
        PushMessage.user_id == user_id, PushMessage.event_key == event_key,
    ))
    if existing:
        return existing
    if not await can_receive(db, user_id, workspace_id, session_id):
        raise HTTPException(403, detail="Notification recipient cannot access the target")
    message = PushMessage(id=uuid4().hex, user_id=user_id, event_key=event_key,
                          workspace_id=workspace_id, created_at=now(),
                          expires_at=now() + timedelta(seconds=ttl_seconds), payload={})
    device = await db.get(PushDevice, user_id)
    login = await db.get(MobileSession, user_id)
    message.payload = {"schemaVersion": 1, "source": "openbox", "eventId": message.id,
                       "dedupeKey": message.id, "type": kind, "recipientId": user_id,
                       "workspaceId": workspace_id, "sessionId": session_id, "actionId": action_id,
                       "notificationId": notification_id,
                       "title": title.strip()[:120], "body": body.strip()[:500],
                       "bindingId": device.binding_id if device else None,
                       **({"guard": guard} if guard else {})}
    db.add(message)
    await db.flush()
    if device and device.enabled and login and login.active and utc(login.expires_at) > now() and device.mobile_session_id == login.session_id:
        db.add(PushDelivery(id=uuid4().hex, message_id=message.id, user_id=user_id,
                            mobile_session_id=login.session_id, binding_id=device.binding_id,
                            status="pending", attempts=0, available_at=now() + timedelta(seconds=delay_seconds)))
    return message


async def valid_binding(db, delivery):
    device = await db.get(PushDevice, delivery.user_id)
    login = await db.get(MobileSession, delivery.user_id)
    if (not device or not device.enabled or device.binding_id != delivery.binding_id
            or device.mobile_session_id != delivery.mobile_session_id
            or not login or not login.active or login.session_id != delivery.mobile_session_id
            or utc(login.expires_at) <= now()):
        return None
    return device
