import asyncio
import os
import weakref

import httpx


class MemoryProviderError(RuntimeError):
    def __init__(self, code: str, status_code: int | None = None):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def bailian_key() -> str:
    key = os.getenv("MEMORY_BAILIAN_API_KEY") or os.getenv("BAILIAN_KEY") or os.getenv("DASHSCOPE_API_KEY")
    if not key:
        raise MemoryProviderError("provider_not_configured")
    return key


def jev_key() -> str:
    key = os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_KEY")
    if not key:
        raise MemoryProviderError("provider_not_configured")
    return key


def response_json(response):
    if response.status_code >= 400:
        code = "rate_limited" if response.status_code == 429 else "authentication_error" if response.status_code in (401, 403) else "provider_error"
        raise MemoryProviderError(code, response.status_code)
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise MemoryProviderError("invalid_response") from None
    if not isinstance(data, dict):
        raise MemoryProviderError("invalid_response")
    return data


# One keep-alive client per event loop and timeout: a recall makes several
# provider calls (routing, embedding, the index, rerank) and used to open a new
# TCP/TLS connection for each. Keyed by the current httpx.AsyncClient too, so a
# test that replaces it gets its own client.
KEEPALIVE_SECONDS = 30.0
_SHARED: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()


def shared_client(timeout: float) -> httpx.AsyncClient:
    """A pooled client for provider calls; callers never close it."""
    clients = _SHARED.setdefault(asyncio.get_running_loop(), {})
    key = (httpx.AsyncClient, timeout)
    client = clients.get(key)
    if client is None or getattr(client, "is_closed", False):
        client = clients[key] = httpx.AsyncClient(
            timeout=timeout, limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SECONDS))
    return client


async def close_shared_clients() -> None:
    """Close this event loop's pooled provider clients (application shutdown)."""
    clients = _SHARED.pop(asyncio.get_running_loop(), {})
    for client in clients.values():
        try:
            await client.aclose()
        except Exception:
            pass
