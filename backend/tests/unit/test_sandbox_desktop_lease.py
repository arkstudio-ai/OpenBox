"""Sandbox requests carry trace identity and a lease across the whole turn."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from agent.driver import RunLease, bind_current_lease, reset_current_lease
from sandbox.client import SandboxClient


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class _Http:
    def __init__(self, sandbox):
        self.sandbox = sandbox
        self.calls = []

    async def post(self, path, *, headers=None, json=None):
        request = httpx.Request(
            "POST",
            f"http://sandbox.test{path}",
            headers=headers,
        )
        await self.sandbox._merge_request_headers(request)
        self.calls.append((path, request.headers, json or {}))
        if path == "/desktop/lease/acquire":
            return _Response({"token": "lease-secret", "wait_ms": 17, "ttl_seconds": 180})
        if path == "/execute":
            return _Response({"exit_code": 0, "stdout": "ok", "stderr": ""})
        if path == "/desktop/lease/release":
            return _Response({"released": True})
        raise AssertionError(path)


@pytest.mark.asyncio
async def test_desktop_lease_and_execute_share_trace_headers(monkeypatch):
    sandbox = SandboxClient("127.0.0.1", 8000, "api-key")
    http = _Http(sandbox)

    @asynccontextmanager
    async def fake_client(*_args, **_kwargs):
        yield http

    monkeypatch.setattr(sandbox, "_client", fake_client)

    async with sandbox.request_context(
        session_id="session-1", tool_call_id="part-1", operation="computer"
    ):
        async with sandbox.desktop_lease(
            session_id="session-1", tool_call_id="part-1"
        ) as lease:
            result = await sandbox.execute("xdotool key Return")

    assert lease["wait_ms"] == 17
    assert result.stdout == "ok"
    assert [call[0] for call in http.calls] == [
        "/desktop/lease/acquire", "/execute", "/desktop/lease/release"
    ]

    acquire_headers = http.calls[0][1]
    execute_headers = http.calls[1][1]
    release_headers = http.calls[2][1]
    assert acquire_headers["X-OpenBox-Instance"]
    assert acquire_headers["X-OpenBox-Session"] == "session-1"
    assert acquire_headers["X-OpenBox-Tool-Call"] == "part-1"
    assert acquire_headers["X-OpenBox-Operation"] == "computer"
    assert "X-OpenBox-Desktop-Lease" not in acquire_headers
    assert execute_headers["X-OpenBox-Desktop-Lease"] == "lease-secret"
    assert release_headers["X-OpenBox-Desktop-Lease"] == "lease-secret"


@pytest.mark.asyncio
async def test_agent_generation_is_forwarded_to_action_server(monkeypatch):
    sandbox = SandboxClient("127.0.0.1", 8000, "api-key")
    sandbox._action_server_capabilities = frozenset({"run_lease_receipt_v2"})
    http = _Http(sandbox)

    @asynccontextmanager
    async def fake_client(*_args, **_kwargs):
        yield http

    monkeypatch.setattr(sandbox, "_client", fake_client)
    lease = RunLease(
        session_id="session-fence",
        user_id="user-fence",
        run_id="run-fence",
        generation=7,
        owner_id="worker-fence",
        abort=asyncio.Event(),
        _lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    token = bind_current_lease(lease)
    try:
        await sandbox.execute("pwd")
    finally:
        reset_current_lease(token)

    headers = http.calls[0][1]
    assert headers["X-OpenBox-Session"] == "session-fence"
    assert headers["X-OpenBox-Run"] == "run-fence"
    assert headers["X-OpenBox-Run-Epoch"] == "7"
    assert "X-OpenBox-Run-Lease-Expires" in headers
    assert "X-OpenBox-Run-Lease-Signature" in headers
