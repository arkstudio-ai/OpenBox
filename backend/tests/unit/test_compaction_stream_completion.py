"""Actual provider adapters must not label an interrupted summary successful."""
import json

import pytest

from agent import llm


@pytest.mark.parametrize("purpose", ["compaction", "compaction_chunk"])
@pytest.mark.parametrize("reason", ["stop", "length", "content_filter", None])
async def test_litellm_summary_requires_a_normal_terminal_chunk(monkeypatch, purpose, reason):
    import litellm
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

    async def provider_chunks():
        yield ModelResponseStream(choices=[StreamingChoices(
            index=0, delta=Delta(content="Summary text"), finish_reason=None,
        )])
        if reason:
            yield ModelResponseStream(choices=[StreamingChoices(
                index=0, delta=Delta(), finish_reason=reason,
            )])
        # Usage after the terminal chunk must not erase its reason.
        yield ModelResponseStream(choices=[], usage={
            "prompt_tokens": 100, "completion_tokens": 4, "total_tokens": 104,
        })

    async def completion(**_kwargs):
        return provider_chunks()

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})
    events = [event async for event in llm._stream_litellm_direct(
        "openai/gpt-4o", [], [{"role": "user", "content": "Summarize"}], {}, purpose=purpose,
    )]
    assert any(event["type"] == "text_delta" for event in events)
    assert any(event["type"] == "usage" for event in events)
    assert events[-1]["type"] == ("finish" if reason == "stop" else "error")
    if reason != "stop":
        assert not any(event["type"] == "finish" for event in events)


@pytest.mark.parametrize("purpose", ["compaction", "compaction_chunk"])
@pytest.mark.parametrize("terminal", ["response.completed", "response.incomplete", None])
async def test_responses_summary_requires_a_completed_event(monkeypatch, purpose, terminal):
    import httpx
    from tests.unit.test_llm_tool_search_responses import _FakeAsyncClient, _FakeResponse

    data = [{"type": "response.output_text.delta", "delta": "Summary text"}]
    if terminal:
        data.append({"type": terminal, "response": {
            "id": "r1", "output": [], "incomplete_details": {"reason": "max_output_tokens"},
        }})
    responses = [_FakeResponse(200, lines=[f"data: {json.dumps(event)}" for event in data]
                               + ["data: [DONE]"])]
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kwargs: _FakeAsyncClient(responses, []))
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {
        "api_key": "fixture-key", "api_base": "https://fixture.invalid/v1",
    })
    events = [event async for event in llm._stream_responses_api(
        "openai/gpt-5.4", [], [{"role": "user", "content": "Summarize"}], {}, purpose=purpose,
    )]
    assert any(event["type"] == "text_delta" for event in events)
    assert events[-1]["type"] == ("finish" if terminal == "response.completed" else "error")
    if terminal != "response.completed":
        assert not any(event["type"] == "finish" for event in events)
