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
        self.request = json
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


@pytest.mark.asyncio
@pytest.mark.parametrize("responses", [False, True])
async def test_reconciliation_explicitly_requests_json_on_both_transports(wiki_request, monkeypatch, responses):
    from memory.reconciliation import SYSTEM

    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _model: responses)
    value = {"revisions": [], "separate": []}
    payload = ({"status": "completed", "output_text": json.dumps(value)} if responses
               else response(json.dumps(value)))
    client = FakeClient(payload)
    actual, _ = await ConfiguredWikiModel(client=client).generate_data(
        model=wiki_request.policy.model, system=SYSTEM,
        data={"existing_memories": [], "proposals": [], "new_user_sources": []}, sources=[])
    instruction = client.request["instructions"] if responses else client.request["messages"][0]["content"]
    assert instruction.startswith(SYSTEM.rstrip())
    assert "json" in instruction.lower()
    assert actual == value


@pytest.mark.parametrize("responses,terminal", [(False, None), (False, "length"), (True, None), (True, "failed"), (True, "in_progress")])
async def test_unconfirmed_provider_completion_cannot_be_grounding_evidence(wiki_request, monkeypatch, responses, terminal):
    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _: responses)
    body = '{"verdicts":[{"index":0,"supported":true}]}'
    payload = {"status": terminal, "output_text": body} if responses else response(body, finish=terminal)
    with pytest.raises(MemoryProviderError, match="wiki_provider_incomplete"):
        await ConfiguredWikiModel(client=FakeClient(payload)).generate(wiki_request)


@pytest.mark.parametrize("base,expected", [
    ("https://dashscope.aliyuncs.com/compatible-mode/v1", "json_schema"),
    ("https://proxy.invalid/v1", "json_object"),
])
async def test_grounding_requests_complete_contract_on_verified_official_route(wiki_request, monkeypatch, base, expected):
    from memory.grounding import GroundingVerifier
    monkeypatch.setattr("agent.llm._get_provider_kwargs", lambda _: {"api_key": "test-only", "api_base": base})
    client = FakeClient(response(json.dumps({"verdicts": [
        {"index": 0, "supported": True, "intent": "enduring_assertion"}]})))
    result, _ = await GroundingVerifier(adapter=ConfiguredWikiModel(client=client)).verify(
        [{"claim": "喜欢读书", "sources": ["我一直喜欢读书"]}], purpose="memory", model="aliyun/qwen3.8-flash")
    assert result == [True] and client.request["response_format"]["type"] == expected
    if expected == "json_schema":
        schema = client.request["response_format"]["json_schema"]["schema"]
        verdict = schema["properties"]["verdicts"]["items"]
        assert set(verdict["required"]) == {"index", "supported", "intent"}
        assert verdict["additionalProperties"] is False


@pytest.mark.parametrize("responses", [False, True])
async def test_review_images_use_inline_pixels_on_both_transports(wiki_request, monkeypatch, responses):
    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _: responses)
    uri = "data:image/png;base64,aW1hZ2U="
    value = {"allowed": True, "violations": []}
    payload = {"status": "completed", "output_text": json.dumps(value)} if responses else response(json.dumps(value))
    client = FakeClient(payload)
    actual, _ = await ConfiguredWikiModel(client=client).generate_data(
        model=wiki_request.policy.model, system="Check this image", data={"image_number": 1}, sources=(), images=(uri,))
    content = client.request["input"][0]["content"] if responses else client.request["messages"][1]["content"]
    assert actual == value and len(content) == 2
    assert content[1] == ({"type": "input_image", "image_url": uri} if responses
                          else {"type": "image_url", "image_url": {"url": uri}})


@pytest.mark.parametrize("images", [("https://private.invalid/image.png",), ("/local/image.png",),
    (None,), "data:image/png;base64,YQ==", ("data:text/html;base64,YQ==",),
    ("data:image/png;base64,YQ==",) * 5, ("data:image/png;base64," + "A" * 14_000_000,)])
async def test_review_rejects_remote_or_unbounded_images_before_provider_call(wiki_request, images):
    client = FakeClient(response("{}"))
    with pytest.raises(MemoryProviderError, match="invalid_review_images"):
        await ConfiguredWikiModel(client=client).generate_data(
            model=wiki_request.policy.model, system="Check", data={}, sources=(), images=images)
    assert client.calls == 0
