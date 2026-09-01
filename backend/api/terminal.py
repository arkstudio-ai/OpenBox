import asyncio
import logging
from urllib.parse import urlencode

import websockets
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from auth.middleware import is_auth_enabled
from auth.ticket import consume_ticket
from project.workspace import (
    ensure_default_project,
    get_project,
    user_scope_for_identity,
    workdir_for_identity,
)
from sandbox import provider
from session import session as session_mod

logger = logging.getLogger(__name__)
router = APIRouter()


async def _terminal_workspace(
    user_id: str,
    *,
    session_id: str = "",
    project_id: str = "",
) -> tuple[str, str, str]:
    """Resolve an owned project into the canonical execution-plane context."""
    selected_project_id = project_id.strip()
    if session_id:
        session = await session_mod.get_session(session_id, user_id=user_id)
        if session is None:
            raise LookupError("Session not found")
        if selected_project_id and selected_project_id != session.project_id:
            raise PermissionError("Session does not belong to the selected project")
        selected_project_id = session.project_id
    project = (
        await get_project(selected_project_id, user_id)
        if selected_project_id
        else await ensure_default_project(user_id)
    )
    if project is None:
        raise LookupError("Project not found")
    return (
        await workdir_for_identity(user_id, project.id),
        user_scope_for_identity(user_id),
        project.name,
    )


def _container_terminal_url(
    info,
    *,
    workdir: str,
    user_scope: str,
    prompt_label: str,
) -> str:
    query = urlencode({
        "api_key": info.api_key or "",
        "workdir": workdir,
        "user_scope": user_scope,
        "prompt_label": prompt_label,
    })
    return f"ws://{info.host}:{info.port}/terminal?{query}"


async def _require_project_terminal_capability(
    container_id: str,
    *,
    user_id: str,
) -> None:
    """Do not silently connect to an old server that ignores project cwd."""
    try:
        response = await provider.forward_to_container(
            container_id,
            "GET",
            "/alive",
            user_id=user_id,
            timeout=5.0,
        )
        payload = response.json() if response.status_code == 200 else {}
    except (LookupError, ValueError, PermissionError):
        raise
    except Exception as exc:
        raise RuntimeError("Cloud desktop terminal is unavailable") from exc
    capabilities = payload.get("capabilities", [])
    if "terminal_project_cwd_v1" not in capabilities:
        raise RuntimeError("Cloud desktop terminal needs a component update")


@router.websocket("/ws/terminal/{container_id}")
async def terminal_websocket(
    websocket: WebSocket,
    container_id: str,
    ticket: str = Query(default=""),
    session_id: str = Query(default=""),
    project_id: str = Query(default=""),
):
    user_id = "default"
    if is_auth_enabled():
        if not ticket:
            await websocket.close(code=4001, reason="Ticket required")
            return
        user_data = await consume_ticket(ticket)
        if not user_data:
            await websocket.close(code=4001, reason="Invalid or expired ticket")
            return
        user_id = user_data["user_id"]

    await websocket.accept()

    try:
        workdir, user_scope, prompt_label = await _terminal_workspace(
            user_id,
            session_id=session_id,
            project_id=project_id,
        )
    except LookupError as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close(code=4004)
        return
    except ValueError as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close(code=4000)
        return
    except PermissionError as exc:
        await websocket.send_json({"type": "error", "data": str(exc) or "Forbidden"})
        await websocket.close(code=4003)
        return

    try:
        info = await provider.get_container(container_id, user_id=user_id)
    except ValueError:
        await websocket.send_json({"type": "error", "data": "Container not found"})
        await websocket.close(code=4004)
        return
    except PermissionError:
        await websocket.send_json({"type": "error", "data": "Forbidden"})
        await websocket.close(code=4003)
        return

    if not info.port:
        await websocket.send_json({"type": "error", "data": "Container port not available"})
        await websocket.close()
        return

    try:
        await _require_project_terminal_capability(container_id, user_id=user_id)
    except PermissionError:
        await websocket.send_json({"type": "error", "data": "Forbidden"})
        await websocket.close(code=4003)
        return
    except (LookupError, ValueError):
        await websocket.send_json({"type": "error", "data": "Container not found"})
        await websocket.close(code=4004)
        return
    except RuntimeError as exc:
        await websocket.send_json({"type": "error", "data": str(exc)})
        await websocket.close(code=1013)
        return

    # The frontend submits only opaque ids.  Canonical cwd and pseudonymous
    # tenant scope are resolved above and forwarded only over the trusted relay.
    container_ws_url = _container_terminal_url(
        info,
        workdir=workdir,
        user_scope=user_scope,
        prompt_label=prompt_label,
    )

    try:
        async with websockets.connect(
            container_ws_url,
            max_size=2**20,
            ping_interval=20,
            ping_timeout=10,
        ) as container_ws:

            async def frontend_to_container():
                """Relay messages from frontend WebSocket to container WebSocket."""
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if "bytes" in message and message["bytes"]:
                            await container_ws.send(message["bytes"])
                        elif "text" in message and message["text"]:
                            await container_ws.send(message["text"])
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug(f"frontend_to_container ended: {e}")

            async def container_to_frontend():
                """Relay messages from container WebSocket to frontend WebSocket."""
                try:
                    async for msg in container_ws:
                        if isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                except Exception as e:
                    logger.debug(f"container_to_frontend ended: {e}")

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(frontend_to_container()),
                    asyncio.create_task(container_to_frontend()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

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
