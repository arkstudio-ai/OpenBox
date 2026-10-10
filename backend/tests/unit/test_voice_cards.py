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


@pytest.mark.parametrize("words, expected", [
    ("确认。", True), ("好的，删吧", True), ("可以", True), ("嗯，确认", True), ("就这样", True), ("是的", True),
    ("好", True), ("嗯嗯", True), ("OK", True), ("yes, go ahead", True), ("停掉吧", True), ("记住吧", True),
    ("确认吗？", False), ("等一下", False), ("不要删", False), ("算了", False), ("这是什么意思", False),
    ("我不确定", False), ("嗯……让我想想", False), ("", False), ("帮我看看别的", False), ("no", False),
    ("好像不太对", False), ("删吧，不过先等等", False),
])
def test_consent_is_a_clear_yes_only(words, expected):
    assert cards.consents(words) is expected


@pytest.mark.parametrize("words, expected", [
    ("确认", True), ("好的", True), ("算了", True), ("不用了", True), ("取消吧", True),
    ("帮我把测试项目删了", False), ("我有哪些任务在进行", False), ("", False),
])
def test_short_yes_or_no_answers_a_card(words, expected):
    assert cards.answers_card(words) is expected


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
    assert value["cards"] == [{"card": "1", "kind": "确认", "title": "删除项目", "what": "删除项目「测试A」。",
                               "impact": "其中2个会话一起删除，目录之后移到回收站。", "options": ["确认", "取消"],
                               "high_risk": True}]
    assert [item["answer_how"] for item in value["task_questions"]] == [
        "用户说了怎么答，就把原话交给 assistant_ask，由个人助理代答", "要用户自己在屏幕上处理"]
    assert desk.card_id("1") == "q-del" and desk.open() == ["q-del"]
    assert (await cards.pending(scope, {}))["cards"][0]["card"] == "1"  # the same card keeps its handle


async def test_nothing_waiting(monkeypatch):
    answering(monkeypatch, waiting=())

    async def list_waiting(**identity):
        return []
    monkeypatch.setattr("assistant.request_answers.list_waiting", list_waiting)
    assert await cards.pending(scoped(Heard())[1], {}) == {"status": "none"}


async def test_a_card_is_answered_only_after_the_user_said_yes(monkeypatch):
    replies = answering(monkeypatch)
    heard = Heard()
    heard.say("把测试项目删了")  # the request itself is no consent to the card that follows
    desk, scope = scoped(heard)
    assert (await cards.answer(scope, {"card": "1", "choice": "确认"}))["status"] == "unknown_card"
    desk.show("q-del")
    assert (await cards.answer(scope, {"card": "1", "choice": "确认"}))["status"] == "need_user_answer"
    heard.say("嗯……这个项目里有什么？")
    value = await cards.answer(scope, {"card": "1", "choice": "确认"})
    assert value["status"] == "not_confirmed" and "再问一次" in value["hint"]
    heard.say("确认，删吧")
    assert (await cards.answer(scope, {"card": "1", "choice": "删除"}))["status"] == "invalid_choice"
    value = await cards.answer(scope, {"card": "1", "choice": "确认"})
    assert value == {"status": "answered", "choice": "确认", "next": "个人助理接着办，办完会以后台备注送来"}
    assert replies == [("q-del", [["确认"]], "u1", {})]
    assert desk.answered == {"q-del": "确认"} and desk.open() == []
    [answer] = desk.answers
    assert (answer["card_id"], answer["what"], answer["after"], answer["words"]) == (
        "q-del", "删除项目「测试A」。", "inbox_0001", "确认，删吧")


async def test_declining_needs_no_yes(monkeypatch):
    replies = answering(monkeypatch)
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show("q-del")
    heard.say("算了，先不删")
    assert (await cards.answer(scope, {"card": "1", "choice": "取消"}))["next"] == "已取消，不会执行"
    assert replies == [("q-del", [["取消"]], "u1", {})]


