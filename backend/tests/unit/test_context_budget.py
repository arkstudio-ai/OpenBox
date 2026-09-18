"""Whole requests, prefix reuse, pruning and measured compaction recovery."""
import json

import pytest
from pydantic import ValidationError

from agent import compaction, llm
from agent.context_budget import RequestPrefix, count_payload, measure_request, request_payload
from core.config import CompactionConfig, ModelConfig, OpenBoxConfig
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed
from tests.unit.test_long_history_context import long_chat, _send  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401


def test_eighty_percent_threshold_and_output_headroom(monkeypatch):
    cfg = OpenBoxConfig(models=[ModelConfig(id="openai/gemini-test", context_limit=1_000_000)])
    monkeypatch.setattr("core.config.get_config", lambda: cfg)
    budget = measure_request("openai/gemini-test", [], RequestPrefix())
    assert budget.threshold == 800000
    cfg.compaction.reserved = 300000
    assert measure_request("openai/gemini-test", [], RequestPrefix()).threshold == 700000
    cfg.compaction.reserved = None
    assert measure_request("openai/gpt-4o", [], RequestPrefix()).threshold == 96000
    with pytest.raises(ValidationError):
        CompactionConfig(threshold_ratio=0.8, retain_ratio=0.9)


def test_request_count_includes_tools_arguments_and_unicode(long_chat):
    tool = long_chat.tools["read"]
    tool.description = "Required tool semantics 中文参数 " * 1500
    source = [{"role": "assistant", "content": "", "tool_calls": [{
        "id": "call", "type": "function", "function": {"name": "read", "arguments": json.dumps({
            "instruction": "必须保留这些约束。" * 2000,
        }, ensure_ascii=False)},
    }]}]
    empty = measure_request("openai/gpt-4o", [], RequestPrefix())
    full = measure_request("openai/gpt-4o", source, RequestPrefix(
        system=["System instruction " * 1000], tools=long_chat.tools,
    ))
    assert full.input_tokens > empty.input_tokens + 15000
    assert count_payload("中文用户约束" * 1000) > len("中文用户约束" * 1000) // 4


def test_unicode_count_remains_conservative_without_a_local_tokenizer(monkeypatch):
    monkeypatch.setattr("agent.tool_payload._proxy_encoding", lambda: None)
    value = "中文用户约束" * 1000
    assert count_payload(value) >= len(value)


def test_schema_property_named_type_is_not_an_image_block():
    schema = {"type": "object", "properties": {"type": {
        "type": "string", "description": "Required event type " * 1000,
    }}}
    assert count_payload(schema) > 1000


@pytest.mark.parametrize("responses", [False, True])
async def test_compaction_reuses_chat_wire_prefix_and_caps_output(long_chat, monkeypatch, responses):
    import httpx
    import litellm
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
    from tests.unit.test_llm_tool_search_responses import _FakeAsyncClient, _FakeResponse

    captured = []
    model = "openai/gpt-5.4" if responses else "openai/gpt-4o"
    prefix = RequestPrefix(system=["Stable system prefix", "Project instructions"],
                           tools=long_chat.tools, cache_key="tenant-session-digest")
    messages = [{"role": "user", "content": "Summarize our work"}]
    if responses:
        reply = {"type": "response.completed", "response": {"id": "r1", "output": []}}
        replies = [_FakeResponse(200, lines=[f"data: {json.dumps(reply)}", "data: [DONE]"])]
        monkeypatch.setattr(httpx, "AsyncClient", lambda **_: _FakeAsyncClient(replies, captured))
        stream = llm._stream_responses_api
    else:
        async def chunks():
            yield ModelResponseStream(choices=[StreamingChoices(index=0, delta=Delta(content="Summary"),
                                                                 finish_reason="stop")])

        async def completion(**kwargs):
            captured.append(kwargs)
            return chunks()
        monkeypatch.setattr(litellm, "acompletion", completion)
        stream = llm._stream_litellm_direct
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {
        "api_key": "fixture", "api_base": "https://fixture.invalid/v1",
    })
    events = [event async for event in stream(
        model, prefix.system, messages, prefix.tools, purpose="compaction",
        cache_key=prefix.cache_key, tool_choice="none", max_output_tokens=2048,
    )]
    assert events[-1]["type"] == "finish"
    payload = captured[0][2]["json"] if responses else captured[0]
    expected = request_payload(model, messages, prefix)
    key = "input" if responses else "messages"
    assert payload[key] == expected[key]
    assert payload["tools"] == expected["tools"]
    assert payload["prompt_cache_key"] == prefix.cache_key
    assert payload["tool_choice"] == "none"
    assert payload["max_output_tokens" if responses else "max_tokens"] == 2048


