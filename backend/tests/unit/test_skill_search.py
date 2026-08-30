"""Atomic large-Skill listing cap and permission-filtered search contracts."""

from __future__ import annotations

import pytest

import skill.skill as skill_directory
from agent.agent import AgentDef
from agent.tool_exposure import (
    build_eligible_catalog,
    portable_plan,
    provider_tools_for_plan,
    step_executable_ids,
)
from agent.tool_resolution import attach_skill_listing, strip_denied
from permission.permission import Rule
from skill.skill import SkillInfo
from tool.batch import BatchArgs, Invocation, execute as execute_batch
from tool.capability_search import capability_search_tool
from tool.mcp_tool import create_mcp_tools
from tool.skill_tool import (
    LISTING_HARD_CHARS_DEFAULT,
    SKILL_SEARCH_MAX_RESULTS,
    SKILL_SEARCH_RESULT_CHARS,
    render_listing,
    skill_search_tool,
    skill_tool,
)
from tool.tool import ToolContext, ToolInfo, ToolResult


DESCRIPTION = (
    "Use this common directory skill for structured files, analysis, and "
    "repeatable project workflows."
)


def _catalogue(count: int, *, prefix: str = "skill") -> list[dict]:
    return [
        {"name": f"{prefix}-{index:04d}", "description": DESCRIPTION}
        for index in range(count)
    ]


class CatalogueSandbox:
    def __init__(self, entries: list[dict], bodies: dict[str, str] | None = None):
        self.entries = entries
        self.bodies = bodies or {}
        self.loads: list[str] = []

    async def list_skills(self):
        return list(self.entries)

    async def get_skill(self, name: str):
        self.loads.append(name)
        if name not in self.bodies:
            raise KeyError(name)
        return {
            "content": self.bodies[name],
            "base_dir": f"/data/skills/{name}",
            "files": [],
        }


@pytest.fixture(autouse=True)
def stable_hard_cap(monkeypatch):
    monkeypatch.setattr(
        "tool.skill_tool._listing_hard_chars",
        lambda: LISTING_HARD_CHARS_DEFAULT,
    )


@pytest.fixture
def no_host_skills(monkeypatch):
    async def empty():
        return []

    async def missing(_name: str):
        return None

    monkeypatch.setattr(skill_directory, "list_skills", empty)
    monkeypatch.setattr(skill_directory, "get_skill", missing)


def _base_tools() -> dict:
    return {"skill": skill_tool, "skill_search": skill_search_tool}


def _listing_from_description(description: str) -> str:
    marker = "<available_skills>"
    assert marker in description
    return marker + description.split(marker, 1)[1]


@pytest.mark.asyncio
async def test_search_is_conditional_and_wire_listing_is_hard_bounded(no_host_skills):
    small = await attach_skill_listing(
        _base_tools(), CatalogueSandbox(_catalogue(3)), ruleset=[]
    )
    assert set(small) == {"skill"}
    assert "skill-0002" in small["skill"].description

    entries = _catalogue(1_000)
    complete_meter = render_listing(entries)
    assert len(complete_meter) > LISTING_HARD_CHARS_DEFAULT
    assert "skill-0999" in complete_meter, "PR#0 must still meter every name"

    large = await attach_skill_listing(
        _base_tools(), CatalogueSandbox(entries), ruleset=[]
    )
    assert set(large) == {"skill", "skill_search"}
    listing = _listing_from_description(large["skill"].description)
    assert len(listing) <= LISTING_HARD_CHARS_DEFAULT
    assert listing.endswith("</available_skills>")
    assert "use skill_search" in listing
    assert "skill-0999" not in listing

    schema = large["skill_search"].parameters.model_json_schema()
    assert set(schema["properties"]) == {"query", "name"}
    result = await large["skill_search"].execute(
        {"query": "common directory"}, ToolContext()
    )
    assert result.metadata["count"] == SKILL_SEARCH_MAX_RESULTS
    assert result.output.count("<skill>") == SKILL_SEARCH_MAX_RESULTS
    assert len(result.output) <= SKILL_SEARCH_RESULT_CHARS


@pytest.mark.asyncio
async def test_large_listing_with_denied_search_companion_fails_closed(
    no_host_skills,
    caplog,
):
    secret_name = "DENIED_SKILL_DIRECTORY_SENTINEL_94c1"
    entries = _catalogue(1_000) + [
        {"name": secret_name, "description": "must never reach the provider wire"}
    ]
    agent = AgentDef(name="qa", description="", tools=["skill"])
    denied = strip_denied(
        _base_tools(),
        [Rule(permission="skill_search", pattern="*", action="deny")],
        agent,
    )
    assert set(denied) == {"skill"}

    resolved = await attach_skill_listing(
        denied,
        CatalogueSandbox(entries),
        ruleset=[Rule(permission="skill_search", pattern="*", action="deny")],
    )

    assert resolved == {}
    assert secret_name not in caplog.text


