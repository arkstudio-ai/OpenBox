"""WUYING catalogue projection, conditional transfer and client cache contracts."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from sandbox.client import SandboxClient


ACTION_SERVER = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
sys.modules.setdefault("psutil", types.SimpleNamespace())
if "sse_starlette.sse" not in sys.modules:
    sse_package = types.ModuleType("sse_starlette")
    sse_module = types.ModuleType("sse_starlette.sse")
    sse_module.EventSourceResponse = type("EventSourceResponse", (), {})
    sys.modules["sse_starlette"] = sse_package
    sys.modules["sse_starlette.sse"] = sse_module
_SPEC = importlib.util.spec_from_file_location(
    "openbox_catalogue_action_server_test", ACTION_SERVER
)
assert _SPEC and _SPEC.loader
action_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(action_server)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _request(etag: str = "") -> Request:
    headers = []
    if etag:
        headers.append((b"if-none-match", etag.encode("ascii")))
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/catalog",
        "headers": headers,
    })


def _payload(generation: str = "g1", tool_name: str = "lookup") -> dict:
    return {
        "catalogue_version": 1,
        "boot_id": "boot-a",
        "started_at": 1.0,
        "skills_generation": f"skills-{generation}",
        "mcp_generation": f"mcp-{generation}",
        "generation": generation,
        "counts": {
            "skills": 1,
            "mcp_servers": 1,
            "mcp_tools": 1,
            "mcp_resources": 1,
        },
        "skills": [{"name": "skill-a", "description": "short"}],
        "mcp_servers": [{"name": "srv", "status": "connected"}],
        "mcp_tools": [{
            "server": "srv",
            "name": tool_name,
            "description": "find things",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        }],
        "mcp_resources": [{
            "server": "srv",
            "uri": "resource://one",
            "name": "one",
            "description": "metadata",
            "mimeType": "text/plain",
        }],
    }


def _mock_client(handler, clock: FakeClock, ttl: float = 2.0) -> SandboxClient:
    transport = httpx.MockTransport(handler)
    client = SandboxClient(
        "sandbox",
        8000,
        "key",
        base_url="http://action.test",
        catalogue_ttl_seconds=ttl,
        catalogue_clock=clock,
    )

    def factory(timeout: float = 30.0):
        return httpx.AsyncClient(
            transport=transport,
            base_url=client.base_url,
            headers=client._headers,
            timeout=timeout,
        )

    client._client = factory
    return client


@pytest.fixture
def projected_server(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    builtin = tmp_path / "builtin"
    skills.mkdir()
    builtin.mkdir()
    config = tmp_path / "mcp.json"
    monkeypatch.setattr(action_server, "SKILLS_DIR", skills)
    monkeypatch.setattr(action_server, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(action_server, "MCP_CONFIG_PATH", config)
    manager = action_server.ContainerMcpManager()
    monkeypatch.setattr(action_server, "mcp_manager", manager)
    return action_server, manager, skills


@pytest.mark.asyncio
async def test_action_server_projection_etag_and_body_free_resources(projected_server):
    server, manager, skills = projected_server
    sentinel = "RESOURCE_BODY_SECRET_4f8c"
    skill_secret = "SKILL_BODY_SECRET_6c21"
    skill_dir = skills / "sample"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: sample\ndescription: short description\n---\n{skill_secret}",
        encoding="utf-8",
    )
    (skill_dir / "script.py").write_text("print('v1')", encoding="utf-8")

    manager._servers["srv"] = {"status": "connected", "error": None}
    manager._tools["srv"] = [{
        "server": "srv",
        "name": "lookup",
        "description": "lookup metadata",
        "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
    }]
    manager._resources["srv"] = [{
        "server": "srv",
        "uri": "resource://secret",
        "name": "secret",
        "description": "safe metadata",
        "mimeType": "text/plain",
        "text": sentinel,
        "blob": sentinel,
        "contents": [{"text": sentinel}],
    }]

    response = await server.get_catalogue_projection(_request())
    assert response.status_code == 200
    etag = response.headers["etag"]
    body = json.loads(response.body)
    encoded = response.body.decode("utf-8")

    assert body["counts"] == {
        "skills": 1,
        "mcp_servers": 0,
        "mcp_tools": 1,
        "mcp_resources": 1,
    }
    # The server was injected directly rather than configured on disk; only
    # connected definitions count, while configured-server count stays honest.
    assert body["mcp_tools"][0]["input_schema"]["properties"]["q"]["type"] == "string"
    assert body["mcp_resources"] == [{
        "server": "srv",
        "uri": "resource://secret",
        "name": "secret",
        "description": "safe metadata",
        "mimeType": "text/plain",
    }]
    assert sentinel not in encoded
    assert skill_secret not in encoded
    assert "content" not in body["skills"][0]
    assert "base_dir" not in body["skills"][0]

    unchanged = await server.get_catalogue_projection(_request(etag))
    assert unchanged.status_code == 304
    assert unchanged.body == b""
    assert unchanged.headers["etag"] == etag

    version = await server.get_catalogue_version(_request())
    version_body = json.loads(version.body)
    assert "skills" not in version_body
    assert version_body["boot_id"] == body["boot_id"]
    assert version_body["generation"] == body["generation"]
    assert version.headers["etag"] == etag
    unchanged_version = await server.get_catalogue_version(_request(etag))
    assert unchanged_version.status_code == 304
    assert unchanged_version.body == b""

    # Supporting-file content participates in Skill generation, without its
    # bytes ever entering the projection.
    (skill_dir / "script.py").write_text("print('v2')", encoding="utf-8")
    changed = await server.get_catalogue_projection(_request(etag))
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag


@pytest.mark.asyncio
async def test_action_server_legacy_lists_support_conditional_304(projected_server):
    server, manager, _skills = projected_server
    manager._servers["srv"] = {"status": "connected", "error": None}
    manager._tools["srv"] = [{
        "server": "srv", "name": "one", "description": "", "input_schema": {}
    }]
    manager._resources["srv"] = [{
        "server": "srv", "uri": "resource://one", "name": "one"
    }]

    tools = await server.list_mcp_tools(_request())
    resources = await server.list_mcp_resources(_request())
    skills = await server.list_skills(_request())
    assert tools.headers["etag"] == resources.headers["etag"]
    assert (await server.list_mcp_tools(_request(tools.headers["etag"]))).status_code == 304
    assert (await server.list_mcp_resources(_request(resources.headers["etag"]))).status_code == 304
    assert (await server.list_skills(_request(skills.headers["etag"]))).status_code == 304


@pytest.mark.asyncio
async def test_mcp_add_remove_refresh_advance_generation(projected_server, monkeypatch):
    server, manager, _skills = projected_server
    initial = server._mcp_catalogue_projection(manager)["generation"]
    manager.add_server("srv", {"type": "stdio", "command": "safe"})
    after_add = server._mcp_catalogue_projection(manager)["generation"]
    assert after_add != initial

    manager._servers["srv"] = {"status": "connected", "error": None}
    manager._tools["srv"] = []

    @asynccontextmanager
    async def fake_session(_name):
        yield object()

    async def unchanged_discovery(_name, _session):
        return None

    monkeypatch.setattr(manager, "_session", fake_session)
    monkeypatch.setattr(manager, "_discover", unchanged_discovery)
    before_refresh = server._mcp_catalogue_projection(manager)["generation"]
    await manager.refresh_server("srv")
    after_refresh = server._mcp_catalogue_projection(manager)["generation"]
    assert after_refresh != before_refresh

    manager.remove_server("srv")
    after_remove = server._mcp_catalogue_projection(manager)["generation"]
    assert after_remove not in {after_add, after_refresh}


@pytest.mark.asyncio
async def test_client_ttl_singleflight_304_and_copy_on_read():
    clock = FakeClock()
    calls = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0.01)
        if request.headers.get("if-none-match") == '"g1"':
            return httpx.Response(304, headers={"ETag": '"g1"'})
        return httpx.Response(200, json=_payload(), headers={"ETag": '"g1"'})

    client = _mock_client(handler, clock)
    cold = await asyncio.gather(
        client.list_skills(),
        client.list_mcp_tools(),
        client.list_mcp_resources(),
        *[client.get_catalogue_projection() for _ in range(17)],
    )
    skills, tools, resources = cold[:3]
    first = cold[3:]
    assert len(calls) == 1
    assert skills[0]["name"] == "skill-a"
    assert tools[0]["name"] == "lookup"
    assert resources[0]["uri"] == "resource://one"
    assert all(item["generation"] == "g1" for item in first)

    first[0]["mcp_tools"][0]["input_schema"]["properties"]["query"]["type"] = "number"
    clean = await client.get_catalogue_projection()
    assert clean["mcp_tools"][0]["input_schema"]["properties"]["query"]["type"] == "string"
    assert len(calls) == 1
    cached_state = await client.get_catalogue_projection_state()
    assert cached_state.availability == "stale"
    assert cached_state.snapshot["generation"] == "g1"
    assert len(calls) == 1

    clock.advance(3)
    refreshed = await asyncio.gather(*[
        client.get_catalogue_projection() for _ in range(20)
    ])
    assert len(calls) == 2
    assert calls[-1].headers["if-none-match"] == '"g1"'
    assert all(item["generation"] == "g1" for item in refreshed)


@pytest.mark.asyncio
async def test_client_404_fallback_coalesces_three_legacy_lists_and_strips_resource_body():
    clock = FakeClock()
    sentinel = "LEGACY_RESOURCE_BODY_SECRET_0c9a"
    counts: dict[str, int] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        counts[request.url.path] = counts.get(request.url.path, 0) + 1
        if request.url.path == "/catalog":
            return httpx.Response(404, json={"detail": "missing"})
        if request.url.path == "/skills":
            return httpx.Response(200, json=[{
                "name": "legacy", "description": "old", "content": "kept for compatibility"
            }])
        if request.url.path == "/mcp/tools":
            return httpx.Response(200, json=[{
                "server": "srv", "name": "legacy_tool", "input_schema": {}
            }])
        if request.url.path == "/mcp/resources":
            return httpx.Response(200, json=[{
                "server": "srv",
                "uri": "resource://legacy",
                "name": "legacy",
                "text": sentinel,
                "contents": [{"text": sentinel}],
            }])
        raise AssertionError(request.url.path)

    client = _mock_client(handler, clock)
    skills, tools, resources = await asyncio.gather(
        client.list_skills(), client.list_mcp_tools(), client.list_mcp_resources()
    )

    assert counts == {
        "/catalog": 1,
        "/skills": 1,
        "/mcp/tools": 1,
        "/mcp/resources": 1,
    }
    assert skills[0]["content"] == "kept for compatibility"
    assert tools[0]["name"] == "legacy_tool"
    assert resources == [{
        "server": "srv",
        "uri": "resource://legacy",
        "name": "legacy",
        "description": "",
        "mimeType": "",
    }]
    assert sentinel not in json.dumps(resources)
    version = await client.get_catalogue_version()
    assert version["catalogue_version"] == 0
    assert version["generation"] == ""


@pytest.mark.asyncio
async def test_client_transient_failure_keeps_snapshot_but_retries_immediately():
    clock = FakeClock()
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, json=_payload("g1"), headers={"ETag": '"g1"'})
        if attempts == 2:
            raise httpx.ConnectError("tunnel disconnected", request=request)
        return httpx.Response(
            200,
            json=_payload("g2", tool_name="after-reconnect"),
            headers={"ETag": '"g2"'},
        )

    client = _mock_client(handler, clock, ttl=1.0)
    available = await client.get_catalogue_projection_state()
    assert available.availability == "available"
    assert available.snapshot["generation"] == "g1"
    clock.advance(2)

    stale = await client.get_catalogue_projection_state()
    assert stale.availability == "stale"
    assert stale.snapshot["generation"] == "g1"
    assert attempts == 2

    recovered = await client.get_catalogue_projection_state()
    assert recovered.availability == "available"
    assert recovered.snapshot["generation"] == "g2"
    assert recovered.snapshot["mcp_tools"][0]["name"] == "after-reconnect"
    assert attempts == 3


@pytest.mark.asyncio
async def test_client_does_not_negative_cache_first_connection_failure():
    clock = FakeClock()
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError("first failure", request=request)
        return httpx.Response(200, json=_payload(), headers={"ETag": '"g1"'})

    client = _mock_client(handler, clock)
    with pytest.raises(httpx.ConnectError):
        await client.get_catalogue_projection()
    cold = await client.get_catalogue_projection_state()
    assert cold.availability == "unavailable"
    assert cold.snapshot is None
    recovered = await client.get_catalogue_projection_state()
    assert recovered.availability == "available"
    assert recovered.snapshot["generation"] == "g1"
    assert attempts == 3


@pytest.mark.asyncio
async def test_client_refresh_invalidates_unexpired_projection():
    clock = FakeClock()
    catalogue_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal catalogue_calls
        if request.url.path == "/catalog":
            catalogue_calls += 1
            generation = f"g{catalogue_calls}"
            return httpx.Response(
                200,
                json=_payload(generation),
                headers={"ETag": f'"{generation}"'},
            )
        if request.url.path == "/mcp/servers/srv/refresh":
            return httpx.Response(200, json={"tools": 1, "resources": 1})
        raise AssertionError(request.url.path)

    client = _mock_client(handler, clock, ttl=100.0)
    assert (await client.get_catalogue_projection())["generation"] == "g1"
    await client.refresh_mcp_server("srv")
    assert (await client.get_catalogue_projection())["generation"] == "g2"
    assert catalogue_calls == 2


@pytest.mark.asyncio
async def test_failed_mutation_keeps_last_known_good_until_reconnect():
    clock = FakeClock()
    catalogue_calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal catalogue_calls
        if request.url.path == "/catalog":
            catalogue_calls += 1
            if catalogue_calls == 1:
                return httpx.Response(
                    200,
                    json=_payload("g1"),
                    headers={"ETag": '"g1"'},
                )
            if catalogue_calls == 2:
                raise httpx.ConnectError("tunnel dropped", request=request)
            return httpx.Response(
                200,
                json=_payload("g2", tool_name="recovered"),
                headers={"ETag": '"g2"'},
            )
        if request.url.path == "/mcp/servers/srv/refresh":
            return httpx.Response(503, json={"detail": "offline"})
        raise AssertionError(request.url.path)

    client = _mock_client(handler, clock, ttl=100.0)
    first = await client.get_catalogue_projection_state()
    assert first.availability == "available"
    with pytest.raises(httpx.HTTPStatusError):
        await client.refresh_mcp_server("srv")

    stale = await client.get_catalogue_projection_state()
    assert stale.availability == "stale"
    assert stale.snapshot["generation"] == "g1"
    recovered = await client.get_catalogue_projection_state()
    assert recovered.availability == "available"
    assert recovered.snapshot["generation"] == "g2"
    assert recovered.snapshot["mcp_tools"][0]["name"] == "recovered"