async def test_an_answered_or_expired_card_is_gone(monkeypatch):
    from question.question import QuestionConflict, QuestionGone
    heard = Heard()
    desk, scope = scoped(heard)
    desk.show("q-del")
    heard.say("确认")
    answering(monkeypatch, waiting=())
    assert (await cards.answer(scope, {"card": "1", "choice": "确认"}))["status"] == "gone"
    answering(monkeypatch, reply=QuestionGone("expired"))
    assert (await cards.answer(scope, {"card": "1", "choice": "确认"}))["status"] == "gone"
    answering(monkeypatch, reply=QuestionConflict("An answer has already been accepted"))
    assert (await cards.answer(scope, {"card": "1", "choice": "确认"}))["status"] == "already_answered"
    assert desk.answers == []


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
    assert "卡片1「删除项目」：删除项目「测试A」。影响：其中2个会话一起删除，目录之后移到回收站。" in note
    assert "选项：「确认」、「取消」" in note and "办好了" not in note
    assert provider.commands("create")[-1] == ("create", phrases.card_instructions("zh"))
    await replay(bridge, item("note-1", text=note), started("read"), audio("read"), done("read"))
    # "确认" handed to assistant_ask would void the card: it is redirected to cards_answer.
    link.waiting_cards = [CARD]
    await said(bridge, "确认", "u-2")
    await replay(bridge, started("r2"), tool_call("call-2", text="确认", response_id="r2"), done("r2"))
    assert provider.commands("output")[-1] == ("output", "call-2", {
        "status": "answer_card", "card": "1", "options": ["确认", "取消"],
        "hint": "用户是在回答这张卡片：明确同意就用 cards_answer 选确认的选项，拒绝就选取消；不要交给 assistant_ask"})
    assert [ref.provider_call_id for ref in link.started] == ["call-1"]
    arguments = json.dumps({"card": "1", "choice": "确认"}, ensure_ascii=False)
    await replay(bridge, started("r3"), tool_call("call-3", name="cards_answer", arguments=arguments,
                                                  response_id="r3"), done("r3"))
    await answered(bridge)
    assert replies == [("q-del", [["确认"]], "u1", {})]
    assert provider.commands("output")[-1][2]["status"] == "answered"
    # The resumed turn is followed like any other; its result is told once.
    [(ref, after)] = link.followed
    assert (after, ref.text, ref.transcript) == ("inbox_0001", "删除项目「测试A」。", "确认")
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
    arguments = json.dumps({"card": "1", "choice": "确认"}, ensure_ascii=False)
    await replay(bridge, started("r2"), tool_call("call-2", name="cards_answer", arguments=arguments,
                                                  response_id="r2"), done("r2"))
    await answered(bridge)
    assert provider.commands("output")[-1][2]["status"] == "need_user_answer"
    assert replies == [] and link.followed == []


def test_cards_tools_are_registered_for_the_front_desk():
    names = [schema["function"]["name"] for schema in tools.schemas()]
    assert names[0] == "assistant_ask" and {"cards_pending", "cards_answer"} <= set(names)
    answer = tools.DIRECT["cards_answer"]
    assert answer.parameters["required"] == ["card", "choice"] and answer.timeout > cards.WORDS_WAIT_SECONDS


@pytest.mark.parametrize("words, expected", [
    ("算了，先不删了", True), ("取消吧", True), ("不用了，谢谢", True), ("先不删", True), ("never mind", True),
    ("确认", False), ("等等，我想想", False), ("好的，删吧", False), ("不不，确认删吧", False), ("这个项目里有什么", False),
])
def test_a_clear_no(words, expected):
    assert cards.refuses(words) is expected


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


async def test_a_clear_no_right_after_the_card_declines_it_on_the_server(call, monkeypatch):
    bridge, provider, link, _ = call
    replies = answering(monkeypatch)
    await read_out(bridge, provider, link)
    await said(bridge, "算了，先不删了", "u-2")  # the front desk only says "好，不删了"
    await drain(30)
    assert replies == [("q-del", [["取消"]], "u1", {})]
    assert bridge.desk.answered == {"q-del": "取消"} and link.followed == []


async def test_a_yes_long_after_the_card_was_read_needs_it_read_again(call, monkeypatch):
    bridge, provider, link, _ = call
    replies = answering(monkeypatch)
    await read_out(bridge, provider, link)
    link.waiting_cards = [CARD]
    for index, words in enumerate(("等等，我想想", "这个项目里都有什么", "那贪吃蛇呢"), start=2):
        await said(bridge, words, f"u-{index}")
    await said(bridge, "好的", "u-9")
    # A bare "好的" now is about something else: handed over, not redirected to the old card.
    await replay(bridge, started("r9"), tool_call("call-9", text="好的", response_id="r9"), done("r9"))
    assert provider.commands("output")[-1][2]["status"] != "answer_card"
    await said(bridge, "确认", "u-10")
    arguments = json.dumps({"card": "1", "choice": "确认"}, ensure_ascii=False)
    await replay(bridge, started("r10"), tool_call("call-10", name="cards_answer", arguments=arguments,
                                                   response_id="r10"), done("r10"))
    await answered(bridge)
    assert provider.commands("output")[-1][2]["status"] == "read_again" and replies == []


