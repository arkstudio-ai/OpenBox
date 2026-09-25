"""Provider argument errors survive both adapters without becoming valid {} calls."""
import json
from types import SimpleNamespace

import httpx
import pytest

from agent import llm


@pytest.mark.parametrize("transport", ["responses", "litellm"])
@pytest.mark.parametrize("raw,expected", [
    ('{"title":"Analysis"},"goal":"Review"}', "Invalid JSON"),
    ('{"title":"Analysis"', "Invalid JSON"),
    ('[]', "expected an object"),
    ('null', "expected an object"),
    ('"secret-content"', "expected an object"),
    ('{}', None),
    ('', None),
    ('{"title":"Analysis","goal":"Review"}', None),
])
async def test_tool_arguments_remain_actionable_across_transports(monkeypatch, transport, raw, expected):
    tools = {"probe": SimpleNamespace(description="Probe", raw_schema={"type": "object", "properties": {}})}
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda *_: {"api_key": "test", "api_base": "https://provider.invalid/v1"})
    monkeypatch.setattr(llm, "_get_variant_kwargs", lambda *_: {})
    monkeypatch.setattr(llm, "_get_max_output_tokens", lambda *_: 1000)
    if transport == "responses":
        event = {"type": "response.completed", "response": {"id": "resp-test", "status": "completed", "output": [
            {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "probe", "arguments": raw},
        ]}}

        class Response:
            status_code = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                pass

            async def aiter_lines(self):
                yield "data: " + json.dumps(event)
                yield "data: [DONE]"

        class Client(Response):
            def __init__(self, **_):
                pass

            def stream(self, *_args, **_kwargs):
                return Response()

        monkeypatch.setattr(httpx, "AsyncClient", Client)
        stream = llm._stream_responses_api("openai/test", [], [], tools)
    else:
        import litellm
        for setting in ("modify_params", "drop_params", "reasoning_auto_summary"):
            monkeypatch.setattr(litellm, setting, getattr(litellm, setting))

        async def chunks():
            # Split the payload to exercise stream accumulation before parsing.
            for i, value in enumerate((raw[:len(raw)//2], raw[len(raw)//2:])):
                yield SimpleNamespace(choices=[SimpleNamespace(
                    finish_reason="tool_calls" if i else None,
                    delta=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(
                        index=0, id="call_test", function=SimpleNamespace(name="probe" if not i else None, arguments=value),
                    )]),
                )])

        async def completion(**_):
            return chunks()

        monkeypatch.setattr(litellm, "acompletion", completion)
        stream = llm._stream_litellm_direct("openai/test", [], [], tools)
    events = [event async for event in stream]
    assert not [event for event in events if event["type"] == "error"]
    call, = [event for event in events if event["type"] == "tool_call"]
    assert call["arguments_raw"] == raw
    assert call["call_id"] == "call_test"
    if expected:
        assert expected in call["arguments_error"]
        assert "secret-content" not in call["arguments_error"]
        assert "not executed" in call["arguments_error"]
        assert call["args"] == {}
    else:
        assert call["arguments_error"] is None
        assert call["args"] == (json.loads(raw) if raw else {})
