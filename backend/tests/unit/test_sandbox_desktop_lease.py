"""Sandbox requests carry trace identity and a lease across the whole turn."""

from contextlib import asynccontextmanager

import pytest

from sandbox.client import SandboxClient


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self.data


class _Http:
    def __init__(self):
        self.calls = []

    async def post(self, path, *, headers=None, json=None):
        self.calls.append((path, headers or {}, json or {}))
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
    http = _Http()

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
