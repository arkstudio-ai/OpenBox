import asyncio

import websockets
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, HTTPException

from auth.ticket import consume_ticket
from auth.middleware import is_auth_enabled, get_current_user
from auth.workspace import get_workspace
from core.log import create_logger
from models.container import ContainerStatus
from sandbox import provider

logger = create_logger("api.dev_browser")

# HTTP routes require user auth
_http_router = APIRouter(
    prefix="/api/containers",
    tags=["dev-browser"],
    dependencies=[Depends(get_workspace)],
)

_ws_router = APIRouter()

router = APIRouter()

# Active WS connections: user_id -> {client_id, ws}
_active_ws: dict[str, dict] = {}


# ── HTTP API: container-specific endpoints (authenticated) ──

@_http_router.post("/{container_id}/dev-browser/start")
async def start_dev_browser(container_id: str, current_user: dict = Depends(get_current_user)):
    from sandbox.ownership import owner_for_request

    try:
        resp = await provider.forward_to_container(
            container_id, "POST", "/dev-browser/start",
            user_id=await owner_for_request(current_user),
        )
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@_http_router.post("/{container_id}/dev-browser/stop")
async def stop_dev_browser(container_id: str, current_user: dict = Depends(get_current_user)):
    from sandbox.ownership import owner_for_request

    try:
        resp = await provider.forward_to_container(
            container_id, "POST", "/dev-browser/stop",
            user_id=await owner_for_request(current_user),
        )
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@_http_router.get("/{container_id}/dev-browser/status")
async def get_dev_browser_status_authed(container_id: str, current_user: dict = Depends(get_current_user)):
    from sandbox.ownership import owner_for_request

    try:
        resp = await provider.forward_to_container(
            container_id, "GET", "/dev-browser/status",
            user_id=await owner_for_request(current_user),
        )
        return resp.json()
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Forbidden")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Extension connection info (authenticated) ──

@_http_router.get("/dev-browser/link-info")
async def get_extension_status(current_user: dict = Depends(get_current_user)):
    """Get current user's extension connection status."""
    user_id = current_user["user_id"]

    active = _active_ws.get(user_id)
    if active:
        return {
            "has_link": True,
            "connected": True,
            "client_id": active["client_id"][:8],
        }

    return {"has_link": False, "connected": False}


# ── WebSocket: auto endpoint (ticket auth, auto-resolve container) ──

@_ws_router.websocket("/ws/dev-browser/auto")
async def dev_browser_ws_auto(
    websocket: WebSocket,
    ticket: str = Query(default=""),
    client_id: str = Query(default=""),
):
    """WebSocket relay with ticket auth. Auto-resolves user's running container.

    Close codes: 4001=replaced, 4003=auth failed, 4004=no container.
    """
    user_id = "default"
    ticket_workspace = None
    user_data = {"user_id": user_id, "client": "web"}

    if is_auth_enabled():
        if not ticket:
            await websocket.accept()
            await websocket.close(code=4003, reason="Ticket required")
            return

        user_data = await consume_ticket(ticket)
        if not user_data:
            await websocket.accept()
            await websocket.close(code=4003, reason="Invalid or expired ticket")
            return
        user_id = user_data["user_id"]
        ticket_workspace = user_data.get("workspace_id")

    from sandbox.ownership import owner_for

    owner = ticket_workspace or await owner_for(user_id)
    try:
        container = await provider.resolve_user_container(owner)
    except Exception as e:
        logger.warning(f"dev-browser ws: cannot resolve container for {owner}: {type(e).__name__}: {e}")
        container = None
    if not container or container.status != ContainerStatus.RUNNING or not container.port:
        logger.info(
            f"dev-browser ws: no running container for {owner} "
            f"(status={getattr(container, 'status', None)}, port={getattr(container, 'port', None)})"
        )
        await websocket.accept()
        await websocket.close(code=4004, reason="No running container")
        return

    await websocket.accept()

    # Kick any existing connection for this user
    if client_id:
        active = _active_ws.get(user_id)
        if active and active["client_id"] != client_id:
            logger.info(f"Kicking client {active['client_id'][:8]}... replaced by {client_id[:8]}...")
            try:
                await active["ws"].close(code=4001, reason="Replaced by new client")
            except Exception as e:
                logger.debug(f"dev-browser ws: closing replaced client failed: {e}")
        _active_ws[user_id] = {"client_id": client_id, "ws": websocket}

    container_id = container.id
    container_ws_url = (
        f"ws://{container.host}:{container.port}/dev-browser/ws"
        f"?api_key={container.api_key or provider._api_keys.get(container_id, '')}"
    )

    try:
        async with websockets.connect(
            container_ws_url, max_size=2**20, ping_interval=20, ping_timeout=10,
        ) as container_ws:

            async def ext_to_ctr():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg["type"] == "websocket.disconnect":
                            logger.info(f"dev-browser ws: extension disconnected (user {user_id[:8]})")
                            break
                        from auth.mobile import validate_ticket
                        await validate_ticket(user_data)
                        if provider.routes_per_user:
                            from sandbox.entitlement import require_sandbox_subscription
                            await require_sandbox_subscription(owner)
                        if "text" in msg and msg["text"]:
                            await container_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"]:
                            await container_ws.send(msg["bytes"])
                except WebSocketDisconnect:
                    logger.info(f"dev-browser ws: extension side closed (user {user_id[:8]})")
                except Exception as e:
                    logger.warning(f"dev-browser ws: extension->relay pump ended (user {user_id[:8]}): {type(e).__name__}: {e}")

            async def ctr_to_ext():
                try:
                    async for m in container_ws:
                        if isinstance(m, bytes):
                            await websocket.send_bytes(m)
                        else:
                            await websocket.send_text(m)
                    logger.info(f"dev-browser ws: relay closed the connection (user {user_id[:8]})")
                except Exception as e:
                    logger.warning(f"dev-browser ws: relay->extension pump ended (user {user_id[:8]}): {type(e).__name__}: {e}")

            pumps = [asyncio.create_task(ext_to_ctr()), asyncio.create_task(ctr_to_ext())]
            from auth.mobile import watch_session
            pumps.append(asyncio.create_task(watch_session(user_data)))
            if provider.routes_per_user:
                from sandbox.entitlement import watch_sandbox_subscription
                pumps.append(asyncio.create_task(watch_sandbox_subscription(owner)))
            done, pending = await asyncio.wait(
                pumps,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
    except Exception as e:
        logger.error(f"Failed to connect to container relay at {container.host}:{container.port}: {type(e).__name__}: {e}")
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except Exception as send_error:
            logger.debug(f"dev-browser ws: could not report relay error to extension: {send_error}")
    finally:
        if client_id:
            active = _active_ws.get(user_id)
            if active and active["client_id"] == client_id:
                _active_ws.pop(user_id, None)
        try:
            await websocket.close()
        except Exception:
            pass  # already closed by the other side; nothing to report


# Combine all routers
router.include_router(_http_router)
router.include_router(_ws_router)
