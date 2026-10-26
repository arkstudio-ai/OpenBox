"""Bound silent or endless provider streams without timing consumer tool work."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass


class ModelResponseDeadline(Exception):
    """An exhausted request budget must not start another automatic request."""

    def __init__(self, phase: str):
        self.phase = phase
        super().__init__("模型响应等待超时，已停止本次请求。已完成的内容会保留，请稍后重试。")


@dataclass(frozen=True)
class StreamLimits:
    first_progress: float = 30
    idle: float = 60
    total: float = 180


def limits_for(model_id: str, variant: str | None, purpose: str) -> StreamLimits:
    from core.config import get_config
    from agent.llm import reasoning_profile
    configured = next((model for model in get_config().models if model.id == model_id), None)
    reasoning = variant or reasoning_profile(model_id).default_variant
    slow = reasoning not in (None, "none", "minimal", "low") or purpose == "compaction"
    defaults = StreamLimits(first_progress=90 if slow else 30, total=600 if slow else 180)
    return StreamLimits(**{key: getattr(configured, config_key, None) or getattr(defaults, key)
        for key, config_key in (("first_progress", "first_progress_timeout_seconds"),
            ("idle", "stream_idle_timeout_seconds"), ("total", "response_timeout_seconds"))})


def meaningful(event: dict) -> bool:
    kind = event.get("type")
    if kind in {"text_delta", "reasoning_delta"}:
        return bool(event.get("text", "").strip())
    if kind == "tool_call_args_delta":
        return bool(event.get("delta", "").strip())
    return kind in {"tool_call_start", "tool_call", "finish", "error"}


async def bounded_stream(stream, limits: StreamLimits):
    # Count time spent awaiting this iterator, not time a consumer spends
    # persisting a chunk or executing a tool between yields. Usage/heartbeats
    # do not reset the first-output or idle deadlines.
    loop = asyncio.get_running_loop()
    waited = quiet = 0.0
    progressed = False
    try:
        while True:
            remaining = limits.total - waited
            phase = "total"
            silence = (limits.idle if progressed else limits.first_progress) - quiet
            if silence < remaining:
                remaining, phase = silence, "idle" if progressed else "first_progress"
            if remaining <= 0:
                raise ModelResponseDeadline(phase)
            started = loop.time()
            timeout = asyncio.timeout(remaining)
            try:
                async with timeout:
                    event = await anext(stream)
            except StopAsyncIteration:
                return
            except TimeoutError:
                if timeout.expired():
                    raise ModelResponseDeadline(phase) from None
                raise  # A provider transport error retains its own policy.
            elapsed = loop.time() - started
            waited += elapsed
            quiet += elapsed
            if meaningful(event):
                progressed, quiet = True, 0.0
            yield event
    finally:
        await stream.aclose()
