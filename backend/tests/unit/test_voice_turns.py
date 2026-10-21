"""assistant_ask and the direct reads inside a call (docs/ASSISTANT_VOICE_FIX_PLAN.md §5.1, §5.2, §5.5).

An immediate acknowledgement; results as background notes told once in the
front desk's own words; real progress steps; reads answered without the
assistant; contextual requests preserve their original human source.
"""
import asyncio
import json

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       of_type, outbox, provider_error, started, tool_call)
from voice import phrases, tools
from voice.assistant_link import VoiceTurnRef
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
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text=text), started(response_id), audio(response_id),
                 tool_call(call_id, text=text, response_id=response_id), done(response_id))


def note_results(note):
    return json.loads(note.removeprefix(phrases.NOTE_PREFIX["zh"]))["results"]


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
    assert provider.commands("output")[0][:2] == ("output", "call-1")
    assert provider.commands("output")[0][2]["operation_state"] == "received"
    assert not provider.commands("output")[0][2]["execution_confirmed"]
    [ref] = link.started
    assert (ref.text, ref.provider_call_id, ref.transcript) == ("帮我看看贪吃蛇进展", "call-1", "帮我看看贪吃蛇进展")
    items = outbox(bridge)
    assert of_type(items, "turn") == [{"type": "turn", "turn_id": ref.id, "state": "accepted", "inbox_id": "inbox-1",
                                       "message_id": None}]
    assert of_type(items, "phase")[-1] == {"type": "phase", "value": "working", "working": True, "late": False}
    assert not provider.response_purposes()  # it said its acknowledgement itself: nothing more to ask for
    link.on_message(ref)  # the run claimed the input: the client can scroll to the message
    assert states(outbox(bridge)) == ["working"]


