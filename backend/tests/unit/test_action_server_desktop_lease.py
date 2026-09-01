"""Remote Action Server lease enforcement for a shared physical desktop."""

import asyncio
import importlib.util
from pathlib import Path
import sys
import time
import types
from types import SimpleNamespace

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


class FakeWebSocket:
    def __init__(self):
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self):
        self.accepted = True

    async def close(self, *, code: int, reason: str):
        self.closed = (code, reason)


@pytest.fixture(autouse=True)
def clear_lease():
    server._desktop_lease = None
    yield
    server._desktop_lease = None


@pytest.mark.asyncio
async def test_action_server_refuses_to_start_without_api_key(monkeypatch):
    monkeypatch.setattr(server, "SESSION_API_KEY", "")
    with pytest.raises(RuntimeError, match="SESSION_API_KEY is required"):
        async with server.lifespan(server.app):
            pass


@pytest.mark.asyncio
async def test_empty_api_key_fails_closed_for_http_and_websockets(monkeypatch):
    monkeypatch.setattr(server, "SESSION_API_KEY", "")
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://action.test"
    ) as client:
        alive = await client.get("/alive")
        denied = await client.get("/catalog")

    terminal_ws = FakeWebSocket()
    await server.terminal_ws(terminal_ws, api_key="")
    browser_ws = FakeWebSocket()
    await server.dev_browser_ws(browser_ws, api_key="")

    assert alive.status_code == 200
    assert denied.status_code == 503
    assert terminal_ws.closed == (
        4003,
        "Action Server API key is not configured",
    )
    assert browser_ws.closed == (
        4003,
        "Action Server API key is not configured",
    )


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
    assert (
        server._desktop_command_kind(
            "obx-file put /tmp/obx-sandbox-screen.png https://oss.invalid"
        )
        == "desktop_oss_upload"
    )
    assert server._desktop_command_kind("obx-x google-chrome about:blank") == "desktop_session"
    assert server._desktop_command_kind("echo hello") == "shell"


def test_workspace_paths_reject_parent_and_symlink_escapes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (workspace / "project").mkdir()
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", workspace.resolve())

    assert server._workspace_path("project") == (workspace / "project").resolve()
    for candidate in (str(outside), "../outside", str(workspace / "escape" / "secret.txt")):
        with pytest.raises(HTTPException) as error:
            server._workspace_path(candidate)
        assert error.value.status_code == 403


