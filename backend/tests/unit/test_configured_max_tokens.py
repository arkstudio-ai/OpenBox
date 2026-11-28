"""The catalogue's ``models[].max_tokens`` caps one response.

It used to be read only by the /config route; requests always used the
built-in per-family allowance. It now lowers that allowance, and never raises
it: provider maximums above the built-in values are unverified and an entry
that omits the field carries ModelConfig's 200,000 default.
"""
import pytest

from agent.llm import OUTPUT_TOKEN_MAX, _get_max_output_tokens, request_output_tokens
from core.config import ModelConfig, OpenBoxConfig


@pytest.fixture
def catalogue(monkeypatch):
    def install(*models):
        config = OpenBoxConfig(model=models[0].id if models else "openai/x", models=list(models))
        monkeypatch.setattr("core.config.get_config", lambda: config)
        return config
    return install


def test_configured_value_below_builtin_takes_effect(catalogue):
    catalogue(ModelConfig(id="openai/qwen3.8-flash", max_tokens=4096))
    assert _get_max_output_tokens("openai/qwen3.8-flash") == 4096
    assert request_output_tokens("openai/qwen3.8-flash") == 4096


def test_example_config_values_do_not_change_the_allowance(catalogue):
    # The committed openbox.jsonc.example sets 64000 and 131072.
    catalogue(
        ModelConfig(id="openai/deepseek-v4-pro", max_tokens=64000),
        ModelConfig(id="openai/qwen3.8-max", max_tokens=131072),
    )
    assert _get_max_output_tokens("openai/deepseek-v4-pro") == OUTPUT_TOKEN_MAX
    assert _get_max_output_tokens("openai/qwen3.8-max") == OUTPUT_TOKEN_MAX


def test_omitted_field_default_is_not_sent(catalogue):
    catalogue(ModelConfig(id="openai/gpt-5.4"))
    assert ModelConfig(id="openai/gpt-5.4").max_tokens == 200000
    assert _get_max_output_tokens("openai/gpt-5.4") == 128_000


def test_undeclared_model_uses_builtin(catalogue):
    catalogue(ModelConfig(id="openai/qwen3.8-flash", max_tokens=1000))
    assert _get_max_output_tokens("openai/other") == OUTPUT_TOKEN_MAX


def test_config_route_reports_the_cap_actually_sent(catalogue):
    from agent.compaction import get_model_context_limit
    from agent.vision import supports_vision
    from api.metadata import _chat_models

    config = catalogue(
        ModelConfig(id="openai/qwen3.8-flash", max_tokens=4096),
        ModelConfig(id="openai/deepseek-v4-pro", max_tokens=64000),
    )
    rows = _chat_models(config, context_limit=get_model_context_limit, supports_vision=supports_vision)
    assert [row["max_tokens"] for row in rows] == [4096, OUTPUT_TOKEN_MAX]
