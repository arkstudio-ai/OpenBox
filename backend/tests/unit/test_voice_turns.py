"""assistant_ask and the direct reads inside a call (docs/ASSISTANT_VOICE_FIX_PLAN.md §5.1, §5.2, §5.5).

An immediate acknowledgement; results as background notes told once in the
front desk's own words; real progress steps; reads answered without the
assistant; half sentences never handed over.
"""
import asyncio

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       of_type, outbox, provider_error, started, tool_call)
from voice import phrases, tools
from voice.bridge import Bridge, REPLY_GRACE_SECONDS
from voice.progress import Progress
from voice.tools import CallScope

SCOPE = CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x")
RESULT = "「贪吃蛇」的收尾自检做完了，一切正常；「配色」还在等你选一个方案。"


@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    progress = Progress(user_id="u1", main_session_id="main-1", clock=clock)
    bridge = Bridge(provider, link, lang="zh", late_after=12, clock=clock, scope=SCOPE, progress=progress,
                    instructions="BASE")
    return bridge, provider, link, clock


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def greeted(bridge, provider):
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    provider.sent.clear()
    outbox(bridge)


async def asked(bridge, call_id="call-1", response_id="ack", text="帮我看看贪吃蛇进展"):
    await replay(bridge, event("user_started"), event("user_stopped"), started(response_id), audio(response_id),
                 tool_call(call_id, text=text, response_id=response_id), done(response_id))


def states(items):
    return [value["state"] for value in of_type(items, "turn")]


def kinds(provider):
    return [command[0] for command in provider.sent if command[0] in ("output", "note", "create", "delete")]


async def finished(bridge, provider, link, speech=RESULT, status="ok", call_id="call-1"):
    """The assistant settles; the note goes in and the provider confirms it."""
    link.finish(call_id, status=status, speech=speech)
    await drain()
    [(_, note)] = provider.commands("note")[-1:]
    await replay(bridge, item("note-1", text=note))
    return note


