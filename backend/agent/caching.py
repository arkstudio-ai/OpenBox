"""Prompt caching for LLM providers.

Applies ephemeral cache markers to system and conversation messages
to reduce token costs and latency for providers that support it.
"""
import copy
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
    """Return provider-normalized message cache breakpoints.

    For Anthropic: adds ephemeral cache control to first 2 system messages
    and last 2 non-system messages.

    OpenAI cache affinity is a top-level wire parameter, not message metadata;
    callers pass ``cache_key`` directly to the Responses/Chat adapter. It is
    accepted here only for compatibility with older callers.

    Args:
        messages: List of LLM message dicts.
        model_id: The model ID (e.g., "anthropic/claude-sonnet-4-20250514").

    Returns:
        Modified messages list with cache markers applied.
    """
    del cache_key
    copied = copy.deepcopy(messages)
    provider = _detect_provider(model_id)

    if provider in ("anthropic", "bedrock"):
        return _apply_anthropic_caching(copied)

    return copied


def _apply_anthropic_caching(messages: list[dict]) -> list[dict]:
    """Add the normalized cache marker understood by LiteLLM.

    LiteLLM translates ``cache_control`` to Anthropic content-block
    ``cache_control`` and Bedrock Converse ``cachePoint``. The former
    ``provider_options.cacheControl/cachePoint`` shape belonged to a different
    SDK and never reached either wire.
    """
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
        existing = messages[i].get("cache_control", {})
        messages[i]["cache_control"] = _deep_merge(
            existing,
            {"type": "ephemeral"},
        )

    return messages


def _detect_provider(model_id: str) -> str:
    """Detect the actual transport provider, preferring its explicit slot."""
    model_lower = model_id.lower()
    prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""

    if prefix in {"openai", "azure"}:
        return "openai"
    if prefix == "anthropic":
        return "anthropic"
    if prefix == "bedrock":
        return "bedrock"
    if prefix:
        return prefix
    if "claude" in model_lower:
        return "anthropic"
    if "gpt" in model_lower or model_lower.startswith(("o1", "o3")):
        return "openai"
    if model_lower.startswith("gemini"):
        return "google"
    if model_lower.startswith("deepseek"):
        return "deepseek"
    if model_lower.startswith("groq"):
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