def _small_window(monkeypatch, *, prune):
    from core.config import get_config
    cfg = get_config()
    cfg.models = [ModelConfig(id="openai/gpt-4o", context_limit=20000)]
    cfg.compaction.prune = prune
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda _: 2048)
    return cfg


async def _submit_compaction_task(harness, text, *, direct):
    if direct:
        # Cron/Task callers commit input before run_loop reserves its lease.
        from session.session import create_user_message
        from tests.unit.test_long_history_context import _drain_tasks

        await create_user_message("s1", text, model="openai/gpt-4o", user_id="u1")
        await harness.loop.run_loop("s1", user_id="u1")
        await _drain_tasks(harness)
    else:
        await _send(harness, text, asynchronous=True)


@pytest.mark.parametrize("direct", [False, True], ids=["api", "direct-run"])
async def test_new_input_triggers_compaction_before_provider_without_previous_usage(
    state, long_chat, monkeypatch, direct,
):
    _small_window(monkeypatch, prune=False)
    await _seed("s1", "u1", turns=5, tool_output="Original source fact " * 600)
    calls = []

    async def summary(**kwargs):
        calls.append(("summary", kwargs))
        yield {"type": "text_delta", "text": "Original work and the new constraints summarized."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    async def provider(**kwargs):
        calls.append(("model", kwargs))
        yield {"type": "text_delta", "text": "Done."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    monkeypatch.setattr("agent.llm.stream_llm", summary)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _submit_compaction_task(long_chat, "新增必要条件。" * 2500, direct=direct)
    kinds = [kind for kind, _ in calls]
    assert kinds[0] == "summary" and kinds[-1] == "model" and kinds.count("model") == 1
    final = calls[-1][1]
    summary_call = next(kwargs for kind, kwargs in calls if kind == "summary")
    assert summary_call["system"] == final["system"]
    assert set(summary_call["tools"]) == set(final["tools"])
    assert summary_call["cache_key"] == final["cache_key"]
    assert measure_request("openai/gpt-4o", final["llm_messages"] if "llm_messages" in final
                           else final["messages"], RequestPrefix(system=final["system"], tools=final["tools"])).input_tokens < 16000


async def test_pruning_remeasures_and_avoids_a_summary_call(state, long_chat, monkeypatch):
    _small_window(monkeypatch, prune=True)
    await _seed("s1", "u1", turns=8, tool_output="Old source data " * 1000)
    calls = []

    async def no_summary(**_kwargs):
        raise AssertionError("Pruning alone should make this request fit")
        yield

    async def provider(**kwargs):
        calls.append(kwargs)
        yield {"type": "text_delta", "text": "Done after pruning."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 10}}

    monkeypatch.setattr("agent.llm.stream_llm", no_summary)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "Continue after pruning old tool results")
    assert len(calls) == 1
    payload = json.dumps(calls[0]["messages"], ensure_ascii=False)
    assert payload.count("Old source data") < 8 * 1000
    assert "Continue after pruning old tool results" in payload


