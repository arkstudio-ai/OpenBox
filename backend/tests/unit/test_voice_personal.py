"""The call follows the user's own settings (assistant/profile.py) and keeps what they say in passing.

A profile changed mid-call rebuilds the session prompt at once; the greeting leaves the past alone
when asked to; task results nobody asked about stay in the conversation when turned off; fuller
answers travel with each request; and a lasting fact or wish said in passing is quietly handed to
the assistant to remember, quoting the user.
"""
from tests.support.voice_fakes import FakeJudge, audio, done, drain, event, item, started
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
    assert instructions == phrases.greeting_instructions("zh", recap=False)
    assert "不希望开场提上次通话" in instructions and "上次通话聊的事" in phrases.greeting_instructions("zh")


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
    assert "关于用户说的“帮我查一下OpenAI的新模型”" in note


async def test_fuller_answers_travel_with_each_request():
    bridge, provider, link = make()
    bridge.detail = "detailed"
    await greeted(bridge, provider)
    assert bridge._call_context("帮我看看贪吃蛇进展")["detail"] == "detailed"
    bridge.detail = "brief"
    assert "detail" not in bridge._call_context("帮我看看贪吃蛇进展")


async def test_a_lasting_fact_said_in_passing_is_handed_over_quietly_to_be_remembered():
    words = "我对花生过敏，以后点外卖别给我推花生的"
    judge = FakeJudge(routes={words: ("chat", 0.93)}, lasting={words: ("lasting", 0.95)})
    bridge, provider, link = make(judge)
    await greeted(bridge, provider)
    await utterance(bridge, words, "好的，记住了，以后不给你推花生。")
    await drain(40)
    [ref] = link.started
    assert (ref.lane, ref.transcript, ref.text) == ("remember", words, phrases.remember_request("zh"))
    assert ref.provider_call_id.startswith("remember:") and judge.lasting_checks == [words]
    assert not provider.commands("create")  # quiet: nothing said about it now; the result is told when it comes


async def test_what_passes_is_not_remembered():
    words = "今天天气真不错啊，心情挺好"
    judge = FakeJudge(routes={words: ("chat", 0.95), "嗯嗯好的": ("chat", 0.99)},
                      lasting={words: ("lasting", 0.62)})
    bridge, provider, link = make(judge)
    await greeted(bridge, provider)
    await utterance(bridge, words, "是啊，适合出去走走。")
    await utterance(bridge, "嗯嗯好的", "好嘞。", response_id="r2", user_item="u-2")
    await drain(40)
    assert link.started == [] and judge.lasting_checks == [words]  # below the bar; too short to ask about
