"""Native model calls are the only business dispatch path; transport never parses intent."""
import asyncio
import json

import pytest
from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event,
                                       item, started, tool_call, transcript)
from voice.bridge import Bridge
from voice.tools import CallScope


def make():
    provider, link = ScriptedProvider(), FakeLink()
    return Bridge(provider, link, clock=FakeClock(), scope=CallScope("u1", "w1", "main-1", "call-x"),
                  instructions="BASE"), provider, link


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def greeted(bridge, provider):
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    provider.sent.clear()


def said(text, response_id):
    return event("assistant_transcript", text=text, response_id=response_id)


async def utterance(bridge, words, reply, *, response_id="r1", user_item="u-1"):
    await replay(bridge, event("user_started"), item(user_item), transcript(words, user_item),
                 event("user_stopped"), started(response_id), said(reply, response_id), audio(response_id), done(response_id))


@pytest.mark.parametrize("words,reply", [
    ("解释一下闭嘴是什么意思", "这是让对方停止说话的表达。"),
    ("先别说了，我看一下", "好。"), ("嗯", "怎么了？"),
    ("帮我发出去", "我马上发。"), ("就按刚才那个方案办", "已经办好了。"),
    ("不用给我解释原理了，消息就按原样发出去", "知道了。"),
    ("换个更温柔的语气", "好，我放慢一点。"),
    ("我对花生过敏", "我记住了。"),
])
async def test_speech_never_creates_business_actions_or_persistent_mute(words, reply):
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await utterance(bridge, words, reply)
    assert not link.started and not provider.commands("cancel")
    assert not hasattr(bridge, "hushed")
    await utterance(bridge, "春雨", "这是名字吗？", response_id="r2", user_item="u-2")
    assert bridge.audio_sent and not link.started
    await bridge.close()


@pytest.mark.parametrize("words,brief", [
    ("春雨", "把刚才的提醒命名为春雨，保留时间和内容"),
    ("那就这样", "按已确认的50秒时长、中文配音、双语字幕制作视频"),
    ("换个温柔语气再帮我查明天天气", "查询用户所在地明天的天气"),
])
async def test_contextual_short_requests_are_not_blocked_or_rewritten(words, brief):
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript(words, "u1"), started("r1"),
                 tool_call("call-1", text=brief, response_id="r1"), done("r1"))
    [ref] = link.started
    assert (ref.text, ref.transcript) == (brief, words)
    receipt = provider.commands("output")[0][2]
    assert receipt["request_id"] == ref.id and receipt["operation_state"] == "queued"
    assert not receipt["execution_confirmed"] and "task_id" not in receipt
    await replay(bridge, tool_call("call-1", text=brief, response_id="r1"))
    assert len(link.started) == len(provider.commands("output")) == 1
    await bridge.close()


async def test_late_transcript_is_bound_to_its_own_response_not_a_later_request():
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await replay(bridge, item("old"), started("r1"), tool_call("call-1", text="执行原方案", response_id="r1"), done("r1"))
    assert not link.started
    await replay(bridge, transcript("原方案", "old"), item("new"), transcript("另一个请求", "new"))
    await asyncio.sleep(0.1)
    [ref] = link.started
    assert ref.transcript == "原方案" and "另一个请求" not in str(ref.context)
    await bridge.close()


async def test_background_delivery_cannot_dispatch_from_an_old_user_utterance():
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await utterance(bridge, "帮我看看进度", "我看一下。")
    bridge.requested = "delivery:any"
    await replay(bridge, started("bg"), tool_call("bad", text="删除所有项目", response_id="bg"), done("bg"))
    assert not link.started
    assert provider.commands("output")[-1][2]["status"] == "not_user_request"
    await bridge.close()


async def test_model_parameters_never_become_human_provenance(monkeypatch):
    monkeypatch.setattr("voice.turns.TRANSCRIPT_WAIT_SECONDS", 0.01)
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), started("r1"), tool_call("bad", text="确认删除", response_id="r1"), done("r1"))
    await asyncio.sleep(0.1)
    assert not link.started
    assert provider.commands("output")[-1][2]["status"] == "need_user_input"
    await bridge.close()


async def test_per_response_purpose_preserves_role_contract_and_current_context():
    bridge, provider, _ = make()
    bridge.base_instructions = "自然交流；工具回执才是事实。"
    bridge.keeper.summary = "正在讨论两个不同项目。"
    await bridge._create("说明刚收到的结果。")
    instructions = provider.commands("create")[-1][1]
    assert instructions.startswith(bridge.base_instructions)
    assert bridge.keeper.summary in instructions
    assert instructions.endswith("说明刚收到的结果。")
    await bridge.close()


async def test_new_speech_start_does_not_reuse_the_previous_transcript():
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await replay(bridge, item("old"), transcript("旧任务", "old"))
    await replay(bridge, event("user_started", item_id="new"), started("r-new"),
                 tool_call("new-call", text="新任务", response_id="r-new"), done("r-new"))
    assert not link.started
    await replay(bridge, transcript("新的业务要求", "new"))
    await asyncio.sleep(.1)
    assert [ref.transcript for ref in link.started] == ["新的业务要求"]
    await bridge.close()
