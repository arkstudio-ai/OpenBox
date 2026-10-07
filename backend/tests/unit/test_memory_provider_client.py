"""Pooled HTTP clients for memory provider calls (routing, embedding, index, rerank)."""
import httpx

from memory.providers import common


async def test_provider_calls_share_one_client_per_loop_and_timeout():
    first = common.shared_client(10)
    assert common.shared_client(10) is first
    other = common.shared_client(20)
    assert other is not first
    await common.close_shared_clients()
    assert first.is_closed and other.is_closed
    fresh = common.shared_client(10)
    assert fresh is not first and not fresh.is_closed
    await common.close_shared_clients()


async def test_a_replaced_http_client_class_gets_its_own_client(monkeypatch):
    real = common.shared_client(10)
    seen = []
    original = httpx.AsyncClient

    def fake(**kwargs):
        seen.append(kwargs)
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    replaced = common.shared_client(10)
    assert replaced is not real and seen and seen[0]["timeout"] == 10
    assert common.shared_client(10) is replaced
    await common.close_shared_clients()
