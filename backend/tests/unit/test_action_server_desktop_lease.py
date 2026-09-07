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




# --- browser diagnostics / trace additions (D1) ---

def test_browser_command_kinds_are_recognised_without_reading_them():
    kind = server._desktop_command_kind
    assert kind(": obx-chrome-probe; printf %s abc | base64 -d | python3") == "browser_probe"
    assert kind("curl -sS --max-time 3 -w '\\nOBX_HTTP=%{http_code}' http://127.0.0.1:9333/json/version") == "browser_probe"
    assert kind("curl -s http://127.0.0.1:9222/") == "browser_probe"
    assert kind(": obx-diag; python3 -c 'loader' 'payload' --lines 60") == "browser_diag"
    assert kind("python3 /opt/openbox/tools/repair_browser_runtime.py --check") == "browser_runtime"
    assert kind("cd /opt/openbox/skills/dev-browser && npm run start-relay") == "browser_relay"
    assert kind("cd /workspace && npx tsx script.ts") == "browser_script"
    assert kind('PATH="$HOME/.local/bin:$PATH" obx-x sh -c \'google-chrome --remote-debugging-port=9333\'') == "browser_launch"


def test_readonly_browser_probes_never_need_the_lease():
    """The status page polls while a turn holds the desktop; probes must not 423."""
    for command in (
        "curl -sS --max-time 3 http://127.0.0.1:9333/json/version",
        ": obx-chrome-probe; printf %s abc | base64 -d | python3",
        ": obx-diag; python3 -c x y --lines 5",
        "python3 /opt/openbox/tools/repair_browser_runtime.py --check",
    ):
        assert not server._requires_desktop_lease(request(), command), command
    assert server._requires_desktop_lease(request(), "obx-x sh -c 'chrome --remote-debugging-port=9333'")
    assert server._requires_desktop_lease(request(), "obx-x xdotool key Return")


@pytest.mark.asyncio
async def test_request_id_is_echoed_on_responses():
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/alive", headers={"X-OpenBox-Request": "req-abc123"})
        assert response.status_code == 200
        assert response.headers["X-OpenBox-Request"] == "req-abc123"
        assert "browser_diag_v1" in response.json()["capabilities"]
        # Unauthenticated callers get no echo of anything but the rejection.
        denied = await client.get("/diag/browser", headers={"X-OpenBox-Request": "req-x"})
        assert denied.status_code == 403


@pytest.mark.asyncio
async def test_diag_endpoint_reports_a_missing_collector(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "SESSION_API_KEY", "k")
    monkeypatch.setattr(server, "DIAG_TOOL", tmp_path / "missing.py")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/diag/browser", headers={"X-API-Key": "k"})
    assert response.status_code == 404
    assert "runtime repair" in response.json()["detail"]


@pytest.mark.asyncio
async def test_diag_endpoint_returns_the_collectors_report(monkeypatch, tmp_path):
    tool = tmp_path / "obx_diag.py"
    tool.write_text(
        "import json,sys\n"
        "print('noise')\n"
        "print(json.dumps({'diag_version':'t','argv':sys.argv[1:],'summary':{'lights':{'chrome':'ok'}}}))\n"
    )
    monkeypatch.setattr(server, "SESSION_API_KEY", "k")
    monkeypatch.setattr(server, "DIAG_TOOL", tool)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/diag/browser", params={"session": "sess 1;rm", "lines": 999}, headers={"X-API-Key": "k"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["diag_version"] == "t"
    # Lines are clamped and the session id is sanitised before reaching argv.
    assert body["argv"] == ["--lines", "400", "--session", "sess1rm"]
