from copy import deepcopy

from agent.caching import apply_caching, session_cache_key


def test_openai_cache_affinity_is_not_embedded_in_messages():
    messages = [{"role": "system", "content": "stable"}]
    assert apply_caching(deepcopy(messages), "openai/gpt-5") == messages

    cached = apply_caching(
        deepcopy(messages),
        "openai/gpt-5",
        cache_key="a" * 64,
    )
    assert cached == messages


def test_anthropic_and_bedrock_use_normalized_cache_control_without_mutating_input():
    messages = [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "hello"},
    ]

    for model in ("anthropic/claude-sonnet", "bedrock/anthropic.claude"):
        cached = apply_caching(messages, model)
        assert cached[0]["cache_control"] == {"type": "ephemeral"}
        assert cached[1]["cache_control"] == {"type": "ephemeral"}
        assert "provider_options" not in cached[0]
    assert messages == [
        {"role": "system", "content": "stable"},
        {"role": "user", "content": "hello"},
    ]


def test_openai_compatible_claude_uses_openai_transport_not_anthropic_markers():
    messages = [{"role": "system", "content": "stable"}]

    assert apply_caching(messages, "openai/claude-opus-5") == messages


def test_normalized_markers_reach_anthropic_and_bedrock_provider_wires():
    from litellm.llms.anthropic.chat.transformation import AnthropicConfig
    from litellm.llms.bedrock.chat.converse_transformation import (
        AmazonConverseConfig,
    )

    normalized = apply_caching(
        [
            {"role": "system", "content": "stable system"},
            {"role": "user", "content": "stable user"},
        ],
        "anthropic/claude-sonnet",
    )
    anthropic = AnthropicConfig().transform_request(
        "claude-sonnet-4-20250514",
        deepcopy(normalized),
        {"max_tokens": 100},
        {},
        {},
    )
    bedrock = AmazonConverseConfig().transform_request(
        "anthropic.claude-3-5-sonnet-20240620-v1:0",
        deepcopy(normalized),
        {"max_tokens": 100},
        {},
        {},
    )

    assert anthropic["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert anthropic["messages"][0]["content"][0]["cache_control"] == {
        "type": "ephemeral"
    }
    assert bedrock["system"][1] == {"cachePoint": {"type": "default"}}
    assert bedrock["messages"][0]["content"][1] == {
        "cachePoint": {"type": "default"}
    }


def test_cache_key_is_salted_and_does_not_contain_raw_identity():
    key = session_cache_key(secret="deployment-secret", user_id="user-private", session_id="session-private")
    assert len(key) == 64
    assert "user-private" not in key
    assert "session-private" not in key
    assert key != session_cache_key(
        secret="deployment-secret", user_id="another-user", session_id="session-private"
    )