@pytest.mark.parametrize("direct", [False, True], ids=["api", "direct-run"])
@pytest.mark.parametrize("old_turns", [0, 5], ids=["first-turn", "existing-history"])
async def test_tool_output_crosses_the_budget_inside_the_same_run(
    state, long_chat, monkeypatch, direct, old_turns,
):
    """Fresh tool bytes, not a fabricated provider usage, trigger the next loop check."""
    from pydantic import BaseModel
    from tool.tool import ToolResult, define_tool

    _small_window(monkeypatch, prune=False)
    if old_turns:
        await _seed("s1", "u1", turns=old_turns)
    calls = []

    class ReadArgs(BaseModel):
        pass

    async def read(_args, _ctx):
        calls.append("tool")
        return ToolResult(title="Synthetic context growth", output="1 " * 9500)

    long_chat.tools["read"] = define_tool(
        "read", description="Read synthetic data", parameters=ReadArgs,
        execute=read, sandbox_required=False,
    )

    async def summary(**kwargs):
        calls.append("chunk" if kwargs.get("billing_kind") == "compaction_chunk" else "summary")
        yield {"type": "text_delta", "text": "Read completed. Synthetic numeric data received. Finish the current task."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 20}}

    async def provider(**kwargs):
        calls.append("model")
        if calls == ["model"]:
            yield {"type": "tool_call", "tool": "read", "args": {}, "call_id": "grow-context"}
            yield {"type": "finish", "reason": "tool_calls", "usage": {"input": 1000, "output": 20}}
        else:
            yield {"type": "text_delta", "text": "Continued after automatic optimization."}
            yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 20}}

    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    monkeypatch.setattr("agent.llm.stream_llm", summary)
    await _submit_compaction_task(long_chat, "Read once, then finish this task.", direct=direct)
    assert calls[:2] == ["model", "tool"]
    assert calls[-2:] == ["summary", "model"]
    assert all(call == "chunk" for call in calls[2:-2])
    assert not [data for kind, data in state if kind == "session.error"]

    # The original anchor must survive lease release and the next history read.
    from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
    from session.session import get_session

    surface = await load_canonical_model_surface("s1", user_id="u1")
    assert surface.messages[-1].finish == "stop"
    assert (await get_session("s1", user_id="u1")).status.value == "idle"
    parity = await verify_agent_event_parity("s1", user_id="u1")
    assert parity.ok, parity


@pytest.mark.parametrize("recovered", [True, False], ids=["second-summary-fits", "bounded-stop"])
async def test_remeasure_after_summary_retries_with_a_bound(state, long_chat, monkeypatch, recovered):
    cfg = _small_window(monkeypatch, prune=False)
    cfg.compaction.max_tokens = 1024
    cfg.compaction.preserve_recent_tokens = 1600
    cfg.compaction.tail_turns = None
    await _seed("s1", "u1", turns=5, tool_output="Earlier task detail " * 500)

    async def system(*_args, **_kwargs):
        return ["prefix " * 14250]
    monkeypatch.setattr(long_chat.loop, "_build_system_prompt", system)
    finals, requests = [], []

    async def summary(**kwargs):
        if kwargs["billing_kind"] == "compaction":
            finals.append(kwargs)
            # Each summary shrinks its source. Only the second, shorter
            # summary in the recovery case leaves enough room for the tail.
            words = 800 if len(finals) == 1 else (100 if recovered else 700)
            value = " detail" * words
        else:
            value = "Chunk facts preserved."
        yield {"type": "text_delta", "text": value}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 20}}

    async def provider(**kwargs):
        requests.append(kwargs)
        yield {"type": "text_delta", "text": "Done after measured compaction."}
        yield {"type": "finish", "reason": "stop", "usage": {"input": 1000, "output": 20}}

    monkeypatch.setattr("agent.llm.stream_llm", summary)
    monkeypatch.setattr(long_chat.processor, "stream_llm", provider)
    await _send(long_chat, "current constraint " * 600)
    assert len(finals) == 2
    assert len(requests) == (1 if recovered else 0)
    if not recovered:
        assert any(data.get("error", {}).get("code") == "COMPACTION_FAILED" for _, data in state)
