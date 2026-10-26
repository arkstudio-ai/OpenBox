import asyncio
import os
import weakref
from urllib.parse import urlsplit

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


def memory_api_key(config, endpoint: str) -> str:
    """Do not forward an official fallback credential to an arbitrary gateway."""
    if key := getattr(config, "api_key", ""):
        return key
    host = (urlsplit(endpoint).hostname or "").lower()
    if not (host in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com",
                     "dashscope-us.aliyuncs.com"}
            or host.endswith(".dashscope.aliyuncs.com") or host.endswith(".maas.aliyuncs.com")):
        raise MemoryProviderError("gateway_credential_not_configured")
    return bailian_key()


def jev_key() -> str:
    key = os.getenv("TYPESAFE_API_KEY") or os.getenv("JEV_KEY")
    if not key:
        raise MemoryProviderError("provider_not_configured")
    return key


def response_json(response):
    if response.status_code >= 400:
        code = ("region_unavailable" if response.status_code == 451 else "rate_limited" if response.status_code == 429
                else "authentication_error" if response.status_code in (401, 403) else "provider_error")
        raise MemoryProviderError(code, response.status_code)
    try:
        data = response.json()
    except (ValueError, TypeError):
        raise MemoryProviderError("invalid_response") from None
    if not isinstance(data, dict):
        raise MemoryProviderError("invalid_response")
    return data


# Provider stages share connections, not timeouts or authorization headers.
# A read's short timeout must not isolate it from a connection already opened
# by extraction/verification. Each request supplies its own timeout. The pool
# is confined to its event loop and proxy policy; HTTPX isolates origins.
KEEPALIVE_SECONDS = 90.0
_SHARED: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict]" = weakref.WeakKeyDictionary()


def shared_client(*, trust_env: bool = False) -> httpx.AsyncClient:
    """A pooled client; callers set request timeouts and never close it."""
    clients = _SHARED.setdefault(asyncio.get_running_loop(), {})
    key = (httpx.AsyncClient, trust_env)
    client = clients.get(key)
    if client is None or getattr(client, "is_closed", False):
        client = clients[key] = httpx.AsyncClient(
            timeout=20, trust_env=trust_env,
            limits=httpx.Limits(keepalive_expiry=KEEPALIVE_SECONDS))
    return client


async def close_shared_clients() -> None:
    """Close this event loop's pooled provider clients (application shutdown)."""
    clients = _SHARED.pop(asyncio.get_running_loop(), {})
    for client in clients.values():
        try:
            await client.aclose()
        except Exception:
            pass
