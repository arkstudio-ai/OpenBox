"""A summary that reaches the output limit is asked for once more, shorter.

It used to fail the whole compaction (and the loop then repeated the same
request), even though a model that writes a long summary usually fits when
asked to be more concise. A second cut still fails, and a cut summary is
never committed.
"""
import pytest

from agent import compaction
from agent.llm import OutputLimitReached
from session.agent_event_log import load_canonical_model_surface
from session.session import get_messages
from tests.unit.test_durable_questions import state  # noqa: F401
from tests.unit.test_full_history import _seed


def _cut(kind):
    if kind == "finish":
        return {"type": "finish", "reason": "length", "usage": {}}
    return {"type": "error", "error": OutputLimitReached("Compaction response did not finish normally: length")}


@pytest.mark.parametrize("kind", ["finish", "error"])
async def test_final_summary_cut_at_limit_is_retried_shorter(state, monkeypatch, kind):
    ids = await _seed("s1", "u1", turns=5)
    await compaction.create_compaction("s1", auto=False, user_id="u1")
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs["messages"])
        if len(requests) == 1:
            yield {"type": "text_delta", "text": "## Goal\nA very long summary that never"}
            yield _cut(kind)
        else:
            yield {"type": "text_delta", "text": "## Goal\nShort summary."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/gpt-4o", auto=False, user_id="u1",
    )

    assert len(requests) == 2
    first, second = requests
    assert second[:-1] == first[:-1]
    assert second[-1]["content"].startswith(first[-1]["content"])
    assert "cut off at the" in second[-1]["content"]
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    summaries = [m for m in surface.messages if m.summary and m.finish]
    assert summaries
    text = "".join(p.text for p in summaries[-1].parts if getattr(p, "type", "") == "text")
    assert "Short summary." in text and "never" not in text
    assert ids[0] not in {m.id for m in surface.messages}


async def test_final_summary_cut_twice_still_fails(state, monkeypatch):
    ids = await _seed("s1", "u1", turns=5)
    await compaction.create_compaction("s1", auto=False, user_id="u1")
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs["billing_kind"])
        yield {"type": "text_delta", "text": "Still too long"}
        yield {"type": "finish", "reason": "length", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    assert await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/gpt-4o", auto=False, user_id="u1",
    ) == "stop"
    assert calls == ["compaction", "compaction"]
    surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
    assert set(ids) <= {m.id for m in surface.messages}
    attempt = (await get_messages("s1", user_id="u1"))[-1]
    assert attempt.error and not attempt.finish


async def test_other_provider_errors_are_not_retried(state, monkeypatch):
    await _seed("s1", "u1", turns=5)
    await compaction.create_compaction("s1", auto=False, user_id="u1")
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs["billing_kind"])
        yield {"type": "error", "error": RuntimeError("Compaction response did not finish normally: content_filter")}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    assert await compaction.process_compaction(
        "s1", await get_messages("s1", user_id="u1"), "openai/gpt-4o", auto=False, user_id="u1",
    ) == "stop"
    assert calls == ["compaction"]


async def test_chunk_summary_cut_at_limit_is_retried_shorter(monkeypatch):
    requests = []

    async def provider(**kwargs):
        requests.append(kwargs["messages"])
        if len(requests) == 1:
            yield {"type": "text_delta", "text": "partial"}
            yield _cut("error")
        else:
            yield {"type": "text_delta", "text": "A brief chunk summary."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr("agent.llm.stream_llm", provider)
    monkeypatch.setattr(compaction, "get_model_context_limit", lambda _model: 1_000_000)
    summaries = await compaction._chunked_summarize(
        [{"role": "user", "content": "short history"}], "test/main", 900_000, "s", "u",
    )
    assert summaries == ["## Part 1/1\nA brief chunk summary."]
    assert len(requests) == 2
    assert requests[1][:-1] == requests[0][:-1]
    assert "cut off at the" in requests[1][-1]["content"]
