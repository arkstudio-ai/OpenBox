"""Request-shaped context measurement shared by chat and compaction.

The local tokenizer is an estimate for non-OpenAI routes. Provider usage is
an additional lower bound, never an excuse to omit newly added input.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agent.tool_payload import build_tool_definitions, proxy_token_count


@dataclass(frozen=True)
class RequestPrefix:
    system: list[str] = field(default_factory=list)
    tools: dict = field(default_factory=dict)
    variant: str | None = None
    cache_key: str = ""
    native_plan: Any = None


@dataclass(frozen=True)
class ContextBudget:
    input_tokens: int
    output_tokens: int
    context_limit: int
    threshold: int

    @property
    def under_pressure(self) -> bool:
        return self.input_tokens >= self.threshold


def count_payload(value: Any) -> int:
    """Count complete text/schema content without treating image bytes as text."""
    images = 0

    def normalized(item):
        nonlocal images
        if isinstance(item, dict):
            kind = item.get("type")
            # In a JSON schema's properties, "type" can itself be a nested
            # schema object. Only provider content blocks carry a string tag.
            if isinstance(kind, str) and kind in {"image_url", "input_image"}:
                images += 1
                return {"type": "image", "detail": item.get("detail", "auto")}
            return {key: normalized(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalized(child) for child in item]
        return item

    text = json.dumps(normalized(value), ensure_ascii=False, separators=(",", ":"), default=str)
    # 4K/image is a conservative proxy for ordinary desktop screenshots.
    # Exact image pricing is provider-specific; observed usage also calibrates
    # the next request and overflow recovery remains available.
    return proxy_token_count(text) + images * 4096


def request_payload(model_id: str, messages: list[dict], prefix: RequestPrefix) -> dict:
    from agent.llm import (_needs_responses_api, build_litellm_messages,
                           build_responses_input, history_has_tool_calls)

    if _needs_responses_api(model_id):
        definitions = (list(prefix.native_plan.tools) if prefix.native_plan is not None
                       else build_tool_definitions(prefix.tools, "responses"))
        return {"input": build_responses_input(messages, prefix.system), "tools": definitions}
    prepared = build_litellm_messages(prefix.system, messages, model_id)
    return {"messages": prepared, "tools": build_tool_definitions(
        prefix.tools, "litellm", include_noop=not prefix.tools and history_has_tool_calls(prepared),
    )}


def threshold_tokens(model_id: str, *, output_tokens: int = 0) -> int:
    from agent.compaction import get_model_context_limit
    from core.config import get_config

    cfg = get_config().compaction
    limit = get_model_context_limit(model_id)
    reserved = max(cfg.reserved or 0, output_tokens)
    return max(1, min(int(limit * cfg.threshold_ratio), limit - reserved))


def measure_request(model_id: str, messages: list[dict], prefix: RequestPrefix,
                    *, max_output_tokens: int | None = None,
                    observed_tokens: int = 0) -> ContextBudget:
    from agent.compaction import get_model_context_limit
    from agent.llm import request_output_tokens

    output = request_output_tokens(model_id, prefix.variant, max_output_tokens)
    return ContextBudget(
        input_tokens=max(count_payload(request_payload(model_id, messages, prefix)), observed_tokens),
        output_tokens=output,
        context_limit=get_model_context_limit(model_id),
        threshold=threshold_tokens(model_id, output_tokens=output),
    )


def summary_output_tokens(model_id: str, prefix: RequestPrefix) -> int:
    from agent.compaction import get_model_context_limit
    from agent.llm import request_output_tokens
    from core.config import get_config

    requested = min(get_config().compaction.max_tokens, max(256, get_model_context_limit(model_id) // 4))
    return request_output_tokens(model_id, prefix.variant, requested)
