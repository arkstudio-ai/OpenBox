"""Remote Action Server lease enforcement for a shared physical desktop."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import httpx
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

from media_jobs import MediaJobConfig, MediaJobManager  # noqa: E402


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


@pytest.mark.asyncio
async def test_authenticated_media_routes_complete_a_durable_job(tmp_path):
    original_manager = server.media_job_manager
    original_key = server.SESSION_API_KEY
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )

    async def fake_render(_job_id, _payload):
        return {"uploaded": True, "has_audio": True, "duration_seconds": 4.0}

    manager._render = fake_render
    server.media_job_manager = manager
    server.SESSION_API_KEY = "route-test-key"
    await manager.start()
    try:
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://action.test") as client:
            payload = {
                "job_id": "render-route-0001",
                "owner": "user-route",
                "session_id": "session-route",
                "idempotency_key": "route-key-v1",
                "inputs": [{
                    "name": "segment.mp4",
                    "mime": "video/mp4",
                    "size": 10,
                    "cache_key": "bucket:key:10",
                    "url": "https://oss.example.test/input.mp4?signature=hidden",
                }],
                "output": {
                    "name": "final.mp4",
                    "mime": "video/mp4",
                    "put_url": "https://oss.example.test/output.mp4?signature=hidden",
                },
                "captions": ["路由集成测试"],
            }
            denied = await client.post("/media/jobs", json=payload)
            assert denied.status_code == 403

            headers = {"X-API-Key": "route-test-key"}
            submitted = await client.post("/media/jobs", headers=headers, json=payload)
            assert submitted.status_code == 200
            job = submitted.json()
            for _ in range(100):
                response = await client.get(
                    f"/media/jobs/{job['job_id']}",
                    headers=headers,
                    params={"owner": "user-route"},
                )
                assert response.status_code == 200
                current = response.json()
                if current["status"] == "completed":
                    break
                await asyncio.sleep(0.01)
            assert current["status"] == "completed"
            assert current["result"]["resource_check"]["temp_removed"] is True
    finally:
        await manager.stop()
        server.media_job_manager = original_manager
        server.SESSION_API_KEY = original_key
