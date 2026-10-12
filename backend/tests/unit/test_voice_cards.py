"""Cards read out and answered inside a call (docs/ASSISTANT_VOICE_FIX_PLAN.md §1.3).

A turn that stops at a confirmation card comes back with the card; the front
desk reads it and asks; the user's clear yes or no is answered with
cards_answer, which the server only accepts for a card this call read out,
after the user spoke, with a yes that is a yes. The resumed turn's result is
told like any other.
"""
import asyncio
import json

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       started, tool_call, transcript)
from voice import cards, phrases, tools
from voice.bridge import Bridge
from voice.progress import Progress
from voice.tools import CallScope

SCOPE = CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x")
CARD = {"card_id": "q-del", "kind": "assistant_confirm", "action": "project_delete", "header": "删除项目",
        "prompt": "删除项目「测试A」。\n影响：其中 2 个会话一起删除，目录之后移到回收站。",
        "impact": "其中 2 个会话一起删除，目录之后移到回收站。", "options": ["确认", "取消"],
        "created_at": "2026-10-07T12:00:00+00:00", "expires_at": "2026-10-07T12:10:00+00:00", "high_risk": True}






class Heard:
    def __init__(self):
        self.count, self.words = 0, ""

    def __call__(self):
        return self.count, self.words

    def say(self, words):
        self.count, self.words = self.count + 1, words


def answering(monkeypatch, waiting=(CARD,), reply=None):
    """The card store and question.reply, scripted."""
    replies = []

    async def pending_cards(user_id, workspace_id, main_id):
        assert (user_id, workspace_id, main_id) == ("u1", "w1", "main-1")
        return [dict(card) for card in waiting]

    async def fake_reply(request_id, answers, user_id="default", **binding):
        if reply is not None:
            raise reply
        replies.append((request_id, answers, user_id, binding))
        return {"ok": True}

    async def latest_inbox_id(session_id, user_id):
        return "inbox_0001"

    monkeypatch.setattr("assistant.confirmations.pending_cards", pending_cards)
    monkeypatch.setattr("question.question.reply", fake_reply)
    monkeypatch.setattr("voice.assistant_link.latest_inbox_id", latest_inbox_id)
    monkeypatch.setattr(cards, "WORDS_WAIT_SECONDS", 0.05)
    return replies


def scoped(heard):
    desk = cards.CardDesk(heard)
    return desk, CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x", desk=desk)


async def test_cards_pending_reads_main_cards_with_handles_and_task_questions(monkeypatch):
    answering(monkeypatch)

    async def list_waiting(**identity):
        return [{"id": "q-video", "session_id": "video", "session_title": "视频生成", "project_name": "短视频", "assistant_may_answer": True,
                 "questions": [{"question": "需要上传参考图吗？", "options": ["不需要", "上传"]}]},
                {"id": "q-publish", "session_id": "publish", "session_title": "发布", "project_name": "抖音", "assistant_may_answer": False,
                 "questions": [{"question": "允许访问你的账号吗？", "options": ["允许", "拒绝"]}]}]
    monkeypatch.setattr("assistant.request_answers.list_waiting", list_waiting)
    desk, scope = scoped(Heard())
    value = await cards.pending(scope, {})
    assert value["status"] == "ok"
    shown = value["cards"][0]
    assert shown["card"] == "1" and shown["high_risk"]
    assert shown["version"] and shown["what"] == "删除项目「测试A」。"
    assert shown["options"] == [{"id": "option_1", "label": "确认"}, {"id": "option_2", "label": "取消"}]
    assert [item["answer_how"] for item in value["task_questions"]] == [
        "用户明确回答全部问题后，用 task_question_answer 提交对应答案和选中资源；含糊或尚有疑问时先解释，不替用户补选", "要用户自己在屏幕上处理"]
    assert desk.card_id("1") == "q-del" and desk.open() == ["q-del"]
    assert (await cards.pending(scope, {}))["cards"][0]["card"] == "1"  # the same card keeps its handle


