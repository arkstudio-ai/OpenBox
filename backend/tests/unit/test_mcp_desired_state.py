"""Durable desired-state contracts for sandbox MCP lifecycle management."""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest


ACTION_SERVER = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
sys.modules.setdefault("psutil", types.SimpleNamespace())
if "sse_starlette.sse" not in sys.modules:
    sse_package = types.ModuleType("sse_starlette")
    sse_module = types.ModuleType("sse_starlette.sse")
    sse_module.EventSourceResponse = type("EventSourceResponse", (), {})
    sys.modules["sse_starlette"] = sse_package
    sys.modules["sse_starlette.sse"] = sse_module
_SPEC = importlib.util.spec_from_file_location(
    "openbox_mcp_desired_state_action_server_test", ACTION_SERVER
)
assert _SPEC and _SPEC.loader
action_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(action_server)


def _stored(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _server_config(*, enabled: bool | None = None) -> dict:
    config: dict = {"type": "stdio", "command": "fixture-server"}
    if enabled is not None:
        config["enabled"] = enabled
    return config


@pytest.mark.asyncio
async def test_disconnect_persists_disabled_before_forgetting_runtime(tmp_path):
    config_path = tmp_path / "mcp.json"
    manager = action_server.ContainerMcpManager(config_path)
    manager.add_server("search", _server_config())
    manager._servers["search"] = {"status": "connected", "error": None}
    manager._tools["search"] = [{"name": "query", "server": "search"}]

    await manager.disconnect("search")

    assert _stored(config_path)["servers"]["search"]["enabled"] is False
    assert manager.list_servers()[0]["enabled"] is False
    assert manager.list_servers()[0]["status"] == "disconnected"
    assert manager.get_all_tools() == []


@pytest.mark.asyncio
async def test_disconnect_write_failure_keeps_current_runtime_state(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp.json"
    manager = action_server.ContainerMcpManager(config_path)
    manager.add_server("search", _server_config())
    manager._servers["search"] = {"status": "connected", "error": None}
    manager._tools["search"] = [{"name": "query", "server": "search"}]

    def fail_save(_config):
        raise OSError("disk unavailable")

    monkeypatch.setattr(manager, "_save_config", fail_save)
    with pytest.raises(OSError, match="disk unavailable"):
        await manager.disconnect("search")

    assert manager.list_servers()[0]["status"] == "connected"
    assert manager.get_all_tools()[0]["name"] == "query"
    assert _stored(config_path)["servers"]["search"]["enabled"] is True


@pytest.mark.asyncio
async def test_startup_reconnect_skips_disabled_and_accepts_legacy_enabled(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps({
            "servers": {
                # Missing enabled is the backwards-compatible enabled state.
                "legacy": _server_config(),
                "paused": _server_config(enabled=False),
            }
        }),
        encoding="utf-8",
    )
    manager = action_server.ContainerMcpManager(config_path)
    calls: list[tuple[str, bool]] = []

    async def record_connect(name: str, *, persist_desired: bool = True) -> bool:
        calls.append((name, persist_desired))
        return True

    monkeypatch.setattr(manager, "connect", record_connect)
    await manager.reconnect_configured()

    assert calls == [("legacy", False)]
    listed = {row["name"]: row for row in manager.list_servers()}
    assert listed["legacy"]["enabled"] is True
    assert listed["paused"]["enabled"] is False


@pytest.mark.asyncio
async def test_startup_rechecks_desired_state_before_each_probe(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp.json"
    manager = action_server.ContainerMcpManager(config_path)
    manager.add_server("first", _server_config())
    manager.add_server("second", _server_config())
    calls: list[str] = []

    async def record_connect(name: str, *, persist_desired: bool = True) -> bool:
        assert persist_desired is False
        calls.append(name)
        if name == "first":
            manager._set_desired_enabled("second", False)
        return True

    monkeypatch.setattr(manager, "connect", record_connect)
    await manager.reconnect_configured()

    assert calls == ["first"]
    assert _stored(config_path)["servers"]["second"]["enabled"] is False


@pytest.mark.asyncio
async def test_disconnect_racing_startup_probe_wins(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp.json"
    manager = action_server.ContainerMcpManager(config_path)
    manager.add_server("search", _server_config())
    discovery_started = action_server.asyncio.Event()
    finish_discovery = action_server.asyncio.Event()

    @asynccontextmanager
    async def fake_session(_name):
        yield object()

    async def delayed_discovery(name, _session):
        discovery_started.set()
        await finish_discovery.wait()
        manager._tools[name] = [{"name": "stale", "server": name}]

    monkeypatch.setattr(manager, "_session", fake_session)
    monkeypatch.setattr(manager, "_discover", delayed_discovery)

    reconnect = action_server.asyncio.create_task(
        manager.connect("search", persist_desired=False)
    )
    await discovery_started.wait()
    await manager.disconnect("search")
    finish_discovery.set()

    assert await reconnect is False
    assert manager.get_all_tools() == []
    assert manager.list_servers()[0]["status"] == "disconnected"
    assert manager.list_servers()[0]["enabled"] is False


@pytest.mark.asyncio
async def test_explicit_connect_reenables_disabled_server(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp.json"
    manager = action_server.ContainerMcpManager(config_path)
    manager.add_server("search", _server_config())
    await manager.disconnect("search")

    @asynccontextmanager
    async def fake_session(_name):
        yield object()

    async def fake_discovery(name, _session):
        manager._tools[name] = [{"name": "query", "server": name}]

    monkeypatch.setattr(manager, "_session", fake_session)
    monkeypatch.setattr(manager, "_discover", fake_discovery)

    assert await manager.connect("search") is True
    assert _stored(config_path)["servers"]["search"]["enabled"] is True
    assert manager.list_servers()[0]["status"] == "connected"


def test_catalogue_projects_desired_state_without_crossing_manager_scope(tmp_path):
    enabled_path = tmp_path / "enabled.json"
    disabled_path = tmp_path / "disabled.json"
    enabled = action_server.ContainerMcpManager(enabled_path, user_scope="u-" + "a" * 20)
    disabled = action_server.ContainerMcpManager(disabled_path, user_scope="u-" + "b" * 20)
    enabled.add_server("same-name", _server_config())
    disabled.add_server("same-name", _server_config(enabled=False))

    enabled_projection = action_server._mcp_catalogue_projection(enabled)
    disabled_projection = action_server._mcp_catalogue_projection(disabled)

    assert enabled_projection["servers"][0]["enabled"] is True
    assert disabled_projection["servers"][0]["enabled"] is False
    assert enabled.config_path != disabled.config_path


@pytest.mark.asyncio
async def test_raw_initialize_notification_keeps_auth_and_negotiated_session_headers():
    calls: list[dict] = []

    class Response:
        def __init__(self, *, notification: bool):
            self.status_code = 204 if notification else 200
            self.headers = {} if notification else {
                "content-type": "application/json",
                "mcp-session-id": "session-1",
            }
            self.text = ""

        def raise_for_status(self):
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": 1, "result": {}}

    class Client:
        async def post(self, _url, *, json, headers):
            calls.append({"payload": json, "headers": dict(headers)})
            return Response(notification="id" not in json)

    session = action_server.RawStreamableHttpSession(
        "https://mcp.example.test",
        headers={
            "Authorization": "Bearer test-token",
            "X-API-Key": "test-key",
        },
    )
    session._client = Client()

    await session.initialize()

    assert [call["payload"]["method"] for call in calls] == [
        "initialize",
        "notifications/initialized",
    ]
    assert all(
        call["headers"]["Authorization"] == "Bearer test-token"
        and call["headers"]["X-API-Key"] == "test-key"
        for call in calls
    )
    assert calls[1]["headers"]["Mcp-Session-Id"] == "session-1"
