"""Durable latest-login-wins mobile sessions, independent of web sessions.

Binding/login mutations share a short PostgreSQL advisory transaction lock.
No network I/O happens under this lock. This also serializes cross-account
installation/token moves without a lock-order inversion. Reads don't lock.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from weakref import WeakKeyDictionary

from fastapi import HTTPException, Request
from sqlalchemy import or_, select, text, update

from db.base import get_db_session
from db.models.push import MobilePresence, MobileSession, PushDelivery, PushDevice

_sqlite_locks = WeakKeyDictionary()
_LOCK_ID = 1329748048


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def session_error(code="AUTH_MOBILE_SESSION_REPLACED"):
    return HTTPException(401, detail={"code": code, "message": "Please sign in again on this device."},
                         headers={"X-Error-Code": code})


async def lock_mutation(db):
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_ID})


@asynccontextmanager
async def mobile_transaction():
    # SQLite is used by desktop mode and tests. PostgreSQL provides the
    # cross-worker serialization in authenticated deployments.
    loop = asyncio.get_running_loop()
    local_lock = _sqlite_locks.setdefault(loop, asyncio.Lock())
    async with local_lock:
        async with get_db_session() as db:
            if db.bind.dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            await lock_mutation(db)
            yield db


async def cancel_deliveries(db, user_id, *, binding_id=None):
    stmt = update(PushDelivery).where(
        PushDelivery.user_id == user_id,
        PushDelivery.status.in_(("pending", "sending")),
    )
    if binding_id:
        stmt = stmt.where(PushDelivery.binding_id == binding_id)
    await db.execute(stmt.values(status="cancelled", error="binding_revoked", lease_id=None, lease_until=None))


async def revoke_locked(db, row):
    row.active = False
    row.installation_id = None
    row.updated_at = now()
    device = await db.get(PushDevice, row.user_id)
    if device:
        device.enabled = False
    await cancel_deliveries(db, row.user_id)


async def begin_login(user_id, installation_id, *, lifetime_days=7):
    async with mobile_transaction() as db:
        rows = list((await db.scalars(select(MobileSession).where(or_(
            MobileSession.user_id == user_id, MobileSession.installation_id == installation_id,
        )))).all())
        current = None
        for row in rows:
            await revoke_locked(db, row)
            if row.user_id == user_id:
                current = row
        await db.flush()  # Release the installation unique key before reassigning it.
        sid = uuid4().hex
        if current is None:
            current = MobileSession(user_id=user_id)
            db.add(current)
        current.session_id = sid
        current.installation_id = installation_id
        current.active = True
        current.expires_at = now() + timedelta(days=lifetime_days)
        current.updated_at = now()
        presence = await db.get(MobilePresence, user_id)
        if presence is None:
            presence = MobilePresence(user_id=user_id)
            db.add(presence)
        presence.mobile_session_id, presence.state = sid, "detached"
        presence.sequence, presence.reported_at = 0, now()
        # Even a preexisting endpoint without a matching session is retired.
        await db.execute(update(PushDevice).where(PushDevice.user_id == user_id).values(enabled=False))
        await cancel_deliveries(db, user_id)
    return sid


async def require_mobile(db, user_id, sid):
    row = await db.get(MobileSession, user_id)
    if not row or not sid or row.session_id != sid or not row.active or utc(row.expires_at) <= now():
        raise session_error()
    return row


async def validate_claims(payload, *, mobile_request=False):
    client = payload.get("client")
    if mobile_request and client != "mobile":
        raise session_error("AUTH_MOBILE_LOGIN_REQUIRED")
    if client == "web":
        return
    async with get_db_session() as db:
        if client == "mobile":
            await require_mobile(db, payload.get("sub"), payload.get("sid"))
        elif client is None:
            # Old tokens don't say whether they belong to web or a phone.
            # Once the account adopts single-mobile login, require a one-time
            # re-login for these legacy sessions; never upgrade them on refresh.
            if await db.get(MobileSession, payload.get("sub")):
                raise session_error("AUTH_MOBILE_LOGIN_REQUIRED")
        else:
            raise session_error("AUTH_MOBILE_LOGIN_REQUIRED")


def is_mobile_request(request: Request):
    return request.headers.get("X-Client-Type", "").lower() == "mobile"


async def login_claims(request, user_id, *, native=False):
    if not native and not is_mobile_request(request):
        return {"client": "web"}
    installation = request.headers.get("X-Installation-Id", "")
    # Old native clients can still log in, but never bypass the exclusive lease.
    if native and not installation:
        installation = "legacy-" + uuid4().hex
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", installation):
        raise HTTPException(400, detail="A valid X-Installation-Id is required for mobile login")
    from auth.jwt import refresh_lifetime_days
    sid = await begin_login(user_id, installation, lifetime_days=refresh_lifetime_days())
    return {"client": "mobile", "sid": sid}


async def refresh_claims(payload, request):
    await validate_claims(payload, mobile_request=is_mobile_request(request))
    if payload.get("client") != "mobile":
        return {"client": "web"}
    from auth.jwt import refresh_lifetime_days
    async with mobile_transaction() as db:
        row = await require_mobile(db, payload["sub"], payload.get("sid"))
        row.expires_at = now() + timedelta(days=refresh_lifetime_days())
        row.updated_at = now()
    # Preserve the session ID: refreshing an old phone can NEVER become login.
    return {"client": "mobile", "sid": payload["sid"]}


async def revoke_session(user_id, sid):
    async with mobile_transaction() as db:
        row = await db.get(MobileSession, user_id)
        if row and row.session_id == sid:
            await revoke_locked(db, row)


async def validate_ticket(identity):
    await validate_claims({"sub": identity["user_id"], "client": identity.get("client"),
                           "sid": identity.get("mobile_session_id")})


async def watch_session(identity, interval=5):
    """Ending this coroutine closes the socket through its FIRST_COMPLETED race."""
    if identity.get("client") != "mobile":
        await asyncio.Future()
    while True:
        await asyncio.sleep(interval)
        await validate_ticket(identity)
