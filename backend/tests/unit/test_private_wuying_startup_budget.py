"""A healthy slow guest uses the existing total startup budget.

The liveness response crosses a real loopback HTTP socket and HTTPX timeout.
Existing SQL/private authority is real; guest identity proof is the explicit
Wuying fixture boundary, and no remote workspace command is executed.
"""
import asyncio
import contextlib
import json

from sandbox.manager import SandboxManager
from tests.unit.test_private_wuying_runtime import assistant_database, wuying_world  # noqa: F401


async def test_slow_original_wuying_alive_completes_before_workspace_preparation(wuying_world):
    w = wuying_world
    manager = SandboxManager()
    requests, durations, handlers, failures = [], [], set(), []

    async def guest(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            first, *raw_headers = head.decode().split("\r\n")
            headers = {name.lower(): value for line in raw_headers if line for name, value in [line.split(": ", 1)]}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            scope = headers["x-openbox-private-scope"]
            binding = w.proofs[scope]
            assert headers["x-api-key"] == w.config.wuying_api_key
            assert headers["x-openbox-private-attempt"] == binding["attempt_id"]
            path = first.split(" ")[1]
            prefix = "/private-runtime/" + binding["id"]
            assert path.startswith(prefix + "/")
            operation = path[len(prefix):]
            requests.append(operation)
            if operation == "/alive":
                started = asyncio.get_running_loop().time()
                await asyncio.sleep(2.1)
                durations.append(asyncio.get_running_loop().time() - started)
                result = {"status": "ok"}
            else:
                assert operation == "/execute"
                assert isinstance(json.loads(body)["command"], str)
                result = {"exit_code": 0, "stdout": "", "stderr": ""}
            payload = json.dumps(result).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                + str(len(payload)).encode() + b"\r\n\r\n" + payload)
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            failures.append(exc)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_server(guest, "127.0.0.1", 0)
    w.config.wuying_endpoint = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        client = await manager.get_client(w.session.id, user_id=w.owner)
        assert client.private_runtime_route.desktop_id == w.config.wuying_desktop_id
        assert requests == ["/alive", "/execute"]
        assert len(durations) == 1 and 2 <= durations[0] < 10
        assert not failures
    finally:
        for client in manager._clients.values():
            await client.aclose()
        server.close()
        await server.wait_closed()
        for task in tuple(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