FORM = {"id": "q-video", "session_id": "video-session", "session_title": "50秒搞笑视频", "project_name": "电影",
        "assistant_may_answer": True, "high_risk": False,
        "questions": [{"header": "时长", "question": "视频时长是否合适？", "options": ["保留50秒", "缩短到30秒"],
                       "custom": False},
                      {"header": "字幕", "question": "需要哪些字幕？", "options": ["中文", "英文"], "multiple": True},
                      {"header": "片名", "question": "请填写片名", "options": [], "custom": True}]}


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
    assert "字幕" in note and "片名" in note and "q-video" in note
    assert provider.commands("create")[-1] == ("create", phrases.question_instructions("zh"))
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


async def test_voice_answer_preserves_form_id_multiple_choices_and_dictation_and_skips_brief_planner(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)

    async def wrong_plan(**kwargs):
        pytest.fail("card answers must reach requests.answer, not be answered locally by the brief planner")
    bridge.planner = wrong_plan
    await bridge.waiting_cards([], [FORM])
    note = provider.commands("note")[-1][1]
    await replay(bridge, item("note-question", text=note), started("read"), audio("read"), done("read"))
    words = "第一题选第一个，字幕中文英文都要，片名叫打工人的离谱日常。"
    await said(bridge, words, "answer-1")
    args = json.dumps({"request": words, "question_id": "q-video"}, ensure_ascii=False)
    await replay(bridge, started("answer"), tool_call("answer-call", text=words, arguments=args,
                                                     response_id="answer"), done("answer"))
    [ref] = link.started
    assert ref.transcript == words and ref.context["task_questions"] == [cards.spoken_question(FORM)]
    assert not bridge.desk.answers  # task answers cannot bypass the assistant's policy through question.reply
    link.finish("answer-call", speech="已填写")
    await drain()


async def test_unknown_or_expired_question_id_cannot_be_redirected_to_another_card(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    await bridge.waiting_cards([], [])
    await said(bridge, "选第二个", "answer-1")
    args = json.dumps({"request": "选第二个", "question_id": "q-video"})
    await replay(bridge, started("answer"), tool_call("answer-call", arguments=args, response_id="answer"), done("answer"))
    assert provider.commands("output")[-1][2]["status"] == "unknown_question" and not link.started


def test_large_form_context_keeps_target_ids_and_followup_questions_refresh_the_answer_window():
    heard = Heard()
    desk = cards.CardDesk(heard)
    huge = {**FORM, "questions": [{"question": "很长的说明" * 3000, "options": ["一", "二"]}]}
    desk.show_question(huge, "zh")
    compact = desk.question_context()
    assert compact == [{"request_id": "q-video", "session_id": "video-session", "questions_omitted": True}]
    for _ in range(4):
        heard.say("继续回答")
    assert desk.question_context() == []
    desk.asked_again({"task_questions": compact})
    assert desk.question_context() == compact
    desk.reconcile([], [])
    desk.asked_again({"task_questions": compact})
    assert desk.question_context("q-video") == []


async def test_task_form_cannot_be_answered_by_the_model_before_the_user_speaks(call, monkeypatch):
    bridge, provider, link, _ = call
    monkeypatch.setattr(cards, "WORDS_WAIT_SECONDS", 0.01)
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    arguments = json.dumps({"request": "全部选默认", "question_id": "q-video"})
    await replay(bridge, started("guess"), tool_call("guess", text="全部选默认", arguments=arguments,
                                                   response_id="guess"), done("guess"))
    await asyncio.sleep(.1)
    assert provider.commands("output")[-1][2]["status"] == "need_user_answer"
    assert not link.started


async def test_rereading_a_form_after_the_spoken_answer_does_not_discard_that_answer():
    heard = Heard()
    desk = cards.CardDesk(heard)
    desk.show_question(FORM, "zh")
    heard.say("中文和英文都要")
    desk.show_question(FORM, "zh")  # the model checks the ID again, using cards_pending
    assert await desk.question_words_after([FORM["id"]]) == "中文和英文都要"


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
    assert sum("卡片1" in note for _, note in provider.commands("note")) == 1
