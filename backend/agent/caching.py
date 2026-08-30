"""Prompt caching for LLM providers.

Applies ephemeral cache markers to system and conversation messages
to reduce token costs and latency for providers that support it.
"""
import hashlib

from core.log import create_logger

log = create_logger("agent.caching")


def session_cache_key(*, secret: str, user_id: str, session_id: str) -> str:
    """Return a non-reversible, tenant/session-scoped prompt-cache key."""
    if not user_id or not session_id:
        return ""
    salt = secret or "openbox-prompt-cache-v1"
    payload = f"{len(user_id)}:{user_id}{len(session_id)}:{session_id}"
    return hashlib.sha256(f"{salt}\0{payload}".encode("utf-8")).hexdigest()


def apply_caching(
    messages: list[dict],
    model_id: str,
    *,
    cache_key: str = "",
) -> list[dict]:
    """Apply prompt caching based on the provider.

    For Anthropic: adds ephemeral cache control to first 2 system messages
    and last 2 non-system messages.

    For OpenAI: adds a session-level cache key.

    Args:
        messages: List of LLM message dicts.
        model_id: The model ID (e.g., "anthropic/claude-sonnet-4-20250514").

    Returns:
        Modified messages list with cache markers applied.
    """
    provider = _detect_provider(model_id)

    if provider in ("anthropic", "bedrock"):
        return _apply_anthropic_caching(messages, provider)
    elif provider == "openai":
        return _apply_openai_caching(messages, cache_key)

    return messages


def _apply_anthropic_caching(messages: list[dict], provider: str) -> list[dict]:
    """Add ephemeral cache markers for Anthropic/Bedrock.

    Marks the first 2 system messages and the last 2 non-system messages
    with cache control metadata.
    """
    cache_options = {
        "anthropic": {"cacheControl": {"type": "ephemeral"}},
        "bedrock": {"cachePoint": {"type": "default"}},
    }

    cache_meta = cache_options.get(provider, cache_options["anthropic"])

    # Find system messages (first 2)
    system_indices = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "system":
            system_indices.append(i)
            if len(system_indices) >= 2:
                break

    # Find non-system messages (last 2)
    non_system_indices = []
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") != "system":
            non_system_indices.append(i)
            if len(non_system_indices) >= 2:
                break

    # Apply cache markers
    cache_indices = set(system_indices + non_system_indices)
    for i in cache_indices:
        msg = messages[i]
        existing = msg.get("provider_options", {})
        msg["provider_options"] = _deep_merge(existing, cache_meta)

    return messages


def _apply_openai_caching(messages: list[dict], cache_key: str = "") -> list[dict]:
    """Add session-level cache key for OpenAI."""
    if not cache_key:
        return messages
    for msg in messages:
        if msg.get("role") == "system":
            existing = msg.get("provider_options", {})
            msg["provider_options"] = _deep_merge(existing, {
                # Never use one cross-tenant literal. Callers pass a salted,
                # session-scoped digest; missing identity disables the hint.
                "setCacheKey": cache_key,
            })
            break  # Only set on first system message

    return messages


def _detect_provider(model_id: str) -> str:
    """Detect the provider from the model ID."""
    model_lower = model_id.lower()

    if model_lower.startswith("anthropic/") or "claude" in model_lower:
        return "anthropic"
    elif model_lower.startswith("bedrock/"):
        return "bedrock"
    elif model_lower.startswith("openai/") or "gpt" in model_lower or model_lower.startswith("o1") or model_lower.startswith("o3"):
        return "openai"
    elif model_lower.startswith("gemini") or model_lower.startswith("google/"):
        return "google"
    elif model_lower.startswith("deepseek/"):
        return "deepseek"
    elif model_lower.startswith("groq/"):
        return "groq"

    return "unknown"


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
