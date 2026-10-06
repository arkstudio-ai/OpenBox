import asyncio
import logging

import anyio
import websockets
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from auth.middleware import is_auth_enabled
from auth.ticket import consume_ticket
from auth.socket_access import SocketAccess
from sandbox import provider

logger = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/terminal/{container_id}")
async def terminal_websocket(websocket: WebSocket, container_id: str, ticket: str = Query(default="")):
    user_id = "default"
    ticket_workspace = None
    user_data = {"user_id": user_id, "client": "web"}
    authenticated = is_auth_enabled()
    if authenticated:
        if not ticket:
            await websocket.close(code=4001, reason="Ticket required")
            return
        user_data = await consume_ticket(ticket)
        if not user_data:
            await websocket.close(code=4001, reason="Invalid or expired ticket")
            return
        user_id = user_data["user_id"]
        ticket_workspace = user_data.get("workspace_id")

    access = SocketAccess.from_ticket(user_data, authenticated=authenticated)
    try:
        await access.check()
    except HTTPException:
        await websocket.close(code=4003, reason="Socket access denied")
        return

    await websocket.accept()

    try:
        from sandbox.ownership import owner_for
        from sandbox.terminal_channel import resolve_terminal_channel

        owner = ticket_workspace or await owner_for(user_id)
        info, channel_access = await resolve_terminal_channel(provider, container_id, owner)
    except ValueError:
        await websocket.send_json({"type": "error", "data": "Container not found"})
        await websocket.close()
        return
    except (HTTPException, PermissionError):
        await websocket.send_json({"type": "error", "data": "Forbidden"})
        await websocket.close(code=4003)
        return

    if not info.port:
        await websocket.send_json({"type": "error", "data": "Container port not available"})
        await websocket.close()
        return

    # Build container WebSocket URL
    container_ws_url = f"ws://{info.host}:{info.port}/terminal?api_key={info.api_key or ''}"

    async def check_access():
        await access.check()
        if provider.routes_per_user:
            from sandbox.entitlement import require_sandbox_subscription
            await require_sandbox_subscription(owner)
        if channel_access is not None:
            await channel_access.check()

    try:
        # Container resolution may have waited on a remote service.
        await check_access()
        async with websockets.connect(
            container_ws_url,
            max_size=2**20,
            ping_interval=20,
            ping_timeout=10,
        ) as container_ws:
            # The remote handshake may have waited while SQL revoked or
            # reassigned this original channel. No frame may use that route.
            await check_access()

            async def frontend_to_container():
                """Relay messages from frontend WebSocket to container WebSocket."""
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        await check_access()
                        if "bytes" in message and message["bytes"]:
                            await container_ws.send(message["bytes"])
                        elif "text" in message and message["text"]:
                            await container_ws.send(message["text"])
                except WebSocketDisconnect:
                    pass
                except (HTTPException, PermissionError):
                    raise
                except Exception as e:
                    logger.debug(f"frontend_to_container ended: {e}")

            async def container_to_frontend():
                """Relay messages from container WebSocket to frontend WebSocket."""
                try:
                    async for msg in container_ws:
                        await check_access()
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except (HTTPException, PermissionError):
                    raise
                except Exception as e:
                    logger.debug(f"container_to_frontend ended: {e}")

            pumps = [asyncio.create_task(frontend_to_container()), asyncio.create_task(container_to_frontend())]
            pumps.append(asyncio.create_task(access.watch()))
            if channel_access is not None:
                pumps.append(asyncio.create_task(channel_access.watch()))
            if provider.routes_per_user:
                from sandbox.entitlement import watch_sandbox_subscription
                pumps.append(asyncio.create_task(watch_sandbox_subscription(owner)))
            try:
                done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()  # Surface a current-authority refusal to the close handler.
            finally:
                with anyio.CancelScope(shield=True):
                    for task in pumps:
                        task.cancel()
                    await asyncio.gather(*pumps, return_exceptions=True)

    except (HTTPException, PermissionError):
        await websocket.close(code=4003, reason="Socket access denied")
    except Exception as e:
        logger.error(f"Failed to connect to container terminal: {e}")
        try:
            await websocket.send_json({"type": "error", "data": f"Failed to connect to container terminal: {e}"})
        except Exception:
            pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass
