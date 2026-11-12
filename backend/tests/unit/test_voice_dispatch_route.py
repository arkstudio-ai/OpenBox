"""voice.dispatch_mode=server: the user's words are routed as they are heard, and work is handed over by the
server whether or not the spoken reply remembers to (docs/VOICE_DISPATCH_PLAN_20261112.md §4.1)."""
import json

import pytest

from tests.support.voice_fakes import audio, done, event, item, started, tool_call, transcript
from tests.unit.test_voice_routing import greeted, make, replay, said
from voice import handoff
from voice.handoff import HandoffGuard


def routed(answers):
    """A call on server dispatch whose router gives ``answers`` (words → what ``route`` returns), counting calls."""
    bridge, provider, link = make()
    asked = []

    async def checker(words, context, *request_words, **shown):
        asked.append(words)
        return answers.get(words, False)

    bridge.handoff = HandoffGuard(bridge, checker, early=True)
    return bridge, provider, link, asked


async def test_work_is_handed_over_when_heard_before_any_reply_has_ended():
    """2026-11-02: three hours of "新对话已经建好" without one assistant_ask."""
    bridge, provider, link, asked = routed({"你直接新建一个对话接着处理": True})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("你直接新建一个对话接着处理", "u1"),
                 event("user_stopped"))
    assert [ref.transcript for ref in link.started] == ["你直接新建一个对话接着处理"]
    assert link.started[0].context["handover_source"] == "server_route"
    assert asked == ["你直接新建一个对话接着处理"]
    await bridge.close()


async def test_a_reply_already_speaking_is_cut_once_the_words_are_handed_over():
    bridge, provider, link = make()
    release = []

    async def checker(words, context, *request_words, **shown):
        import asyncio
        gate = asyncio.Event()
        release.append(gate)
        await gate.wait()
        return True

    bridge.handoff = HandoffGuard(bridge, checker, early=True)
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("帮我做一条新品视频", "u1"),
                 event("user_stopped"), started("r1"), audio("r1", size=48000))
    assert not link.started and not provider.commands("cancel")  # the router has not answered yet
    release[0].set()
    await replay(bridge)
    assert len(link.started) == 1 and provider.commands("cancel")  # "好的，已经交给帮手了" never finishes
    await bridge.close()


async def test_talk_and_quick_reads_stay_on_the_phone_and_are_routed_only_once():
    bridge, provider, link, asked = routed({})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("口播是什么意思", "u1"), event("user_stopped"),
                 started("r1"), said("就是人对着镜头讲。", "r1"), audio("r1"), done("r1"))
    assert not link.started and asked == ["口播是什么意思"]  # the reply's end does not ask again
    assert not provider.commands("cancel")
    await bridge.close()


async def test_the_models_own_handover_of_the_same_words_is_the_same_request():
    bridge, provider, link, _ = routed({"把字幕改成黄色": True})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("把字幕改成黄色", "u1"), event("user_stopped"))
    await replay(bridge, started("r1"), tool_call("ask-1", text="把字幕改成黄色", response_id="r1"), done("r1"))
    assert len(link.started) == 1
    assert provider.commands("output")[-1][2]["request_id"] == link.started[0].id
    await bridge.close()


async def test_a_handover_the_model_made_first_is_not_routed_into_a_second_one():
    bridge, provider, link = make()
    answers = []

    async def late(words, context, *request_words, **shown):
        answers.append(words)
        return True

    bridge.handoff = HandoffGuard(bridge, late, early=False)  # the reply's tool call comes before the words here
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), event("user_stopped"), started("r1"),
                 transcript("帮我做一条新品视频", "u1"), tool_call("ask-1", text="帮我做一条新品视频", response_id="r1"))
    bridge.handoff.early = True
    bridge.handoff.heard("u1")
    await replay(bridge, done("r1"))
    assert len(link.started) == 1 and not answers
    await bridge.close()


async def test_the_call_knows_from_its_first_line_what_it_has_handed_over():
    """2026-11-07: "你已经在做了吗" was answered "是的" with nothing submitted."""
    bridge, provider, _, _ = routed({})
    assert "没有任何任务交办成功" in bridge._handoff_facts()
    plain, _, _ = make()
    assert plain._handoff_facts() == ""  # the model-dispatch call keeps its greeting and first ask as they are
    await bridge.close()
    await plain.close()


@pytest.mark.parametrize("answer, expected", [
    ('{"route":"dispatch"}', True), ('```json\n{"route":"dispatch"}\n```', True),
    ('{"route":"chat"}', False), ('{"route":"lookup"}', False),
    ('{"route":"card"}', False),  # no card was shown: nothing to choose
])
async def test_route_answers_like_the_long_check(monkeypatch, answer, expected):
    seen = {}

    async def complete(model, system, text, timeout):
        seen.update(model=model, system=system, payload=json.loads(text), timeout=timeout)
        return answer

    monkeypatch.setattr("voice.summary.complete", complete)
    assert await handoff.route("帮我做一条新品视频", "assistant: 你好", ("先想想",)) is expected
    assert seen["model"] == "openai/qwen3.8-flash" and seen["timeout"] == handoff.ROUTE_TIMEOUT_SECONDS
    assert seen["payload"] == {"context": "assistant: 你好", "latest_user": "帮我做一条新品视频",
                               "same_request_user_words": ["先想想"]}


@pytest.mark.parametrize("answer", ['{"route":"submit"}', '["dispatch"]', "dispatch"])
async def test_an_answer_that_is_no_route_is_an_error_never_a_guess(monkeypatch, answer):
    async def complete(*_):
        return answer

    monkeypatch.setattr("voice.summary.complete", complete)
    with pytest.raises(ValueError):
        await handoff.route("帮我做一条新品视频", "")


async def test_a_card_choice_is_bound_by_the_long_check_to_the_users_words_and_the_shown_options(monkeypatch):
    cards = ({"card": "card-1", "version": "v1", "title": "方案确认", "what": "十五秒",
              "options": [{"id": "confirm", "label": "确认并开始制作"}, {"id": "cancel", "label": "取消"}]},)
    replies = iter(['{"route":"card"}', json.dumps({"decision": "card", "card": "card-1", "option_id": "confirm",
                                                    "authorization_source": "latest_user"})])
    models = []

    async def complete(model, system, text, timeout):
        models.append(model)
        return next(replies)

    monkeypatch.setattr("voice.summary.complete", complete)
    assert await handoff.route("确认，开始做吧", "", (), cards) == {"card": "card-1", "version": "v1",
                                                                    "option_id": "confirm"}
    assert models == ["openai/qwen3.8-flash"] * 2  # both questions on the fast model
