"""Model reasoning catalogue and conversation-selection contract."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from api.metadata import _chat_models
from api.sessions import PromptBody, _remember_variant


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


@pytest.mark.asyncio
async def test_omitted_variant_inherits_the_conversation(monkeypatch):
    session = SimpleNamespace(id="session-1", variant="high")
    update = AsyncMock()
    monkeypatch.setattr("api.sessions.session_mod.update_session", update)
    body = PromptBody(text="continue")

    selected = await _remember_variant(
        session,
        "openai/claude-opus-5",
        body.variant,
        explicit="variant" in body.model_fields_set,
        user_id="user-1",
    )

    assert selected == "high"
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_explicit_null_clears_the_conversation_variant(monkeypatch):
    session = SimpleNamespace(id="session-1", variant="high")
    update = AsyncMock()
    monkeypatch.setattr("api.sessions.session_mod.update_session", update)
    body = PromptBody(text="use the default", variant=None)

    selected = await _remember_variant(
        session,
        "openai/claude-opus-5",
        body.variant,
        explicit="variant" in body.model_fields_set,
        user_id="user-1",
    )

    assert selected is None
    update.assert_awaited_once_with("session-1", variant=None, user_id="user-1")


@pytest.mark.asyncio
async def test_explicit_unsupported_variant_returns_422_before_persistence(monkeypatch):
    session = SimpleNamespace(id="session-1", variant=None)
    update = AsyncMock()
    monkeypatch.setattr("api.sessions.session_mod.update_session", update)
    body = PromptBody(text="think", variant="medium")

    with pytest.raises(HTTPException) as caught:
        await _remember_variant(
            session,
            "openai/deepseek-v4-flash",
            body.variant,
            explicit="variant" in body.model_fields_set,
            user_id="user-1",
        )

    assert caught.value.status_code == 422
    assert "supported variants: off, low, high, max" in caught.value.detail
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_switch_clears_an_inherited_foreign_variant(monkeypatch):
    session = SimpleNamespace(id="session-1", variant="medium")
    update = AsyncMock()
    monkeypatch.setattr("api.sessions.session_mod.update_session", update)

    selected = await _remember_variant(
        session,
        "openai/deepseek-v4-flash",
        None,
        explicit=False,
        user_id="user-1",
    )

    assert selected is None
    update.assert_awaited_once_with("session-1", variant=None, user_id="user-1")
