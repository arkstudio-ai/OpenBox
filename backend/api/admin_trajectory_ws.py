"""Read-only, revocable super-admin trajectory watermark subscriptions."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth import middleware
from auth.jwt import decode_access_token
from auth.ticket import consume_ticket, create_ticket
from bus import bus
from db.base import get_db_session
from trajectory.auth import NoStoreRoute, assert_admin, require_trajectory_admin
from trajectory.repository import get_session_header

router = APIRouter(tags=["Admin trajectories"], route_class=NoStoreRoute)
_bearer = HTTPBearer(auto_error=False)
_AUDIENCE = "admin_trajectories"
_MAX_SUBSCRIPTIONS = 16


@router.post("/api/admin/trajectories/ticket")
async def trajectory_ticket(
    viewer: dict = Depends(require_trajectory_admin),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
):
    """Keep the access-token revocation identity without exposing its value."""
    claims = decode_access_token(credentials.credentials) if credentials else {}
    if middleware.is_auth_enabled() and not claims:
        raise HTTPException(401, detail="Not authenticated")
    ticket = await create_ticket(
        viewer["user_id"], "admin", client=(claims or {}).get("client", "web"),
        mobile_session_id=(claims or {}).get("sid"), audience=_AUDIENCE,
        auth_jti=(claims or {}).get("jti"), auth_expires_at=(claims or {}).get("exp"),
    )
    return {"ticket": ticket}


async def validate_viewer(identity: dict) -> None:
    await assert_admin(identity["user_id"])
    expires = identity.get("auth_expires_at")
    if expires is not None and float(expires) <= time.time():
        raise HTTPException(401, detail="Token expired")
    jti = identity.get("auth_jti")
    if jti and middleware._cache and await middleware._cache.exists(f"jwt_bl:{jti}"):
        raise HTTPException(401, detail="Token revoked")
    from auth.mobile import validate_ticket
    await validate_ticket(identity)


async def _header(session_id: str) -> dict:
    async with get_db_session() as db:
        return await get_session_header(db, session_id)


def _watermark(header: dict) -> dict:
    return {"user_id": header["user_id"], "owner_user_id": header["user_id"],
            "session_id": header["session_id"], "trajectory_id": header["trajectory_id"],
            "committed_seq": header["committed_seq"]}


@router.websocket("/ws/admin/trajectories")
async def trajectory_websocket(websocket: WebSocket, ticket: str = Query(default="")):
    try:
        identity = await consume_ticket(ticket, audience=_AUDIENCE) if ticket else None
        if identity is None:
            await websocket.close(code=4401)
            return
        await validate_viewer(identity)
    except HTTPException as exc:
        await websocket.close(code=4401 if exc.status_code == 401 else 4403)
        return
    await websocket.accept()
    subscriptions: set[str] = set()
    # Coalesce watermarks when a client is slow. Durable HTTP seq reads recover
    # every event; this queue never contains execution content.
    pending: dict[str, dict] = {}
    wake = asyncio.Event()
    send_lock = asyncio.Lock()

    async def send(message: dict):
        async with send_lock:
            await validate_viewer(identity)
            await websocket.send_json(message)

    async def notify(event: dict):
        data = event.get("data", {})
        sid = data.get("session_id")
        if sid in subscriptions:
            pending[sid] = data
            wake.set()

    async def receive():
        while True:
            try:
                message = await websocket.receive_json()
            except ValueError:
                await send({"type": "error", "data": {"code": "INVALID_MESSAGE"}})
                continue
            await validate_viewer(identity)
            if not isinstance(message, dict):
                await send({"type": "error", "data": {"code": "INVALID_MESSAGE"}})
                continue
            kind = message.get("type")
            sid = message.get("session_id")
            if kind == "ping":
                await send({"type": "pong", "data": {}})
            elif kind == "unsubscribe" and isinstance(sid, str):
                subscriptions.discard(sid)
                pending.pop(sid, None)
                await send({"type": "unsubscribed", "data": {"session_id": sid}})
            elif kind == "subscribe" and isinstance(sid, str) and 0 < len(sid) <= 64:
                if sid not in subscriptions and len(subscriptions) >= _MAX_SUBSCRIPTIONS:
                    await send({"type": "error", "data": {"code": "SUBSCRIPTION_LIMIT"}})
                    continue
                try:
                    header = await _header(sid)
                except (HTTPException, LookupError):
                    await send({"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": sid}})
                    continue
                if sid not in subscriptions:
                    from db.repository.audit_repo import PgAuditRepo
                    await PgAuditRepo().create(
                        identity["user_id"], "trajectory.subscribe", resource_type="trajectory",
                        resource_id=header.get("trajectory_id") or sid,
                        details={"owner_user_id": header["user_id"], "session_id": sid,
                                 "through_seq": header["committed_seq"]},
                    )
                subscriptions.add(sid)
                await send({"type": "subscribed", "data": _watermark(header)})
            else:
                await send({"type": "error", "data": {"code": "READ_ONLY",
                                                        "message": "Only subscriptions and ping are accepted"}})

    async def publish():
        while True:
            await wake.wait()
            wake.clear()
            for sid in list(pending):
                pending.pop(sid, None)
                if sid not in subscriptions:
                    continue
                try:
                    header = await _header(sid)
                except (HTTPException, LookupError):
                    subscriptions.discard(sid)
                    await send({"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": sid}})
                    continue
                await send({"type": "trajectory.available", "data": _watermark(header)})

    async def watch():
        while True:
            await asyncio.sleep(1)
            await validate_viewer(identity)

    unsubscribe = bus.subscribe("trajectory.available", notify)
    tasks = [asyncio.create_task(receive()), asyncio.create_task(publish()), asyncio.create_task(watch())]
    close_code = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except HTTPException as exc:
        close_code = 4401 if exc.status_code == 401 else 4403
    except WebSocketDisconnect:
        pass
    finally:
        unsubscribe()
        subscriptions.clear()
        pending.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=close_code)
