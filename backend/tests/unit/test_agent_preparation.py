"""Normal chat prepares one candidate while keeping every step's live checks."""
import json
from copy import copy
from unittest.mock import AsyncMock

from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_long_history_context import long_chat, _send  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401


async def test_normal_build_projects_once_per_step_and_resolves_fresh_tools(
    state, long_chat, monkeypatch,
):
    from agent import tool_runtime
    from core.config import get_config
    from session import internal_parts
    from tool.capability_search import capability_search_tool
    from tool.tool import ToolResult

    get_config().tool_exposure.mode = "portable"
    long_chat.tools["capability_search"] = capability_search_tool
    image_tool = copy(long_chat.tools["read"])
    image_tool.id = "image_gen"
    long_chat.tools["image_gen"] = image_tool
    resolve = AsyncMock(wraps=long_chat.loop.resolve_step_tools)
    images = AsyncMock(wraps=long_chat.loop.resolve_images)
    reveals = AsyncMock(wraps=internal_parts.get_valid_revealed_ids)
    catalogue_builds = []
    build_catalogue = tool_runtime.build_eligible_catalog

    def build(tools):
        catalogue_builds.append(tuple(tools))
        return build_catalogue(tools)

    async def read(_args, _ctx):
        # A changed contract takes effect on the next step, never from a run cache.
        long_chat.tools["read"].description = "Fresh read contract after first step"
        long_chat.tools.pop("image_gen")
        return ToolResult(title="read", output="fresh result")

    long_chat.tools["read"].execute = read
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs)
        if len(requests) == 1:
            yield {"type": "tool_call", "tool": "read", "args": {}, "call_id": "read-once"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 1000, "output": 10}}
        else:
            yield {"type": "text_delta", "text": "Done."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    monkeypatch.setattr(long_chat.loop, "resolve_step_tools", resolve)
    monkeypatch.setattr(long_chat.loop, "resolve_images", images)
    monkeypatch.setattr(internal_parts, "get_valid_revealed_ids", reveals)
    monkeypatch.setattr(tool_runtime, "build_eligible_catalog", build)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "Read the fixture once and summarize it.")

    assert len(requests) == 2
    assert images.await_count == 2  # Formerly both preflight and checkpoint per step.
    assert resolve.await_count == reveals.await_count == len(catalogue_builds) == 2
    assert "image_gen" in catalogue_builds[0] and "image_gen" not in catalogue_builds[1]
    assert requests[0]["tools"]["read"].description == "Read a regression fixture"
    assert requests[1]["tools"]["read"].description == "Fresh read contract after first step"
    assert "fresh result" in json.dumps(requests[1]["messages"])


async def test_normal_candidate_reprojects_after_event_prefix_changes(state, long_chat, monkeypatch):
    from session import agent_event_log
    from session.session import create_user_message

    images = AsyncMock(wraps=long_chat.loop.resolve_images)
    checkpoint = agent_event_log.checkpoint_model_request
    attempts = []
    requests = []

    async def drift_once(*args, **kwargs):
        attempts.append(kwargs["expected_event_sequence"])
        if len(attempts) == 1:
            await create_user_message("s1", "New constraint while preparing", user_id="u1", synthetic=True)
        return await checkpoint(*args, **kwargs)

    async def provider(**kwargs):
        requests.append(kwargs)
        yield {"type": "text_delta", "text": "Included the new constraint."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    monkeypatch.setattr(long_chat.loop, "resolve_images", images)
    monkeypatch.setattr(agent_event_log, "checkpoint_model_request", drift_once)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "Respond once with the current constraints.")

    assert len(requests) == 1
    assert len(attempts) == images.await_count == 2
    assert attempts[1] > attempts[0]
    assert "New constraint while preparing" in json.dumps(requests[0]["messages"])
