"""The memory router sees the exchange before this turn, as planned."""
from types import SimpleNamespace

from agent.loop import _recent_exchange
from models.message import TextPart


def message(id, role, *texts, synthetic=False):
    return SimpleNamespace(id=id, role=role, parts=[TextPart(text=text, synthetic=synthetic) for text in texts])


def test_router_context_is_the_last_exchange_before_this_turn_without_synthetic_text():
    messages = [message("m1", "user", "我们周六去哪儿？"), message("m2", "assistant", "可以去西湖。"),
                message("m3", "user", "系统提示", synthetic=True), message("m4", "assistant", "x" * 900),
                message("m5", "user", "那就定那里吧"), message("m6", "assistant", "后来的回答")]
    assert _recent_exchange(messages, "m5") == [{"role": "assistant", "text": "可以去西湖。"},
                                                {"role": "assistant", "text": "x" * 400}]
    assert _recent_exchange(messages, "m1") == []