async def test_nothing_waiting(monkeypatch):
    answering(monkeypatch, waiting=())

    async def list_waiting(**identity):
        return []
    monkeypatch.setattr("assistant.request_answers.list_waiting", list_waiting)
    assert await cards.pending(scoped(Heard())[1], {}) == {"status": "none"}










# -- inside a call --

@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", late_after=12, clock=clock, scope=SCOPE,
                    progress=Progress(user_id="u1", main_session_id="main-1", clock=clock), instructions="BASE")
    return bridge, provider, link, clock


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def greeted(bridge, provider):
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    provider.sent.clear()


async def answered(bridge):
    """A cards_answer call waits (briefly, in real time) for the user's words."""
    await asyncio.sleep(0.2)
    await drain(30)


async def said(bridge, words, item_id):
    await replay(bridge, event("user_started"), event("user_stopped"), transcript(words, item_id))




async def test_a_turn_stopped_at_a_card_is_read_out_then_answered_by_voice(call, monkeypatch):
    bridge, provider, link, _ = call
    replies = answering(monkeypatch)
    await greeted(bridge, provider)
    await said(bridge, "把测试项目A删了", "u-1")
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-1", text="把测试项目A删了", response_id="ack"),
                 done("ack"))
    link.finish("call-1", speech="", cards=[CARD])  # the run stopped at the card
    await drain()
    [(_, note)] = provider.commands("note")
    assert "删除项目" in note and "option_1" in note and "version" in note and "办好了" not in note
    assert provider.response_purposes()[-1] == ("create", phrases.card_instructions("zh"))
    await replay(bridge, item("note-1", text=note), started("read"), audio("read"), done("read"))
    await said(bridge, "就按你刚才那个方案办", "u-2")
    arguments = json.dumps({"card": "1", "version": cards.version(CARD), "option_id": "option_1"}, ensure_ascii=False)
    await replay(bridge, started("r3"), tool_call("call-3", name="cards_answer", arguments=arguments,
                                                  response_id="r3"), done("r3"))
    await answered(bridge)
    assert replies == [("q-del", [["确认"]], "u1", {})]
    assert provider.commands("output")[-1][2]["status"] == "decision_recorded"
    # The resumed turn is followed like any other; its result is told once.
    [(ref, after)] = link.followed
    assert (after, ref.text, ref.transcript) == ("inbox_0001", "删除项目「测试A」。", "就按你刚才那个方案办")
    assert ref.provider_call_id in bridge.pending_calls
    await replay(bridge, started("r4"), audio("r4"), done("r4"))  # it answers the tool output: "好，删了"
    link.finish(ref.provider_call_id, speech="测试项目A已经删除了，两个会话也一起删了。")
    await drain()
    assert "测试项目A已经删除了" in provider.commands("note")[-1][1]


async def test_a_yes_spoken_before_the_card_was_read_is_no_consent(call, monkeypatch):
    bridge, provider, link, _ = call
    replies = answering(monkeypatch)
    await greeted(bridge, provider)
    await said(bridge, "确认删除测试项目A", "u-1")
    await replay(bridge, started("ack"), tool_call("call-1", text="确认删除测试项目A", response_id="ack"), done("ack"))
    link.finish("call-1", speech="", cards=[CARD])
    await drain()
    arguments = json.dumps({"card": "1", "version": cards.version(CARD), "option_id": "option_1"}, ensure_ascii=False)
    await replay(bridge, started("r2"), tool_call("call-2", name="cards_answer", arguments=arguments,
                                                  response_id="r2"), done("r2"))
    await answered(bridge)
    assert provider.commands("output")[-1][2]["status"] in ("need_user_answer", "not_user_request", "need_user_input")
    assert replies == [] and link.followed == []


