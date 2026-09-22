"""Team -> sandbox API -> real local MCP HTTP transport, including revocation."""
import asyncio
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from sandbox.client import SandboxClient
from team import runtime_binding
from tests.unit.test_catalogue_projection import action_server, projected_server
from tests.unit.test_mcp_security import _ctx
from tests.unit.test_team_mcp import delegated
from tool.integrations.mcp_tool import _canonical_tool_id, create_mcp_resource_tool, create_mcp_tools


@pytest.fixture(params=["json", "sse"])
async def live_mcp(request):
    calls, auth_failures = [], []
    mode = request.param

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = payload["method"]
            if self.headers.get("Authorization") != "Bearer fixture-only":
                auth_failures.append(method)
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "local-team-fixture", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [{"name": name, "description": "Local fixture",
                    "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}
                    for name in [*[f"search_fixture_{i}" for i in range(45)], "unapproved_tool"]]}
            elif method == "resources/list":
                result = {"resources": [{"uri": "docs://public/guide", "name": "guide", "mimeType": "text/plain"}]}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method == "tools/call":
                calls.append((method, payload["params"]))
                result = {"content": [{"type": "text", "text": "Live approved result"}], "isError": False}
            elif method == "resources/read":
                calls.append((method, payload["params"]))
                result = {"contents": [{"uri": payload["params"]["uri"], "text": "Live approved resource"}]}
            else:
                raise AssertionError(method)
            body = json.dumps({"jsonrpc": "2.0", "id": payload["id"], "result": result})
            data = (("event: message\ndata: " + body + "\n\n") if mode == "sse" else body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if mode == "sse" else "application/json")
            self.send_header("Mcp-Session-Id", "fixture-session")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp", calls, auth_failures
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        worker.join(timeout=2)


async def test_live_http_delegation_filters_large_catalogue_and_revokes_before_transport(
    live_mcp, projected_server, delegated, monkeypatch,
):
    url, calls, auth_failures = live_mcp
    _, binding, grant = delegated
    refs = [{"server": "srv", "tools": ["search_*", "resource:docs://public/*"]}]
    runtime_binding._current.set(replace(binding, spec={**binding.spec, "mcp_refs": refs}))
    grant["mcp_refs"] = refs
    manager = action_server.ContainerMcpManager()
    monkeypatch.setattr(action_server, "mcp_manager", manager)
    monkeypatch.setattr(action_server, "SESSION_API_KEY", "action-fixture-only")
    manager.add_server("srv", {"type": "remote", "url": url, "timeout": 3,
        "headers": {"Authorization": "Bearer fixture-only"}})
    transport = httpx.ASGITransport(app=action_server.app)
    client = SandboxClient("local-fixture", 8000, "action-fixture-only", base_url="http://action.test")
    client._client = lambda timeout=30: httpx.AsyncClient(transport=transport,
        base_url=client.base_url, headers=client._headers, timeout=timeout)
    await client.connect_mcp("srv")
    tools = await create_mcp_tools(client, agent_id="team_member")
    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    ctx = _ctx(client, workspace_id="workspace-a", agent_id="team_member")
    found = await tools["mcp_find_tool"].execute({"query": "search_fixture_0"}, ctx)
    assert "search_fixture_0" in found.output and "unapproved_tool" not in found.output
    permitted = _canonical_tool_id("srv", "search_fixture_0")
    result = await tools["mcp_call_tool"].execute({"canonical_id": permitted, "arguments": {"value": "ok"}}, ctx)
    assert "Live approved result" in result.output
    denied = await tools["mcp_call_tool"].execute({"canonical_id": _canonical_tool_id("srv", "unapproved_tool")}, ctx)
    assert denied.metadata["blocked"] and len(calls) == 1
    resource = create_mcp_resource_tool()
    result = await resource.execute({"server": "srv", "uri": "docs://public/guide"}, ctx)
    assert "Live approved resource" in result.output and len(calls) == 2
    grant["mcp_refs"] = []
    for tool, args in [(tools["mcp_call_tool"], {"canonical_id": permitted}),
                       (resource, {"server": "srv", "uri": "docs://public/guide"})]:
        result = await tool.execute(args, ctx)
        assert result.metadata["blocked"]
    assert len(calls) == 2 and not auth_failures
