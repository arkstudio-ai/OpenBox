from copy import deepcopy

from agent.caching import apply_caching, session_cache_key


def test_openai_cache_hint_requires_a_session_scoped_digest():
    messages = [{"role": "system", "content": "stable"}]
    assert apply_caching(deepcopy(messages), "openai/gpt-5") == messages

    cached = apply_caching(
        deepcopy(messages),
        "openai/gpt-5",
        cache_key="a" * 64,
    )
    assert cached[0]["provider_options"]["setCacheKey"] == "a" * 64
    assert cached[0]["provider_options"]["setCacheKey"] != "default"


def test_different_sessions_do_not_share_a_cache_key():
    messages = [{"role": "system", "content": "stable"}]
    first = apply_caching(deepcopy(messages), "gpt-5", cache_key="1" * 64)
    second = apply_caching(deepcopy(messages), "gpt-5", cache_key="2" * 64)
    assert first[0]["provider_options"] != second[0]["provider_options"]


def test_cache_key_is_salted_and_does_not_contain_raw_identity():
    key = session_cache_key(secret="deployment-secret", user_id="user-private", session_id="session-private")
    assert len(key) == 64
    assert "user-private" not in key
    assert "session-private" not in key
    assert key != session_cache_key(
        secret="deployment-secret", user_id="another-user", session_id="session-private"
    )