@pytest.mark.asyncio
async def test_skill_mcp_and_capability_search_share_one_step_budget(no_host_skills):
    skills = await attach_skill_listing(
        _base_tools(),
        CatalogueSandbox(_catalogue(1_000)),
        ruleset=[],
    )

    class McpSandbox:
        sandbox_id = "mixed-search-sandbox"

        async def list_mcp_tools(self):
            return [
                {
                    "server": "mixed",
                    "name": f"lookup_{index}",
                    "description": "mixed aggregate search fixture",
                    "input_schema": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }
                for index in range(41)
            ]

    sandbox = McpSandbox()
    mcp = await create_mcp_tools(sandbox, [], agent_id="build")
    assert set(mcp) == {"mcp_find_tool", "mcp_call_tool"}

    class EmptyParams:
        @classmethod
        def model_json_schema(cls):
            return {"type": "object", "properties": {}}

    async def no_execute(_args, _ctx):
        return ToolResult(title="hidden")

    hidden = ToolInfo(
        id="hidden_lookup",
        description="hidden capability lookup",
        parameters=EmptyParams,
        execute=no_execute,
    )
    catalogue = build_eligible_catalog({"hidden_lookup": hidden})
    ctx = ToolContext(
        user_id="user-mixed",
        project_id="project-mixed",
        session_id="session-mixed",
        run_id="run-mixed",
        agent_id="build",
        sandbox=sandbox,
        _capability_catalog=catalogue,
        _capability_discovery_ids=frozenset({"hidden_lookup"}),
        _capability_max_search_calls=2,
        _capability_max_reveals=5,
        _capability_max_result_chars=2_000,
    )

    skill_result = await skills["skill_search"].execute(
        {"name": "skill-0999"}, ctx
    )
    mcp_result = await mcp["mcp_find_tool"].execute(
        {"query": "aggregate", "server": "mixed"}, ctx
    )
    capability_result = await capability_search_tool.execute(
        {"query": "hidden", "names": []}, ctx
    )

    assert skill_result.metadata["count"] == 1
    assert mcp_result.metadata.get("blocked") is not True
    assert capability_result.metadata["blocked"] is True
    assert ctx._capability_search_calls == 2
    assert len(ctx._capability_revealed_ids) <= 5
    assert ctx._capability_result_chars <= 2_000


@pytest.mark.asyncio
async def test_skill_search_is_not_callable_through_batch(
    no_host_skills,
    monkeypatch,
):
    tools = await attach_skill_listing(
        _base_tools(),
        CatalogueSandbox(_catalogue(1_000)),
        ruleset=[],
    )
    search = tools["skill_search"]
    assert search.parallel_safe is False
    monkeypatch.setattr(
        "tool.registry.get_tool",
        lambda tool_id: search if tool_id == "skill_search" else None,
    )
    ctx = ToolContext(available_tools=frozenset({"skill_search"}))

    result = await execute_batch(
        BatchArgs(
            invocations=[
                Invocation(
                    tool="skill_search",
                    parameters={"name": "skill-0999"},
                )
            ]
        ),
        ctx,
    )

    assert "not safe for parallel execution" in result.output
    assert ctx._capability_search_calls == 0


@pytest.mark.asyncio
async def test_permission_filter_precedes_overflow_decision_and_search_index(no_host_skills):
    secret_name = "secret-tail"
    secret_hint = "DENIED_DESCRIPTION_SENTINEL_7d92"
    rules = [
        Rule(permission="skill", pattern="*", action="allow"),
        Rule(permission="skill", pattern="secret-*", action="deny"),
    ]

    # A huge raw directory that becomes small after permission filtering must
    # neither truncate nor expose a now-useless search tool.
    mostly_denied = _catalogue(1_000, prefix="secret") + [
        {"name": "visible", "description": "allowed"}
    ]
    small = await attach_skill_listing(
        _base_tools(), CatalogueSandbox(mostly_denied), ruleset=rules
    )
    assert set(small) == {"skill"}
    assert "visible" in small["skill"].description
    assert "secret-" not in small["skill"].description

    # With a genuinely large permitted directory, an exact denied lookup is
    # still absent because the row was removed before index construction.
    entries = _catalogue(1_000) + [{
        "name": secret_name,
        "description": secret_hint,
        "content": "DENIED_BODY_SENTINEL_1b64",
    }]
    large = await attach_skill_listing(
        _base_tools(), CatalogueSandbox(entries), ruleset=rules
    )
    assert "skill_search" in large
    encoded_listing = large["skill"].description
    assert secret_name not in encoded_listing
    assert secret_hint not in encoded_listing

    denied = await large["skill_search"].execute(
        {"name": secret_name}, ToolContext()
    )
    assert denied.metadata["count"] == 0
    assert secret_name not in denied.output
    assert secret_hint not in denied.output


