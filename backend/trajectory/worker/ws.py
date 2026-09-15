"""Read-only, revocable admin trajectory watermark subscriptions (SPEC §8.12).

The protocol of docs/trajectory-rearch/maps/api.md §3. A refusal happens after
``accept`` so browsers see 4401/4403 instead of 1006, and uvicorn pings every
20 s (``ws_ping_interval``). A notification is forwarded as the published
5-key watermark; only ``subscribe`` reads the trace database, and only the
trajectory row (the watermark), never the session header. A socket's viewer
is checked once per ``WATCH_SECONDS``: a send or a received message within
that window reuses the last check, so a revocation closes the socket within
a second at one authority check per second per socket.
"""
import asyncio
import json
import time
from contextlib import nullcontext, suppress

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect

import auth.ticket as tickets
from bus import bus
from core.log import create_logger
from trajectory import repository
from trajectory.auth import (NoStoreRoute, record_audit, require_admin_account, require_mobile_session,
    require_trajectory_admin, token_revoked, viewer_facts)
from trajectory.store.database import READ_POOL_RESERVE, trace_read_session

log = create_logger("trajectory.worker.ws")

router = APIRouter(tags=["Admin trajectories"], route_class=NoStoreRoute)
AUDIENCE = "admin_trajectories"
MAX_SUBSCRIPTIONS = 16
WATCH_SECONDS = 1.0
WATERMARK_KEYS = ("user_id", "owner_user_id", "session_id", "trajectory_id", "committed_seq")


@router.post("/api/admin/trajectories/ticket")
async def trajectory_ticket(request: Request, viewer: dict = Depends(require_trajectory_admin)):
    """Keep the access-token revocation identity without exposing its value."""
    # The dependency verified these claims; a token without a client claim is a web token.
    identity = request.state.trajectory_identity
    ticket = await tickets.create_ticket(
        viewer["user_id"], "admin", client=identity["client"] or "web",
        mobile_session_id=identity["sid"], audience=AUDIENCE,
        auth_jti=identity["jti"], auth_expires_at=identity["exp"],
    )
    return {"ticket": ticket}


async def claim_ticket(ticket: str) -> dict | None:
    """Single-use consumption in the shared ticket store (``ticket:{t}`` JSON, atomic ``ticket_claim:{t}``).

    The formats are those of ``auth.ticket``. The mobile session is part of
    ``validate_viewer``, so no business database is needed here.
    """
    cache = tickets._cache
    if cache is None:
        raise RuntimeError("Ticket store not initialized")
    key = f"ticket:{ticket}"
    data = await cache.get(key)
    if data is None:
        return None
    identity = json.loads(data) if isinstance(data, str) else data
    if not isinstance(identity, dict) or identity.get("audience") != AUDIENCE or not identity.get("user_id"):
        return None
    if await cache.incr(f"ticket_claim:{ticket}", ttl=30) != 1:
        return None
    await cache.delete(key)
    return identity


async def validate_viewer(identity: dict) -> None:
    """The in-process socket's checks in its order: account, role and allowlist, then token expiry, revocation
    and the mobile session. When several fail at once, the first decides the close code, as it did there."""
    client, jti = identity.get("client"), identity.get("auth_jti")
    facts = await viewer_facts(identity["user_id"], client=client, sid=identity.get("mobile_session_id"), jti=jti)
    require_admin_account(facts)
    expires = identity.get("auth_expires_at")
    if expires is not None and float(expires) <= time.time():
        raise HTTPException(401, detail="Token expired")
    if await token_revoked(jti):
        raise HTTPException(401, detail="Token revoked")
    require_mobile_session(facts, client)


def close_code(exc: HTTPException) -> int:
    """401 → 4401; an unreachable authority → 1011, which the client retries; any other refusal → 4403."""
    if exc.status_code == 401:
        return 4401
    if exc.status_code == 503:
        return 1011
    return 4403


def watermark(data: dict) -> dict:
    """Exactly the five watermark keys; the sequence travels as a decimal string."""
    value = {key: data.get(key) for key in WATERMARK_KEYS}
    if value["committed_seq"] is not None:
        value["committed_seq"] = str(value["committed_seq"])
    return value


