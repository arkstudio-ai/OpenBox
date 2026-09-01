"""Persistent MCP owner, catalogue generation, and lifecycle contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


ACTION_SERVER = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
sys.modules.setdefault("psutil", types.SimpleNamespace())
if "sse_starlette.sse" not in sys.modules:
    package = types.ModuleType("sse_starlette")
    module = types.ModuleType("sse_starlette.sse")
    module.EventSourceResponse = type("EventSourceResponse", (), {})
    sys.modules["sse_starlette"] = package
    sys.modules["sse_starlette.sse"] = module
_SPEC = importlib.util.spec_from_file_location(
    "openbox_mcp_supervisor_action_server_test", ACTION_SERVER
)
assert _SPEC and _SPEC.loader
action_server = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = action_server
_SPEC.loader.exec_module(action_server)


def _tool(name: str):
    return SimpleNamespace(name=name, description=name, input_schema={"type": "object"})


class StatefulSession:
    def __init__(self):
        self._openbox_server_capabilities = {
            "tools": {"listChanged": True},
            "resources": {"listChanged": True},
            "prompts": {"listChanged": True},
        }
        self._openbox_notification_handler_registered = True
        self.version = "old"
        self.block_resources = False
        self.fail_resources = False
        self.resources_started = asyncio.Event()
        self.resources_release = asyncio.Event()
        self.task_ids: list[int] = []

    def _record(self):
        self.task_ids.append(id(asyncio.current_task()))

    async def list_tools(self, cursor=None):
        self._record()
        return SimpleNamespace(tools=[_tool(f"tool-{self.version}")], next_cursor=None)

    async def list_resources(self, cursor=None):
        self._record()
        if self.block_resources:
            self.resources_started.set()
            await self.resources_release.wait()
        if self.fail_resources:
            raise RuntimeError("resource discovery failed")
        resource = SimpleNamespace(
            uri=f"resource://{self.version}",
            name=f"resource-{self.version}",
            description="",
            mime_type="text/plain",
        )
        return SimpleNamespace(resources=[resource], next_cursor=None)

    async def list_prompts(self, cursor=None):
        self._record()
        prompt = SimpleNamespace(
            name=f"prompt-{self.version}", description="", arguments=[]
        )
        return SimpleNamespace(prompts=[prompt], next_cursor=None)

    async def call_tool(self, name, arguments):
        self._record()
        return SimpleNamespace(
            content=[SimpleNamespace(text=f"{name}:{arguments['value']}")],
            is_error=False,
        )


def _manager(tmp_path, **kwargs):
    manager = action_server.ContainerMcpManager(
        tmp_path / "mcp.json",
        owner_tick_seconds=0.01,
        notification_poll_seconds=60,
        backoff_jitter_ratio=0,
        **kwargs,
    )
    manager.add_server("srv", {"type": "stdio", "command": "fixture", "timeout": 5})
    return manager


@pytest.mark.asyncio
async def test_same_task_timeout_supports_python_310_without_spawning_task(
    monkeypatch,
):
    class LegacyAsyncioTimeoutError(Exception):
        pass

    monkeypatch.delattr(action_server.asyncio, "timeout", raising=False)
    monkeypatch.setattr(
        action_server.asyncio, "TimeoutError", LegacyAsyncioTimeoutError
    )
    owner_task = id(asyncio.current_task())
    observed: list[int] = []

    async with action_server._same_task_timeout(1):
        observed.append(id(asyncio.current_task()))

    assert observed == [owner_task]
    with pytest.raises(LegacyAsyncioTimeoutError):
        async with action_server._same_task_timeout(0.001):
            await asyncio.sleep(1)


@pytest.mark.asyncio
async def test_one_owner_keeps_one_session_for_many_calls_and_exits_same_task(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    session = StatefulSession()
    lifecycle: list[tuple[str, int]] = []

    @asynccontextmanager
    async def fake_session(_name, notification_handler=None):
        lifecycle.append(("enter", id(asyncio.current_task())))
        session._record()  # initialize/connect runs in the owner too
        try:
            yield session
        finally:
            lifecycle.append(("exit", id(asyncio.current_task())))

    monkeypatch.setattr(manager, "_session", fake_session)
    assert await manager.connect("srv") is True
    first = await manager.call_tool("srv", "echo", {"value": 1})
    second = await manager.call_tool("srv", "echo", {"value": 2})
    await manager.refresh_server("srv")
    await manager.disconnect("srv")

    assert first["content"][0]["text"] == "echo:1"
    assert second["content"][0]["text"] == "echo:2"
    assert [event for event, _ in lifecycle] == ["enter", "exit"]
    task_ids = [task_id for _, task_id in lifecycle] + session.task_ids
    assert len(set(task_ids)) == 1
    assert manager._owners == {}


@pytest.mark.asyncio
async def test_discovery_exhausts_pagination_before_single_publish(tmp_path, monkeypatch):
    manager = _manager(tmp_path)
    session = StatefulSession()
    calls: list[str | None] = []

    async def paged_tools(cursor=None):
        calls.append(cursor)
        if cursor is None:
            return SimpleNamespace(tools=[_tool("first")], next_cursor="page-2")
        return SimpleNamespace(tools=[_tool("second")], next_cursor=None)

    session.list_tools = paged_tools

    @asynccontextmanager
    async def fake_session(_name, notification_handler=None):
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)
    assert await manager.connect("srv") is True
    assert calls == [None, "page-2"]
    assert [tool["name"] for tool in manager.get_all_tools()] == ["first", "second"]
    assert manager.catalogue_revision == 2  # add_server + one full publication
    await manager.shutdown()


@pytest.mark.asyncio
async def test_list_changed_publishes_one_atomic_generation_and_failed_refresh_keeps_lkg(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    session = StatefulSession()
    notification = None

    @asynccontextmanager
    async def fake_session(_name, notification_handler=None):
        nonlocal notification
        notification = notification_handler
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)
    await manager.connect("srv")
    before = manager._catalogue_state["srv"]
    before_revision = manager.catalogue_revision

    session.version = "new"
    session.block_resources = True
    notification("notifications/tools/list_changed")
    await asyncio.wait_for(session.resources_started.wait(), timeout=1)
    during = manager._catalogue_state["srv"]
    assert during.generation == before.generation
    assert during.tools[0]["name"] == "tool-old"
    assert during.resources[0]["uri"] == "resource://old"

    session.resources_release.set()
    for _ in range(100):
        if manager.catalogue_revision > before_revision:
            break
        await asyncio.sleep(0.01)
    after = manager._catalogue_state["srv"]
    assert manager.catalogue_revision == before_revision + 1
    assert after.tools[0]["name"] == "tool-new"
    assert after.resources[0]["uri"] == "resource://new"
    assert after.prompts[0]["name"] == "prompt-new"

    stable_generation = after.generation
    stable_revision = manager.catalogue_revision
    session.block_resources = False
    session.fail_resources = True
    with pytest.raises(RuntimeError, match="resource discovery failed"):
        await manager.refresh_server("srv")
    assert manager._catalogue_state["srv"].generation == stable_generation
    assert manager.catalogue_revision == stable_revision
    assert manager.list_servers()[0]["refresh_error"] == "resource discovery failed"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_slow_refresh_disconnect_cannot_publish_or_leave_an_owner(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    session = StatefulSession()

    @asynccontextmanager
    async def fake_session(_name, notification_handler=None):
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)
    await manager.connect("srv")
    session.version = "stale"
    session.block_resources = True
    refresh = asyncio.create_task(manager.refresh_server("srv"))
    await asyncio.wait_for(session.resources_started.wait(), timeout=1)
    await manager.disconnect("srv")
    with pytest.raises(RuntimeError, match="supervisor stopped"):
        await refresh
    session.resources_release.set()
    assert manager._owners == {}
    assert manager.get_all_tools() == []
    assert manager.list_servers()[0]["enabled"] is False


@pytest.mark.asyncio
async def test_reconnect_budget_withdraws_unexecutable_last_known_good(
    tmp_path, monkeypatch
):
    manager = _manager(
        tmp_path,
        reconnect_failure_budget=3,
        backoff_initial_seconds=0,
        backoff_max_seconds=0,
    )
    manager._tools["srv"] = [{"name": "lkg", "server": "srv"}]
    revision_before_exhaustion = manager.catalogue_revision
    attempts = 0

    @asynccontextmanager
    async def failing_session(_name, notification_handler=None):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("offline")
        yield

    monkeypatch.setattr(manager, "_session", failing_session)
    with pytest.raises(RuntimeError, match="offline"):
        await manager.connect("srv")
    owner = manager._owners["srv"]
    await asyncio.wait_for(owner.task, timeout=1)
    state = manager.list_servers()[0]
    assert attempts == 3
    assert state["reconnect_exhausted"] is True
    assert state["consecutive_failures"] == 3
    # Keep the generation for diagnostics/recovery, but never advertise a tool
    # whose stopped owner would reject every call.
    assert state["last_known_good"] is True
    assert state["tools"] == []
    assert manager._catalogue_state["srv"].tools[0]["name"] == "lkg"
    assert manager.get_all_tools() == []
    assert manager.catalogue_revision == revision_before_exhaustion + 1

    recovered = StatefulSession()

    @asynccontextmanager
    async def recovered_session(_name, notification_handler=None):
        yield recovered

    monkeypatch.setattr(manager, "_session", recovered_session)
    assert await manager.connect("srv") is True
    assert manager.get_all_tools()[0]["name"] == "tool-old"
    assert manager.list_servers()[0]["reconnect_exhausted"] is False
    await manager.shutdown()


@pytest.mark.asyncio
async def test_shutdown_disposes_runtime_projection_and_can_reconnect(
    tmp_path, monkeypatch
):
    manager = _manager(tmp_path)
    session = StatefulSession()

    @asynccontextmanager
    async def fake_session(_name, notification_handler=None):
        yield session

    monkeypatch.setattr(manager, "_session", fake_session)
    assert await manager.connect("srv") is True
    assert manager.get_all_tools()
    revision = manager.catalogue_revision

    await manager.shutdown()
    assert manager._owners == {}
    assert manager._catalogue_state == {}
    assert manager._servers == {}
    assert manager._remote_transport == {}
    assert manager.get_all_tools() == []
    assert manager.catalogue_revision == revision + 1

    assert await manager.connect("srv") is True
    assert manager.get_all_tools()[0]["name"] == "tool-old"
    await manager.shutdown()


@pytest.mark.asyncio
async def test_alive_advertises_v10_supervisor_without_dropping_v9_capabilities():
    payload = await action_server.alive()
    assert payload["version"] == "2026.08.31-run-lease-receipt-v12"
    assert "mcp_supervisor_v1" in payload["capabilities"]
    assert "run_lease_receipt_v2" in payload["capabilities"]
    assert "skill_archive_bounded_v1" in payload["capabilities"]
    assert "mcp_desired_state_v1" in payload["capabilities"]


@pytest.mark.asyncio
async def test_raw_notification_get_keeps_auth_and_negotiated_session_header():
    seen: dict = {}
    task_ids: list[int] = []

    class Response:
        headers = {"content-type": "text/event-stream"}

        def raise_for_status(self):
            return None

        async def aiter_lines(self):
            task_ids.append(id(asyncio.current_task()))
            yield "data: " + json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/tools/list_changed",
            })
            yield ""
            await asyncio.Event().wait()

    class Stream:
        async def __aenter__(self):
            task_ids.append(id(asyncio.current_task()))
            return Response()

        async def __aexit__(self, *_args):
            task_ids.append(id(asyncio.current_task()))
            return None

    class Client:
        def stream(self, method, _url, *, headers):
            task_ids.append(id(asyncio.current_task()))
            seen.update({"method": method, "headers": dict(headers)})
            return Stream()

        async def aclose(self):
            task_ids.append(id(asyncio.current_task()))
            return None

    changed = asyncio.Event()
    session = action_server.RawStreamableHttpSession(
        "https://mcp.example.test",
        headers={"Authorization": "Bearer token", "X-API-Key": "key"},
        notification_handler=lambda _method: changed.set(),
    )
    session._client = Client()
    session._session_id = "session-1"
    session._server_info = {
        "capabilities": {"tools": {"listChanged": True}}
    }
    assert await session.start_notification_receiver() is True
    assert await session.receive_notification(0.1) is True
    assert await session.receive_notification(0.1) is True
    await asyncio.wait_for(changed.wait(), timeout=1)
    assert seen["method"] == "GET"
    assert seen["headers"]["Authorization"] == "Bearer token"
    assert seen["headers"]["X-API-Key"] == "key"
    assert seen["headers"]["Mcp-Session-Id"] == "session-1"
    await session.close()
    assert len(set(task_ids)) == 1
