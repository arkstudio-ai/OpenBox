from pydantic import BaseModel

from agent.tool_exposure import build_eligible_catalog
from tool.capability_search import (
    CapabilitySearchArgs,
    MAX_REVEALS_PER_STEP,
    MAX_RESULT_CHARS_PER_STEP,
    capability_search_tool,
    execute_capability_search,
)
from tool.tool import ToolContext, ToolInfo, ToolResult


class Args(BaseModel):
    target: str = ""


async def _noop(args, ctx):
    return ToolResult(title="ok", output="ok")


def _catalogue(count: int = 8):
    tools = {
        f"qa_tool_{index}": ToolInfo(
            id=f"qa_tool_{index}",
            description=f"Handle unique topic {index} and quality assurance workflow.",
            parameters=Args,
            execute=_noop,
        )
        for index in range(count)
    }
    return build_eligible_catalog(tools)


def _ctx(catalogue, observed):
    async def commit(ids, generation, digests):
        observed.append((ids, generation, digests))

    return ToolContext(
        session_id="session_1",
        user_id="user_1",
        run_id="run_1",
        _capability_catalog=catalogue,
        _commit_tool_reveal=commit,
    )


async def test_exact_search_commits_through_typed_callback_only():
    catalogue = _catalogue()
    observed = []
    ctx = _ctx(catalogue, observed)

    result = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_7"]),
        ctx,
    )

    assert observed[0][0] == ("qa_tool_7",)
    assert observed[0][1] == catalogue.generation
    assert observed[0][2] == {"qa_tool_7": catalogue.entries["qa_tool_7"].schema_digest}
    assert result.metadata == {"count": 1}
    assert "revealed_ids" not in result.metadata
    assert "typed schema will be available on the next step" in result.output


async def test_unknown_or_permission_filtered_exact_name_returns_nothing():
    catalogue = _catalogue(2)
    observed = []
    result = await execute_capability_search(
        CapabilitySearchArgs(names=["denied_secret_tool"]),
        _ctx(catalogue, observed),
    )
    assert observed == []
    assert result.metadata == {"count": 0}
    assert "denied_secret_tool" not in result.output


async def test_already_direct_tool_is_not_revealed_again():
    catalogue = _catalogue(2)
    observed = []
    ctx = _ctx(catalogue, observed)
    ctx._capability_discovery_ids = frozenset({"qa_tool_1"})
    result = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_0"]), ctx
    )
    assert observed == []
    assert result.metadata == {"count": 0}


async def test_lexical_search_is_stable_and_bounded():
    catalogue = _catalogue(20)
    observed = []
    ctx = _ctx(catalogue, observed)

    result = await execute_capability_search(
        CapabilitySearchArgs(query="quality assurance workflow"),
        ctx,
    )

    assert len(observed[0][0]) == MAX_REVEALS_PER_STEP
    assert observed[0][0] == tuple(sorted(observed[0][0]))
    assert len(result.output) <= MAX_RESULT_CHARS_PER_STEP


async def test_multiple_searches_cannot_enumerate_the_catalogue():
    catalogue = _catalogue(20)
    observed = []
    ctx = _ctx(catalogue, observed)

    await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_0", "qa_tool_1", "qa_tool_2"]), ctx
    )
    second = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_3", "qa_tool_4", "qa_tool_5"]), ctx
    )
    third = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_6"]), ctx
    )

    assert sum(len(item[0]) for item in observed) == MAX_REVEALS_PER_STEP
    assert "qa_tool_5" not in second.output
    assert third.metadata.get("blocked") is True


async def test_request_context_can_tighten_all_search_budgets():
    catalogue = _catalogue(8)
    observed = []
    ctx = _ctx(catalogue, observed)
    ctx._capability_max_search_calls = 1
    ctx._capability_max_reveals = 2
    ctx._capability_max_result_chars = 500

    first = await execute_capability_search(
        CapabilitySearchArgs(
            names=["qa_tool_0", "qa_tool_1", "qa_tool_2", "qa_tool_3"]
        ),
        ctx,
    )
    second = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_4"]),
        ctx,
    )

    assert observed[0][0] == ("qa_tool_0", "qa_tool_1")
    assert len(first.output) <= 500
    assert second.metadata.get("blocked") is True


async def test_duplicate_reveal_does_not_consume_another_slot():
    catalogue = _catalogue()
    observed = []
    ctx = _ctx(catalogue, observed)
    await execute_capability_search(CapabilitySearchArgs(names=["qa_tool_0"]), ctx)
    result = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_0", "qa_tool_1"]), ctx
    )

    assert observed[-1][0] == ("qa_tool_1",)
    assert "qa_tool_0" not in result.output


async def test_no_commit_channel_means_no_apparent_reveal():
    ctx = ToolContext(_capability_catalog=_catalogue())
    result = await execute_capability_search(
        CapabilitySearchArgs(names=["qa_tool_0"]), ctx
    )
    assert result.metadata.get("blocked") is True
    assert ctx._capability_revealed_ids == set()


def test_search_tool_cannot_run_inside_generic_batch():
    assert capability_search_tool.parallel_safe is False
