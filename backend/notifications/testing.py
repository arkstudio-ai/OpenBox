"""Admin-only diagnostics for the caller's active phone; never a broadcast API."""
from uuid import uuid4

from fastapi import Depends, HTTPException
from sqlalchemy import select

from auth.middleware import require_admin
from auth.mobile import mobile_transaction, now, utc
from db.base import get_db_session
from db.models.push import MobilePresence, MobileSession, PushDelivery, PushDevice, PushMessage
from db.models.user import User
from notifications import presence
from notifications.store import device_status, enqueue_notification, valid_binding
from notifications.templates import TEMPLATES, test_template


async def check_admin(db, user):
    row = await db.get(User, user['user_id'])
    if not row or row.role != 'admin' or not row.is_active or row.is_deleted:
        raise HTTPException(403, detail={'code': 'FORBIDDEN'})


async def live_admin(user=Depends(require_admin)):
    async with get_db_session() as db:
        await check_admin(db, user)
    return user


async def active_device(db, user):
    device = await db.get(PushDevice, user['user_id'])
    login = await db.get(MobileSession, user['user_id'])
    if not (device and login and login.active and utc(login.expires_at) > now()
            and login.session_id == device.mobile_session_id):
        return None
    if user.get('client') == 'mobile' and device.mobile_session_id != user.get('mobile_session_id'):
        return None
    return device


def test_record(message, delivery):
    receipt = message.payload.get('testReceipt') or {}
    status = delivery.status if delivery else 'unbound'
    return {'id': message.id, 'template': (message.payload.get('guard') or {}).get('template', 'system_test'),
            'title': message.payload['title'], 'body': message.payload['body'],
            'status': status, 'error': delivery.error if delivery else None,
            'receipt': receipt.get('kind'), 'receiptAt': receipt.get('at'),
            'attempts': delivery.attempts if delivery else 0,
            'createdAt': message.created_at.isoformat(), 'expiresAt': message.expires_at.isoformat(),
            'availableAt': delivery.available_at.isoformat() if delivery else None}


async def overview(user, enabled, locale):
    async with get_db_session() as db:
        await check_admin(db, user)
        device = await active_device(db, user)
        state = await db.get(MobilePresence, user['user_id'])
        if not device or not state or state.mobile_session_id != device.mobile_session_id:
            state = None
        rows = (await db.execute(select(PushMessage, PushDelivery).outerjoin(
            PushDelivery, PushDelivery.message_id == PushMessage.id).where(
            PushMessage.user_id == user['user_id'], PushMessage.event_key.like('system_test:%'),
        ).order_by(PushMessage.created_at.desc()).limit(20))).all()
        locale = ("zh-CN" if device.locale.startswith("zh") else "en-US") if device else locale
        current = device_status(device)
        configured = bool(device and device.provider in enabled)
        return {'device': {**current, 'appVersion': device.app_version if device else None,
                          'providerConfigured': configured,
                          'ready': bool(configured and device.enabled)},
                'presence': presence.public_status(state),
                'providers': [{'id': name, 'configured': name in enabled} for name in ['apns', 'jpush']],
                'templates': [{'id': kind, 'title': test_template(kind, locale)[0],
                               'body': test_template(kind, locale)[1]} for kind in TEMPLATES],
                'tests': [test_record(message, delivery) for message, delivery in rows],
                'delaySeconds': 10, 'cooldownSeconds': 30}


async def send_test(user, enabled, *, template='system_test', binding_id=None, request_id=None):
    event_key = 'system_test:' + str(request_id or uuid4())
    async with mobile_transaction() as db:
        await check_admin(db, user)
        device = await active_device(db, user)
        if not device or not device.enabled:
            raise HTTPException(409, detail={'code': 'PUSH_DEVICE_NOT_READY'})
        if binding_id is not None and device.binding_id != binding_id:
            raise HTTPException(409, detail={'code': 'PUSH_BINDING_CHANGED'})
        if device.provider not in enabled:
            raise HTTPException(409, detail={'code': 'PUSH_NOT_CONFIGURED'})
        existing = await db.scalar(select(PushMessage).where(
            PushMessage.user_id == user['user_id'], PushMessage.event_key == event_key))
        if existing:
            if (existing.payload.get('bindingId') != device.binding_id or
                    (existing.payload.get('guard') or {}).get('template') != template):
                raise HTTPException(409, detail={'code': 'PUSH_TEST_REQUEST_CONFLICT'})
            delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == existing.id))
            return test_record(existing, delivery)
        latest = await db.scalar(select(PushMessage).where(
            PushMessage.user_id == user['user_id'], PushMessage.event_key.like('system_test:%'),
        ).order_by(PushMessage.created_at.desc()).limit(1))
        if latest and (now() - utc(latest.created_at)).total_seconds() < 30:
            raise HTTPException(429, detail={'code': 'PUSH_TEST_RATE_LIMITED'})
        title, body = test_template(template, device.locale)
        message = await enqueue_notification(db, user_id=user['user_id'], event_key=event_key,
            kind='system_test', title=title, body=body, ttl_seconds=300, delay_seconds=10,
            guard={'kind': 'admin_test', 'template': template})
        await db.flush()
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message.id))
        return test_record(message, delivery)


async def acknowledge(user, message_id, kind):
    async with mobile_transaction() as db:
        await check_admin(db, user)
        message = await db.get(PushMessage, message_id)
        delivery = await db.scalar(select(PushDelivery).where(PushDelivery.message_id == message_id))
        if not message or message.user_id != user['user_id'] or not message.event_key.startswith('system_test:'):
            raise HTTPException(404)
        if (not delivery or delivery.mobile_session_id != user.get('mobile_session_id')
                or not await valid_binding(db, delivery) or delivery.attempts < 1
                or delivery.status not in {'sending', 'accepted'}):
            raise HTTPException(409, detail={'code': 'PUSH_BINDING_CHANGED'})
        old = message.payload.get('testReceipt') or {}
        # Receiving again must not erase an earlier tap acknowledgement.
        if old.get('kind') != 'opened' and (old.get('kind') != kind):
            message.payload = {**message.payload, 'testReceipt': {'kind': kind, 'at': now().isoformat()}}
        return {'ok': True}
