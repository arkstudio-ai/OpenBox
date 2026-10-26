"""Pooled HTTP clients for memory provider calls (routing, embedding, index, rerank)."""
import httpx

from memory.providers import common


async def test_provider_calls_share_one_client_until_shutdown():
    first = common.shared_client()
    assert common.shared_client() is first
    await common.close_shared_clients()
    assert first.is_closed
    fresh = common.shared_client()
    assert fresh is not first and not fresh.is_closed
    await common.close_shared_clients()


async def test_a_replaced_http_client_class_gets_its_own_client(monkeypatch):
    real = common.shared_client()
    seen = []
    original = httpx.AsyncClient

    def fake(**kwargs):
        seen.append(kwargs)
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    replaced = common.shared_client()
    assert replaced is not real and seen
    assert common.shared_client() is replaced
    await common.close_shared_clients()


async def test_proxy_policy_is_explicit_and_isolates_connection_pools(monkeypatch):
    seen = []
    original = httpx.AsyncClient

    def fake(**kwargs):
        seen.append(kwargs["trust_env"])
        return original(transport=httpx.MockTransport(lambda request: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake)
    direct = common.shared_client()
    proxied = common.shared_client(trust_env=True)
    assert direct is not proxied
    assert common.shared_client(trust_env=False) is direct
    assert seen == [False, True]
    await common.close_shared_clients()


async def test_shared_transport_does_not_leak_authorization_or_change_request_deadlines(monkeypatch):
    original = httpx.AsyncClient
    observed = []

    def respond(request):
        observed.append((str(request.url), request.headers.get("authorization"), request.extensions["timeout"]["read"]))
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    client = common.shared_client()
    try:
        await client.post("https://a.invalid/v1/chat/completions", headers={"Authorization": "Bearer a"}, timeout=1.5)
        await client.post("https://a.invalid/v1/chat/completions", headers={"Authorization": "Bearer b"}, timeout=90)
        await client.get("https://b.invalid/health", timeout=.5)
        assert [row[1:] for row in observed] == [("Bearer a", 1.5), ("Bearer b", 90), (None, .5)]
    finally:
        await common.close_shared_clients()
