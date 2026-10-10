"""Provider contract errors keep actual usage and never relax source grounding."""
import json

import httpx
import pytest

from core.config import OpenBoxConfig
from memory.providers.common import MemoryProviderError
from memory.wiki.provider import ConfiguredWikiModel
from wiki_compiler import CompilePolicy, CompileRequest, SourceSnapshot, TargetSnapshot
from wiki_compiler.hashing import text_hash


class FakeClient:
    def __init__(self, payload):
        self.payload, self.calls, self.deadline = payload, 0, None

    async def post(self, _url, *, json, headers, timeout):
        self.calls += 1
        self.deadline = timeout
        return httpx.Response(200, json=self.payload)


@pytest.fixture
def wiki_request(monkeypatch):
    config = OpenBoxConfig(model="provider/strong", memory={"compilation_timeout_seconds": 120, "provider_timeout_seconds": 20})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _model: {"api_key": "synthetic-test-key", "api_base": "https://wiki-provider.invalid/v1"})
    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _model: False)
    text = "这个项目使用中文答复。"
    return CompileRequest("agreement", "项目约定", "opaque-authorized-domain",
        (SourceSnapshot("source-1", 1, text, text_hash(text), "opaque-authorized-domain", 1),),
        TargetSnapshot("page-1"), CompilePolicy(model=config.model))


def response(body, *, finish="stop"):
    return {"choices": [{"finish_reason": finish, "message": {"content": body}}],
            "usage": {"prompt_tokens": 25, "completion_tokens": 10}}


@pytest.mark.asyncio
async def test_compilation_uses_separate_deadline_and_preserves_unknown_cost(wiki_request):
    output = {"paragraphs": [{"text": "中文答复", "citations": [{"source_id": "source-1", "quote": "中文答复"}]}]}
    client = FakeClient(response(json.dumps(output)))
    model = ConfiguredWikiModel(client=client)
    actual, usage = await model.generate(wiki_request)
    assert actual == output and client.deadline == 120
    assert usage["input_tokens"] == 25 and usage["output_tokens"] == 10
    assert usage["estimated_cost"] is None and usage["billed_cost"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("body, finish, code", [("{incomplete", "stop", "wiki_provider_invalid_response"),
    ("{partial", "length", "wiki_provider_incomplete"),
    ("Commentary before ```json\n{}\n```", "stop", "wiki_provider_invalid_response")])
async def test_invalid_or_incomplete_output_retains_reported_usage(wiki_request, body, finish, code):
    model = ConfiguredWikiModel(client=FakeClient(response(body, finish=finish)))
    with pytest.raises(MemoryProviderError, match=code):
        await model.generate(wiki_request)
    assert model.last_usage["input_tokens"] == 25 and model.last_usage["output_tokens"] == 10
    assert model.last_usage["estimated_cost"] is None


@pytest.mark.asyncio
async def test_invalid_api_envelope_has_safe_reason_and_unknown_usage(wiki_request):
    model = ConfiguredWikiModel(client=FakeClient([]))
    with pytest.raises(MemoryProviderError, match="wiki_provider_invalid_response"):
        await model.generate(wiki_request)
    assert model.last_usage is None


@pytest.mark.asyncio
async def test_exact_json_fence_is_supported_without_relaxing_the_contract(wiki_request):
    value = {"verdicts": [{"index": 0, "supported": False}]}
    client = FakeClient(response("```json\n" + json.dumps(value) + "\n```"))
    result, usage = await ConfiguredWikiModel(client=client).generate(wiki_request)
    assert result == value and usage["input_tokens"] == 25
