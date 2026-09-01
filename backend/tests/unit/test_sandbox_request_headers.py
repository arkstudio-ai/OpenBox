"""Every Action Server request carries the current protected transport identity."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import io
import zipfile

import httpx
import pytest

from agent.driver import RunLease, bind_current_lease, reset_current_lease
from sandbox.client import PathResolveTarget, SandboxClient


USER_SCOPE = "u-" + "a" * 20
CAPABILITIES = [
    "run_lease_receipt_v2",
    "tenant_catalogue_scopes_v1",
    "confined_file_delete_v1",
    "confined_path_resolve_v1",
    "sensitive_search_filter_v1",
    "skill_archive_create_only_v1",
    "skill_restore_fence_v1",
]


def _valid_skill_zip(name: str = "matrix-skill") -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: matrix\n---\nbody\n",
        )
    return archive.getvalue()


def _lease(*, generation: int = 17) -> RunLease:
    return RunLease(
        session_id="session-matrix",
        user_id="user-matrix",
        run_id=f"run-{generation}",
        generation=generation,
        owner_id="worker-matrix",
        abort=asyncio.Event(),
        _lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )


def _install_recording_transport(monkeypatch):
    calls: list[dict] = []
    skill_archive = _valid_skill_zip()

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append({
            "method": request.method,
            "path": request.url.path,
            "headers": {name.lower(): value for name, value in request.headers.items()},
        })
        path = request.url.path
        if path == "/alive":
            return httpx.Response(200, json={"capabilities": CAPABILITIES})
        if path == "/catalog":
            if request.headers.get("if-none-match") == '"matrix-etag"':
                return httpx.Response(304, headers={"ETag": '"matrix-etag"'})
            return httpx.Response(200, headers={"ETag": '"matrix-etag"'}, json={
                "catalogue_version": 1,
                "boot_id": "matrix-boot",
                "started_at": 1.0,
                "skills_generation": "skills-1",
                "mcp_generation": "mcp-1",
                "generation": "matrix-1",
                "counts": {
                    "skills": 0,
                    "mcp_servers": 0,
                    "mcp_tools": 0,
                    "mcp_resources": 0,
                },
                "skills": [],
                "mcp_servers": [],
                "mcp_tools": [],
                "mcp_resources": [],
            })
        if path == "/read_file":
            return httpx.Response(200, json={"content": "content"})
        if path == "/download":
            return httpx.Response(200, content=b"frame")
        if path == "/resolve_paths":
            return httpx.Response(200, json={
                "targets": [{
                    "canonical_path": "/workspace/project/file.txt",
                    "workspace_relative": "project/file.txt",
                }],
            })
        if path == "/glob":
            return httpx.Response(200, json={"files": ["file.txt"]})
        if path == "/grep":
            return httpx.Response(200, json={"output": "file.txt:1:value"})
        if path == "/list_files":
            return httpx.Response(200, json={"entries": []})
        if path == "/skills/matrix-skill/archive":
            return httpx.Response(200, content=skill_archive)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handle)
    real_async_client = httpx.AsyncClient

    def use_recording_transport(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    # Patch the constructor used by SandboxClient while retaining the real
    # implementation, including its request-event-hook behavior.
    monkeypatch.setattr(httpx, "AsyncClient", use_recording_transport)
    return calls, skill_archive


@pytest.mark.asyncio
async def test_agent_run_headers_cover_every_action_server_request_family(monkeypatch):
    calls, skill_archive = _install_recording_transport(monkeypatch)
    sandbox = SandboxClient(
        "sandbox",
        8000,
        "matrix-key",
        user_scope=USER_SCOPE,
        catalogue_ttl_seconds=0.0,
    )
    lease_token = bind_current_lease(_lease())
    try:
        async with sandbox.request_context(
            session_id="session-matrix",
            tool_call_id="tool-matrix",
            operation="matrix-operation",
        ):
            await sandbox.read_file("/workspace/project/file.txt")
            await sandbox.write_file("/workspace/project/file.txt", "content")
            assert await sandbox.download_file_bytes(
                "/workspace/project/frame.png", max_bytes=32
            ) == b"frame"
            await sandbox.delete_file("/workspace/project/file.txt")
            await sandbox.resolve_paths([
                PathResolveTarget("/workspace/project/file.txt", allow_missing=True),
            ])
            await sandbox.glob("*.txt", "/workspace/project")
            await sandbox.grep("value", "/workspace/project")
            await sandbox.list_files("/workspace/project")
            await sandbox.kill_command(123)

            await sandbox._get("/generic-get")
            await sandbox._post(
                "/generic-post",
                json={"ok": True},
                headers={
                    "X-Business-Header": "preserved",
                    "X-API-Key": "must-not-win",
                    "X-OpenBox-User-Scope": "u-" + "b" * 20,
                    "X-OpenBox-Session": "wrong-session",
                    "X-OpenBox-Run": "wrong-run",
                    "X-OpenBox-Run-Epoch": "999",
                    "X-OpenBox-Run-Lease-Expires": "9999999999999",
                    "X-OpenBox-Run-Lease-Signature": "0" * 64,
                },
            )
            await sandbox._delete("/generic-delete")

            await sandbox.get_catalogue_projection()
            await sandbox.get_catalogue_projection()

            await sandbox.start_dev_browser()
            await sandbox.stop_dev_browser()
            await sandbox.get_dev_browser_status()

            await sandbox.download_skill_archive("matrix-skill")
            await sandbox.export_skill_archive("matrix-skill")
            await sandbox.install_skill(name="matrix-skill", content="body")
            await sandbox.upload_skill_archive(
                skill_archive,
                "matrix-skill.zip",
                "matrix-skill",
                create_only=True,
                restore_generation=2,
            )
            await sandbox.uninstall_skill("matrix-skill", mutation_generation=3)

            await sandbox.add_mcp_server(
                "server", {"type": "stdio", "command": "true"}
            )
            await sandbox.remove_mcp_server("server")
            await sandbox.connect_mcp("server")
            await sandbox.disconnect_mcp("server")
            await sandbox.refresh_mcp_server("server")
            await sandbox.call_mcp_tool("server", "mutate", {"value": 1})
    finally:
        reset_current_lease(lease_token)

    required = {
        ("POST", "/read_file"),
        ("POST", "/write_file"),
        ("GET", "/download"),
        ("POST", "/delete_file"),
        ("POST", "/resolve_paths"),
        ("POST", "/glob"),
        ("POST", "/grep"),
        ("POST", "/list_files"),
        ("POST", "/kill"),
        ("GET", "/generic-get"),
        ("POST", "/generic-post"),
        ("DELETE", "/generic-delete"),
        ("GET", "/catalog"),
        ("POST", "/dev-browser/start"),
        ("POST", "/dev-browser/stop"),
        ("GET", "/dev-browser/status"),
        ("GET", "/skills/matrix-skill/archive"),
        ("POST", "/skills/matrix-skill/export"),
        ("POST", "/skills/install"),
        ("POST", "/skills/upload"),
        ("DELETE", "/skills/matrix-skill"),
        ("POST", "/mcp/servers"),
        ("DELETE", "/mcp/servers/server"),
        ("POST", "/mcp/servers/server/connect"),
        ("POST", "/mcp/servers/server/disconnect"),
        ("POST", "/mcp/servers/server/refresh"),
        ("POST", "/mcp/tools/server/mutate"),
    }
    observed = {(call["method"], call["path"]) for call in calls}
    assert required <= observed

    request_ids = set()
    for call in calls:
        headers = call["headers"]
        assert headers["x-api-key"] == "matrix-key"
        assert headers["x-openbox-user-scope"] == USER_SCOPE
        assert headers["x-openbox-session"] == "session-matrix"
        assert headers["x-openbox-run"] == "run-17"
        assert headers["x-openbox-run-epoch"] == "17"
        expires_at_ms = int(headers["x-openbox-run-lease-expires"])
        receipt_payload = (
            f"session-matrix\nrun-17\n17\n{expires_at_ms}"
        ).encode("utf-8")
        assert headers["x-openbox-run-lease-signature"] == hmac.new(
            b"matrix-key", receipt_payload, hashlib.sha256
        ).hexdigest()
        assert headers["x-openbox-tool-call"] == "tool-matrix"
        assert headers["x-openbox-operation"] == "matrix-operation"
        assert headers["x-openbox-instance"]
        assert headers["x-openbox-request"]
        request_ids.add(headers["x-openbox-request"])
    assert len(request_ids) == len(calls)

    generic_post = next(call for call in calls if call["path"] == "/generic-post")
    assert generic_post["headers"]["x-business-header"] == "preserved"
    skill_upload = next(call for call in calls if call["path"] == "/skills/upload")
    assert skill_upload["headers"]["content-type"].startswith("multipart/form-data;")
    conditional_catalogue = next(
        call
        for call in calls
        if call["path"] == "/catalog" and "if-none-match" in call["headers"]
    )
    assert conditional_catalogue["headers"]["if-none-match"] == '"matrix-etag"'


@pytest.mark.asyncio
async def test_reused_http_client_reads_fence_per_request_and_keeps_control_plane_compatible(
    monkeypatch,
):
    calls, _archive = _install_recording_transport(monkeypatch)
    sandbox = SandboxClient(
        "sandbox",
        8000,
        "matrix-key",
        user_scope=USER_SCOPE,
    )
    sandbox._action_server_capabilities = frozenset(CAPABILITIES)

    async with sandbox._client() as http:
        await http.post(
            "/control-before",
            headers={
                "X-Business-Header": "preserved",
                "X-API-Key": "stale-key",
                "X-OpenBox-User-Scope": "u-" + "b" * 20,
                "X-OpenBox-Instance": "stale-instance",
                "X-OpenBox-Request": "stale-request",
                "X-OpenBox-Session": "stale-session",
                "X-OpenBox-Tool-Call": "stale-tool",
                "X-OpenBox-Operation": "stale-operation",
                "X-OpenBox-Desktop-Lease": "stale-desktop-lease",
                "X-OpenBox-Run": "stale-run",
                "X-OpenBox-Run-Epoch": "99",
                "X-OpenBox-Run-Lease-Expires": "9999999999999",
                "X-OpenBox-Run-Lease-Signature": "0" * 64,
            },
        )

        lease_token = bind_current_lease(_lease(generation=23))
        try:
            async with sandbox.request_context(
                session_id="trace-session-must-not-win",
                tool_call_id="tool-dynamic",
                operation="dynamic",
            ):
                await http.post("/agent-during")
        finally:
            reset_current_lease(lease_token)

        await http.post("/control-after")

    before, during, after = calls
    for control in (before, after):
        headers = control["headers"]
        assert headers["x-api-key"] == "matrix-key"
        assert headers["x-openbox-user-scope"] == USER_SCOPE
        assert "x-openbox-session" not in headers
        assert "x-openbox-tool-call" not in headers
        assert "x-openbox-operation" not in headers
        assert "x-openbox-desktop-lease" not in headers
        assert "x-openbox-run" not in headers
        assert "x-openbox-run-epoch" not in headers
        assert "x-openbox-run-lease-expires" not in headers
        assert "x-openbox-run-lease-signature" not in headers
    assert before["headers"]["x-business-header"] == "preserved"
    assert before["headers"]["x-openbox-instance"] != "stale-instance"
    assert before["headers"]["x-openbox-request"] != "stale-request"

    headers = during["headers"]
    assert headers["x-openbox-session"] == "session-matrix"
    assert headers["x-openbox-run"] == "run-23"
    assert headers["x-openbox-run-epoch"] == "23"
    assert "x-openbox-run-lease-expires" in headers
    assert "x-openbox-run-lease-signature" in headers


@pytest.mark.asyncio
async def test_run_fence_lookup_failure_stops_transport(monkeypatch):
    calls, _archive = _install_recording_transport(monkeypatch)
    sandbox = SandboxClient("sandbox", 8000, "matrix-key")

    import agent.driver as driver

    def broken_fence_lookup():
        raise RuntimeError("fence context unavailable")

    monkeypatch.setattr(driver, "current_run_transport_lease", broken_fence_lookup)
    with pytest.raises(RuntimeError, match="fence context unavailable"):
        await sandbox.write_file("/workspace/file.txt", "must-not-send")
    assert calls == []


@pytest.mark.asyncio
async def test_revoked_run_lease_stops_transport_before_request(monkeypatch):
    calls, _archive = _install_recording_transport(monkeypatch)
    sandbox = SandboxClient("sandbox", 8000, "matrix-key")
    lease = _lease()
    token = bind_current_lease(lease)
    try:
        lease._transport_revoked = True
        with pytest.raises(RuntimeError, match="transport lease unavailable"):
            await sandbox.write_file("/workspace/file.txt", "must-not-send")
    finally:
        reset_current_lease(token)
    assert calls == []


@pytest.mark.asyncio
async def test_old_action_server_is_rejected_before_agent_side_effect(monkeypatch):
    calls, _archive = _install_recording_transport(monkeypatch)
    sandbox = SandboxClient("sandbox", 8000, "matrix-key")
    # Simulate a successful capability probe against a pre-receipt server.
    sandbox._action_server_capabilities = frozenset({"run_fencing_v1"})
    token = bind_current_lease(_lease())
    try:
        with pytest.raises(RuntimeError, match="signed Agent run lease receipts"):
            await sandbox.write_file("/workspace/file.txt", "must-not-send")
    finally:
        reset_current_lease(token)
    assert calls == []
