"""Gateway wire formats and credential boundaries; no live providers."""
import json

import httpx
import pytest

from core.config import MemoryConfig, OpenBoxConfig, _apply_env_overrides
from memory.index.embedding import BailianEmbedding
from memory.providers.common import MemoryProviderError, memory_api_key
from memory.rerank import rerank


def test_gateway_configuration_and_official_fallback_boundary(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "official-secret")
    monkeypatch.delenv("MEMORY_BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("BAILIAN_KEY", raising=False)
    config = MemoryConfig()
    assert memory_api_key(config, config.embedding_url) == "official-secret"
    for url in ("http://gateway:3000/v1/embeddings", "https://dashscope.aliyuncs.com.evil.test/v1"):
        with pytest.raises(MemoryProviderError, match="gateway_credential_not_configured"):
            memory_api_key(config, url)
    monkeypatch.setenv("MEMORY_API_KEY", "gateway-only")
    monkeypatch.setenv("MEMORY_RERANK_PROTOCOL", "standard")
    config = OpenBoxConfig(**_apply_env_overrides({})).memory
    assert memory_api_key(config, "http://gateway:3000/v1/embeddings") == "gateway-only"
    assert config.rerank_protocol == "standard"


async def test_embedding_gateway_uses_explicit_key_and_preserves_dimensions(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-leave-backend")
    config = MemoryConfig(api_key="gateway-only", embedding_url="http://gateway/v1/embeddings", embedding_dimensions=64)
    def respond(request):
        assert request.headers["authorization"] == "Bearer gateway-only"
        body = json.loads(request.content)
        assert body["dimensions"] == 64 and body["input"] == ["park", "tea"]
        return httpx.Response(200, json={"data": [
            {"index": 1, "embedding": [.2] * 64}, {"index": 0, "embedding": [.1] * 64}],
            "usage": {"total_tokens": 8}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        vectors, usage = await BailianEmbedding(config, client=client).embed(["park", "tea"])
    assert vectors[0][0] == .1 and vectors[1][0] == .2 and usage["total_tokens"] == 8


@pytest.mark.parametrize("protocol", ["dashscope", "standard"])
async def test_rerank_protocols_have_equivalent_validated_output(protocol):
    config = MemoryConfig(api_key="explicit-key", rerank_protocol=protocol)
    def respond(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer explicit-key"
        expected = {"query": "quiet", "documents": ["party", "park"]}
        if protocol == "standard":
            assert body == {"model": config.rerank_model, **expected, "top_n": 2, "return_documents": False}
        else:
            assert body == {"model": config.rerank_model, "input": expected,
                            "parameters": {"top_n": 2, "return_documents": False}}
        results = {"results": [{"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": .9}]}
        return httpx.Response(200, json={**(results if protocol == "standard" else {"output": results}),
                                        "usage": {"total_tokens": 12}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        ranks, usage = await rerank("quiet", ["party", "park"], config, client=client)
    assert ranks == [(1, .9), (0, .1)] and usage["total_tokens"] == 12


@pytest.mark.parametrize("results", [
    [{"index": 0, "relevance_score": .1}, {"index": 0, "relevance_score": .9}],
    [{"index": 0, "relevance_score": .1}],
    [{"index": 0, "relevance_score": .1}, {"index": 1, "relevance_score": True}],
])
async def test_gateway_still_rejects_invalid_ranks(results):
    config = MemoryConfig(api_key="gateway-only", rerank_protocol="standard")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": results}))) as client:
        with pytest.raises(MemoryProviderError, match="invalid_response"):
            await rerank("quiet", ["party", "park"], config, client=client)
