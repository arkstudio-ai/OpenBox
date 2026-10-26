"""Release completed SSE responses to the pool without keeping silent tails alive.

The SDK stops at [DONE], before HTTPX sees the HTTP body terminator. Closing
that unread HTTP/1.1 response discards the connection. Drain only an already
terminated SSE response, within a small time/byte budget; cancellation and
unfinished streams still close immediately. Never change or replay model data.
"""
import asyncio

import httpx

DRAIN_SECONDS = 0.1
DRAIN_BYTES = 65536
_DONE = tuple(b"\n" + line + ending + ending for line in
              (b"data: [DONE]", b"data:[DONE]") for ending in (b"\n", b"\r\n"))


class CompletedSSEStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream
        self.iterator = stream.__aiter__()
        self.done = self.eof = self.closed = False
        self.tail = b"\n"

    async def __aiter__(self):
        async for chunk in self.iterator:
            if not self.done:
                candidate = self.tail + chunk
                self.done = any(marker in candidate for marker in _DONE)
                self.tail = candidate[-32:]
            yield chunk
        self.eof = True

    async def aclose(self):
        if self.closed:
            return
        self.closed = True
        try:
            task = asyncio.current_task()
            if self.done and not self.eof and not (task and task.cancelling()):
                from agent.latency import provider_wait
                try:
                    with provider_wait():
                        async with asyncio.timeout(DRAIN_SECONDS):
                            consumed = 0
                            async for chunk in self.iterator:
                                consumed += len(chunk)
                                if consumed > DRAIN_BYTES:
                                    break
                            else:
                                self.eof = True
                except (TimeoutError, httpx.HTTPError):
                    pass  # The completed answer stands; this socket is discarded.
        finally:
            await self.stream.aclose()


async def preserve_completed_sse(response):
    # Raw response streams precede HTTPX's decompression. Do not inspect
    # compressed data or interfere with other protocols/error responses.
    if (response.status_code == 200
            and response.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "text/event-stream"
            and response.headers.get("content-encoding", "identity").lower() == "identity"):
        response.stream = CompletedSSEStream(response.stream)