async def test_native_conversation_keeps_answering_while_main_work_is_pending(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    clock.advance(1)
    outbox(bridge)
    await replay(bridge, event("user_started", item_id="hello"),
                 event("user_transcript", item_id="hello", text="你好，能听到我说话吗？"),
                 started("native-hello"), audio("native-hello"), done("native-hello"))
    assert any(isinstance(value, bytes) for value in outbox(bridge))
    assert len(link.started) == 1 and bridge.pending_calls  # no main inbox for a greeting
    assert not bridge.unified and bridge.state == "idle"
    assert any(line.item_id == "hello" for line in bridge.spoken.lines)


async def test_handover_cannot_acknowledge_before_durable_acceptance(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    saved_start = link.start
    reached, release = asyncio.Event(), asyncio.Event()

    async def slow_start(ref):
        reached.set()
        await release.wait()
        await saved_start(ref)

    link.start = slow_start
    await replay(bridge, event("user_transcript", text="帮我做一份开业计划"), started("request"))
    dispatch = asyncio.create_task(bridge.on_provider_event(tool_call("durable", response_id="request")))
    await reached.wait()
    assert not provider.commands("output") and not of_type(outbox(bridge), "turn")
    release.set()
    await dispatch
    assert link.started[0].inbox_id
    assert provider.commands("output")[-1][2]["status"] == "accepted"


async def test_sharing_live_context_does_not_block_native_audio_or_wake_main(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    stored, release = [], asyncio.Event()
    async def slow_sink(lines):
        await release.wait()
        stored.append(lines)
    bridge.context_sink = slow_sink
    await replay(bridge, event("user_started", item_id="park"), event("user_stopped"),
                 event("user_transcript", text="周末想去青桐公园走走", item_id="park"),
                 started("chat"), audio("chat"), done("chat"))
    assert not link.started and not stored
    assert any(isinstance(value, bytes) for value in outbox(bridge))
    release.set()
    await drain()
    assert stored[-1][-1].text == "周末想去青桐公园走走"


async def test_a_result_is_a_note_told_once_in_the_front_desks_own_words(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    outbox(bridge)
    note = await finished(bridge, provider, link)
    assert note_results(note)[0]["facts"] == RESULT
    assert note_results(note)[0]["request"] == "帮我看看贪吃蛇进展"
    assert kinds(provider) == ["output", "note", "create"]  # the note, then our reply at once
    [(_, instructions)] = provider.response_purposes()
    assert instructions == phrases.delivery_instructions(RESULT, "zh")
    assert "自然转述" in instructions and "细节按需要展开" in instructions and "与事实一致" in instructions
    assert "逐字" not in instructions and "我这边查到了" not in instructions.replace("不要用“我这边查到了”", "")
    assert "「贪吃蛇」" in note_results(note)[0]["facts"] and "「配色」" in note_results(note)[0]["facts"]
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert states(outbox(bridge)) == ["delivered"]
    assert len(provider.response_purposes()) == 1 and len(provider.commands("output")) == 1 and not bridge.deliveries
    assert (link.started[0].id, {"delivered": True, "outcome": "delivered"}) in link.records
    assert [line.role for line in bridge.spoken.lines] == ["user", "note"]


async def test_amounts_and_offered_choices_are_said_as_written():
    speech = "这个月用了 ¥12.50，余额 300 积分。要选一个方案：「暗色」还是「亮色」？"
    instructions = phrases.delivery_instructions(speech, "zh")
    assert "名称、数字、决定与事实一致" in instructions


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
    assert len(provider.response_purposes()) == 1 and not bridge.deliveries  # never told a second time
    clock.advance(30)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1


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
    assert len(provider.response_purposes()) == 2 and len(provider.commands("note")) == 1  # the note is already in
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
    assert note_results(note)[0]["status"] == "timeout"
    [(_, instructions)] = provider.response_purposes()
    assert instructions == phrases.notice_instructions("zh") and "未知结果不能说成功" in instructions
    await replay(bridge, started("notice"), audio("notice"), done("notice"))
    assert states(outbox(bridge)) == ["accepted", "timeout"]  # never "delivered"


async def test_failed_acceptance_and_unknown_tools_are_answered(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-x", name="weather"), done("ack"))
    assert provider.commands("output") == [("output", "call-x", {"status": "unknown_tool"})]
    link.fail_start = True
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack-2"), audio("ack-2"), tool_call("call-1"), done("ack-2"))
    assert provider.commands("output")[-1][2]["status"] == "unavailable"  # never claim acceptance before the durable write
    [(_, note)] = provider.commands("note")
    assert note_results(note)[0]["status"] == "unavailable"
    assert states(outbox(bridge)) == ["failed"]
    assert provider.response_purposes()[-1][1] == phrases.notice_instructions("zh")


async def test_another_refusal_says_the_result_is_in_the_text(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await finished(bridge, provider, link)
    await replay(bridge, provider_error("other", param="response.create"))
    assert states(outbox(bridge)) == ["accepted", "failed"] and not bridge.deliveries
    await bridge.fill_idle()
    assert provider.response_purposes()[-1] == ("create", phrases.phrase_instructions("result_in_text", "zh"))


@pytest.mark.parametrize("error", [
    # Measured on Audio 2026-10-10, as the provider sends them.
    {"code": "invalid_value", "message": "Cannot find item with id: item_x", "param": "conversation.item.delete"},
    {"code": "invalid_value", "param": "session.update",
     "message": "Cannot update 'turn_detection' after session has started processing audio."},
])
async def test_an_item_or_session_error_while_a_delivery_waits_does_not_drop_it(call, error):
    """Production after the Audio switch: a tidy-up delete of an item Audio had already dropped was taken for a
    refusal of the delivery asked for at that moment, and the front desk said the result was in the text."""
    from voice.provider import translate
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await finished(bridge, provider, link)
    assert bridge.requested and bridge.requested.startswith("delivery:")
    refused = translate({"type": "error", "error": {"type": "invalid_request_error", **error}})
    assert refused.reason in ("item", "session") and refused.param == error["param"]
    await replay(bridge, refused)
    assert bridge.requested.startswith("delivery:") and bridge.deliveries[0].delivery == "creating"
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert states(outbox(bridge)) == ["accepted", "delivered"] and not bridge.deliveries
    assert bridge.next_phrase is None and len(provider.response_purposes()) == 1


async def test_an_error_naming_no_request_leaves_the_delivery_waiting_and_an_unanswered_one_is_asked_again(call):
    from voice.bridge import REQUEST_TIMEOUT_SECONDS
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    await finished(bridge, provider, link)
    await replay(bridge, provider_error("other"))  # no param: not taken for a refusal of ours
    assert bridge.deliveries[0].delivery == "creating" and not states(outbox(bridge))[1:]
    clock.advance(REQUEST_TIMEOUT_SECONDS - 1)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1
    clock.advance(1)  # neither started nor refused: given up and asked for again, the note already in
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 2 and len(provider.commands("note")) == 1
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert states(outbox(bridge))[-1] == "delivered" and not bridge.deliveries


async def test_each_call_id_gets_exactly_one_output(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, tool_call("call-1"))  # the provider repeats the same call
    assert len(link.started) == 1 and len(provider.commands("output")) == 1
    await finished(bridge, provider, link)
    await replay(bridge, provider_error("duplicate_output"), provider_error("item"))  # nothing to retry
    assert len(provider.commands("output")) == 1 and len(provider.response_purposes()) == 1






async def test_a_handover_without_words_is_acknowledged_only_if_no_result_comes_soon(call):
    from voice.turns import ACK_WAIT_SECONDS
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"),
                 tool_call("call-1", response_id="ack"), done("ack"))  # no audio at all
    assert not provider.response_purposes() and len(link.started) == 1  # the result may well come first
    clock.advance(ACK_WAIT_SECONDS - 0.5)
    await bridge.fill_idle()
    assert not provider.response_purposes()
    clock.advance(0.5)
    await bridge.fill_idle()
    assert provider.response_purposes() == [("create", phrases.tool_result_instructions("zh"))]  # nothing yet: the model says so in its own words
    await replay(bridge, started("follow"), audio("follow"), done("follow"))
    clock.advance(ACK_WAIT_SECONDS)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1  # once


async def test_a_result_that_comes_soon_is_told_without_an_acknowledgement_before_it(call):
    """2026-10-09: a handover got two fillers back to back, then the result."""
    from voice.turns import ACK_WAIT_SECONDS
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"),
                 tool_call("call-1", response_id="ack"), done("ack"))
    clock.advance(ACK_WAIT_SECONDS - 1)
    await finished(bridge, provider, link)
    assert kinds(provider) == ["output", "note", "create"]
    assert provider.response_purposes() == [("create", phrases.delivery_instructions(RESULT, "zh"))]
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    clock.advance(ACK_WAIT_SECONDS)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1  # nothing to acknowledge afterwards either


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
    assert not provider.response_purposes()  # its own reply is still speaking
    await replay(bridge, done("ask"))
    assert provider.response_purposes() == [("create", phrases.tool_result_instructions("zh"))] and not link.started
    assert (seen[0].user_id, seen[0].main_session_id, seen[0].transcript) == ("u1", "main-1", "我有哪些任务")


async def test_a_reply_that_follows_the_output_by_itself_needs_no_follow_up(call, monkeypatch):
    bridge, provider, _, _ = call

    async def projects(scope, arguments):
        return {"status": "ok", "projects": ["贪吃蛇"]}
    monkeypatch.setitem(tools.DIRECT, "projects_list", tools.DirectTool("…", projects))
    await greeted(bridge, provider)
    await replay(bridge, started("ask"), tool_call("call-1", name="projects_list", arguments="{}"), done("ask"))
    await replay(bridge, event("user_started"))  # the user speaks before the follow-up could start
    assert provider.response_purposes() == [("create", phrases.tool_result_instructions("zh"))]
    await replay(bridge, done("x"), event("user_stopped"), started("vad"), audio("vad"), done("vad"))
    assert len(provider.response_purposes()) == 1


async def test_direct_reads_are_bounded_and_never_raise(monkeypatch):
    async def slow(scope, arguments):
        await asyncio.sleep(5)

    async def broken(scope, arguments):
        raise PermissionError("not yours")
    monkeypatch.setitem(tools.DIRECT, "slow", tools.DirectTool("…", slow, timeout=0.05))
    monkeypatch.setitem(tools.DIRECT, "broken", tools.DirectTool("…", broken))
    assert await tools.run("slow", SCOPE, "{}") == {"status": "unavailable"}
    assert await tools.run("broken", SCOPE, "not json") == {"status": "invalid_arguments", "this_call_applied": False}
    names = [schema["function"]["name"] for schema in tools.schemas()]
    assert names[:10] == ["assistant_ask", "task_question_answer", "resources_list", "platforms_status", "models_available",
                         "tasks_overview", "memory_search", "schedules_list", "projects_list", "credits"]


async def test_progress_is_one_line_per_request_after_twenty_quiet_seconds_and_a_minute_apart(call):
    """2026-10-09: three lines per request, twelve seconds apart, made "稍等…" 53 times in 50 calls."""
    from core.config import VoiceConfig
    from voice.bridge import PROGRESS_GAP_SECONDS
    bridge, provider, link, clock = call
    bridge.late_after = VoiceConfig().late_after_seconds
    assert bridge.late_after == 20 and Bridge(ScriptedProvider(), FakeLink()).late_after == 20
    await greeted(bridge, provider)
    await asked(bridge)
    bridge.progress.on_event({"type": "tool.running", "data": {"userId": "u1", "sessionId": "main-1",
                                                               "tool": "tasks.list"}})
    bridge.progress.on_event({"type": "tool.running", "data": {"userId": "u1", "sessionId": "other",
                                                               "tool": "history.read"}})  # another session
    clock.advance(19)
    await bridge.fill_idle()
    assert not provider.response_purposes()
    # The session prompt says what is still being done, never the step: answers to new requests wandered into it.
    assert [text for _, text in provider.commands("instructions")] == ["BASE\n已交给个人助理等待结果（不代表任务已开始执行）：“帮我看看贪吃蛇进展”。"]
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.response_purposes() == [("create", phrases.progress_instructions("在翻任务列表", "zh"))]
    await replay(bridge, started("p1"), audio("p1"), done("p1"))
    items = outbox(bridge)
    assert of_type(items, "phrase") == [{"type": "phrase", "key": "progress"}]
    assert states(items) == ["accepted", "late"]
    assert of_type(items, "phase")[-1]["late"] is True
    clock.advance(5)
    await asked(bridge, "call-2", "ack-2", "再建一个五子棋项目")  # another request
    bridge.progress.on_event({"type": "message.text_delta", "data": {"userId": "u1", "sessionId": "main-1"}})
    clock.advance(20)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1  # its twenty seconds are up, but the last line was 25 s ago
    clock.advance(PROGRESS_GAP_SECONDS - 26)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 1
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.response_purposes()[1] == ("create", phrases.progress_instructions("在整理回复", "zh"))
    await replay(bridge, started("p2"), audio("p2"), done("p2"))
    assert states(outbox(bridge)) == ["accepted", "late"]  # the second request's own "late"
    clock.advance(10 * PROGRESS_GAP_SECONDS)
    await bridge.fill_idle()
    assert len(provider.response_purposes()) == 2  # one line per request, however long it takes


async def test_the_prompt_only_says_what_is_still_being_done(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge, "call-1", "ack-1", "帮我看看贪吃蛇进展")
    await asked(bridge, "call-2", "ack-2", "再建一个五子棋项目")
    link.started[0].message_id = "m1"  # the run took the first one
    clock.advance(3)
    await bridge.fill_idle()
    assert provider.commands("instructions")[-1][1] == "BASE\n已交给个人助理等待结果（不代表任务已开始执行）：“帮我看看贪吃蛇进展”、“再建一个五子棋项目”。"
    await finished(bridge, provider, link, call_id="call-1")
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    clock.advance(3)
    await bridge.fill_idle()
    assert provider.commands("instructions")[-1][1] == "BASE\n已交给个人助理等待结果（不代表任务已开始执行）：“再建一个五子棋项目”。"
    remember = VoiceTurnRef(id="t-9", provider_call_id="remember:r9", text="记住用户长期偏好",
                            transcript="我对花生过敏", requested=clock(), lane="remember")
    assert bridge.progress.section([remember]) == ""  # nothing the user waits for


def test_step_phrases_cover_the_assistant_tools():
    from voice.progress import step_phrase
    assert step_phrase("tasks.list") == "在翻任务列表"
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








REPORT = "打开抖音创作者中心受阻，因为云桌面自动化未就绪。"


async def test_a_task_report_nobody_asked_for_is_told_unasked_with_a_lead_in(call):
    """Measured: a handed-over task's result reached only the conversation, never the call."""
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await bridge.report("制作iPhone 18口播视频", REPORT, "inbox-9")
    [(_, note)] = provider.commands("note")
    assert note_results(note)[0]["facts"] == REPORT
    assert note_results(note)[0]["task"] == "制作iPhone 18口播视频"
    assert provider.response_purposes()[-1] == ("create", phrases.together_instructions(1, True, [REPORT], "zh"))
    assert "其他结果" in provider.response_purposes()[-1][1]
    await replay(bridge, item("note-1", text=note), started("tell"), audio("tell"), done("tell"))
    assert not bridge.deliveries and link.records == []  # told; no voice turn of this call to record


async def test_a_report_waits_for_the_answer_the_user_is_waiting_for_and_comes_after_it(call):
    """2026-10-09: replies to three unrelated things in a row; now "另外，刚才那个…" after the answer."""
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await bridge.report("制作iPhone 18口播视频", REPORT, "inbox-9")
    await drain()
    assert not provider.commands("note") and len(bridge.deliveries) == 1  # the user waits for something else
    link.finish("call-1", speech=RESULT)
    await drain()
    [(_, note)] = provider.commands("note")
    assert [r["facts"] for r in note_results(note)] == [RESULT, REPORT]
    [(_, instructions)] = provider.response_purposes()
    assert "先回答当前事项" in instructions


async def test_a_quiet_remember_is_closed_without_a_word(call):
    """Something said in passing goes to be remembered; its result was told unasked (2026-10-09)."""
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    ref = VoiceTurnRef(id="t-remember", provider_call_id="remember:r1", text="记住用户长期偏好",
                       transcript="我对花生过敏，以后点外卖别推花生", requested=clock(), lane="remember")
    bridge.pending_calls[ref.provider_call_id] = ref
    bridge.refs.append(ref)
    await link.start(ref)
    bridge._spawn(bridge._observe_transcript(ref))
    await drain()
    clock.advance(60)
    await bridge.fill_idle()
    assert not provider.response_purposes()  # no progress line about it either
    link.finish("remember:r1", speech="记下了：您对花生过敏，以后点外卖不推花生。")
    await drain()
    assert not provider.commands("note") and not provider.response_purposes() and not bridge.deliveries
    assert states(outbox(bridge))[-1] == "delivered"  # closed for the client: nothing left "在办"
    assert (ref.id, {"outcome": "delivered", "delivered": False}) in link.records
    await bridge.close()
    assert [fields for ref_id, fields in link.records if ref_id == ref.id].count(
        {"outcome": "delivered", "delivered": False}) == 1  # recorded once, not again at hang-up






PASSED_ON = "已经帮你安排去联网查询OpenAI今年的最新技术进展了，目前正在后台检索整理中。查好之后我第一时间把结果告诉你。"
FOUND = "OpenAI近期的核心重心转向了测试时计算扩展与统一动态推理架构。"


async def test_a_result_that_only_passed_the_work_on_is_told_as_passed_on_and_the_report_answers_it(call):
    """Measured: told "the result has come", the front desk said "查好了" and made one up while the task still ran."""
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge, text="帮我联网查一下OpenAI今年的技术发展")
    link.finish("call-1", speech=PASSED_ON, running=[{"task_id": "task-7", "title": "查询OpenAI最新技术发展"}])
    await drain()
    [(_, note)] = provider.commands("note")
    assert note_results(note)[0]["status"] == "running"
    assert note_results(note)[0]["facts"] == PASSED_ON
    # Nothing was found yet, whatever the reply's wording: never the "result has come" request.
    assert provider.response_purposes()[-1] == ("create", phrases.notice_instructions("zh"))
    await replay(bridge, item("note-1", text=note), started("tell"), audio("tell"), done("tell"))
    assert not bridge.deliveries

    # The task's report, when it comes in the call, is the answer to that request, not news nobody asked for.
    await bridge.report("查询OpenAI最新技术发展", FOUND, "inbox-9", "task-7")
    [(_, answer)] = provider.commands("note")[1:]
    assert note_results(answer)[0]["facts"] == FOUND
    assert note_results(answer)[0]["request"] == "帮我联网查一下OpenAI今年的技术发展"
    assert provider.response_purposes()[-1] == ("create", phrases.delivery_instructions(FOUND, "zh"))
    await replay(bridge, item("note-2", text=answer), started("tell2"), audio("tell2"), done("tell2"))
    # Told once: the same task reporting again is news again.
    await bridge.report("查询OpenAI最新技术发展", FOUND, "inbox-10", "task-7")
    assert note_results(provider.commands("note")[-1][1])[0]["task"] == "查询OpenAI最新技术发展"


async def test_a_result_passed_on_is_told_alone(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, event("user_started"))  # the user is talking: both wait
    link.finish("call-1", speech=PASSED_ON, running=[{"task_id": "task-7", "title": ""}])
    await bridge.report("制作iPhone 18口播视频", REPORT, "inbox-9")
    await drain()
    await replay(bridge, event("user_stopped", invalid=True))
    [(_, note)] = provider.commands("note")
    assert note_results(note)[0]["status"] == "running" and len(note_results(note)) == 1  # no title: still said as passed on


async def test_results_waiting_together_are_told_in_one_reply(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, event("user_started"))  # the user is talking: nothing can be said yet
    link.finish("call-1", speech=RESULT)
    await bridge.report("制作iPhone 18口播视频", REPORT, "inbox-9")
    await drain()
    assert not provider.commands("note")
    await replay(bridge, event("user_stopped", invalid=True))
    [(_, note)] = provider.commands("note")
    assert [r["facts"] for r in note_results(note)] == [RESULT, REPORT]
    [(_, instructions)] = [command for command in provider.response_purposes() if command[1]]
    assert "先回答当前事项" in instructions
    # Not heard (cut off): both wait for the next quiet moment, still together.
    await replay(bridge, item("note-1", text=note), started("tell"), event("user_started"),
                 done("tell", status="cancelled"))
    assert [ref.delivery for ref in bridge.deliveries] == ["queued", "queued"]
    await replay(bridge, event("user_stopped", invalid=True))
    assert len(provider.commands("note")) == 2 and len(note_results(provider.commands("note")[-1][1])) == 2
    await replay(bridge, item("note-2", text=provider.commands("note")[-1][1]), started("again"), audio("again"),
                 done("again"))
    assert not bridge.deliveries
    assert states(outbox(bridge))[-1] == "delivered"  # the user's own turn is reported as told
    assert [fields.get("outcome") for _, fields in link.records if "outcome" in fields] == ["delivered"]
