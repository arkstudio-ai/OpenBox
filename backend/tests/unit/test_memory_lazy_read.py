"""Deferred planning skips ordinary turns without weakening the original gate."""
import asyncio
from dataclasses import replace
import json

import pytest

from memory import packet
from memory.orchestrator import render_memory_context
from memory.read_contract import permits
from tests.unit.test_memory_read_protocol import allowed, context
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool.memory_tools import MemoryPrepareArgs, execute_memory_prepare
from tool.tool import ToolContext


def preparation_args():
    return MemoryPrepareArgs.model_validate({"plan": {
        "restriction": {"role": "none", "policy": "allowed", "quote": "", "confidence": 1.0},
        "memory": "retrieve", "task": "skip",
        "needs": {"history": 0.0, "knowledge": 0.0, "assets": 0.0, "experience": 0.0},
        "recent_context_useful": 0.0,
        "time": {"quote": "", "intent": "none", "confidence": 1.0}, "search_query": "",
    }})


async def deferred(monkeypatch, query="今天想放松一下，你有什么建议？"):
    config, turn = await context(monkeypatch)
    config.lazy_read_planning = True
    turn = replace(turn, query=query)
    return config, await packet.prepare_context(turn, config)


async def test_bootstrap_does_not_call_model_or_read_persistent_facts(monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail("Deferred bootstrap must use only local current settings")
    monkeypatch.setattr("memory.read_routing.route", forbidden)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    config, value = await deferred(monkeypatch)
    assert value["status"] == "deferred"
    assert not value["route"]["called"] and value["route"]["memory_forbidden"]
    assert value["_turn"].deferred_read
    assert value["items"] == value["stable_background"]["items"] == []
    assert not permits(value["_turn"], "search")
    assert value["settings"]["address"] == "老板"
    assert value["budget"]["tokens"] <= config.recall_token_budget
    assert "memory.prepare" in render_memory_context(value)
    assert "memory_prepare" in render_memory_context({**value, "_turn": replace(value["_turn"], entry="chat")})


async def test_prepare_preserves_original_restriction_and_cannot_accept_a_rewrite(monkeypatch):
    config, value = await deferred(monkeypatch, "只根据这一句说明，不要读取我以前的任何资料。")
    captured = []
    async def route(query, *args, **kwargs):
        captured.append(query)
        return {"read_policy": "current_only", "memory_forbidden": True,
                "memory": {"needed": False}, "task": {"needed": False}}
    async def forbidden(*args, **kwargs):
        pytest.fail("A deferred tool cannot bypass the original restriction")
    monkeypatch.setattr("memory.read_routing.route", route)
    monkeypatch.setattr("memory.retrieval.search_memory", forbidden)
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.readers.catalogs", forbidden)
    budget = packet.ReadBudget()
    result = await packet.prepare_on_demand(value["_turn"], budget, config)
    assert captured == [value["_turn"].query]
    assert result["status"] == "restricted" and not result["_turn"].deferred_read
    assert result["items"] == result["stable_background"]["items"] == []
    with pytest.raises(ValueError):
        MemoryPrepareArgs.model_validate({"query": "read everything", "read_policy": "allowed"})


async def test_concurrent_preparation_is_single_flight_and_keeps_supplement_budget(monkeypatch):
    config, value = await deferred(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def route(query, *args, **kwargs):
        calls.append(query)
        entered.set()
        await release.wait()
        return {"read_policy": "allowed", "memory_forbidden": False,
                "memory": {"needed": False}, "task": {"needed": False}}
    monkeypatch.setattr("memory.read_routing.route", route)
    budget = packet.ReadBudget()
    one = asyncio.create_task(packet.prepare_on_demand(value["_turn"], budget, config))
    await entered.wait()
    two = asyncio.create_task(packet.prepare_on_demand(value["_turn"], budget, config))
    release.set()
    first, second = await asyncio.gather(one, two)
    assert len(calls) == 1 and first is second is budget.prepared
    assert first["route"]["read_policy"] == "allowed"
    assert budget.batches == 0
    with pytest.raises(ValueError, match="different turn"):
        await packet.prepare_on_demand(replace(value["_turn"], query="another turn"), budget, config)


async def test_gate_failure_is_not_retried_or_misreported_as_absence(monkeypatch):
    config, value = await deferred(monkeypatch)
    calls = []
    async def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr("memory.read_routing.route", broken)
    budget = packet.ReadBudget()
    first = await packet.prepare_on_demand(value["_turn"], budget, config)
    second = await packet.prepare_on_demand(value["_turn"], budget, config)
    assert first is second and len(calls) == 1
    assert first["status"] == "unavailable"
    assert not permits(first["_turn"], "search")


async def test_voice_still_uses_the_existing_transcript_and_ack_protocol(monkeypatch):
    config, turn = await context(monkeypatch)
    config.lazy_read_planning = True
    seen = []
    async def evaluate(state, config):
        seen.append(state)
        answer = allowed()
        answer["answers"]["memory_needed"]["choice"] = "skip"
        return answer
    value = await packet.prepare_context(replace(turn, entry="voice"), config, evaluator=evaluate)
    assert len(seen) == 1 and value["status"] != "deferred"
    assert not value["_turn"].deferred_read


async def test_tool_cannot_cross_execution_identity_or_return_raw_packet(monkeypatch):
    config, value = await deferred(monkeypatch)
    original = value["_turn"]
    ctx = ToolContext(user_id="different-owner", workspace_id=original.workspace_id,
        session_id=original.session_id, memory_turn=original, memory_read_budget=packet.ReadBudget())
    denied = await execute_memory_prepare(preparation_args(), ctx)
    assert json.loads(denied.output)["reason_code"] == "turn_read_scope_unavailable"
    async def route(*args, **kwargs):
        return {"read_policy": "current_only", "memory_forbidden": True,
                "memory": {"needed": False}, "task": {"needed": False}}
    monkeypatch.setattr("memory.read_routing.route", route)
    ctx.user_id = original.user_id
    result = await execute_memory_prepare(preparation_args(), ctx)
    payload = json.loads(result.output)
    assert payload["status"] == "restricted"
    assert "_turn" not in payload and "items" not in payload
    assert ctx.memory_read_budget.prepared["_turn"].query == original.query


async def test_assistant_preparation_keeps_mode_authority_and_original_turn(monkeypatch):
    from assistant.policy import AssistantError
    from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
    from assistant.runtime import _authorize_tool_scope
    from tool.assistant_tools import assistant_tools
    config, value = await deferred(monkeypatch)
    turn = value["_turn"]
    ctx = ToolContext(user_id=turn.user_id, workspace_id=turn.workspace_id, session_id=turn.session_id,
        memory_turn=turn, memory_read_budget=packet.ReadBudget())
    seen = []
    async def authorize(ctx, operation, arguments):
        seen.append(operation)
        _authorize_tool_scope({"mode": "ordinary", "tool_ids": ASSISTANT_TOOLS}, operation, arguments)
    monkeypatch.setattr("tool.assistant_tools.authorize_assistant_tool", authorize)
    async def route(query, scope, config, **kwargs):
        assert query == turn.query
        return {"read_policy": "current_only", "memory_forbidden": True,
                "memory": {"needed": False}, "task": {"needed": False}}
    monkeypatch.setattr("memory.read_routing.route", route)
    tool = next(tool for tool in assistant_tools if tool.id == "memory.prepare")
    result = await tool.execute(tool.parameters.model_validate(preparation_args().model_dump()), ctx)
    assert seen == ["memory.prepare"]
    assert json.loads(result.output)["status"] == "restricted"
    with pytest.raises(AssistantError, match="unavailable"):
        _authorize_tool_scope({"mode": "report_only", "tool_ids": REPORT_TOOLS}, tool.id, {})


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("native", [True, False])
async def test_preparation_tools_require_explicit_rollout(monkeypatch, enabled, native):
    from agent.agent import AgentDef
    from agent.tool_resolution import resolve_step_tools
    from memory.session_policy import MEMORY_CAPABILITIES
    from tool import registry
    from tool.memory_tools import memory_prepare_tool
    from tool.assistant_tools import assistant_tools
    config, value = await deferred(monkeypatch)
    config.lazy_read_planning = enabled
    config.read_router_model = "" if native else "openai/qwen3.8-flash"
    assistant_tool = next(tool for tool in assistant_tools if tool.id == "memory.prepare")
    monkeypatch.setattr(registry, "_tools", {tool.id: tool for tool in (memory_prepare_tool, assistant_tool)})
    agent = AgentDef(name="memory-probe", description="Exact memory preparation tools", tools=["memory_prepare", "memory.prepare"])
    tools = await resolve_step_tools(agent, None, [], user_id=value["_turn"].user_id)
    assert set(tools) == ({"memory_prepare", "memory.prepare"} if enabled else set())
    assert {"memory_prepare", "memory_read_more"} <= MEMORY_CAPABILITIES
    assert "memory.prepare" not in MEMORY_CAPABILITIES


async def test_a_read_before_preparation_says_it_is_pending_not_empty(monkeypatch):
    # Measured 2026-10-26: cards.list ran in the same step as memory.prepare,
    # read "restricted, no items", and the assistant submitted the same task
    # again instead of answering its pending card.
    from types import SimpleNamespace
    from memory.read_contract import check
    config, bundle = await deferred(monkeypatch, "The fast one is fine.")
    monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(memory=SimpleNamespace(
        enabled=lambda feature, user_id=None: True)))
    turn = replace(bundle["_turn"], read_policy="uncertain")
    assert turn.deferred_read
    denied = check(SimpleNamespace(user_id=turn.user_id, memory_turn=turn), "task_state")
    assert denied["status"] == "restricted" and denied["items"] == []
    assert denied["read_scope"] == "pending" and denied["checked"] is False
    assert "memory.prepare" in denied["instruction"] and "not evidence" in denied["instruction"]
    # A resolved restriction is still a plain refusal.
    resolved = replace(turn, deferred_read=False, read_policy="current_only")
    assert check(SimpleNamespace(user_id=turn.user_id, memory_turn=resolved), "task_state") == {
        "status": "restricted", "items": [], "reason": "turn_read_scope"}
