"""Real loopback TCP proves pooling without sharing request authority or cookies."""
import asyncio
from contextlib import suppress
import json
from types import SimpleNamespace

import httpx
import pytest

from sandbox.private_http import close_private_http_transports, private_http_transport, start_private_http_transports
from sandbox.private_runtime import PrivateRuntimeError
from sandbox.private_wuying import ATTEMPT_HEADER, SCOPE_HEADER, _request, scope_id


@pytest.fixture
async def remote():
    start_private_http_transports()
    state = SimpleNamespace(requests=[], connections=0, closed=set(), handlers=set(),
                            entered=asyncio.Event(), release=asyncio.Event(), stall=False,
                            redirect=False, failures=[])

    async def serve(reader, writer):
        task = asyncio.current_task()
        state.handlers.add(task)
        state.connections += 1
        connection = state.connections
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                first, *lines = head.decode().split("\r\n")
                headers = {key.lower(): value for line in lines if line
                           for key, value in [line.split(": ", 1)]}
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                method, path, _ = first.split(" ")
                state.requests.append(SimpleNamespace(connection=connection, method=method,
                    path=path, headers=headers, body=body))
                if state.stall:
                    state.stall = False
                    state.entered.set()
                    await state.release.wait()
                payload = json.dumps({"observed": len(state.requests)}).encode()
                status = b"302 Found" if state.redirect else b"200 OK"
                if state.redirect:
                    payload = b'{}'
                writer.write(b"HTTP/1.1 " + status + b"\r\nContent-Type: application/json\r\n"
                    b"Connection: keep-alive\r\nSet-Cookie: secret-cookie=must-not-replay\r\n"
                    b"Location: /unexpected-redirect\r\nContent-Length: "
                    + str(len(payload)).encode() + b"\r\n\r\n" + payload)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.failures.append(exc)
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()
            state.closed.add(connection)
            state.handlers.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    state.base = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        yield state
        assert not state.failures
    finally:
        state.release.set()
        await close_private_http_transports()
        server.close()
        await server.wait_closed()
        for task in tuple(state.handlers):
            task.cancel()
        await asyncio.gather(*state.handlers, return_exceptions=True)


async def call(remote, *, key="fixture-key", owner="owner", attempt="attempt_original", path="actor_original"):
    scope = SimpleNamespace(workspace_id="fixture-workspace", user_id=owner)
    endpoint = SimpleNamespace(base_url=remote.base + ("" if path == "actor_original" else "/alternate"), api_key=key)
    return await _request(endpoint, f"/private-runtime/{path}/identity", scope=scope, attempt=attempt)


async def test_recreated_private_client_reuses_tcp_but_not_response_cookies(remote):
    first, second = await call(remote), await call(remote)
    assert (first["observed"], second["observed"]) == (1, 2)
    assert remote.connections == 1
    assert [request.connection for request in remote.requests] == [1, 1]
    assert all("cookie" not in request.headers for request in remote.requests)
    assert all(request.headers["x-api-key"] == "fixture-key" for request in remote.requests)
    assert all(request.headers["x-openbox-private-attempt"] == "attempt_original" for request in remote.requests)


async def test_endpoint_credential_scope_and_attempt_rotation_never_borrow_old_connection(remote):
    await call(remote)
    for change in ({"key": "rotated-key"}, {"owner": "peer"},
                   {"attempt": "attempt_new"}, {"path": "actor_other"}):
        await call(remote, **change)
    assert [request.connection for request in remote.requests] == [1, 2, 3, 4, 5]
    assert remote.requests[1].headers["x-api-key"] == "rotated-key"
    assert remote.requests[2].headers["x-openbox-private-scope"] != remote.requests[0].headers["x-openbox-private-scope"]
    assert remote.requests[3].headers["x-openbox-private-attempt"] == "attempt_new"
    assert all("cookie" not in request.headers for request in remote.requests)
    await call(remote)
    assert remote.requests[-1].connection == 1


async def test_private_transport_does_not_follow_redirects(remote):
    remote.redirect = True
    with pytest.raises(PrivateRuntimeError):
        await call(remote)
    assert len(remote.requests) == 1
    assert remote.requests[0].path != "/unexpected-redirect"


async def test_cancelled_request_does_not_return_its_response_to_the_next_call(remote):
    remote.stall = True
    task = asyncio.create_task(call(remote))
    await asyncio.wait_for(remote.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    result = await call(remote)
    assert result["observed"] == 2
    assert remote.connections == 2
    remote.release.set()


async def test_request_timeout_remains_local_and_pool_survives(remote):
    remote.stall = True
    scope = scope_id(SimpleNamespace(workspace_id="fixture-workspace", user_id="owner"))
    headers = {"X-API-Key": "fixture-key", SCOPE_HEADER: scope, ATTEMPT_HEADER: "attempt_original"}
    # A caller's own short timeout, on the same pooled route as _request.
    async with private_http_transport(endpoint=remote.base, api_key="fixture-key", scope=scope,
                                      attempt="attempt_original") as transport:
        async with httpx.AsyncClient(timeout=.05, follow_redirects=False, trust_env=False,
                                     transport=transport) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.get(remote.base + "/private-runtime/actor_original/identity", headers=headers)
    assert (await call(remote))["observed"] == 2
    assert remote.connections == 2
    remote.release.set()


async def test_different_event_loops_own_and_close_distinct_connections(remote):
    async def other_lifetime():
        start_private_http_transports()
        try:
            await call(remote)
            await call(remote)
        finally:
            await close_private_http_transports()

    for _ in range(2):
        await asyncio.to_thread(lambda: asyncio.run(other_lifetime()))
    assert [request.connection for request in remote.requests] == [1, 1, 2, 2]
    assert remote.connections == 2


async def test_shutdown_drains_inflight_requests_closes_sockets_and_blocks_new_borrowers(remote):
    remote.stall = True
    task = asyncio.create_task(call(remote))
    await asyncio.wait_for(remote.entered.wait(), 2)
    closing = asyncio.create_task(close_private_http_transports())
    await asyncio.sleep(0)
    assert not closing.done()
    with pytest.raises(RuntimeError, match="shutting down"):
        await call(remote)
    assert len(remote.requests) == 1
    remote.release.set()
    assert (await task)["observed"] == 1
    await asyncio.wait_for(closing, 2)
    for _ in range(20):
        if 1 in remote.closed:
            break
        await asyncio.sleep(.01)
    assert 1 in remote.closed
    with pytest.raises(RuntimeError, match="shutting down"):
        await call(remote)
    start_private_http_transports()
    assert (await call(remote))["observed"] == 2
    assert remote.connections == 2