async def _header(session_id: str, app=None) -> dict:
    """A subscription's watermark: the owner ``user_id``, ``session_id``, ``trajectory_id`` and ``committed_seq``
    (None and "0" for a session that has not started recording), read by at most READ_POOL_RESERVE sockets
    of ``app`` at once.

    Only the trajectory row is read: statistics and agents are the viewer's HTTP header probe. The read
    pool keeps that many connections beyond the HTTP read slots, so admin reads that fill their slots
    cannot make a subscription wait for a connection until the pool times out.
    """
    state = getattr(app, "state", None)
    reads = getattr(state, "trajectory_header_reads", None) if state is not None else None
    if state is not None and reads is None:
        reads = state.trajectory_header_reads = asyncio.Semaphore(READ_POOL_RESERVE)
    async with reads if reads is not None else nullcontext():
        async with trace_read_session() as db:
            session, trajectory = await repository.get_trajectory(db, session_id, optional=True)
    return {"user_id": session.user_id, "session_id": session_id,
            "trajectory_id": trajectory.id if trajectory is not None else None,
            "committed_seq": str(trajectory.committed_seq) if trajectory is not None else "0"}


@router.websocket("/ws/admin/trajectories")
async def trajectory_websocket(websocket: WebSocket, ticket: str = Query(default="")):
    await websocket.accept()
    try:
        identity = await claim_ticket(ticket) if ticket else None
        if identity is None:
            await websocket.close(code=4401)
            return
        validated = time.monotonic()
        await validate_viewer(identity)
    except HTTPException as exc:
        await websocket.close(code=close_code(exc))
        return
    except Exception as exc:
        log.warning("Trajectory socket authorization failed error_type=%s", type(exc).__name__)
        await websocket.close(code=1011)
        return
    subscriptions: set[str] = set()
    # Coalesce watermarks when a client is slow. Durable HTTP seq reads recover
    # every event; this queue never contains execution content.
    pending: dict[str, dict] = {}
    wake = asyncio.Event()
    send_lock = asyncio.Lock()
    check_lock = asyncio.Lock()

    async def revalidate():
        """The viewer's checks once the last one started WATCH_SECONDS ago: a message within that window reuses
        it, and callers that find it due at the same time share one check."""
        nonlocal validated
        async with check_lock:
            if time.monotonic() - validated < WATCH_SECONDS:
                return
            started = time.monotonic()
            await validate_viewer(identity)
            validated = started

    async def send(message: dict):
        async with send_lock:
            await revalidate()
            await websocket.send_json(message)

    def notify(event: dict):
        data = event.get("data") or {}
        sid = data.get("session_id")
        if isinstance(sid, str) and sid in subscriptions:
            # A deletion is final. Redis fan-out does not keep publish order, so
            # a watermark that arrives after it must not replace it unsent.
            if not (pending.get(sid) or {}).get("deleted"):
                pending[sid] = data
            wake.set()

    async def receive():
        while True:
            try:
                message = await websocket.receive_json()
            except (ValueError, KeyError, TypeError):
                await send({"type": "error", "data": {"code": "INVALID_MESSAGE"}})
                continue
            await revalidate()
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
                if sid not in subscriptions and len(subscriptions) >= MAX_SUBSCRIPTIONS:
                    await send({"type": "error", "data": {"code": "SUBSCRIPTION_LIMIT"}})
                    continue
                try:
                    header = await _header(sid, websocket.app)
                except (HTTPException, LookupError):
                    await send({"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": sid}})
                    continue
                if sid not in subscriptions:
                    await record_audit(identity["user_id"], "trajectory.subscribe",
                                       target_id=header.get("trajectory_id") or sid,
                                       details={"owner_user_id": header["user_id"], "session_id": sid,
                                                "through_seq": header["committed_seq"]})
                subscriptions.add(sid)
                await send({"type": "subscribed", "data": watermark({**header, "owner_user_id": header["user_id"]})})
            else:
                await send({"type": "error", "data": {"code": "READ_ONLY",
                                                        "message": "Only subscriptions and ping are accepted"}})

    async def publish():
        while True:
            await wake.wait()
            wake.clear()
            for sid in list(pending):
                data = pending.pop(sid, None)
                if data is None or sid not in subscriptions:
                    continue
                if data.get("deleted"):
                    subscriptions.discard(sid)
                    await send({"type": "error", "data": {"code": "SESSION_NOT_FOUND", "session_id": sid}})
                    continue
                await send({"type": "trajectory.available", "data": watermark(data)})

    async def watch():
        # Wakes WATCH_SECONDS after the last check, wherever it happened, so an idle socket is checked every
        # second and a busy one no more often.
        while True:
            await asyncio.sleep(max(0.0, validated + WATCH_SECONDS - time.monotonic()))
            await revalidate()

    unsubscribe = bus.subscribe("trajectory.available", notify)
    tasks = [asyncio.create_task(receive()), asyncio.create_task(publish()), asyncio.create_task(watch())]
    close_with = 1000
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    except HTTPException as exc:
        close_with = close_code(exc)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("Trajectory socket ended error_type=%s", type(exc).__name__)
        close_with = 1011
    finally:
        unsubscribe()
        subscriptions.clear()
        pending.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=close_with)
