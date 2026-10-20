"""The call follows the user's own settings (assistant/profile.py) and keeps what they say in passing.

A profile changed mid-call rebuilds the session prompt at once; the greeting leaves the past alone
when asked to; task results nobody asked about stay in the conversation when turned off; fuller
answers travel with each request; and a lasting fact or wish said in passing is quietly handed to
the assistant to remember, quoting the user.
"""
from tests.support.voice_fakes import tool_call, transcript, audio, done, drain, event, item, started
from tests.unit.test_voice_routing import greeted, make, replay, said, utterance
from voice import phrases
from voice.prompt import with_sections


async def test_a_profile_changed_mid_call_rebuilds_the_prompt_at_once():
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await bridge.rebase("BASE 用户给你取的名字是「小七」", tell_reports=False, detail="detailed")
    [(_, sent)] = provider.commands("instructions")[-1:]
    assert sent == with_sections("BASE 用户给你取的名字是「小七」")
    assert bridge.base_instructions.endswith("「小七」") and not bridge.tell_reports and bridge.detail == "detailed"
    bridge.closing = True
    await bridge.rebase("BASE later")  # a call that is ending keeps what it had
    assert not bridge.base_instructions.endswith("later")


async def test_the_greeting_leaves_the_past_alone_when_the_user_asked():
    bridge, provider, _ = make()
    bridge.recap = False
    await bridge.start()
    [(_, instructions)] = [command for command in provider.sent if command[0] == "create"][:1]
    assert instructions == bridge._instructions() + "\n\n" + phrases.greeting_instructions("zh", recap=False)
    assert "不主动提任务" in instructions and "不主动提任务" in phrases.greeting_instructions("zh")


def test_opening_never_replays_old_decisions_but_regular_context_retains_history():
    from voice.prompt import FrontFacts, front_instructions, local_now
    facts = FrontFacts(recap=True, last_call="旧卡还等确认", recent="素材准备好了")
    opening = front_instructions(facts, "zh", local_now(), opening=True)
    normal = front_instructions(facts, "zh", local_now())
    assert "旧卡还等确认" not in opening and "素材准备好了" not in opening
    assert "旧卡还等确认" in normal and "可能已经过时" in normal


async def test_results_nobody_asked_about_stay_in_the_conversation_when_turned_off():
    bridge, provider, link = make()
    await greeted(bridge, provider)
    bridge.tell_reports = False
    await bridge.report("制作华为宣传口播视频", "做好了。", "inbox-9")
    await drain()
    assert not provider.commands("note") and not bridge.deliveries
    bridge._passed_on["task-7"] = "帮我查一下OpenAI的新模型"  # a request of this call is still answered
    await bridge.report("查询OpenAI最新模型", "查好了。", "inbox-10", "task-7")
    await drain()
    [(_, note)] = provider.commands("note")
    assert "帮我查一下OpenAI的新模型" in note


async def test_fuller_answers_travel_with_each_request():
    bridge, provider, link = make()
    bridge.detail = "detailed"
    await greeted(bridge, provider)
    assert bridge._call_context("帮我看看贪吃蛇进展")["detail"] == "detailed"
    bridge.detail = "brief"
    assert "detail" not in bridge._call_context("帮我看看贪吃蛇进展")


async def test_a_native_remember_call_keeps_the_fact_quietly():
    import json
    words = "我对花生过敏，以后点外卖别给我推花生的"
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await replay(bridge, transcript(words, "u1"), started("r1"),
                 tool_call("mem-1", arguments=json.dumps({"request": "记住用户对花生过敏", "mode": "remember"}),
                           response_id="r1"), audio("r1"), done("r1"))
    [ref] = link.started
    assert ref.lane == "remember" and ref.transcript == words
    link.finish(ref.provider_call_id, speech="记下了")
    await drain(40)
    assert not provider.commands("note")
    assert (ref.id, {"outcome": "delivered", "delivered": False}) in link.records
    await bridge.close()
