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
        return [{"session_title": "视频生成", "project_name": "短视频", "assistant_may_answer": True,
                 "questions": [{"question": "需要上传参考图吗？", "options": ["不需要", "上传"]}]},
                {"session_title": "发布", "project_name": "抖音", "assistant_may_answer": False,
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
