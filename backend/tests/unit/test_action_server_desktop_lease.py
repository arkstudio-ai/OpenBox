"""Remote Action Server lease enforcement for a shared physical desktop."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import pytest
from fastapi import HTTPException
from starlette.requests import Request

_SERVER_PATH = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
sys.modules.setdefault("psutil", types.SimpleNamespace())
if "sse_starlette.sse" not in sys.modules:
    sse_package = types.ModuleType("sse_starlette")
    sse_module = types.ModuleType("sse_starlette.sse")
    sse_module.EventSourceResponse = type("EventSourceResponse", (), {})
    sys.modules["sse_starlette"] = sse_package
    sys.modules["sse_starlette.sse"] = sse_module
_SPEC = importlib.util.spec_from_file_location("openbox_action_server_test", _SERVER_PATH)
assert _SPEC and _SPEC.loader
server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


def request(**headers: str) -> Request:
    encoded = [(name.lower().encode(), value.encode()) for name, value in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/", "headers": encoded})


@pytest.fixture(autouse=True)
def clear_lease():
    server._desktop_lease = None
    yield
    server._desktop_lease = None


@pytest.mark.asyncio
async def test_unleased_legacy_desktop_command_is_rejected():
    with pytest.raises(HTTPException) as error:
        await server._validate_desktop_lease(request(), "obx-x xdotool key Return")
    assert error.value.status_code == 423


@pytest.mark.asyncio
async def test_valid_lease_allows_desktop_command_and_release():
    traced = request(
        **{
            "X-OpenBox-Instance": "backend-a",
            "X-OpenBox-Session": "session-a",
            "X-OpenBox-Tool-Call": "part-a",
        }
    )
    lease = await server.acquire_desktop_lease(
        server.DesktopLeaseRequest(owner="backend-a:session-a:part-a"), traced
    )
    leased = request(
        **{
            "X-OpenBox-Instance": "backend-a",
            "X-OpenBox-Operation": "computer",
            "X-OpenBox-Desktop-Lease": lease["token"],
        }
    )

    await server._validate_desktop_lease(leased, "obx-x obx-shot 1280 800 /tmp/obx-screen.png")
    released = await server.release_desktop_lease(
        server.DesktopLeaseReleaseRequest(token=lease["token"]), leased
    )
    assert released == {"released": True}


@pytest.mark.asyncio
async def test_second_backend_waits_until_first_releases():
    first_request = request(**{"X-OpenBox-Instance": "backend-a"})
    second_request = request(**{"X-OpenBox-Instance": "backend-b"})
    first = await server.acquire_desktop_lease(
        server.DesktopLeaseRequest(owner="backend-a", wait_timeout=1), first_request
    )

    waiting = asyncio.create_task(server.acquire_desktop_lease(
        server.DesktopLeaseRequest(owner="backend-b", wait_timeout=1), second_request
    ))
    await asyncio.sleep(0.03)
    assert not waiting.done()

    await server.release_desktop_lease(
        server.DesktopLeaseReleaseRequest(token=first["token"]), first_request
    )
    second = await waiting
    assert second["token"] != first["token"]
    assert second["wait_ms"] >= 20


def test_command_classification_does_not_log_command_contents():
    assert server._desktop_command_kind("obx-x xdotool click 1") == "desktop_input"
    assert server._desktop_command_kind("obx-x obx-shot 1 1 /tmp/x") == "desktop_capture"
    assert server._desktop_command_kind("obx-x google-chrome about:blank") == "desktop_session"
    assert server._desktop_command_kind("echo hello") == "shell"