def test_cards_tools_are_registered_for_the_front_desk():
    names = [schema["function"]["name"] for schema in tools.schemas()]
    assert names[0] == "assistant_ask" and {"cards_pending", "cards_answer"} <= set(names)
    answer = tools.DIRECT["cards_answer"]
    assert answer.parameters["required"] == ["card", "version", "option_id"] and answer.timeout > cards.WORDS_WAIT_SECONDS




async def read_out(bridge, provider, link):
    """A delete request whose turn stopped at the card, read out to the user."""
    await greeted(bridge, provider)
    await said(bridge, "把测试项目A删了", "u-1")
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-1", text="把测试项目A删了", response_id="ack"),
                 done("ack"))
    link.finish("call-1", speech="", cards=[CARD])
    await drain()
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("note-1", text=note), started("read"), audio("read"), done("read"))






FORM = {"id": "q-video", "session_id": "video-session", "session_title": "50秒搞笑视频", "project_name": "电影",
        "assistant_may_answer": True, "high_risk": False,
        "questions": [{"header": "时长", "question": "视频时长是否合适？", "options": ["保留50秒", "缩短到30秒"],
                       "custom": False},
                      {"header": "字幕", "question": "需要哪些字幕？", "options": ["中文", "英文"], "multiple": True},
                      {"header": "片名", "question": "请填写片名", "options": [], "custom": True}]}


def test_a_desktop_takeover_says_where_the_user_finishes_it():
    """The user said three times they could not see the card: the call says where to act and what to press."""
    takeover = {"id": "q-login", "session_id": "orders", "session_title": "抓取订单", "project_name": "店铺",
                "card_kind": "desktop_takeover", "host": "login.example.com", "browser": "local",
                "done_option": "我已完成，继续", "link": "/app/s/orders", "assistant_may_answer": False,
                "questions": [{"header": "需要你接管", "question": "页面要求登录，且登录过程带有人机验证，需要你亲自完成。",
                               "options": ["我已完成，继续", "跳过这一步", "放弃任务"]}]}
    spoken = cards.spoken_question(takeover)
    assert spoken["kind"] == "desktop_takeover" and spoken["link"] == "/app/s/orders"
    assert spoken["host"] == "login.example.com"
    assert "在『抓取订单』里打开云桌面完成验证（login.example.com），再点『我已完成，继续』" in spoken["answer_how"]
    assert "assistant_ask" in spoken["answer_how"]  # only on the user's own word that they finished
    extension = cards.spoken_question({**takeover, "browser": "extension"})
    assert "在你自己的浏览器里完成验证" in extension["answer_how"] and "云桌面" not in extension["answer_how"]
    plain = cards.spoken_question(FORM)
    assert plain["kind"] == "question" and "task_question_answer" in plain["answer_how"]


def test_task_form_keeps_every_field_and_exact_labels():
    spoken = cards.spoken_question(FORM)
    assert spoken["request_id"] == "q-video" and spoken["session_id"] == "video-session"
    assert [q["number"] for q in spoken["questions"]] == [1, 2, 3]
    assert spoken["questions"][0]["options"] == ["保留50秒", "缩短到30秒"]
    assert not spoken["questions"][0]["custom"]
    assert spoken["questions"][1]["multiple"] and spoken["questions"][2]["custom"]


async def test_watch_reads_the_same_user_workspace_and_main_as_the_card_tool(monkeypatch):
    answering(monkeypatch)
    identities = []

    async def list_waiting(**identity):
        identities.append(identity)
        return [FORM]
    monkeypatch.setattr("assistant.request_answers.list_waiting", list_waiting)
    assert await cards.waiting(SCOPE) == ([CARD], [FORM])
    assert identities == [{"user_id": "u1", "workspace_id": "w1", "main_id": "main-1", "limit": cards.WATCH_LIMIT}]


