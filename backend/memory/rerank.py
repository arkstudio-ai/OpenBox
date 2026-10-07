import math

import httpx

from memory.providers.common import MemoryProviderError, bailian_key, response_json, shared_client
from memory.redaction import redact_text


async def rerank(query: str, documents: list[str], config, *, client=None) -> tuple[list[tuple[int, float]], dict]:
    if len(documents) > config.rerank_max_documents:
        raise ValueError("rerank budget exceeded")
    client = client or shared_client(config.provider_timeout_seconds)
    try:
        response = await client.post(config.rerank_url, headers={"Authorization": "Bearer " + bailian_key()},
            json={"model": config.rerank_model, "input": {"query": redact_text(query, len(query)),
                  "documents": [redact_text(text, len(text)) for text in documents]},
                  "parameters": {"top_n": len(documents), "return_documents": False}})
        data = response_json(response)
        results = data.get("output", {}).get("results")
        if not isinstance(results, list) or len(results) != len(documents):
            raise MemoryProviderError("invalid_response")
        ranked, seen = [], set()
        for item in results:
            index, score = item.get("index"), item.get("relevance_score")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(documents) or index in seen:
                raise MemoryProviderError("invalid_response")
            if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                raise MemoryProviderError("invalid_response")
            seen.add(index)
            ranked.append((index, float(score)))
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        tokens = data.get("usage", {}).get("total_tokens")
        if tokens is not None and (isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0):
            raise MemoryProviderError("invalid_response")
        price = config.rerank_price_per_million
        usage = {"model": config.rerank_model, "total_tokens": tokens, "pairs": len(documents),
                 "estimated_cost": None if price is None or tokens is None else tokens * price / 1_000_000,
                 "currency": config.embedding_currency, "price_version": config.price_version}
        return ranked, usage
    except httpx.TimeoutException:
        raise MemoryProviderError("timeout") from None
    except httpx.HTTPError:
        raise MemoryProviderError("network_error") from None
