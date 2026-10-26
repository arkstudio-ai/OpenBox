"""Bailian realtime usage compatibility for New API versions using singular keys.

Runs only on the gateway's private Docker network. Credentials are supplied by
New API; this process neither stores them nor accepts configurable destinations.
All original events/fields are preserved. Only response.done usage gets aliases.
"""
import asyncio
import json
import os
from urllib.parse import parse_qs, urlencode, urlsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

MODELS = frozenset(("qwen-audio-3.1-realtime-plus", "qwen3.8-omni-flash-realtime"))
UPSTREAM = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"


def compatible_event(message):
    """Do not rewrite audio, tool calls, settings or existing usage fields."""
    if not isinstance(message, str):
        return message
    try:
        data = json.loads(message)
    except (ValueError, TypeError):
        return message
    if not isinstance(data, dict) or data.get("type") != "response.done":
        return message
    response = data.get("response")
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return message
    changed = False
    for direction in ("input", "output"):
        source, target = direction + "_tokens_details", direction + "_token_details"
        if target not in usage and isinstance(usage.get(source), dict):
            usage[target] = usage[source]
            changed = True
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")) if changed else message


def request_model(request):
    parsed = urlsplit(request.path)
    query = parse_qs(parsed.query)
    values = query.get("model", [])
    if parsed.path != "/v1/realtime" or len(values) != 1 or values[0] not in MODELS:
        return None
    return values[0]


def preflight(connection, request):
    if request.path == "/health":
        return connection.respond(200, "ok\n")
    if request_model(request) is None:
        return connection.respond(400, "Unsupported realtime route\n")
    if not request.headers.get("Authorization", "").startswith("Bearer "):
        return connection.respond(401, "Authentication required\n")


async def copy_messages(source, destination, *, normalize=False):
    async for message in source:
        await destination.send(compatible_event(message) if normalize else message)


async def relay(client):
    model = request_model(client.request)
    tasks = []
    try:
        async with connect(UPSTREAM + "?" + urlencode({"model": model}),
                           additional_headers={"Authorization": client.request.headers["Authorization"]},
                           proxy=None, open_timeout=8, close_timeout=3, max_size=8 * 1024 * 1024) as upstream:
            tasks = [asyncio.create_task(copy_messages(client, upstream)),
                     asyncio.create_task(copy_messages(upstream, client, normalize=True))]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
    except ConnectionClosed:
        pass
    except Exception:
        # Provider exceptions may include headers. Never log them or send them
        # downstream; New API and the provider retain their own request logs.
        try:
            await client.send('{"type":"error","error":{"code":"upstream_unavailable",'
                              '"message":"Realtime provider unavailable"}}')
        except ConnectionClosed:
            pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.close()


async def main():
    async with serve(relay, "0.0.0.0", int(os.getenv("PORT", "8080")), process_request=preflight,
                     max_size=8 * 1024 * 1024, close_timeout=3, max_queue=16):
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    asyncio.run(main())