async def test_pending_form_is_announced_once_without_a_tool_call_even_when_reports_are_off(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.tell_reports = False
    await bridge.waiting_cards([], [FORM])
    note = provider.commands("note")[-1][1]
    assert "q-video" in note and "50秒" in note
    assert "字幕" in note and "片名" in note  # all source questions remain available
    assert len(bridge.desk.questions[FORM["id"]]["questions"]) == 3  # still available for handover
    assert provider.response_purposes()[-1] == ("create", phrases.question_instructions("zh"))
    await replay(bridge, item("note-question", text=note), started("read"), audio("read"), done("read"))
    await bridge.waiting_cards([], [FORM])
    assert len(provider.commands("note")) == 1
    assert not link.started and not link.records  # a reminder creates no user input or billed voice turn
    assert bridge._asked[1].questions == [FORM]


async def test_card_appearing_while_user_speaks_waits_and_is_withdrawn_if_answered_on_screen(call):
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"))
    await bridge.waiting_cards([], [FORM])
    assert not provider.commands("note") and len(bridge.deliveries) == 1
    await bridge.waiting_cards([], [])
    assert not bridge.deliveries and not bridge.desk.questions


async def test_second_form_waits_for_answer_and_confirmation_cards_take_priority(call):
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    other = {**FORM, "id": "q-other", "session_id": "another-task"}
    await bridge.waiting_cards([], [FORM, other])
    note = provider.commands("note")[-1][1]
    await replay(bridge, item("note-question", text=note), started("read"), audio("read"), done("read"))
    await bridge.fill_idle()
    assert len(provider.commands("note")) == 1  # do not switch cards before the user has answered
    await bridge.waiting_cards([CARD], [FORM, other])
    assert len(provider.commands("note")) == 2 and "删除项目" in provider.commands("note")[-1][1]


async def test_native_request_retains_complete_form_and_human_words(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    words = "第一题选第一个，字幕中文英文都要，片名叫打工人的离谱日常。"
    await said(bridge, words, "u1")
    brief = "提交视频表单：时长50秒，双语字幕，片名打工人的离谱日常"
    await replay(bridge, started("r1"), tool_call("a1", text=brief, response_id="r1",
        arguments=json.dumps({"request": brief, "question_id": "q-video"})), done("r1"))
    [ref] = link.started
    assert ref.transcript == words and ref.text == brief
    assert ref.context["task_questions"] == [cards.spoken_question(FORM)]
    assert not bridge.desk.answers
    await bridge.close()



async def test_new_request_does_not_inherit_an_unrelated_pending_form(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    await said(bridge, "帮我查看今天的销售报表", "u1")
    await replay(bridge, started("r1"), tool_call("a1", text="查询今天的销售报表", response_id="r1"), done("r1"))
    [ref] = link.started
    assert "task_questions" not in ref.context
    await bridge.close()





async def test_unknown_or_expired_question_id_cannot_be_redirected_to_another_card(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    await bridge.waiting_cards([], [])
    await said(bridge, "选第二个", "answer-1")
    args = json.dumps({"request": "选第二个", "question_id": "q-video"})
    await replay(bridge, started("answer"), tool_call("answer-call", arguments=args, response_id="answer"), done("answer"))
    assert provider.commands("output")[-1][2]["status"] == "unknown_question" and not link.started


def test_explicit_form_target_survives_intervening_conversation_but_not_invalidation():
    heard = Heard()
    desk = cards.CardDesk(heard)
    desk.show_question({**FORM, "questions": [{"question": "长说明" * 3000}]}, "zh")
    compact = [{"request_id": "q-video", "session_id": "video-session", "questions_omitted": True}]
    for _ in range(5):
        heard.say("继续")
    assert desk.question_context("q-video") == compact
    assert desk.question_context() == []
    desk.reconcile([], [])
    assert desk.question_context("q-video") == []



async def test_task_form_cannot_be_answered_by_the_model_before_the_user_speaks(call, monkeypatch):
    bridge, provider, link, _ = call
    monkeypatch.setattr(cards, "WORDS_WAIT_SECONDS", 0.01)
    monkeypatch.setattr("voice.turns.TRANSCRIPT_WAIT_SECONDS", 0.01)
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    arguments = json.dumps({"request": "全部选默认", "question_id": "q-video"})
    await replay(bridge, started("guess"), tool_call("guess", text="全部选默认", arguments=arguments,
                                                   response_id="guess"), done("guess"))
    await asyncio.sleep(.1)
    assert provider.commands("output")[-1][2]["status"] in ("need_user_answer", "not_user_request", "need_user_input")
    assert not link.started


async def test_rereading_a_form_after_the_spoken_answer_does_not_discard_that_answer(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    await said(bridge, "中文和英文都要", "answer-both")
    bridge.desk.show_question(FORM, "zh")
    args = json.dumps({"request": "中文和英文都要", "question_id": "q-video"})
    await replay(bridge, started("answer"), tool_call("reread", arguments=args, response_id="answer"), done("answer"))
    assert [ref.transcript for ref in link.started] == ["中文和英文都要"]


async def test_a_task_answer_waits_for_late_asr_without_blocking_the_provider_pump(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    arguments = json.dumps({"request": "第一题选第一个", "question_id": "q-video"})
    await replay(bridge, started("answer"), tool_call("late-asr", text="第一题选第一个", arguments=arguments,
                                                   response_id="answer"), done("answer"))
    assert not link.started and not provider.commands("output")
    await replay(bridge, transcript("第一题选第一个，保留五十秒", "late-transcript"))
    await asyncio.sleep(.2)
    await drain()
    assert len(link.started) == 1 and link.started[0].transcript == "第一题选第一个，保留五十秒"
    link.finish("late-asr", speech="第二题字幕选哪些？")
    await drain()


async def test_main_confirmation_watch_and_turn_result_do_not_read_the_card_twice(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "把测试项目删了", "u-1")
    await replay(bridge, started("ack"), audio("ack"), tool_call("request", response_id="ack"), done("ack"))
    await bridge.waiting_cards([CARD], [])
    assert not provider.commands("note")  # the in-flight turn carries its own confirmation and receipt
    link.finish("request", speech="", cards=[CARD])
    await drain()
    note = provider.commands("note")[-1][1]
    await replay(bridge, item("note-main", text=note), started("read"), audio("read"), done("read"))
    await bridge.waiting_cards([CARD], [])
    assert sum('"card": "1"' in note for _, note in provider.commands("note")) == 1


def selection(card=CARD, option=0, **fields):
    return {"card": "1", "version": cards.version(card), "option_id": cards.option_id(option), **fields}


@pytest.mark.parametrize("words", ["就按你刚才那个方案办", "不用解释，消息就按原样发", "嗯", "Proceed with the approach we discussed"])
async def test_model_decision_is_not_reclassified_by_a_wordlist(monkeypatch, words):
    replies = answering(monkeypatch)
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(CARD)
    for _ in range(4):
        heard.say("谈谈别的")
    heard.say(words)
    value = await cards.answer(scope, selection())
    assert value["status"] == "decision_recorded" and value["this_call_applied"] and not value["execution_confirmed"]
    assert replies == [("q-del", [["确认"]], "u1", {})]
    again = await cards.answer(scope, selection())
    assert again["status"] == "already_answered" and not again["this_call_applied"] and len(replies) == 1


async def test_no_post_card_human_input_cannot_authorize_an_answer(monkeypatch):
    replies = answering(monkeypatch)
    heard = Heard()
    heard.say("确认")
    desk, scope = scoped(heard)
    desk.show(CARD)
    result = await cards.answer(scope, selection())
    assert result["status"] == "need_user_answer" and replies == []


@pytest.mark.parametrize("changes,status", [
    ({"card": "nonexistent"}, "unknown_card"), ({"version": "old"}, "conflict"),
    ({"option_id": "确认"}, "invalid_choice"), ({"project_name": "春雨"}, "invalid_arguments"),
])
async def test_identity_version_and_option_contract_precede_mutation(monkeypatch, changes, status):
    replies = answering(monkeypatch)
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(CARD)
    heard.say("按这个来")
    value = await cards.answer(scope, {**selection(), **changes})
    assert value["status"] == status and not value["this_call_applied"] and not replies


async def test_changed_card_content_cannot_use_a_stale_version(monkeypatch):
    replies = answering(monkeypatch, waiting=({**CARD, "impact": "变更了范围"},))
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(CARD)
    heard.say("按这个来")
    assert (await cards.answer(scope, selection()))["status"] == "conflict" and not replies


@pytest.mark.parametrize("status", ["expired", "already_answered", "superseded", "unavailable"])
async def test_missing_cards_do_not_claim_their_operations_succeeded(monkeypatch, status):
    answering(monkeypatch, waiting=())
    async def missing(*args):
        assert args == ("u1", "w1", "main-1", "q-del")
        return status
    monkeypatch.setattr("assistant.confirmations.card_status", missing)
    desk, scope = scoped(Heard())
    desk.show(CARD)
    desk.reconcile([], [])
    assert not desk.open() and not desk.answered
    value = await cards.answer(scope, selection())
    assert value["status"] == status and not value["this_call_applied"] and not value["execution_confirmed"]


async def test_simultaneous_conflicting_choices_apply_once(monkeypatch):
    replies = answering(monkeypatch)
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(CARD)
    heard.say("按这个来")
    values = await asyncio.gather(cards.answer(scope, selection()), cards.answer(scope, selection(option=1)))
    assert len(replies) == 1 and sum(v["this_call_applied"] for v in values) == 1


async def test_new_project_choice_carries_user_supplied_name(monkeypatch):
    placement = {**CARD, "kind": "assistant_placement", "options": ["在默认空间新建对话", "新建项目", "取消"]}
    replies = answering(monkeypatch, waiting=(placement,))
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(placement)
    heard.say("不要第一个，第二个就行，项目叫春季上新")
    result = await cards.answer(scope, selection(placement, 1, project_name="春季上新"))
    assert result["status"] == "decision_recorded"
    assert replies == [("q-del", [["新建项目"]], "u1", {"placement_project_name": "春季上新"})]


async def test_no_match_card_can_name_the_new_project_in_its_first_option(monkeypatch):
    placement = {**CARD, "kind": "assistant_placement", "options": ["新建项目「门店筹备」", "取消"],
                 "new_project_option": "新建项目「门店筹备」", "high_risk": False}
    replies = answering(monkeypatch, (placement,))
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show(placement)
    heard.say("行，你安排吧，名字改叫春季上新")
    result = await cards.answer(scope, selection(placement, 0, project_name="春季上新"))
    assert result["status"] == "decision_recorded"
    assert replies == [("q-del", [["新建项目「门店筹备」"]], "u1", {"placement_project_name": "春季上新"})]


async def test_unrelated_negative_words_never_decline_any_of_several_cards(call, monkeypatch):
    bridge, provider, _, _ = call
    replies = answering(monkeypatch)
    await greeted(bridge, provider)
    bridge.desk.show(CARD)
    bridge.desk.show({**CARD, "card_id": "q-other"})
    await said(bridge, "不用给我解释原理了", "u1")
    assert not replies and len(bridge.desk.open()) == 2


async def test_card_choice_records_its_original_utterance_even_when_later_speech_arrives(call, monkeypatch):
    bridge, provider, _, _ = call
    replies = answering(monkeypatch)
    await greeted(bridge, provider)
    bridge.desk.show(CARD)
    await replay(bridge, item("first"), event("user_transcript", item_id="first", text="按原方案办"), started("first-response"))
    await replay(bridge, item("second"), event("user_transcript", item_id="second", text="另外问个问题"))
    await replay(bridge, tool_call("choice", name="cards_answer", arguments=json.dumps(selection()), response_id="first-response"))
    assert replies
    assert bridge.refs[-1].transcript == "按原方案办"
