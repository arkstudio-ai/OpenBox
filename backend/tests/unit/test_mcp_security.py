"""Security contracts for dynamic MCP discovery and execution."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.hooks import ToolHooks
from agent.tool_runtime import assemble_tool_runtime
from agent.tool_resolution import (
    _CatalogueSnapshotSandbox,
    merge_sandbox_tools,
    resolve_step_tools,
)
from permission.permission import Rule
from tool.mcp_tool import (
    MCP_CANONICAL_PREFIX,
    MCP_FAILURE_IDENTITY_CHARS,
    MCP_FAILURE_MAX_BYTES,
    MCP_META_INDEX_PARAMETER_LIMIT,
    MCP_RESOURCE_CANONICAL_PREFIX,
    _DISCOVERY_EVIDENCE,
    _build_bindings,
    _build_meta_search_index,
    _canonical_resource_id,
    _canonical_tool_id,
    _clear_mcp_normalization_cache,
    create_mcp_resource_tool,
    create_mcp_tools,
)
from tool.tool import ToolContext, ToolInfo, ToolResult
from tool.truncation import MAX_BYTES, MAX_LINES


def _raw_tool(index: int, *, server: str = "srv", name: str | None = None) -> dict:
    tool_name = name or f"search_fixture_{index}"
    return {
        "server": server,
        "name": tool_name,
        "description": f"fixture capability {index}",
        "input_schema": {
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    }


class Sandbox:
    def __init__(self, tools: list[dict], sandbox_id: str = "sandbox-a"):
        self.tools = tools
        self.sandbox_id = sandbox_id
        self.calls: list[tuple[str, str, dict]] = []
        self.raise_call: Exception | None = None
        self.tool_list_calls = 0
        self.resource_list_calls = 0

    async def list_mcp_tools(self):
        self.tool_list_calls += 1
        return self.tools

    async def list_mcp_resources(self):
        self.resource_list_calls += 1
        return []

    async def call_mcp_tool(self, server: str, tool: str, arguments: dict):
        self.calls.append((server, tool, arguments))
        if self.raise_call is not None:
            raise self.raise_call
        return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

    async def write_file(self, _path: str, _content: str):
        return None


class ProjectedSandbox(Sandbox):
    """Minimal coherent projection carrying an authoritative raw digest."""

    def __init__(
        self,
        tools: list[dict],
        *,
        generation: str = "mcp-generation-a",
        user_id: str = "user-a",
        project_id: str = "project-a",
        sandbox_id: str = "sandbox-a",
    ):
        super().__init__(tools, sandbox_id=sandbox_id)
        self._snapshot = {
            "boot_id": "boot-a",
            "mcp_generation": generation,
            "generation": f"catalogue-{generation}",
        }
        self._sandbox = SimpleNamespace(
            user_id=user_id,
            account_id="account-a",
            project_id=project_id,
            sandbox_id=sandbox_id,
            base_url="http://action.test",
            region="region-a",
            api_key="credential-a",
        )

    async def list_mcp_tools(self):
        self.tool_list_calls += 1
        await asyncio.sleep(0)
        return self.tools

    def set_generation(self, generation: str) -> None:
        self._snapshot["mcp_generation"] = generation
        self._snapshot["generation"] = f"catalogue-{generation}"


def _ctx(sandbox: Sandbox, authorize=None, **overrides) -> ToolContext:
    values = {
        "user_id": "user-a",
        "project_id": "project-a",
        "session_id": "session-a",
        "run_id": "run-a",
        "agent_id": "build",
        "sandbox": sandbox,
        "_authorize_tool": authorize or (lambda _tool_id, _args: _allow()),
    }
    values.update(overrides)
    return ToolContext(**values)


async def _allow():
    return None


@pytest.fixture(autouse=True)
def _clear_evidence():
    _DISCOVERY_EVIDENCE.clear()
    _clear_mcp_normalization_cache()
    yield
    _DISCOVERY_EVIDENCE.clear()
    _clear_mcp_normalization_cache()


def test_canonical_id_is_bounded_and_stable_for_untrusted_long_names():
    canonical = _canonical_tool_id("server/" + "s" * 10_000, "tool/" + "t" * 10_000)
    assert canonical.startswith(MCP_CANONICAL_PREFIX)
    assert len(canonical) == 59
    assert canonical == _canonical_tool_id(
        "server/" + "s" * 10_000,
        "tool/" + "t" * 10_000,
    )


def test_resource_permission_id_is_bounded_stable_and_tuple_unambiguous():
    canonical = _canonical_resource_id(
        "server/" + "s" * 10_000,
        "resource://" + "u" * 100_000,
    )

    assert canonical.startswith(MCP_RESOURCE_CANONICAL_PREFIX)
    assert len(canonical) == len(MCP_RESOURCE_CANONICAL_PREFIX) + 52
    assert canonical == _canonical_resource_id(
        "server/" + "s" * 10_000,
        "resource://" + "u" * 100_000,
    )
    assert _canonical_resource_id("a/b", "c") != _canonical_resource_id("a", "b/c")


@pytest.mark.asyncio
async def test_resource_read_authorizes_exact_uri_with_last_match_rules():
    class ResourceSandbox(Sandbox):
        def __init__(self):
            super().__init__([])
            self.reads: list[tuple[str, str]] = []

        async def read_mcp_resource(self, server: str, uri: str):
            self.reads.append((server, uri))
            return {"contents": [{"text": "short resource"}]}

    sandbox = ResourceSandbox()
    denied_uri = "resource://private"
    allowed_uri = "resource://public"
    denied_id = _canonical_resource_id("srv", denied_uri)
    allowed_id = _canonical_resource_id("srv", allowed_uri)
    hooks = ToolHooks(
        "session-a",
        "user-a",
        config_rules=[
            Rule(permission="mcp_read_resource", pattern="*", action="allow"),
            Rule(permission="mcp_read_resource", pattern=allowed_id, action="deny"),
            Rule(permission="mcp_read_resource", pattern=denied_id, action="deny"),
            # The later exact allow proves the existing last-match semantics
            # are retained for one resource without widening its neighbor.
            Rule(permission="mcp_read_resource", pattern=allowed_id, action="allow"),
        ],
    )
    tool = create_mcp_resource_tool()
    ctx = _ctx(sandbox)

    denied = await hooks.wrap_execute(
        "mcp_read_resource",
        tool.execute,
        {"server": "srv", "uri": denied_uri},
        ctx,
    )
    allowed = await hooks.wrap_execute(
        "mcp_read_resource",
        tool.execute,
        {"server": "srv", "uri": allowed_uri},
        ctx,
    )

    assert denied.metadata["blocked"] is True
    assert denied_uri not in denied.output
    assert allowed.output == "short resource"
    assert allowed.metadata["truncated"] is False
    assert sandbox.reads == [("srv", allowed_uri)]


@pytest.mark.asyncio
async def test_resource_body_is_safely_bounded_without_copying_blob_or_unknown_content(caplog):
    blob_secret = "BLOB_SECRET_934e"
    unknown_secret = "CONTENT_SECRET_c612"
    text_marker = "TEXT_BODY_5c78"

    class ResourceSandbox(Sandbox):
        def __init__(self):
            super().__init__([])
            self.reads = 0
            self.writes = 0

        async def read_mcp_resource(self, _server: str, _uri: str):
            self.reads += 1
            return {
                "contents": [
                    {
                        "blob": blob_secret * 100_000,
                        "mimeType": "application/octet-stream" + "m" * 10_000,
                    },
                    {"content": unknown_secret * 100_000},
                    {"text": (text_marker + "\n") * 100_000},
                ]
            }

        async def write_file(self, _path: str, _content: str):
            self.writes += 1

    sandbox = ResourceSandbox()
    tool = create_mcp_resource_tool()
    hooks = ToolHooks(
        "session-a",
        "user-a",
        config_rules=[
            Rule(permission="mcp_read_resource", pattern="*", action="allow")
        ],
    )
    result = await hooks.wrap_execute(
        "mcp_read_resource",
        tool.execute,
        {"server": "srv", "uri": "resource://large"},
        _ctx(sandbox),
    )

    assert result.metadata["truncated"] is True
    assert text_marker in result.output
    assert blob_secret not in result.output
    assert unknown_secret not in result.output
    assert "MCP resource output truncated" in result.output
    assert len(result.output.encode("utf-8")) <= MAX_BYTES
    assert result.output.count("\n") + 1 <= MAX_LINES
    assert sandbox.reads == 1
    assert sandbox.writes == 0
    assert blob_secret not in caplog.text
    assert unknown_secret not in caplog.text


@pytest.mark.asyncio
async def test_all_mcp_toolinfo_is_explicitly_sandbox_plane():
    direct = await create_mcp_tools(
        Sandbox([_raw_tool(1, name="direct")]), [], agent_id="build"
    )
    direct_name, direct_info = next(iter(direct.items()))
    direct_id = _canonical_tool_id("srv", "direct")
    assert (
        direct_info.source,
        direct_info.plane,
        direct_info.canonical_id,
        direct_info.provider_name,
        direct_info.pack,
        direct_info.same_response_safe,
    ) == ("mcp", "sandbox", direct_id, direct_name, None, False)

    meta = await create_mcp_tools(
        Sandbox([_raw_tool(i) for i in range(41)]), [], agent_id="build"
    )
    for name, info in meta.items():
        assert (
            info.source,
            info.plane,
            info.canonical_id,
            info.provider_name,
            info.pack,
            info.same_response_safe,
        ) == ("mcp", "sandbox", name, name, None, False)

    resource = create_mcp_resource_tool()
    assert (
        resource.source,
        resource.plane,
        resource.canonical_id,
        resource.provider_name,
        resource.pack,
        resource.same_response_safe,
    ) == (
        "mcp",
        "sandbox",
        "mcp_read_resource",
        "mcp_read_resource",
        None,
        False,
    )


@pytest.mark.asyncio
async def test_explicit_empty_agent_tool_allowlist_never_discovers_sandbox_tools():
    class EmptyAgent:
        name = "locked-down"
        tools = []
        permission = []

    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    tools = await resolve_step_tools(EmptyAgent(), sandbox, [])

    assert tools == {}
    assert sandbox.tool_list_calls == 0
    assert sandbox.resource_list_calls == 0


@pytest.mark.asyncio
async def test_resolution_marks_cold_catalogue_unavailable_then_recovers(
    monkeypatch,
):
    from agent import tool_resolution as resolution

    class Params:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {}}

    async def execute(_args, _ctx):
        return ToolResult(title="platform")

    platform = ToolInfo(
        id="platform_tool",
        parameters=Params,
        description="platform",
        execute=execute,
    )
    monkeypatch.setattr(
        resolution,
        "get_tools_for_agent",
        lambda _ids: {"platform_tool": platform},
    )

    class Agent:
        name = "build"
        tools = ["platform_tool"]
        permission = []

    sandbox = Sandbox([_raw_tool(99, name="must_not_be_read_directly")])
    state = SimpleNamespace(availability="unavailable", snapshot=None)

    async def get_state():
        return state

    sandbox.get_catalogue_projection_state = get_state
    cold = await resolve_step_tools(
        Agent(), sandbox, [], return_catalogue_state=True
    )
    assert cold.catalogue_availability == "unavailable"
    assert cold.tools == {"platform_tool": platform}
    assert sandbox.tool_list_calls == sandbox.resource_list_calls == 0

    snapshot = {
        "skills": [],
        "mcp_tools": [_raw_tool(1, name="after_reconnect")],
        "mcp_resources": [],
    }
    state = SimpleNamespace(availability="stale", snapshot=snapshot)
    stale = await resolve_step_tools(
        Agent(), sandbox, [], return_catalogue_state=True
    )
    assert stale.catalogue_availability == "stale"
    assert any(
        tool.canonical_id == _canonical_tool_id("srv", "after_reconnect")
        for tool in stale.tools.values()
    )

    state = SimpleNamespace(availability="available", snapshot=snapshot)
    recovered = await resolve_step_tools(
        Agent(), sandbox, [], return_catalogue_state=True
    )
    assert recovered.catalogue_availability == "available"
    assert "platform_tool" in recovered.tools
    assert any(
        tool.canonical_id == _canonical_tool_id("srv", "after_reconnect")
        for tool in recovered.tools.values()
    )
    # MCP and resource listings came from the same captured snapshot, not a
    # second live directory request that could cross generations.
    assert sandbox.tool_list_calls == sandbox.resource_list_calls == 0

    state = SimpleNamespace(availability="stale", snapshot={})
    malformed = await resolve_step_tools(
        Agent(), sandbox, [], return_catalogue_state=True
    )
    assert malformed.catalogue_availability == "unavailable"
    assert malformed.tools == {"platform_tool": platform}

    class BrokenLegacySandbox:
        async def list_skills(self):
            raise ConnectionError("legacy tunnel offline")

        async def list_mcp_tools(self):
            raise AssertionError("must stop after the first failed list")

        async def list_mcp_resources(self):
            raise AssertionError("must stop after the first failed list")

    legacy = await resolve_step_tools(
        Agent(), BrokenLegacySandbox(), [], return_catalogue_state=True
    )
    assert legacy.catalogue_availability == "unavailable"
    assert legacy.tools == {"platform_tool": platform}


@pytest.mark.asyncio
async def test_stale_projection_keeps_revealed_mcp_binding_and_executes_once(
    monkeypatch,
):
    """A warm tunnel outage fails at execution, not historical replay."""

    from agent import tool_resolution as resolution
    from tool.capability_search import capability_search_tool

    target = _raw_tool(1, name="get_sum")
    target_id = _canonical_tool_id("srv", "get_sum")

    class OfflineSandbox(Sandbox):
        async def get_catalogue_projection_state(self):
            return SimpleNamespace(
                availability="stale",
                snapshot={
                    "skills": [],
                    "mcp_tools": [target],
                    "mcp_resources": [],
                },
            )

        async def call_mcp_tool(self, server: str, tool: str, arguments: dict):
            self.calls.append((server, tool, arguments))
            import httpx

            request = httpx.Request("POST", "http://wuying.test/mcp")
            raise httpx.ConnectError("tunnel disconnected", request=request)

    class Agent:
        name = "build"
        tools = ["capability_search"]
        permission = []

    monkeypatch.setattr(
        resolution,
        "get_tools_for_agent",
        lambda _ids: {"capability_search": capability_search_tool},
    )
    sandbox = OfflineSandbox([target])
    resolved = await resolve_step_tools(
        Agent(), sandbox, [], return_catalogue_state=True
    )
    runtime = assemble_tool_runtime(
        resolved.tools,
        mode="portable",
        agent_name="build",
        revealed_ids={target_id},
    )

    assert resolved.catalogue_availability == "stale"
    assert target_id in runtime.provider_plan.direct_ids
    assert target_id in runtime.step_executable_ids
    assert target_id in runtime.provider_to_canonical.values()

    result = await runtime.execution_lookup[target_id].execute(
        {"value": 3},
        _ctx(sandbox),
    )

    assert "Could not reach the MCP server" in result.output
    assert sandbox.calls == [("srv", "get_sum", {"value": 3})]


@pytest.mark.asyncio
async def test_direct_mode_resolves_truncation_collision_with_stable_wire_names():
    common = "x" * 100
    raw = [
        _raw_tool(1, name=f"{common}-alpha"),
        _raw_tool(2, name=f"{common}-beta"),
    ]
    sandbox = Sandbox(raw)

    first = await create_mcp_tools(sandbox, [], agent_id="build")
    second = await create_mcp_tools(
        Sandbox(list(reversed(raw))), [], agent_id="build"
    )

    assert len(first) == 2
    assert set(first) == set(second)
    assert all(len(name) <= 64 for name in first)
    assert len(set(first)) == 2

    authorized = []

    async def authorize(tool_id, arguments):
        authorized.append((tool_id, arguments))
        return None

    ctx = _ctx(sandbox, authorize)
    for provider_name, info in first.items():
        await info.execute({"value": provider_name, "ignored": None}, ctx)

    assert {call[:2] for call in sandbox.calls} == {
        ("srv", f"{common}-alpha"),
        ("srv", f"{common}-beta"),
    }
    assert len({tool_id for tool_id, _args in authorized}) == 2
    assert all(tool_id.startswith(MCP_CANONICAL_PREFIX) for tool_id, _args in authorized)
    assert all(args == {"value": name} for (_tool, args), name in zip(authorized, first))


@pytest.mark.asyncio
async def test_canonical_deny_filters_before_direct_index_without_hiding_neighbor():
    raw = [_raw_tool(1, name="alpha"), _raw_tool(2, name="beta")]
    denied = _canonical_tool_id("srv", "beta")
    tools = await create_mcp_tools(
        Sandbox(raw),
        [Rule(permission=denied, pattern="*", action="deny")],
        agent_id="build",
    )

    assert len(tools) == 1
    only_executor = next(iter(tools.values())).execute
    assert only_executor._mcp_raw_identity == ("srv", "alpha")


@pytest.mark.asyncio
async def test_ambiguous_legacy_deny_fails_closed_but_canonical_allow_is_exact():
    raw = [
        _raw_tool(1, name="report.v1"),
        _raw_tool(2, name="report/v1"),
    ]
    legacy_collision = "mcp_srv_report_v1"
    selected = _canonical_tool_id("srv", "report.v1")
    rules = [
        Rule(permission=legacy_collision, pattern="*", action="deny"),
        Rule(permission=selected, pattern="*", action="allow"),
    ]

    tools = await create_mcp_tools(
        Sandbox(raw), rules, agent_id="build"
    )

    assert len(tools) == 1
    executor = next(iter(tools.values())).execute
    assert executor._mcp_canonical_id == selected
    assert executor._mcp_raw_identity == ("srv", "report.v1")


@pytest.mark.asyncio
async def test_threshold_uses_permission_filtered_count():
    raw = [_raw_tool(i) for i in range(41)]
    rules = [
        Rule(
            permission=_canonical_tool_id("srv", f"search_fixture_{i}"),
            pattern="*",
            action="deny",
        )
        for i in (0, 1)
    ]
    tools = await create_mcp_tools(Sandbox(raw), rules, agent_id="build")

    assert len(tools) == 39
    assert "mcp_find_tool" not in tools
    assert "mcp_call_tool" not in tools


@pytest.mark.asyncio
async def test_small_catalogue_with_one_oversized_schema_uses_meta_without_truncation():
    sentinel = "OVERSIZED_MCP_SCHEMA_SENTINEL_35d4"
    raw = [_raw_tool(1, name="large")]
    raw[0]["input_schema"]["properties"][sentinel] = {
        "type": "string",
        "description": "x" * 6_000,
    }

    tools = await create_mcp_tools(Sandbox(raw), [], agent_id="build")

    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    assert sentinel not in tools["mcp_find_tool"].description


@pytest.mark.asyncio
async def test_small_catalogue_over_total_definition_budget_uses_meta():
    raw = []
    for index in range(8):
        tool = _raw_tool(index)
        tool["input_schema"]["properties"][f"payload_{index}"] = {
            "type": "string",
            "description": str(index) * 4_500,
        }
        raw.append(tool)

    tools = await create_mcp_tools(Sandbox(raw), [], agent_id="build")

    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}


@pytest.mark.asyncio
async def test_meta_search_obeys_aggregate_call_id_and_output_limits():
    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    ctx = _ctx(
        sandbox,
        _capability_max_search_calls=1,
        _capability_max_reveals=2,
        _capability_max_result_chars=700,
    )

    first = await tools["mcp_find_tool"].execute(
        {"query": "fixture", "server": "srv"}, ctx
    )
    second = await tools["mcp_find_tool"].execute(
        {"query": "fixture", "server": "srv"}, ctx
    )

    assert first.metadata.get("blocked") is not True
    assert first.output.count("canonical_id:") <= 2
    assert len(first.output) <= 700
    assert len(ctx._capability_revealed_ids) <= 2
    assert second.metadata["blocked"] is True


@pytest.mark.asyncio
async def test_meta_search_uses_bounded_index_but_call_keeps_full_arguments():
    class CountingProperties(dict):
        visits = 0

        def items(self):
            for item in super().items():
                type(self).visits += 1
                yield item

    late_parameter = "late_parameter_9999"
    properties = CountingProperties({
        **{
            f"parameter_{index:04d}": {
                "type": "string",
                "description": "parameter hint " + ("x" * 1_000),
            }
            for index in range(10_000)
        },
        late_parameter: {
            "type": "string",
            "description": "must stay in the execution contract",
        },
    })
    target = {
        "server": "bounded-server-" + ("s" * 10_000),
        "name": "bounded_index_target_" + ("n" * 10_000),
        "description": "bounded-search " + ("d" * 100_000),
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [late_parameter] * 10_000,
        },
    }
    target_binding = _build_bindings([target])
    CountingProperties.visits = 0
    bounded_index = _build_meta_search_index(target_binding)
    assert CountingProperties.visits <= MCP_META_INDEX_PARAMETER_LIMIT
    assert len(bounded_index) == 1
    assert len(bounded_index[0].searchable) <= 402
    assert len(bounded_index[0].description_hint) <= 200
    assert len(bounded_index[0].parameter_details) <= MCP_META_INDEX_PARAMETER_LIMIT

    raw = [target, *[_raw_tool(index) for index in range(40)]]
    sandbox = Sandbox(raw)
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    assert len(tools["mcp_find_tool"].description) < 2_000

    # Catalogue digesting may inspect the full contract once. Repeated search
    # must use only the precomputed bounded projection and never revisit it.
    CountingProperties.visits = 0
    ctx = _ctx(sandbox)
    found = await tools["mcp_find_tool"].execute(
        {"query": "bounded-search", "server": ""}, ctx
    )
    assert CountingProperties.visits == 0
    assert len(found.output) <= 2_000
    assert found.output.count("parameter_") <= MCP_META_INDEX_PARAMETER_LIMIT
    assert late_parameter not in found.output

    target_id = _canonical_tool_id(target["server"], target["name"])
    called = await tools["mcp_call_tool"].execute(
        {
            "canonical_id": target_id,
            "arguments": {late_parameter: "preserved"},
        },
        ctx,
    )
    assert called.metadata.get("blocked") is not True
    assert sandbox.calls == [
        (target["server"], target["name"], {late_parameter: "preserved"})
    ]


@pytest.mark.asyncio
async def test_normalization_cache_singleflights_and_does_not_rewalk_same_generation(
    monkeypatch,
):
    from tool import mcp_tool as mcp_module

    class CountingProperties(dict):
        visits = 0

        def items(self):
            for item in super().items():
                type(self).visits += 1
                yield item

    properties = CountingProperties({
        f"parameter_{index}": {"type": "string"}
        for index in range(10_000)
    })
    target = {
        "server": "srv",
        "name": "large_cached_schema",
        "description": "cache target",
        "input_schema": {"type": "object", "properties": properties},
    }
    sandbox = ProjectedSandbox(
        [target, *[_raw_tool(index) for index in range(40)]]
    )
    original = mcp_module._build_normalization_artifacts
    builds = 0

    def counted(raw):
        nonlocal builds
        builds += 1
        return original(raw)

    monkeypatch.setattr(mcp_module, "_build_normalization_artifacts", counted)
    first = await asyncio.gather(*[
        create_mcp_tools(sandbox, [], agent_id="build")
        for _ in range(12)
    ])

    assert builds == 1
    assert CountingProperties.visits >= 10_000
    assert all(set(tools) == {"mcp_find_tool", "mcp_call_tool"} for tools in first)

    CountingProperties.visits = 0
    await create_mcp_tools(sandbox, [], agent_id="build")
    assert builds == 1
    assert CountingProperties.visits == 0


@pytest.mark.asyncio
async def test_normalization_cache_filters_permission_after_every_read(monkeypatch):
    from tool import mcp_tool as mcp_module

    sentinel = "CACHED_DENIED_MCP_SENTINEL_583c"
    target = _raw_tool(99, name=sentinel)
    target["description"] = sentinel
    raw = [target, *[_raw_tool(index) for index in range(41)]]
    sandbox = ProjectedSandbox(raw)
    original = mcp_module._build_normalization_artifacts
    builds = 0

    def counted(items):
        nonlocal builds
        builds += 1
        return original(items)

    monkeypatch.setattr(mcp_module, "_build_normalization_artifacts", counted)
    allowed = await create_mcp_tools(sandbox, [], agent_id="build")
    denied_id = _canonical_tool_id("srv", sentinel)
    denied = await create_mcp_tools(
        sandbox,
        [Rule(permission=denied_id, pattern="*", action="deny")],
        agent_id="build",
    )
    result = await denied["mcp_find_tool"].execute(
        {"query": sentinel}, _ctx(sandbox)
    )

    assert builds == 1
    assert set(allowed) == set(denied) == {"mcp_find_tool", "mcp_call_tool"}
    assert sentinel not in result.title + result.output


@pytest.mark.asyncio
async def test_normalization_cache_is_scope_generation_and_schema_isolated(monkeypatch):
    from tool import mcp_tool as mcp_module

    original = mcp_module._build_normalization_artifacts
    builds = 0

    def counted(items):
        nonlocal builds
        builds += 1
        return original(items)

    monkeypatch.setattr(mcp_module, "_build_normalization_artifacts", counted)
    raw = [_raw_tool(1, name="scoped")]
    sandbox = ProjectedSandbox(raw)

    first = await create_mcp_tools(sandbox, [], agent_id="build")
    await create_mcp_tools(sandbox, [], agent_id="build")
    assert builds == 1

    # Every available isolation dimension participates in the opaque key.
    for attribute, value in (
        ("user_id", "user-b"),
        ("project_id", "project-b"),
        ("sandbox_id", "sandbox-b"),
        ("region", "region-b"),
        ("api_key", "credential-b"),
    ):
        setattr(sandbox._sandbox, attribute, value)
        await create_mcp_tools(sandbox, [], agent_id="build")
    assert builds == 6

    schema = next(iter(first.values())).parameters.model_json_schema()
    schema["properties"]["value"]["type"] = "number"
    same_generation = await create_mcp_tools(sandbox, [], agent_id="build")
    clean = next(iter(same_generation.values())).parameters.model_json_schema()
    assert clean["properties"]["value"]["type"] == "string"

    raw[0]["input_schema"]["properties"]["changed"] = {"type": "boolean"}
    sandbox.set_generation("mcp-generation-b")
    changed = await create_mcp_tools(sandbox, [], agent_id="build")
    changed_schema = next(iter(changed.values())).parameters.model_json_schema()
    assert changed_schema["properties"]["changed"]["type"] == "boolean"
    assert builds == 7


@pytest.mark.asyncio
async def test_normalization_cache_ttl_capacity_and_failure_recovery(monkeypatch):
    from tool import mcp_tool as mcp_module

    now = 10.0
    monkeypatch.setattr(mcp_module, "_mcp_normalization_now", lambda: now)
    monkeypatch.setattr(mcp_module, "MCP_NORMALIZATION_CACHE_TTL_SECONDS", 5.0)
    monkeypatch.setattr(mcp_module, "MCP_NORMALIZATION_CACHE_MAX_ENTRIES", 2)
    original = mcp_module._build_normalization_artifacts
    attempts = 0

    def flaky(items):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient normalization failure")
        return original(items)

    monkeypatch.setattr(mcp_module, "_build_normalization_artifacts", flaky)
    first_sandbox = ProjectedSandbox([_raw_tool(1)])
    assert await create_mcp_tools(first_sandbox, [], agent_id="build") == {}
    assert await create_mcp_tools(first_sandbox, [], agent_id="build")
    assert attempts == 2
    assert await create_mcp_tools(first_sandbox, [], agent_id="build")
    assert attempts == 2

    now += 6.0
    assert await create_mcp_tools(first_sandbox, [], agent_id="build")
    assert attempts == 3

    await create_mcp_tools(
        ProjectedSandbox([_raw_tool(2)], generation="generation-2"),
        [],
        agent_id="build",
    )
    await create_mcp_tools(
        ProjectedSandbox([_raw_tool(3)], generation="generation-3"),
        [],
        agent_id="build",
    )
    assert len(mcp_module._MCP_NORMALIZATION_CACHE) <= 2
    assert attempts == 5


@pytest.mark.asyncio
async def test_meta_connect_failure_never_reflects_unbounded_raw_identity():
    import httpx

    sentinel = "RAW_MCP_IDENTITY_SECRET_19af"
    target = {
        "server": sentinel + ("s" * 100_000),
        "name": sentinel + ("n" * 100_000),
        "description": "bounded-failure-target",
        "input_schema": {"type": "object", "properties": {}},
    }
    sandbox = Sandbox([target, *[_raw_tool(index) for index in range(40)]])
    sandbox.raise_call = httpx.ConnectError("tunnel offline")
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    ctx = _ctx(sandbox)
    target_id = _canonical_tool_id(target["server"], target["name"])

    found = await tools["mcp_find_tool"].execute(
        {"query": "bounded-failure-target"}, ctx
    )
    result = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, ctx
    )

    assert target_id in found.output
    assert sentinel not in result.title + result.output
    assert target["server"] not in result.title + result.output
    assert target["name"] not in result.title + result.output
    assert target_id in result.title + result.output
    assert len(result.title.encode("utf-8")) <= MCP_FAILURE_IDENTITY_CHARS + 40
    assert len(result.output.encode("utf-8")) <= MCP_FAILURE_MAX_BYTES
    assert result.metadata == {
        "truncated": True,
        "identity_redacted": True,
    }
    assert sandbox.calls == [(target["server"], target["name"], {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_direct_result_title_never_reflects_unbounded_raw_identity(is_error):
    sentinel = "RAW_DIRECT_MCP_IDENTITY_SECRET_283a"
    target = {
        "server": "srv",
        "name": sentinel + ("n" * 100_000),
        "description": "direct bounded result title",
        "input_schema": {"type": "object", "properties": {}},
    }
    remote_result = {
        "isError": is_error,
        "content": [{"type": "text", "text": "unchanged direct output"}],
    }

    class ResultSandbox(Sandbox):
        async def call_mcp_tool(self, server: str, tool: str, arguments: dict):
            self.calls.append((server, tool, arguments))
            return remote_result

    sandbox = ResultSandbox([target])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    assert set(tools) != {"mcp_find_tool", "mcp_call_tool"}
    result = await next(iter(tools.values())).execute({}, _ctx(sandbox))
    target_id = _canonical_tool_id(target["server"], target["name"])

    assert result.title == f"{'Error: ' if is_error else ''}{target_id}"
    assert sentinel not in result.title
    assert target["name"] not in result.title
    assert result.output == json.dumps(remote_result, ensure_ascii=False, default=str)
    assert sandbox.calls == [(target["server"], target["name"], {})]


@pytest.mark.asyncio
@pytest.mark.parametrize("is_error", [False, True])
async def test_meta_result_title_never_reflects_unbounded_raw_identity(is_error):
    sentinel = "RAW_META_MCP_IDENTITY_SECRET_b717"
    target = {
        "server": sentinel + ("s" * 100_000),
        "name": sentinel + ("n" * 100_000),
        "description": "meta bounded result title",
        "input_schema": {"type": "object", "properties": {}},
    }
    remote_result = {
        "isError": is_error,
        "content": [{"type": "text", "text": "unchanged meta output"}],
    }

    class ResultSandbox(Sandbox):
        async def call_mcp_tool(self, server: str, tool: str, arguments: dict):
            self.calls.append((server, tool, arguments))
            return remote_result

    sandbox = ResultSandbox([target, *[_raw_tool(index) for index in range(40)]])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    ctx = _ctx(sandbox)
    target_id = _canonical_tool_id(target["server"], target["name"])
    await tools["mcp_find_tool"].execute({"query": "bounded result title"}, ctx)
    result = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, ctx
    )

    assert result.title == f"{'Error: ' if is_error else ''}{target_id}"
    assert sentinel not in result.title
    assert target["server"] not in result.title
    assert target["name"] not in result.title
    assert result.output == json.dumps(remote_result, ensure_ascii=False, default=str)
    assert sandbox.calls == [(target["server"], target["name"], {})]


@pytest.mark.asyncio
async def test_meta_index_never_leaks_or_executes_denied_tool():
    sentinel = "DENIED_MCP_SENTINEL_7d12"
    raw = [_raw_tool(i) for i in range(42)]
    raw[-1] = {
        "server": f"server-{sentinel}",
        "name": f"tool-{sentinel}",
        "description": f"description-{sentinel}",
        "input_schema": {
            "type": "object",
            "properties": {sentinel: {"type": "string"}},
        },
    }
    denied_id = _canonical_tool_id(raw[-1]["server"], raw[-1]["name"])
    sandbox = Sandbox(raw)
    tools = await create_mcp_tools(
        sandbox,
        [Rule(permission=denied_id, pattern="*", action="deny")],
        agent_id="build",
    )
    assert set(tools) == {"mcp_find_tool", "mcp_call_tool"}
    assert sentinel not in tools["mcp_find_tool"].description

    ctx = _ctx(sandbox)
    found = await tools["mcp_find_tool"].execute(
        {"query": sentinel, "server": ""}, ctx
    )
    guessed = await tools["mcp_call_tool"].execute(
        {"canonical_id": denied_id, "arguments": {"secret": sentinel}}, ctx
    )

    assert sentinel not in found.title + found.output + str(found.metadata)
    assert guessed.metadata["blocked"] is True
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_meta_call_requires_exact_discovery_and_authorizes_canonical_args():
    raw = [_raw_tool(i) for i in range(41)]
    sandbox = Sandbox(raw)
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_17")
    authorized = []

    async def authorize(tool_id, arguments):
        authorized.append((tool_id, arguments))
        return None

    ctx = _ctx(sandbox, authorize)
    guessed = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {"value": "before"}}, ctx
    )
    fuzzy = await tools["mcp_call_tool"].execute(
        {"canonical_id": "search_fixture_17", "arguments": {}}, ctx
    )
    assert guessed.metadata["blocked"] is True
    assert fuzzy.metadata["blocked"] is True
    assert sandbox.calls == []
    assert authorized == []

    found = await tools["mcp_find_tool"].execute(
        {"query": "fixture_17", "server": "srv"}, ctx
    )
    assert target_id in found.output
    result = await tools["mcp_call_tool"].execute(
        {
            "canonical_id": target_id,
            "arguments": {"value": "after", "drop": None},
        },
        ctx,
    )

    assert result.metadata.get("blocked") is not True
    assert authorized == [(target_id, {"value": "after"})]
    assert sandbox.calls == [("srv", "search_fixture_17", {"value": "after"})]


@pytest.mark.asyncio
async def test_discovery_evidence_survives_per_step_tool_reconstruction():
    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    first_step = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_12")
    ctx = _ctx(sandbox)
    await first_step["mcp_find_tool"].execute({"query": "fixture_12"}, ctx)

    second_step = await create_mcp_tools(sandbox, [], agent_id="build")
    result = await second_step["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {"value": "next-step"}},
        ctx,
    )

    assert result.metadata.get("blocked") is not True
    assert sandbox.calls == [
        ("srv", "search_fixture_12", {"value": "next-step"})
    ]


@pytest.mark.asyncio
async def test_discovery_evidence_unwraps_snapshot_but_isolates_real_sandboxes():
    class RuntimeSandbox:
        # Deliberately identical across the two live objects: isolation must not
        # rely on an endpoint being globally unique.
        base_url = "http://shared-action.test"

        def __init__(self):
            self.calls: list[tuple[str, str, dict]] = []

        async def call_mcp_tool(self, server: str, tool: str, arguments: dict):
            self.calls.append((server, tool, arguments))
            return {"isError": False, "content": [{"type": "text", "text": "ok"}]}

    raw = [_raw_tool(index) for index in range(41)]
    snapshot = {
        "boot_id": "boot-a",
        "mcp_generation": "mcp-generation-a",
        "generation": "catalogue-generation-a",
        "skills": [],
        "mcp_tools": raw,
        "mcp_resources": [],
    }
    real = RuntimeSandbox()
    first_step = await create_mcp_tools(
        _CatalogueSnapshotSandbox(real, snapshot), [], agent_id="build"
    )
    target_id = _canonical_tool_id("srv", "search_fixture_17")
    ctx = _ctx(real)

    found = await first_step["mcp_find_tool"].execute(
        {"query": "fixture_17", "server": "srv"}, ctx
    )
    assert target_id in found.output

    # A new per-step snapshot wrapper around the same real client must retain
    # the evidence recorded by the preceding search.
    second_step = await create_mcp_tools(
        _CatalogueSnapshotSandbox(real, snapshot), [], agent_id="build"
    )
    called = await second_step["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {"value": "same-client"}},
        ctx,
    )
    assert called.metadata.get("blocked") is not True
    assert real.calls == [
        ("srv", "search_fixture_17", {"value": "same-client"})
    ]

    # A distinct live client with the same public URL cannot reuse that bearer
    # evidence.
    other_real = RuntimeSandbox()
    isolated = await second_step["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, _ctx(other_real)
    )
    assert isolated.metadata["blocked"] is True
    assert other_real.calls == []


@pytest.mark.asyncio
async def test_call_time_underlying_authorization_can_block_execution():
    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_5")

    async def deny(_tool_id, _arguments):
        return ToolResult(
            title="Permission denied",
            output="blocked",
            metadata={"blocked": True},
        )

    ctx = _ctx(sandbox, deny)
    await tools["mcp_find_tool"].execute({"query": "fixture_5"}, ctx)
    result = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {"value": "x"}}, ctx
    )

    assert result.title == "Permission denied"
    assert sandbox.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "other"),
    [
        ("user_id", "user-b"),
        ("project_id", "project-b"),
        ("session_id", "session-b"),
        ("run_id", "run-b"),
    ],
)
async def test_discovery_evidence_is_bound_to_context_scope(field, other):
    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_9")
    original = _ctx(sandbox)
    await tools["mcp_find_tool"].execute({"query": "fixture_9"}, original)

    changed = replace(original, **{field: other})
    result = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, changed
    )

    assert result.metadata["blocked"] is True
    assert sandbox.calls == []


@pytest.mark.asyncio
async def test_evidence_is_bound_to_agent_sandbox_generation_and_schema_digest():
    raw = [_raw_tool(i) for i in range(41)]
    sandbox = Sandbox(raw)
    build_tools = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_3")
    ctx = _ctx(sandbox)
    await build_tools["mcp_find_tool"].execute({"query": "fixture_3"}, ctx)

    other_agent_tools = await create_mcp_tools(sandbox, [], agent_id="plan")
    other_agent = await other_agent_tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, ctx
    )

    other_sandbox = Sandbox(raw, sandbox_id="sandbox-b")
    sandbox_changed = await build_tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, _ctx(other_sandbox)
    )

    refreshed = [dict(item) for item in raw]
    refreshed[3] = {
        **refreshed[3],
        "description": "schema generation changed",
        "input_schema": {
            "type": "object",
            "properties": {"new_value": {"type": "integer"}},
        },
    }
    refreshed_tools = await create_mcp_tools(
        Sandbox(refreshed, sandbox_id="sandbox-a"), [], agent_id="build"
    )
    generation_changed = await refreshed_tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, ctx
    )

    assert other_agent.metadata["blocked"] is True
    assert sandbox_changed.metadata["blocked"] is True
    assert generation_changed.metadata["blocked"] is True
    assert sandbox.calls == []
    assert other_sandbox.calls == []


@pytest.mark.asyncio
async def test_expired_evidence_cannot_execute():
    sandbox = Sandbox([_raw_tool(i) for i in range(41)])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    target_id = _canonical_tool_id("srv", "search_fixture_7")
    ctx = _ctx(sandbox)
    await tools["mcp_find_tool"].execute({"query": "fixture_7"}, ctx)
    for key in list(_DISCOVERY_EVIDENCE):
        _DISCOVERY_EVIDENCE[key] = 0.0

    result = await tools["mcp_call_tool"].execute(
        {"canonical_id": target_id, "arguments": {}}, ctx
    )
    assert result.metadata["blocked"] is True
    assert sandbox.calls == []


def test_ambiguous_canonical_digest_fails_closed(monkeypatch):
    forced = MCP_CANONICAL_PREFIX + "a" * 52
    monkeypatch.setattr("tool.mcp_tool._canonical_tool_id", lambda _server, _name: forced)
    bindings = _build_bindings([
        _raw_tool(1, server="one", name="alpha"),
        _raw_tool(2, server="two", name="beta"),
    ])
    assert bindings == []


@pytest.mark.asyncio
async def test_merge_passes_rules_and_never_overwrites_platform_namespace(monkeypatch):
    captured = {}

    class Params:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {}}

    async def execute(_args, _ctx):
        return ToolResult(title="mcp")

    async def fake_create(_sandbox, ruleset, *, agent_id):
        captured["ruleset"] = ruleset
        captured["agent_id"] = agent_id
        return {
            "mcp_collision": ToolInfo(
                id="mcp_collision", parameters=Params, description="mcp", execute=execute
            )
        }

    monkeypatch.setattr("tool.mcp_tool.create_mcp_tools", fake_create)
    original = ToolInfo(
        id="mcp_collision", parameters=Params, description="platform", execute=execute
    )
    rules = [Rule(permission="mcp:*", pattern="*", action="deny")]
    tools = {"mcp_collision": original}

    result = await merge_sandbox_tools(
        tools, Sandbox([]), rules, agent_id="build"
    )

    assert result["mcp_collision"] is original
    assert captured == {"ruleset": rules, "agent_id": "build"}


@pytest.mark.asyncio
async def test_merge_never_installs_partial_meta_pair_over_platform_tool(monkeypatch):
    class Params:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {}}

    async def execute(_args, _ctx):
        return ToolResult(title="ok")

    async def fake_create(_sandbox, _ruleset, *, agent_id):
        assert agent_id == "build"
        return {
            name: ToolInfo(
                id=name, parameters=Params, description=name, execute=execute
            )
            for name in ("mcp_find_tool", "mcp_call_tool")
        }

    monkeypatch.setattr("tool.mcp_tool.create_mcp_tools", fake_create)
    platform_find = ToolInfo(
        id="mcp_find_tool",
        parameters=Params,
        description="platform",
        execute=execute,
    )
    result = await merge_sandbox_tools(
        {"mcp_find_tool": platform_find}, Sandbox([]), [], agent_id="build"
    )

    assert result == {"mcp_find_tool": platform_find}


@pytest.mark.asyncio
async def test_success_and_failure_logs_never_contain_argument_values(monkeypatch):
    sentinel = "SECRET_ARGUMENT_SENTINEL_92b1"
    sandbox = Sandbox([_raw_tool(1, name="safe_tool")])
    tools = await create_mcp_tools(sandbox, [], agent_id="build")
    messages = []

    def capture(message, *args, **_kwargs):
        messages.append(message % args if args else str(message))

    monkeypatch.setattr("tool.mcp_tool.log.info", capture)
    monkeypatch.setattr("tool.mcp_tool.log.error", capture)
    info = next(iter(tools.values()))
    ctx = _ctx(sandbox)
    await info.execute({"value": sentinel}, ctx)
    sandbox.raise_call = RuntimeError(f"rejected {sentinel}")
    await info.execute({"value": sentinel}, ctx)

    assert sentinel not in "\n".join(messages)
    assert any("argument_count=1" in message for message in messages)
