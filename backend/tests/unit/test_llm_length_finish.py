"""A chat reply stopped at the output limit is kept and marked truncated.

It used to raise "模型回复未完整结束（length）", so the turn ended in an error
and the received answer was demoted to commentary. Tool arguments cut by the
same limit must still never be dispatched.
"""
import pytest
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

from agent import llm


def _install(monkeypatch, rows):
    import litellm

    async def chunks():
        for delta, finish in rows:
            yield ModelResponseStream(choices=[StreamingChoices(index=0, finish_reason=finish, delta=delta)])

    async def completion(**kwargs):
        return chunks()

    monkeypatch.setattr(litellm, "acompletion", completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _: {})


async def _events(purpose="chat"):
    return [e async for e in llm._stream_litellm_direct(
        "anthropic/claude-test", [], [{"role": "user", "content": "write a long essay"}], {},
        purpose=purpose,
    )]


@pytest.mark.asyncio
async def test_length_finish_keeps_text_and_marks_truncation(monkeypatch):
    _install(monkeypatch, [(Delta(content="Part one. "), None), (Delta(content="Part two"), "length")])
    events = await _events()

    text = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert text == "Part one. Part two" + llm.TRUNCATED_REPLY_NOTE
    assert not any(e["type"] == "error" for e in events)
    assert events[-1]["type"] == "finish"
    assert events[-1]["reason"] == "stop"
    assert events[-1]["truncated"] is True


@pytest.mark.asyncio
async def test_length_finish_with_tool_call_still_fails(monkeypatch):
    _install(monkeypatch, [
        (Delta(content="Writing the file."), None),
        (Delta(tool_calls=[{"index": 0, "id": "c1", "type": "function",
                            "function": {"name": "write", "arguments": '{"path": "a'}}]), "length"),
    ])
    events = await _events()
    assert events[-1]["type"] == "error"
    assert not any(e["type"] in {"tool_call", "finish"} for e in events)


@pytest.mark.asyncio
async def test_length_finish_without_text_still_fails(monkeypatch):
    _install(monkeypatch, [(Delta(reasoning_content="thinking..."), "length")])
    events = await _events()
    assert events[-1]["type"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["title", "suggestions", "compaction"])
async def test_non_chat_purposes_still_fail_on_length(monkeypatch, purpose):
    _install(monkeypatch, [(Delta(content="partial"), "length")])
    events = await _events(purpose)
    assert events[-1]["type"] == "error"
    assert not any(e.get("text") == llm.TRUNCATED_REPLY_NOTE for e in events)
