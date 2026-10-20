"""Gateway transport evidence and progress deadlines, without hidden retries.

Waiting for headers includes gateway queuing and model work; it is not a
measurement of model compute alone. Never log request bodies, headers or URLs.
The body span is elapsed stream lifetime (including consumer pauses); exact
external wait versus application time still comes from agent.latency.
"""
import asyncio
import inspect
import json
import time
import weakref
from contextvars import ContextVar

import httpx

from agent.retry import RetryableError
from core.log import create_logger

log = create_logger("agent.gateway")
FIRST_PROGRESS_SECONDS = 60.0
CAPTION_FIRST_PROGRESS_SECONDS = 30.0
IDLE_PROGRESS_SECONDS = 60.0
_current = ContextVar("gateway_observation", default=None)
_clients = weakref.WeakKeyDictionary()


class GatewayTimeout(RetryableError):
    def __init__(self, phase):
        self.phase = phase
        super().__init__(f"模型网关等待超时（{phase}），请稍后重试。")


class Observation:
    def __init__(self, model, purpose):
        self.model, self.purpose = model, purpose
        self.started = time.monotonic()
        self.first_seconds = (CAPTION_FIRST_PROGRESS_SECONDS if purpose == "asset_description"
                              else FIRST_PROGRESS_SECONDS)
        self.deadline = asyncio.get_running_loop().time() + self.first_seconds
        self.first_progress_ms = None
        self.spans, self.active = {}, {}
        self.phase, self.failure_phase, self.error = "dispatch", None, None
        self.proxy_tunnel, self.proxy_seen = False, False
        self.status = None

    async def on_request(self, request):
        request.extensions["trace"] = self.trace

    async def trace(self, event, info):
        # httpcore traces expose credentials in info; inspect only method and
        # exception type. CONNECT response headers belong to the proxy, not LLM.
        stage, _, state = event.rpartition(".")
        action = stage.rsplit(".", 1)[-1]
        if action == "send_request_headers" and state == "started":
            self.proxy_tunnel = getattr(info.get("request"), "method", None) == b"CONNECT"
            self.proxy_seen |= self.proxy_tunnel
        phase = {"connect_tcp": "connect", "start_tls": "tls",
                 "send_request_headers": "send", "send_request_body": "send",
                 "receive_response_headers": "upstream_headers",
                 "receive_response_body": "stream_read"}.get(action)
        if phase is None:
            return
        if self.proxy_tunnel and action != "start_tls":
            phase = "proxy_connect"
        if state == "started":
            self.active[stage] = (phase, time.monotonic())
            self.phase = phase
        else:
            span = self.active.pop(stage, None)
            if span:
                name, start = span
                self.spans[name] = self.spans.get(name, 0) + (time.monotonic() - start) * 1000
            if state == "failed" and self.failure_phase is None:
                self.failure_phase = phase
                self.error = type(info.get("exception")).__name__
            if action == "receive_response_headers" and state == "complete" and not self.proxy_tunnel:
                value = info.get("return_value")
                if isinstance(value, tuple) and len(value) > 1:
                    self.status = value[1]

    async def dispatch(self, operation):
        token = _current.set(self)
        try:
            async with asyncio.timeout_at(self.deadline):
                return await operation()
        except TimeoutError as exc:
            raise GatewayTimeout("first_progress") from exc
        finally:
            _current.reset(token)

    async def chunks(self, stream):
        iterator = stream.__aiter__()
        while True:
            try:
                async with asyncio.timeout_at(self.deadline):
                    chunk = await anext(iterator)
            except StopAsyncIteration:
                return
            except TimeoutError as exc:
                raise GatewayTimeout("first_progress" if self.first_progress_ms is None else "stream_idle") from exc
            # Role-only chunks and SSE heartbeats do not prove model progress.
            choices = getattr(chunk, "choices", []) or []
            if any(getattr(c, "finish_reason", None) or any(
                    getattr(getattr(c, "delta", None), field, None)
                    for field in ("content", "reasoning_content", "tool_calls", "function_call")) for c in choices):
                if self.first_progress_ms is None:
                    self.first_progress_ms = (time.monotonic() - self.started) * 1000
                self.deadline = asyncio.get_running_loop().time() + IDLE_PROGRESS_SECONDS
            handed_off = asyncio.get_running_loop().time()
            yield chunk
            # DB/trajectory/UI work by the consumer is not provider inactivity.
            self.deadline += asyncio.get_running_loop().time() - handed_off

    def finish(self, outcome, error=None):
        now = time.monotonic()
        spans = dict(self.spans)
        for name, start in self.active.values():
            spans[name] = spans.get(name, 0) + (now - start) * 1000
        log.info("gateway timing %s", json.dumps({
            "model": self.model, "purpose": self.purpose, "outcome": outcome,
            "total_ms": round((now - self.started) * 1000, 1),
            "first_progress_ms": round(self.first_progress_ms, 1) if self.first_progress_ms is not None else None,
            "phase_ms": {name: round(value, 1) for name, value in spans.items()},
            "last_phase": self.failure_phase or self.phase, "proxy_tunnel": self.proxy_seen,
            # Closing an SSE iterator after [DONE] raises GeneratorExit inside
            # httpcore; it is normal teardown, not a failed model response.
            "status": self.status, "error": (type(error).__name__ if error else self.error)
                if outcome != "completed" else None,
        }, ensure_ascii=False))


async def _on_request(request):
    observation = _current.get()
    if observation is not None:
        await observation.on_request(request)


def sdk_client(provider, timeout):
    """Use the same verified proxy policy, with a reusable observable HTTP pool."""
    if not provider.get("api_key") or not provider.get("api_base"):
        return None  # preserve SDK/environment resolution for unconfigured routes
    from openai import AsyncOpenAI
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None or client.is_closed:
        client = _clients[loop] = httpx.AsyncClient(
            timeout=timeout, event_hooks={"request": [_on_request]},
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=30))
    return AsyncOpenAI(api_key=provider["api_key"], base_url=provider["api_base"],
                       organization=provider.get("organization"), http_client=client,
                       max_retries=0, timeout=timeout)


async def close_stream(stream):
    """LiteLLM 1.81's wrapper has no aclose; its OpenAI stream has async close.

    Explicitly close even on early consumer exit or timeout between chunks.
    Otherwise a live response can occupy the pool until garbage collection.
    """
    if stream is None:
        return
    target = stream
    if not getattr(target, "aclose", None) and getattr(target, "completion_stream", None) is not None:
        target = target.completion_stream
    closer = getattr(target, "aclose", None) or getattr(target, "close", None)
    if closer is not None:
        result = closer()
        if inspect.isawaitable(result):
            await result


async def close_clients():
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
