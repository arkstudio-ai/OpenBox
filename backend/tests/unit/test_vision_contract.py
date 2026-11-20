from types import SimpleNamespace as NS

import pytest

from video.vision_contract import output, request_options


def test_minimax_visual_report_uses_supported_choice_and_bounded_reasoning():
    provider = {"api_base": "https://example.invalid/v1", "extra_body": {"foo": "bar"}}
    result = request_options("openai/MiniMax/MiniMax-M3.1-Flash", "review", provider)
    assert result["tool_choice"] == "auto"
    assert result["extra_body"] == {"foo": "bar", "reasoning_effort": "medium"}
    assert provider["extra_body"] == {"foo": "bar"}
    ordinary = request_options("openai/other", "review", provider)
    assert ordinary["tool_choice"]["function"]["name"] == "review"
    assert ordinary["extra_body"] == {"foo": "bar"}


def test_structured_report_accepts_function_or_content_but_never_reasoning_or_truncation():
    message = NS(tool_calls=[NS(function=NS(name="review", arguments='{"ok":false}'))],
                 content="", reasoning_content='{"ok":true}')
    response = NS(choices=[NS(message=message, finish_reason="tool_calls")])
    assert output(response, "review") == '{"ok":false}'
    message.tool_calls = []
    assert output(response, "review") == ""
    message.content = '{"ok":false}'
    assert output(response, "review") == '{"ok":false}'
    response.choices[0].finish_reason = "length"
    with pytest.raises(ValueError, match="truncated"):
        output(response, "review")
