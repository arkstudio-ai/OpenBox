import asyncio
import math

import httpx

from memory.providers.common import MemoryProviderError, bailian_key, response_json, shared_client
from memory.redaction import redact_text


class BailianEmbedding:
    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], dict]:
        if not texts:
            return [], {"total_tokens": 0, "model": self.config.embedding_model}
        vectors, tokens = [], 0
        client = self.client or shared_client(self.config.provider_timeout_seconds)
        try:
            for start in range(0, len(texts), self.config.embedding_batch_size):
                batch = texts[start:start + self.config.embedding_batch_size]
                response = await client.post(self.config.embedding_url,
                    headers={"Authorization": "Bearer " + bailian_key()},
                    json={"model": self.config.embedding_model, "input": [redact_text(text, len(text)) for text in batch],
                          "dimensions": self.config.embedding_dimensions, "encoding_format": "float"})
                data = response_json(response)
                rows = data.get("data")
                if not isinstance(rows, list) or len(rows) != len(batch):
                    raise MemoryProviderError("invalid_response")
                by_index = {}
                for row in rows:
                    index, vector = row.get("index"), row.get("embedding")
                    if isinstance(index, bool) or not isinstance(index, int) or index in by_index or not 0 <= index < len(batch):
                        raise MemoryProviderError("invalid_response")
                    if not isinstance(vector, list) or len(vector) != self.config.embedding_dimensions or any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
                        raise MemoryProviderError("invalid_response")
                    by_index[index] = vector
                vectors.extend(by_index[i] for i in range(len(batch)))
                usage = data.get("usage", {})
                count = usage.get("total_tokens", usage.get("prompt_tokens"))
                if count is not None and (isinstance(count, bool) or not isinstance(count, int) or count < 0):
                    raise MemoryProviderError("invalid_response")
                tokens = tokens + count if tokens is not None and count is not None else None
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise MemoryProviderError("timeout") from None
        except httpx.HTTPError:
            raise MemoryProviderError("network_error") from None
        price = self.config.embedding_price_per_million
        return vectors, {"total_tokens": tokens, "model": self.config.embedding_model,
                         "currency": self.config.embedding_currency, "price_version": self.config.price_version,
                         "estimated_cost": None if price is None or tokens is None else tokens * price / 1_000_000}
