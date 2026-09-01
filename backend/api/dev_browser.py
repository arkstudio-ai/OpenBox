import asyncio
import hashlib
import json
import logging

import websockets
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, HTTPException

from auth.ticket import consume_ticket
from auth.middleware import is_auth_enabled, get_current_user
from models.container import ContainerStatus
from sandbox import provider

logger = logging.getLogger(__name__)

# HTTP routes require user auth
_http_router = APIRouter(
    prefix="/api/containers",
    tags=["dev-browser"],
    dependencies=[Depends(get_current_user)],
)

_ws_router = APIRouter()

router = APIRouter()

# Active WS connections: user_id -> {client_id, ws}
_active_ws: dict[str, dict] = {}
# The workbench Browser tab is a screenshot/input stream, not the extension
# relay above. Keep its ownership separate so opening the UI never disconnects
# an Agent's dev-browser extension session.
_active_browser_views: dict[str, WebSocket] = {}


def _browser_view_client(user_id: str, container):
    """Build a tenant-scoped Action Server client for the live desktop."""
    from sandbox.client import SandboxClient
    from sandbox.manager import _user_scope

    return SandboxClient(
        host=container.host or "127.0.0.1",
        port=container.port,
        api_key=container.api_key or "",
        base_url=getattr(provider, "client_base_url", None),
        user_scope=_user_scope(user_id),
    )


# ── HTTP API: container-specific endpoints (authenticated) ──

@_http_router.post("/{container_id}/dev-browser/start")
async def start_dev_browser(container_id: str, current_user: dict = Depends(get_current_user)):
    try:
        resp = await provider.forward_to_container(
            container_id, "POST", "/dev-browser/start", user_id=current_user["user_id"]
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
    try:
        resp = await provider.forward_to_container(
            container_id, "POST", "/dev-browser/stop", user_id=current_user["user_id"]
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
    try:
        resp = await provider.forward_to_container(
            container_id, "GET", "/dev-browser/status", user_id=current_user["user_id"]
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

    container = provider.get_user_container(user_id)
    if not container or container.status != ContainerStatus.RUNNING or not container.port:
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
            except Exception:
                pass
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
                            break
                        if "text" in msg and msg["text"]:
                            await container_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"]:
                            await container_ws.send(msg["bytes"])
                except (WebSocketDisconnect, Exception):
                    pass

            async def ctr_to_ext():
                try:
                    async for m in container_ws:
                        if isinstance(m, bytes):
                            await websocket.send_bytes(m)
                        else:
                            await websocket.send_text(m)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [asyncio.create_task(ext_to_ctr()), asyncio.create_task(ctr_to_ext())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
    except Exception as e:
        logger.error(f"Failed to connect to container relay: {e}")
        try:
            await websocket.send_json({"type": "error", "data": str(e)})
        except Exception:
            pass
    finally:
        if client_id:
            active = _active_ws.get(user_id)
            if active and active["client_id"] == client_id:
                _active_ws.pop(user_id, None)
        try:
            await websocket.close()
        except Exception:
            pass


@_ws_router.websocket("/ws/browser-view/auto")
async def browser_view_ws_auto(
    websocket: WebSocket,
    ticket: str = Query(default=""),
):
    """Stream the managed cloud Chrome as PNG frames with bounded UI input.

    This endpoint intentionally does not reuse ``/ws/dev-browser/auto``: that
    route carries the extension/CDP protocol and has no screenshot semantics.
    Close codes: 4001=replaced, 4003=auth failed, 4004=no container.
    """
    user_id = "default"
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

    container = provider.get_user_container(user_id)
    if not container or container.status != ContainerStatus.RUNNING or not container.port:
        await websocket.accept()
        await websocket.close(code=4004, reason="No running container")
        return

    await websocket.accept()
    previous = _active_browser_views.get(user_id)
    if previous is not None and previous is not websocket:
        try:
            await previous.close(code=4001, reason="Replaced by new browser view")
        except Exception:
            pass
    _active_browser_views[user_id] = websocket

    from sandbox.browser_view import BrowserViewController, BrowserViewProtocolError
    from sandbox.manager import _user_scope

    controller = BrowserViewController(
        _browser_view_client(user_id, container),
        container_key=container.id,
        user_scope=_user_scope(user_id),
    )

    try:
        current_url = await controller.start()
        await websocket.send_json({"type": "ready"})
        if current_url:
            await websocket.send_json({"type": "url", "url": current_url})

        async def send_frames():
            last_digest: bytes | None = None
            last_url = current_url
            frames_until_url_probe = 0
            consecutive_failures = 0
            while True:
                try:
                    _geometry, frame = await controller.capture()
                    digest = hashlib.sha256(frame).digest()
                    if digest != last_digest:
                        await websocket.send_bytes(frame)
                        last_digest = digest
                    frames_until_url_probe -= 1
                    if frames_until_url_probe <= 0:
                        observed_url = await controller.current_url()
                        if observed_url and observed_url != last_url:
                            await websocket.send_json({"type": "url", "url": observed_url})
                            last_url = observed_url
                        frames_until_url_probe = 5
                    consecutive_failures = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
                    consecutive_failures += 1
                    if consecutive_failures >= 3:
                        raise
                    await asyncio.sleep(0.5)
                    continue
                await asyncio.sleep(0.8)

        async def receive_commands():
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
                text = message.get("text")
                if not text:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_command",
                        "message": "Browser commands must be JSON text",
                    })
                    continue
                try:
                    payload = json.loads(text)
                    response = await controller.handle(payload)
                except (json.JSONDecodeError, BrowserViewProtocolError) as exc:
                    await websocket.send_json({
                        "type": "error",
                        "code": "invalid_command",
                        "message": str(exc)[:200],
                    })
                    continue
                except Exception as exc:
                    logger.warning("Browser view input failed: %s", type(exc).__name__)
                    await websocket.send_json({
                        "type": "error",
                        "code": "action_failed",
                        "message": str(exc)[:200],
                    })
                    continue
                if response:
                    await websocket.send_json(response)

        tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive_commands())]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("Browser view stopped: %s", type(exc).__name__)
        try:
            await websocket.send_json({
                "type": "error",
                "code": "stream_failed",
                "message": str(exc)[:200],
            })
        except Exception:
            pass
    finally:
        if _active_browser_views.get(user_id) is websocket:
            _active_browser_views.pop(user_id, None)
        await controller.close()
        try:
            await websocket.close()
        except Exception:
            pass


# Combine all routers
router.include_router(_http_router)
router.include_router(_ws_router)
