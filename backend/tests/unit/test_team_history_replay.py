"""Completed team history survives a switch back to ordinary chat models."""
from types import SimpleNamespace

import pytest

from agent.llm import build_responses_input
from agent.loop import _resolve_history_tool_names, _to_llm_messages
from session.tool_part_identity import ToolPartReplayError
from team.policy import COORDINATOR_TOOLS


def history(tool_ids, dialect):
    return [SimpleNamespace(role="assistant", error=None, parts=[
        {
            "id": f"part_{index}", "type": "tool", "tool": tool_id,
            "status": "completed", "input": {}, "output": "47",
            "call_id": f"call_{index}", "canonical_tool_id": tool_id,
            "wire_tool_name": tool_id, "provider_binding_digest": "a" * 64,
            "provider_dialect": dialect, "stream_seq": index,
        }
        for index, tool_id in enumerate(tool_ids)
    ])]


async def replay(messages, target, current):
    return await _resolve_history_tool_names(
        messages, session_id="completed-team-root", user_id="owner",
        current_binding_digest="b" * 64, current_provider_dialect=target,
        current_wire_by_canonical=current, legacy_aliases={},
    )


@pytest.mark.parametrize("source,target", [("responses", "litellm"), ("litellm", "responses")])
async def test_completed_team_calls_replay_across_protocols_without_reenabling_tools(source, target):
    tools = sorted(COORDINATOR_TOOLS)
    messages = history(tools, source)
    ordinary_tools = {"read": "read"}
    names = await replay(messages, target, ordinary_tools)

    assert ordinary_tools == {"read": "read"}
    assert names == {f"part_{index}": tool for index, tool in enumerate(tools)}
    converted = _to_llm_messages(messages, tool_replay_names=names)
    calls = converted[0]["tool_calls"]
    assert [call["function"]["name"] for call in calls] == tools
    assert [item["tool_call_id"] for item in converted[1:]] == [call["id"] for call in calls]
    assert all(item["content"] == "47" for item in converted[1:])
    responses = build_responses_input(converted)
    assert [item["name"] for item in responses if item["type"] == "function_call"] == tools
    assert len([item for item in responses if item["type"] == "function_call_output"]) == len(tools)


async def test_current_team_wire_mapping_wins_over_default_history_name():
    current = {"team_finish": "finish_current"}
    names = await replay(history(["team_finish"], "responses"), "litellm", current)
    assert names == {"part_0": "finish_current"}
    assert current == {"team_finish": "finish_current"}


async def test_history_mapping_does_not_allow_a_foreign_tool_to_take_a_team_wire_name():
    with pytest.raises(ToolPartReplayError, match="collision"):
        await replay(history(["team_finish"], "responses"), "litellm",
            {"mcp:v2:" + "a" * 52: "team_finish"})


async def test_unknown_dynamic_tool_still_needs_explicit_cross_protocol_mapping():
    with pytest.raises(ToolPartReplayError, match="unavailable"):
        await replay(history(["mcp:v2:" + "a" * 52], "responses"), "litellm", {})