async def test_assistant_ask_is_acknowledged_at_once_and_reports_working(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    assert provider.commands("output") == [("output", "call-1", {"status": "accepted", "note": "结果稍后以后台备注送到"})]
    [ref] = link.started
    assert (ref.text, ref.provider_call_id, ref.transcript) == ("帮我看看贪吃蛇进展", "call-1", "帮我看看贪吃蛇进展")
    items = outbox(bridge)
    assert of_type(items, "turn") == [{"type": "turn", "turn_id": ref.id, "state": "accepted", "inbox_id": "inbox-1",
                                       "message_id": None}]
    assert of_type(items, "phase")[-1] == {"type": "phase", "value": "working", "working": True, "late": False}
    assert not provider.commands("create")  # it said its acknowledgement itself: nothing more to ask for
    link.on_message(ref)  # the run claimed the input: the client can scroll to the message
    assert states(outbox(bridge)) == ["working"]


async def test_a_result_is_a_note_told_once_in_the_front_desks_own_words(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    outbox(bridge)
    note = await finished(bridge, provider, link)
    assert note == "（后台备注，不是用户说的话）关于用户说的“帮我看看贪吃蛇进展”：个人助理回来了：" + RESULT
    assert kinds(provider) == ["output", "note", "create"]  # the note, then our reply at once
    [(_, instructions)] = provider.commands("create")
    assert instructions == phrases.delivery_instructions(RESULT, "zh")
    assert "用你自己的话" in instructions and "三句以内" in instructions and "备注一致" in instructions
    assert "逐字" not in instructions and "我这边查到了" not in instructions.replace("不要用“我这边查到了”", "")
    assert instructions.endswith("这些要原文说：「贪吃蛇」、「配色」。")  # it offers a choice: the labels stay
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert states(outbox(bridge)) == ["delivered"]
    assert len(provider.commands("create")) == 1 and len(provider.commands("output")) == 1 and not bridge.deliveries
    assert (link.started[0].id, {"delivered": True, "outcome": "delivered"}) in link.records
    assert [line.role for line in bridge.spoken.lines] == ["note"]


async def test_amounts_and_offered_choices_are_said_as_written():
    speech = "这个月用了 ¥12.50，余额 300 积分。要选一个方案：「暗色」还是「亮色」？"
    instructions = phrases.delivery_instructions(speech, "zh")
    assert instructions.endswith("这些要原文说：¥12.50、300 积分、「暗色」、「亮色」。")


async def test_a_vad_reply_that_saw_the_note_counts_as_the_delivery(call):
    """The user spoke as the result arrived: the reply that won the race already told it."""
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    outbox(bridge)
    await finished(bridge, provider, link)  # the provider confirms the note before anything else
    clock.advance(0.4)
    await replay(bridge, started("vad"), provider_error("active_response"))  # our request lost the race
    assert bridge.deliveries[0].delivery == "covered"
    await replay(bridge, audio("vad"), done("vad"))
    assert states(outbox(bridge)) == ["delivered"]
    assert len(provider.commands("create")) == 1 and not bridge.deliveries  # never told a second time
    clock.advance(30)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1


async def test_a_vad_reply_that_started_before_the_note_does_not(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    link.finish("call-1", speech=RESULT)
    await drain()
    [(_, note)] = provider.commands("note")
    await replay(bridge, started("vad"), item("note-1", text=note), provider_error("active_response"))
    assert bridge.deliveries[0].delivery == "queued"
    await replay(bridge, audio("vad"), done("vad"))
    assert len(provider.commands("create")) == 2 and len(provider.commands("note")) == 1  # the note is already in
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert states(outbox(bridge)) == ["accepted", "delivered"]


async def test_a_note_injected_long_before_a_reply_is_not_covered_by_it(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    await finished(bridge, provider, link)
    clock.advance(2.0)  # beyond the 1.5 s window
    await replay(bridge, started("vad"), provider_error("active_response"))
    assert bridge.deliveries[0].delivery == "queued"


async def test_an_unheard_delivery_takes_its_note_out_and_tells_it_again(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    clock.advance(REPLY_GRACE_SECONDS)
    await finished(bridge, provider, link)
    await replay(bridge, started("deliver"), event("user_started"), done("deliver", status="cancelled"))
    assert provider.commands("delete") == [("delete", "note-1")]  # the reply to the user's words cannot read it
    assert not bridge.deliveries[0].note_item
    await replay(bridge, event("user_stopped", invalid=True))
    assert kinds(provider)[-2:] == ["note", "create"]  # injected again, then told
    await replay(bridge, item("note-2", text=provider.commands("note")[-1][1]), started("deliver-2"),
                 audio("deliver-2"), done("deliver-2"))
    assert states(outbox(bridge))[-1] == "delivered"


async def test_timeouts_and_failures_are_notes_without_a_claimed_result(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    note = await finished(bridge, provider, link, status="timeout", speech=phrases.speech_text("timeout", "zh"))
    assert note.endswith("个人助理还在办，超过两分钟了，办好后结果会写在对话里。")
    [(_, instructions)] = provider.commands("create")
    assert instructions == phrases.notice_instructions("zh") and "不要说查到了什么" in instructions
    await replay(bridge, started("notice"), audio("notice"), done("notice"))
    assert states(outbox(bridge)) == ["accepted", "timeout"]  # never "delivered"


async def test_failed_acceptance_and_unknown_tools_are_answered(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-x", name="weather"), done("ack"))
    assert provider.commands("output") == [("output", "call-x", {"status": "unknown_tool"})]
    link.fail_start = True
    await replay(bridge, started("ack-2"), audio("ack-2"), tool_call("call-1"), done("ack-2"))
    assert provider.commands("output")[-1][2]["status"] == "accepted"  # acknowledged before the handover failed
    [(_, note)] = provider.commands("note")
    assert note.endswith("没能交给个人助理，请用户过一会儿再说一次。")
    assert states(outbox(bridge)) == ["failed"]
    assert provider.commands("create")[-1][1] == phrases.notice_instructions("zh")


async def test_another_refusal_says_the_result_is_in_the_text(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await finished(bridge, provider, link)
    await replay(bridge, provider_error("other"))
    assert states(outbox(bridge)) == ["accepted", "failed"] and not bridge.deliveries
    await bridge.fill_idle()
    assert provider.commands("create")[-1] == ("create", phrases.phrase_instructions("result_in_text", "zh"))


async def test_each_call_id_gets_exactly_one_output(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, tool_call("call-1"))  # the provider repeats the same call
    assert len(link.started) == 1 and len(provider.commands("output")) == 1
    await finished(bridge, provider, link)
    await replay(bridge, provider_error("duplicate_output"), provider_error("item"))  # nothing to retry
    assert len(provider.commands("output")) == 1 and len(provider.commands("create")) == 1


async def test_half_sentences_are_never_handed_over(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="嗯，就是。"),
                 started("ack"), tool_call("call-1", text="就是", response_id="ack"), done("ack"))
    assert provider.commands("output") == [("output", "call-1", {"status": "need_more", "hint": "问用户想做什么"})]
    assert not link.started and not of_type(outbox(bridge), "turn")
    assert provider.commands("create") == [("create", None)]  # it asks what the user wants, from the output


@pytest.mark.parametrize("words, fragment", [
    ("嗯，就是。", True), ("就是。", True), ("帮我", True), ("那个", True), ("我要", True), ("", True), ("哦", True),
    ("嗯", False), ("嗯嗯。", False),  # a yes to the front desk's own question
    ("好的", False), ("确认", False), ("可以", False), ("新建", False), ("查一下", False),
    ("新建一个。", False), ("帮我看看贪吃蛇进展", False), ("嗯，删吧", False),
])
def test_fragment_guard(words, fragment):
    assert tools.is_fragment(words) is fragment


async def test_an_acknowledgement_without_words_is_followed_up(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), started("ack"),
                 tool_call("call-1", response_id="ack"), done("ack"))  # no audio at all
    assert provider.commands("create") == [("create", None)]
    assert len(link.started) == 1


async def test_a_direct_read_is_answered_from_its_output_without_the_assistant(call, monkeypatch):
    bridge, provider, link, _ = call
    seen = []

    async def overview(scope, arguments):
        seen.append(scope)
        return {"status": "ok", "tasks": [{"title": "贪吃蛇", "state": "进行中"}]}
    monkeypatch.setitem(tools.DIRECT, "tasks_overview", tools.DirectTool("…", overview))
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="我有哪些任务"),
                 started("ask"), audio("ask"), tool_call("call-1", name="tasks_overview", arguments="{}"))
    assert provider.commands("output") == [("output", "call-1",
                                            {"status": "ok", "tasks": [{"title": "贪吃蛇", "state": "进行中"}]})]
    assert not provider.commands("create")  # its own reply is still speaking
    await replay(bridge, done("ask"))
    assert provider.commands("create") == [("create", None)] and not link.started
    assert (seen[0].user_id, seen[0].main_session_id, seen[0].transcript) == ("u1", "main-1", "我有哪些任务")


async def test_a_reply_that_follows_the_output_by_itself_needs_no_follow_up(call, monkeypatch):
    bridge, provider, _, _ = call

    async def projects(scope, arguments):
        return {"status": "ok", "projects": ["贪吃蛇"]}
    monkeypatch.setitem(tools.DIRECT, "projects_list", tools.DirectTool("…", projects))
    await greeted(bridge, provider)
    await replay(bridge, started("ask"), tool_call("call-1", name="projects_list", arguments="{}"), done("ask"))
    await replay(bridge, event("user_started"))  # the user speaks before the follow-up could start
    assert provider.commands("create") == [("create", None)]
    await replay(bridge, done("x"), event("user_stopped"), started("vad"), audio("vad"), done("vad"))
    assert len(provider.commands("create")) == 1


async def test_direct_reads_are_bounded_and_never_raise(monkeypatch):
    async def slow(scope, arguments):
        await asyncio.sleep(5)

    async def broken(scope, arguments):
        raise PermissionError("not yours")
    monkeypatch.setitem(tools.DIRECT, "slow", tools.DirectTool("…", slow, timeout=0.05))
    monkeypatch.setitem(tools.DIRECT, "broken", tools.DirectTool("…", broken))
    assert await tools.run("slow", SCOPE, "{}") == {"status": "unavailable"}
    assert await tools.run("broken", SCOPE, "not json") == {"status": "unavailable"}
    names = [schema["function"]["name"] for schema in tools.schemas()]
    assert names[:6] == ["assistant_ask", "tasks_overview", "memory_search", "schedules_list", "projects_list",
                         "credits"]


async def test_progress_says_the_real_step_at_most_three_times_twelve_seconds_apart(call):
    """Twelve seconds apart for a new step, thirty for the same one; three per turn at most."""
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    bridge.progress.on_event({"type": "tool.running", "data": {"userId": "u1", "sessionId": "main-1",
                                                               "tool": "tasks.list"}})
    bridge.progress.on_event({"type": "tool.running", "data": {"userId": "u1", "sessionId": "other",
                                                               "tool": "history.read"}})  # another session
    clock.advance(11)
    await bridge.fill_idle()
    assert not provider.commands("create")
    assert [text for _, text in provider.commands("instructions")] == [
        "BASE\n当前后台进度：个人助理正在办“帮我看看贪吃蛇进展”。",  # right after the acknowledgement
        "BASE\n当前后台进度：个人助理正在办“帮我看看贪吃蛇进展”，在翻你的任务列表。"]
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.commands("create") == [("create", phrases.progress_instructions("在翻你的任务列表", "zh"))]
    assert "不要重复之前说过的话" in provider.commands("create")[0][1]
    await replay(bridge, started("p1"), audio("p1"), done("p1"))
    items = outbox(bridge)
    assert of_type(items, "phrase") == [{"type": "phrase", "key": "progress"}]
    assert states(items) == ["accepted", "late"]
    assert of_type(items, "phase")[-1]["late"] is True
    bridge.progress.on_event({"type": "message.text_delta", "data": {"userId": "u1", "sessionId": "main-1"}})
    clock.advance(11)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1  # quiet for 11 s only
    clock.advance(1)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 2  # a new step: twelve seconds apart
    await replay(bridge, started("p2"), audio("p2"), done("p2"))
    assert provider.commands("create")[1][1] == phrases.progress_instructions("在整理回复", "zh")
    clock.advance(12)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 2  # the same step again only after thirty seconds
    clock.advance(18)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 3
    await replay(bridge, started("p3"), audio("p3"), done("p3"))
    clock.advance(60)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 3  # at most three per turn
    assert states(outbox(bridge)) == []  # "late" is said once


async def test_a_second_request_is_reported_as_queued(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge, "call-1", "ack-1", "帮我看看贪吃蛇进展")
    await asked(bridge, "call-2", "ack-2", "再建一个五子棋项目")
    link.started[0].message_id = "m1"  # the run took the first one
    clock.advance(3)
    await bridge.fill_idle()
    assert provider.commands("instructions")[-1][1].endswith(
        "当前后台进度：个人助理正在办“帮我看看贪吃蛇进展”；排队 1 件：“再建一个五子棋项目”。")
    await finished(bridge, provider, link, call_id="call-1")
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    clock.advance(3)
    await bridge.fill_idle()
    assert provider.commands("instructions")[-1][1].endswith("当前后台进度：个人助理正在办“再建一个五子棋项目”。")


def test_step_phrases_cover_the_assistant_tools():
    from voice.progress import step_phrase
    assert step_phrase("tasks.list") == "在翻你的任务列表"
    assert step_phrase("history.read") == "在看那个对话的记录"
    assert step_phrase("tasks.submit") == "在把活交给项目"
    assert step_phrase("memory.search") == "在翻记忆"
    assert step_phrase("projects.create") == "在建项目"
    assert step_phrase("requests.answer") == step_phrase("requests.list") == "在看那张卡片"
    assert (step_phrase("projects.delete"), step_phrase("sessions.delete"), step_phrase("tasks.delete")) == (
        "在删项目", "在删会话", "在停掉任务")
    assert step_phrase("something.new") == "在处理" and step_phrase("tasks.list", "en") == "looking through your tasks"


async def test_progress_follows_the_bus_only_while_subscribed():
    from bus import bus
    progress = Progress(user_id="u1", main_session_id="main-1")
    progress.start()
    try:
        bus.publish("tool.running", {"userId": "u1", "sessionId": "main-1", "tool": "memory.search"})
        assert progress.step == "在翻记忆"
    finally:
        progress.stop()
    bus.publish("tool.running", {"userId": "u1", "sessionId": "main-1", "tool": "tasks.submit"})
    assert progress.step == "在翻记忆"


def said_text(text, response_id):
    return event("assistant_transcript", text=text, item_id=f"item-{response_id}", response_id=response_id)


async def test_a_promise_without_a_tool_call_is_kept(call):
    """Measured after a declined card: "这就去删" twice, no assistant_ask. The words are handed over."""
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"),
                 event("user_transcript", text="把语音播报这个项目删掉", item_id="u-1"),
                 started("r1"), audio("r1"), said_text("行，我这就去把语音播报项目删掉。", "r1"), done("r1"))
    [ref] = link.started
    assert ref.provider_call_id == "promise:r1" and ref.text == ref.transcript == "把语音播报这个项目删掉"
    assert not provider.commands("output")  # no provider call to answer: it was never made


@pytest.mark.parametrize("user, reply, tool", [
    ("把语音播报这个项目删掉", "行，我这就去把语音播报项目删掉。", True),   # it did call: nothing more
    ("你好呀", "你好，我在，有事我来处理。", False),                         # small talk is not work
    ("把语音播报这个项目删掉", "你刚说先不删，现在又改主意了吗？", False),     # asking back is no promise
])
async def test_no_promise_to_keep(call, user, reply, tool):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    events = [event("user_started"), event("user_stopped"), event("user_transcript", text=user, item_id="u-1"),
              started("r1"), audio("r1"), said_text(reply, "r1")]
    if tool:
        events.append(tool_call("call-1", text=user, response_id="r1"))
    await replay(bridge, *events, done("r1"))
    assert [ref.provider_call_id for ref in link.started] == (["call-1"] if tool else [])


async def test_a_late_assistant_ask_for_a_kept_promise_is_not_a_second_turn(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"),
                 event("user_transcript", text="把语音播报这个项目删掉。", item_id="u-1"),
                 started("r1"), audio("r1"), said_text("行，我这就去把语音播报项目删掉。", "r1"), done("r1"),
                 started("r2"), tool_call("call-2", text="把语音播报这个项目删掉", response_id="r2"), done("r2"))
    assert [ref.provider_call_id for ref in link.started] == ["promise:r1"]
    assert provider.commands("output")[-1][2]["status"] == "accepted"
