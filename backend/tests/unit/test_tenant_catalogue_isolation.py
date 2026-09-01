"""Tenant boundaries for the shared WUYING catalogue acceptance topology."""

import importlib.util
from pathlib import Path
import sys
import types

import httpx
import pytest
from fastapi import HTTPException

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
    "openbox_tenant_catalogue_action_server_test", ACTION_SERVER
)
assert _SPEC and _SPEC.loader
action_server = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(action_server)


SCOPE_A = "u-11111111111111111111"
SCOPE_B = "u-22222222222222222222"


@pytest.fixture
def scoped_server(monkeypatch, tmp_path):
    skills = tmp_path / "data" / "skills"
    builtin = tmp_path / "builtin"
    workspace = tmp_path / "workspace"
    mcp_config = tmp_path / "data" / "mcp" / "config.json"
    skills.mkdir(parents=True)
    builtin.mkdir()
    workspace.mkdir()
    monkeypatch.setattr(action_server, "SKILLS_DIR", skills)
    monkeypatch.setattr(action_server, "BUILTIN_SKILLS_DIR", builtin)
    monkeypatch.setattr(action_server, "WORKSPACE_ROOT", workspace)
    monkeypatch.setattr(action_server, "SKILL_EXPORTS_DIR", workspace / "exports")
    monkeypatch.setattr(action_server, "MCP_CONFIG_PATH", mcp_config)
    monkeypatch.setattr(action_server, "_scoped_mcp_managers", {})
    monkeypatch.setattr(action_server, "SESSION_API_KEY", "tenant-test-key")
    return action_server, skills, workspace, mcp_config


def _scope(server, value: str):
    return server._request_user_scope.set(value)


def _skill_md(name: str, body: str) -> str:
    return f"---\nname: {name}\ndescription: tenant test\n---\n{body}\n"


@pytest.mark.asyncio
async def test_same_skill_name_is_isolated_and_exports_do_not_collide(scoped_server):
    server, skills, workspace, _ = scoped_server

    token = _scope(server, SCOPE_A)
    try:
        await server.create_skill(server.CreateSkillRequest(
            name="same-name", skill_md=_skill_md("same-name", "tenant A")
        ))
        export_a = await server.export_skill_archive("same-name")
    finally:
        server._request_user_scope.reset(token)

    token = _scope(server, SCOPE_B)
    try:
        await server.create_skill(server.CreateSkillRequest(
            name="same-name", skill_md=_skill_md("same-name", "tenant B")
        ))
        visible_b = await server.get_skill("same-name")
        export_b = await server.export_skill_archive("same-name")
        with pytest.raises(HTTPException) as denied:
            await server.read_file(server.ReadFileRequest(
                path=str(skills / SCOPE_A / "same-name" / "SKILL.md")
            ))
    finally:
        server._request_user_scope.reset(token)

    assert "tenant B" in visible_b["content"]
    assert Path(export_a["path"]).is_relative_to(
        workspace / "openbox" / "users" / SCOPE_A
    )
    assert Path(export_b["path"]).is_relative_to(
        workspace / "openbox" / "users" / SCOPE_B
    )
    assert export_a["path"] != export_b["path"]
    assert denied.value.status_code == 403


def test_mcp_config_and_runtime_cache_are_isolated(scoped_server):
    server, _, _, mcp_config = scoped_server

    token = _scope(server, SCOPE_A)
    try:
        manager_a = server._active_mcp_manager()
        manager_a.add_server("same-server", {
            "type": "remote", "url": "https://tenant-a.invalid/mcp"
        })
    finally:
        server._request_user_scope.reset(token)

    token = _scope(server, SCOPE_B)
    try:
        manager_b = server._active_mcp_manager()
        manager_b.add_server("same-server", {
            "type": "remote", "url": "https://tenant-b.invalid/mcp"
        })
    finally:
        server._request_user_scope.reset(token)

    assert manager_a is not manager_b
    assert manager_a._server_config("same-server")["url"].startswith("https://tenant-a")
    assert manager_b._server_config("same-server")["url"].startswith("https://tenant-b")
    assert manager_a.config_path == mcp_config.parent / SCOPE_A / "config.json"
    assert manager_b.config_path == mcp_config.parent / SCOPE_B / "config.json"
    assert manager_a.config_path.stat().st_mode & 0o777 == 0o600
    assert manager_b.config_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_wuying_catalogue_routes_require_valid_scope(scoped_server, monkeypatch):
    server, _, _, _ = scoped_server
    monkeypatch.setattr(server, "REQUIRE_USER_SCOPE", True)
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://action.test"
    ) as client:
        missing = await client.get("/catalog")
        malformed = await client.get(
            "/catalog",
            headers={
                "X-API-Key": "tenant-test-key",
                "X-OpenBox-User-Scope": "../../other",
            },
        )
        scoped = await client.get(
            "/catalog",
            headers={
                "X-API-Key": "tenant-test-key",
                "X-OpenBox-User-Scope": SCOPE_A,
            },
        )

    assert missing.status_code == 403
    assert malformed.status_code == 400
    assert scoped.status_code == 200
    assert scoped.json()["counts"]["skills"] == 0


@pytest.mark.asyncio
async def test_scoped_client_sends_hash_and_rejects_legacy_global_server(monkeypatch):
    paths: list[str] = []

    def legacy(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["X-OpenBox-User-Scope"] == SCOPE_A
        if request.url.path == "/alive":
            return httpx.Response(200, json={"capabilities": []})
        return httpx.Response(200, json={})

    transport = httpx.MockTransport(legacy)
    client = SandboxClient(
        "sandbox", 8000, "key", base_url="http://action.test", user_scope=SCOPE_A
    )

    def factory(timeout: float = 30.0):
        return httpx.AsyncClient(
            transport=transport,
            base_url=client.base_url,
            headers=client._headers,
            timeout=timeout,
        )

    monkeypatch.setattr(client, "_client", factory)
    with pytest.raises(RuntimeError, match="tenant-scoped"):
        await client.get_catalogue_projection()
    assert paths == ["/alive"]
