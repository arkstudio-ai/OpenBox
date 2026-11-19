"""Structured visual reports across the configured OpenAI-compatible providers."""

from __future__ import annotations


def request_options(model: str, function: str, provider: dict) -> dict:
    options = dict(provider)
    if "minimax-m3.1-flash" in model.lower():
        # Bailian MiniMax only supports auto; its default max reasoning can use
        # the entire short visual-report budget without emitting the report.
        # extra_body bypasses LiteLLM's stale model-specific parameter allowlist.
        options["tool_choice"] = "auto"
        options["extra_body"] = {
            **(options.get("extra_body") or {}), "reasoning_effort": "medium",
        }
    else:
        options["tool_choice"] = {"type": "function", "function": {"name": function}}
    return options


def output(response, function: str) -> str:
    choice = response.choices[0]
    if getattr(choice, "finish_reason", None) == "length":
        raise ValueError("Visual report was truncated before its structured output finished")
    for call in getattr(choice.message, "tool_calls", None) or []:
        if call.function.name == function:
            return call.function.arguments
    # Validate content with the caller's schema. Never treat reasoning as evidence.
    return choice.message.content or ""


def response_evidence(response, content: str) -> dict:
    usage = getattr(response, "usage", None)
    return {
        "output": content,
        "finish_reason": getattr(response.choices[0], "finish_reason", None),
        "usage": usage.model_dump() if hasattr(usage, "model_dump") else None,
    }