@pytest.mark.asyncio
async def test_project_global_container_are_searchable_but_bodies_never_are(monkeypatch):
    resource_body = "RESOURCE_BODY_SENTINEL_63ac"
    host_body = "HOST_SKILL_BODY_SENTINEL_c278"
    malicious_name = '000-evil<&"'
    entries = _catalogue(997) + [
        {
            "name": "container-only",
            "description": "container lookup hint",
            "content": resource_body,
            "contents": [{"text": resource_body}],
            "resource": {"body": resource_body},
        },
        {
            "name": malicious_name,
            "description": "escape <script>& lookup",
        },
    ]

    async def host_skills():
        return [
            SkillInfo("global-only", "global lookup hint", "global", host_body),
            SkillInfo("project-only", "project lookup hint", "project", host_body),
        ]

    monkeypatch.setattr(skill_directory, "list_skills", host_skills)
    tools = await attach_skill_listing(
        _base_tools(), CatalogueSandbox(entries), ruleset=[]
    )
    assert "skill_search" in tools
    assert resource_body not in tools["skill"].description
    assert host_body not in tools["skill"].description
    assert malicious_name not in tools["skill"].description
    assert "000-evil&lt;&amp;&quot;" in tools["skill"].description

    for name in ("container-only", "global-only", "project-only"):
        result = await tools["skill_search"].execute(
            {"name": name}, ToolContext()
        )
        assert result.metadata["count"] == 1
        assert name in result.output
        assert resource_body not in result.output
        assert host_body not in result.output

    escaped = await tools["skill_search"].execute(
        {"name": malicious_name}, ToolContext()
    )
    assert malicious_name not in escaped.output
    assert "000-evil&lt;&amp;&quot;" in escaped.output


@pytest.mark.asyncio
async def test_tail_exact_search_then_load_does_not_change_tool_authority(no_host_skills):
    tail = "zz-tail-exact-skill"
    body = (
        "---\nname: zz-tail-exact-skill\n"
        "allowed-tools: [bash, image_gen, mcp_call_tool]\n---\n"
        "TAIL_SKILL_BODY_LOADED"
    )
    sandbox = CatalogueSandbox(
        _catalogue(1_000) + [{"name": tail, "description": "unique tail workflow"}],
        bodies={tail: body},
    )
    tools = await attach_skill_listing(_base_tools(), sandbox, ruleset=[])
    assert tail not in _listing_from_description(tools["skill"].description)

    before_keys = tuple(tools)
    before_catalogue = build_eligible_catalog(tools)
    before_plan = portable_plan(before_catalogue, agent_name="build")
    assert set(before_plan.direct_ids) == {"skill", "skill_search"}
    assert before_plan.schema_chars == sum(
        before_catalogue.entries[tool_id].schema_chars
        for tool_id in before_plan.direct_ids
    )

    ctx = ToolContext(
        sandbox=sandbox,
        available_tools=frozenset(tools),
    )
    found = await tools["skill_search"].execute({"name": tail}, ctx)
    assert found.metadata["count"] == 1
    assert tail in found.output
    # The prefixed entry is only the shared per-step result-budget marker; it
    # is never committed to the tool exposure ledger or treated as a tool ID.
    assert ctx._capability_revealed_ids == {f"skill:{tail}"}

    loaded = await tools["skill"].execute({"skill": tail}, ctx)
    assert "TAIL_SKILL_BODY_LOADED" in loaded.output
    assert sandbox.loads == [tail]

    after_catalogue = build_eligible_catalog(tools)
    after_plan = portable_plan(after_catalogue, agent_name="build")
    assert tuple(tools) == before_keys
    assert after_catalogue.generation == before_catalogue.generation
    assert after_plan.direct_ids == before_plan.direct_ids
    assert set(provider_tools_for_plan(after_catalogue, after_plan)) == set(
        provider_tools_for_plan(before_catalogue, before_plan)
    )
    assert step_executable_ids(after_plan) == step_executable_ids(before_plan)
    assert ctx.available_tools == frozenset(before_keys)
    assert "bash" not in tools
    assert "image_gen" not in tools
    assert "mcp_call_tool" not in tools


def test_agent_definition_keeps_search_as_a_skill_companion():
    with_skill = AgentDef(name="with", description="", tools=["read", "skill"])
    without_skill = AgentDef(
        name="without", description="", tools=["read", "skill_search"]
    )
    empty = AgentDef(name="empty", description="", tools=[])

    assert with_skill.tools == ["read", "skill", "skill_search"]
    assert without_skill.tools == ["read"]
    assert empty.tools == []
