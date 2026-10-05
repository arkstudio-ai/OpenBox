"""Lifespan-owned connections for fixed private Wuying HTTP routes.

Only transports are shared. Every caller still creates its own HTTP client,
headers, cookies and timeout, and performs its existing authority/proof checks.
Neither a connection nor its pool entry is evidence of current permission.
"""
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import json
from weakref import WeakKeyDictionary

import httpx


_MAX_IDLE_ROUTES = 64
_pools = WeakKeyDictionary()


@dataclass
class _Entry:
    transport: httpx.AsyncHTTPTransport
    users: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Pool:
    entries: OrderedDict = field(default_factory=OrderedDict)
    closing: bool = False
    closed: bool = False


class _BorrowedTransport(httpx.AsyncBaseTransport):
    def __init__(self, transport):
        self._transport = transport

    async def handle_async_request(self, request):
        return await self._transport.handle_async_request(request)

    async def aclose(self):
        # The request-local client does not own the shared connections.
        pass


def start_private_http_transports() -> None:
    """Open a new application lifetime; never replace an active loop's pool."""
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None or pool.closed:
        _pools[loop] = _Pool()
    elif pool.closing:
        raise RuntimeError("Private HTTP connections are shutting down")


@asynccontextmanager
async def private_http_transport(*, endpoint, api_key, scope=None, attempt=None):
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None:
        pool = _pools[loop] = _Pool()
    if pool.closing:
        raise RuntimeError("Private HTTP connections are shutting down")
    # No credential is retained in a printable key. Route changes cannot join
    # an older endpoint, credential, actor scope or provisioning attempt.
    key = hashlib.sha256(json.dumps([endpoint, api_key, scope, attempt],
        separators=(",", ":")).encode()).digest()
    entry = pool.entries.get(key)
    if entry is None:
        entry = _Entry(httpx.AsyncHTTPTransport(trust_env=False,
            limits=httpx.Limits(max_connections=32, max_keepalive_connections=8,
                               keepalive_expiry=60.0)))
        pool.entries[key] = entry
    pool.entries.move_to_end(key)
    entry.users += 1
    entry.drained.clear()
    try:
        # Bound dormant route pools without closing another active request.
        for old_key, old in tuple(pool.entries.items()):
            if len(pool.entries) <= _MAX_IDLE_ROUTES:
                break
            if old is not entry and old.users == 0:
                pool.entries.pop(old_key)
                await old.transport.aclose()
        yield _BorrowedTransport(entry.transport)
    finally:
        entry.users -= 1
        if entry.users == 0:
            entry.drained.set()


async def close_private_http_transports() -> None:
    """Deny new borrowers, drain requests, then close this loop's connections."""
    pool = _pools.get(asyncio.get_running_loop())
    if pool is None or pool.closed:
        return
    pool.closing = True

    async def close(entry):
        if entry.users:
            await entry.drained.wait()
        await entry.transport.aclose()

    await asyncio.gather(*(close(entry) for entry in pool.entries.values()))
    pool.entries.clear()
    pool.closed = True
