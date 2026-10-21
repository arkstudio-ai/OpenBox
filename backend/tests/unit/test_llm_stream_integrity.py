"""Actual OpenAI SDK + LiteLLM normalization must not turn EOF into success."""
import json

import httpx
import pytest
from openai import AsyncOpenAI

from agent import llm


@pytest.mark.parametrize("finish", [None, "length", "content_filter", "stop"])
@pytest.mark.parametrize("purpose", ["chat", "compaction"])
async def test_wire_completion_is_required_for_a_full_filename(monkeypatch, finish, purpose):
    import litellm
    original = litellm.acompletion
    source = [
        {"choices": [{"index": 0, "delta": {"content": "VID_2026"}, "finish_reason": None}]},
    ]
    if finish is not None:
        # Some gateways deliver the last content and finish in the same chunk.
        source.append({"choices": [{"index": 0, "delta": {"content": "1019_0716.mp4"},
                                     "finish_reason": finish}]})
    source.append({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 12,
                                             "total_tokens": 22}})
    wire = "".join("data: " + json.dumps({"id": "qa", "created": 1, "model": "qa",
        "object": "chat.completion.chunk", **row}) + "\n\n" for row in source)
    wire += "data: [DONE]\n\n"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "text/event-stream"}, text=wire)
    )) as http:
        async with AsyncOpenAI(api_key="fixture", base_url="https://fixture.invalid/v1",
                               http_client=http, max_retries=0) as sdk:
            wrappers = []
            async def completion(**kwargs):
                response = await original(**kwargs, client=sdk)
                wrappers.append(response)
                return response
            monkeypatch.setattr(litellm, "acompletion", completion)
            monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})
            events = [event async for event in llm._stream_litellm_direct(
                "openai/gpt-4o", [], [{"role": "user", "content": "Return the filename"}], {},
                purpose=purpose,
            )]
    assert wrappers[0].received_finish_reason == finish
    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == ("VID_2026" if finish is None else "VID_20261019_0716.mp4")
    assert events[-1]["type"] == ("finish" if finish == "stop" else "error")
    if finish != "stop":
        assert not any(e["type"] in {"finish", "tool_call"} for e in events)


async def test_incomplete_tool_arguments_are_never_dispatched(monkeypatch):
    import litellm
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices
    async def chunks():
        yield ModelResponseStream(choices=[StreamingChoices(index=0, finish_reason=None,
            delta=Delta(tool_calls=[{"index": 0, "id": "call1", "type": "function",
                                    "function": {"name": "write", "arguments": '{"path":'}}]))])
    async def completion(**kwargs):
        return chunks()
    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})
    events = [e async for e in llm._stream_litellm_direct(
        "openai/gpt-4o", [], [{"role": "user", "content": "test"}], {})]
    assert events[-1]["type"] == "error"
    assert not any(e["type"] == "tool_call" for e in events)