def test_runner_environment_is_an_explicit_secret_free_allowlist(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_runner_account", lambda: None)
    monkeypatch.setattr(server, "RUNNER_HOME", tmp_path / "runner-home")
    monkeypatch.setenv("SESSION_API_KEY", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("DATABASE_URL", "must-not-leak")

    env = server._runner_env()

    assert env["PATH"]
    assert "SESSION_API_KEY" not in env
    assert "OPENAI_API_KEY" not in env
    assert "DATABASE_URL" not in env


def test_terminal_workspace_is_tenant_confined_and_unicode_safe(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    scope = "u-" + "a" * 20
    project = workspace / "openbox" / "users" / scope / "projects" / "项目"
    other = workspace / "openbox" / "users" / ("u-" + "b" * 20) / "projects" / "other"
    project.mkdir(parents=True)
    other.mkdir(parents=True)
    monkeypatch.setattr(server, "WORKSPACE_ROOT", workspace.resolve())

    assert server._terminal_workspace_path(str(project), scope) == project.resolve()
    with pytest.raises(HTTPException) as cross_tenant:
        server._terminal_workspace_path(str(other), scope)
    assert cross_tenant.value.status_code == 403
    with pytest.raises(HTTPException) as invalid_scope:
        server._terminal_workspace_path(str(project), "alice")
    assert invalid_scope.value.status_code == 400


def test_terminal_workspace_creates_a_missing_owned_project(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    scope = "u-" + "c" * 20
    project = workspace / "openbox" / "users" / scope / "projects" / "新项目"
    monkeypatch.setattr(server, "WORKSPACE_ROOT", workspace.resolve())
    monkeypatch.setattr(server, "_runner_account", lambda: None)

    resolved = server._terminal_workspace_path(str(project), scope)

    assert resolved == project.resolve()
    assert project.is_dir()
    assert (workspace / "openbox" / "users" / scope / ".openbox" / "home").is_dir()


def test_terminal_runner_locale_defaults_to_utf8(monkeypatch):
    monkeypatch.setattr(server, "_runner_account", lambda: None)
    monkeypatch.setenv("LANG", "")
    monkeypatch.setenv("LC_ALL", "")

    env = server._runner_env(user_scope="u-" + "a" * 20)

    assert env["LANG"] == "C.UTF-8"
    assert env["LC_ALL"] == "C.UTF-8"


def test_runner_command_uses_setpriv_when_server_is_root(monkeypatch):
    account = SimpleNamespace(pw_uid=2345, pw_gid=3456, pw_name="sandbox")
    monkeypatch.setattr(server, "_runner_account", lambda: account)
    monkeypatch.setattr(server.os, "geteuid", lambda: 0)
    monkeypatch.setattr(server.shutil, "which", lambda name: "/usr/bin/setpriv")

    argv = server._runner_argv(["bash", "-lc", "id -u"])

    assert argv[:5] == [
        "/usr/bin/setpriv",
        "--reuid=2345",
        "--regid=3456",
        "--init-groups",
        "--no-new-privs",
    ]
    assert argv[-3:] == ["bash", "-lc", "id -u"]


def test_runner_shell_skips_desktop_login_profile(monkeypatch):
    monkeypatch.setattr(server, "_runner_argv", lambda argv: ["runner", *argv])

    argv = server._runner_shell_argv("printf '中文\\n'")

    assert argv == [
        "runner",
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        "printf '中文\\n'",
    ]


@pytest.mark.asyncio
async def test_canceled_stream_terminates_and_reaps_process(monkeypatch):
    killed = []

    class Process:
        returncode = None

        async def wait(self):
            self.returncode = -9
            return self.returncode

    process = Process()
    monkeypatch.setattr(server, "_kill_process_tree", lambda item: killed.append(item))

    assert await server._terminate_process_tree(process) is True
    assert killed == [process]
    assert process.returncode == -9
    assert await server._terminate_process_tree(process) is False


@pytest.mark.asyncio
async def test_run_epoch_fence_survives_restart_and_rejects_stale_worker(
    tmp_path, monkeypatch
):
    fence_path = tmp_path / "run-fences.json"
    monkeypatch.setattr(server, "SESSION_API_KEY", "run-fence-test-key")
    monkeypatch.setattr(server, "_RUN_FENCE_PATH", fence_path)
    monkeypatch.setattr(server, "_run_fences", {})

    expires_at_ms = int(time.time() * 1000) + 30_000

    def fenced(run_id: str, epoch: int) -> Request:
        return request(**{
            "X-OpenBox-Session": "session-fenced",
            "X-OpenBox-Run": run_id,
            "X-OpenBox-Run-Epoch": str(epoch),
            "X-OpenBox-Run-Lease-Expires": str(expires_at_ms),
            "X-OpenBox-Run-Lease-Signature": server._run_lease_signature(
                "session-fenced", run_id, epoch, expires_at_ms
            ),
        })

    first = fenced("run-one", 1)
    second = fenced("run-two", 2)
    await server._validate_run_fence(first)
    await server._validate_run_fence(second)

    # Simulate an Action Server restart: the durable high-water mark is the
    # authority, not the old module's dictionary identity.
    monkeypatch.setattr(server, "_run_fences", server._load_run_fences())
    with pytest.raises(HTTPException) as stale:
        await server._validate_run_fence(first)
    assert stale.value.status_code == 409

    same_epoch_other_run = fenced("run-imposter", 2)
    with pytest.raises(HTTPException) as imposter:
        await server._validate_run_fence(same_epoch_other_run)
    assert imposter.value.status_code == 409


@pytest.mark.asyncio
async def test_run_fence_requires_a_valid_unexpired_database_lease_receipt(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(server, "SESSION_API_KEY", "run-receipt-test-key")
    monkeypatch.setattr(server, "_RUN_FENCE_PATH", tmp_path / "run-fences.json")
    monkeypatch.setattr(server, "_run_fences", {})
    now_ms = int(time.time() * 1000)

    def fenced(expires_at_ms: int, *, signature: str | None = None) -> Request:
        return request(**{
            "X-OpenBox-Session": "session-receipt",
            "X-OpenBox-Run": "run-receipt",
            "X-OpenBox-Run-Epoch": "7",
            "X-OpenBox-Run-Lease-Expires": str(expires_at_ms),
            "X-OpenBox-Run-Lease-Signature": signature or server._run_lease_signature(
                "session-receipt", "run-receipt", 7, expires_at_ms
            ),
        })

    incomplete = request(**{
        "X-OpenBox-Session": "session-receipt",
        "X-OpenBox-Run": "run-receipt",
        "X-OpenBox-Run-Epoch": "7",
    })
    with pytest.raises(HTTPException) as missing:
        await server._validate_run_fence(incomplete)
    assert missing.value.status_code == 400

    with pytest.raises(HTTPException) as forged:
        await server._validate_run_fence(
            fenced(now_ms + 30_000, signature="0" * 64)
        )
    assert forged.value.status_code == 403

    with pytest.raises(HTTPException) as expired:
        await server._validate_run_fence(fenced(now_ms - 1))
    assert expired.value.status_code == 409

    with pytest.raises(HTTPException) as excessive:
        await server._validate_run_fence(
            fenced(now_ms + server._RUN_LEASE_MAX_FUTURE_MS + 1_000)
        )
    assert excessive.value.status_code == 400

    await server._validate_run_fence(fenced(now_ms + 30_000))
    assert server._run_fences["session-receipt"] == {
        "epoch": 7,
        "run_id": "run-receipt",
    }


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
