"""Per-model parameters must suit the model actually being called.

A variant (reasoning effort / thinking budget) rides on the message, not the
model, so it outlives a mid-conversation model switch and a provider fallback.
Forwarded verbatim it reaches families that reject it — and the rejection
arrives as a provider 400 that names neither the variant nor the switch.
"""
import pytest

import agent.llm as llm
from agent.llm import (
    THINKING_OUTPUT_RESERVE,
    _get_max_output_tokens,
    _get_variant_kwargs,
    reasoning_profile,
    validate_reasoning_variant,
)


@pytest.mark.parametrize("model", ["openai/gpt-5.4", "openai/gpt-5.2"])
def test_max_becomes_xhigh_only_where_xhigh_exists(model):
    assert _get_variant_kwargs(model, "max") == {"reasoning_effort": "xhigh"}


def test_max_is_clamped_for_plain_gpt5_which_rejects_it():
    """Plain GPT-5 knows neither 'max' nor 'xhigh'; sending either is a 400."""
    assert _get_variant_kwargs("openai/gpt-5", "max") == {"reasoning_effort": "high"}


def test_an_unrecognised_variant_is_dropped_rather_than_forwarded():
    assert _get_variant_kwargs("openai/gpt-5", "wildly-invalid") == {}


def test_claude4_max_budget_would_collide_with_the_output_cap():
    """Anthropic draws thinking out of the output budget and requires
    max_tokens > budget_tokens. The generic 32k cap equals the 'max' budget
    exactly, so the request needs headroom added."""
    kwargs = _get_variant_kwargs("anthropic/claude-sonnet-4-20250514", "max")
    budget = kwargs["thinking"]["budget_tokens"]
    assert budget == _get_max_output_tokens("anthropic/claude-sonnet-4-20250514"), (
        "this collision is the bug being guarded"
    )
    assert budget + THINKING_OUTPUT_RESERVE > budget


# ── Exact-route reasoning capability ──────────────────────────
# Capability ids belong to the exact upstream model, even when several
# families share the same OpenAI-compatible transport.

def test_family_is_derived_after_the_openai_gateway_prefix():
    profile = reasoning_profile("openai/deepseek-v4-flash")
    assert profile.variants == ("off", "low", "high", "max")
    assert profile.default_variant == "high"


@pytest.mark.parametrize("variant", ["low", "high", "max"])
def test_deepseek_preserves_its_own_effort_ids(variant):
    assert _get_variant_kwargs("openai/deepseek-v4-flash", variant) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": variant,
    }


def test_deepseek_off_disables_thinking_without_a_wire_effort():
    assert _get_variant_kwargs("deepseek/deepseek-v4-flash", "off") == {
        "thinking": {"type": "disabled"},
    }


@pytest.mark.parametrize("variant", ["medium", "minimal", "none", "turbo"])
def test_deepseek_drops_an_invalid_historical_variant(variant):
    assert _get_variant_kwargs("deepseek/deepseek-v4-flash", variant) == {}


def test_new_invalid_variant_is_rejected_before_provider_io():
    with pytest.raises(ValueError, match="supported variants: off, low, high, max"):
        validate_reasoning_variant("openai/deepseek-v4-flash", "medium")


def test_gateway_claude_exposes_only_harmonized_efforts():
    assert reasoning_profile("openai/claude-opus-5").variants == (
        "low", "medium", "high",
    )
    assert _get_variant_kwargs("openai/claude-opus-5", "high") == {
        "reasoning_effort": "high",
    }


@pytest.mark.parametrize("model", ["qwen3.8-max", "qwen3.8-flash"])
def test_qwen_38_exposes_distinct_dashscope_efforts(model):
    profile = reasoning_profile(f"openai/{model}")
    assert profile.variants == ("none", "low", "medium", "xhigh")
    assert profile.default_variant == "xhigh"
    for effort in profile.variants:
        assert _get_variant_kwargs(f"openai/{model}", effort) == {
            "reasoning_effort": effort,
        }


def test_older_qwen_route_does_not_claim_tiered_effort_support():
    assert reasoning_profile("openai/qwen3.7-plus").variants == ()


def test_native_adaptive_claude_sends_the_selected_output_effort():
    assert _get_variant_kwargs("anthropic/claude-opus-4-6", "low") == {
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "low"},
    }


def test_recent_codex_alias_exposes_distinct_wire_efforts_with_declared_default():
    profile = reasoning_profile("openai/gpt-5.6-luna")
    assert profile.variants == ("low", "medium", "high", "xhigh")
    assert profile.default_variant == "medium"
    assert reasoning_profile("openai/gpt-5.6-sol").default_variant == "low"
    # Historical `max` messages still replay as xhigh, but the catalogue does
    # not present two labels for the same wire value.
    assert _get_variant_kwargs("openai/gpt-5.6-luna", "max") == {
        "reasoning_effort": "xhigh",
    }


def test_non_reasoning_model_has_no_variants_or_default_parameters():
    assert reasoning_profile("openai/kimi-k2.5") == reasoning_profile("unknown/model")
    assert _get_variant_kwargs("openai/kimi-k2.5", None) == {}


@pytest.mark.parametrize(("model", "variant", "wire"), [
    (
        "deepseek/deepseek-v4-flash",
        "max",
        {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
    ),
    (
        "openai/qwen3.8-flash",
        "medium",
        {"reasoning_effort": "medium"},
    ),
])
@pytest.mark.asyncio
async def test_provider_owned_effort_reaches_extra_body(monkeypatch, model, variant, wire):
    """Keep non-standard effort fields intact through LiteLLM dispatch."""

    import litellm

    captured = {}

    class EmptyStream:
        _hidden_params = {}
        usage = None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    async def fake_completion(**kwargs):
        captured.update(kwargs)
        return EmptyStream()

    for setting in ("modify_params", "drop_params", "reasoning_auto_summary"):
        monkeypatch.setattr(litellm, setting, getattr(litellm, setting))
    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    monkeypatch.setattr(llm, "_get_provider_kwargs", lambda _model: {})

    _ = [
        event
        async for event in llm._stream_litellm_direct(
            model, [], [], {}, variant=variant,
        )
    ]

    assert captured["extra_body"] == wire
    assert "reasoning_effort" not in captured
    assert "thinking" not in captured
