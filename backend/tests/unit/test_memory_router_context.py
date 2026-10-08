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


async def test_small_talk_skips_routing_and_routing_rules_decide_what_can_start_early():
    from memory import routing
    from core.config import MemoryConfig

    config = MemoryConfig(route_jev=True)
    scope = SimpleNamespace(actor_user_id="u", project_id=None)

    async def must_not_be_called(state, settings):
        raise AssertionError("a greeting needs no routing call")

    routed = await routing.route_context_needs("谢谢！", scope, config, evaluator=must_not_be_called)
    assert routed["rule"] == "small_talk" and not routed["memory"]["needed"] and not routed["called"]
    assert not routing.may_retrieve("好的，谢谢！", scope, config)
    # An approval may start a task that memory still shapes.
    assert routing.may_retrieve("好的", scope, config)
    assert not routing.may_retrieve("只根据本轮材料回答", scope, config)
    assert not routing.may_retrieve("不要查历史记忆，直接写首诗", scope, config)
    assert routing.may_retrieve("我之前说过不要吃辣", scope, config)
    assert routing.may_retrieve("周末去哪儿玩比较好", scope, config)
    assert not routing.may_retrieve("周末去哪儿玩比较好", scope, MemoryConfig(route_jev=False))
