"""Model reasoning catalogue and conversation-selection contract."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException

from api.metadata import _chat_models
from api.sessions import PromptBody, _resolve_prompt_variant


def test_config_models_advertise_exact_route_variants():
    config = SimpleNamespace(
        model="openai/deepseek-v4-flash",
        models=[
            SimpleNamespace(
                id="openai/deepseek-v4-flash",
                name="DeepSeek V4 Flash",
                provider=None,
                max_tokens=64_000,
            ),
            SimpleNamespace(
                id="openai/kimi-k2.5",
                name="Kimi K2.5",
                provider=None,
                max_tokens=64_000,
            ),
            SimpleNamespace(
                id="openai/qwen3.8-flash",
                name="Qwen3.8 Flash",
                provider=None,
                max_tokens=128_000,
            ),
        ],
    )

    rows = _chat_models(
        config,
        context_limit=lambda _model: 1_000_000,
        supports_vision=lambda model: "kimi" in model,
    )

    assert rows[0]["variants"] == ["off", "low", "high", "max"]
    assert rows[0]["default_variant"] == "high"
    assert rows[1]["variants"] == []
    assert rows[1]["default_variant"] is None
    assert rows[2]["variants"] == ["none", "low", "medium", "xhigh"]
    assert rows[2]["default_variant"] == "xhigh"


def test_omitted_variant_inherits_the_conversation():
    session = SimpleNamespace(id="session-1", variant="high")
    body = PromptBody(text="continue")

    selected = _resolve_prompt_variant(session, body, "openai/claude-opus-5")

    assert selected == "high"


def test_explicit_null_clears_the_queued_conversation_variant():
    session = SimpleNamespace(id="session-1", variant="high")
    body = PromptBody(text="use the default", variant=None)

    selected = _resolve_prompt_variant(session, body, "openai/claude-opus-5")

    assert selected is None


def test_explicit_unsupported_variant_returns_422_before_acceptance():
    session = SimpleNamespace(id="session-1", variant=None)
    body = PromptBody(text="think", variant="medium")

    with pytest.raises(HTTPException) as caught:
        _resolve_prompt_variant(session, body, "openai/deepseek-v4-flash")

    assert caught.value.status_code == 422
    assert "supported variants: off, low, high, max" in caught.value.detail


def test_model_switch_clears_an_inherited_foreign_variant_for_queued_turn():
    session = SimpleNamespace(id="session-1", variant="medium")
    body = PromptBody(text="switch")

    selected = _resolve_prompt_variant(session, body, "openai/deepseek-v4-flash")

    assert selected is None
