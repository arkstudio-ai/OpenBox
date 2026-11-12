"""voice.dispatch_mode=server, the rest of M1 (docs/VOICE_DISPATCH_PLAN_20261112.md §4.2, §4.3, §4.8):
status from one verified line, the opened task card said once, the conclusion first, a change bound to its card."""
import json

from tests.support.voice_fakes import audio, done, event, item, started, transcript
from tests.unit.test_voice_cards import FORM
from tests.unit.test_voice_dispatch_route import routed
from tests.unit.test_voice_routing import greeted, make, replay
from voice import facts, handoff, phrases
from voice.control_notes import created_text


def line(**given):
    base = dict(lang="zh", status="ok", reason="ok", recorded=True, submissions=[], running=None)
    return facts.line(**{**base, **given})


def test_the_fact_line_says_what_the_rows_say_and_nothing_the_prose_says():
    assert line() == ("这一轮没有交办任何任务，下面是助理的回答。", False)
    said, tasks = line(submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "accepted", "outcome": None}])
    assert tasks and said == "任务单：「酒吧视频」已交给帮手，排着队还没开始。"
    assert "在做" in line(submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "claimed"}])[0]
    assert "等你" in line(submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "accepted",
                                       "blocked_by": {"id": "q1"}}])[0]
    assert "停了" in line(submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "canceled"}])[0]
    assert "出错了" in line(submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "settled",
                                        "outcome": "error"}])[0]
    # A task the turn gave work to and its receipt name the same task once.
    said, _ = line(reason="running", submissions=[{"task_id": "t1", "title": "酒吧视频", "state": "claimed"}],
                   running=[{"task_id": "t1", "title": "酒吧视频", "state": "running"}])
    assert said.count("酒吧视频") == 1
    assert line(submissions=None)[0].startswith("交办情况没能核实")
    assert line(light_redirect=True)[0].startswith("没有交办")
    assert line(status="timeout", reason="timeout")[0].startswith("这一次没有拿到回复")
    assert line(recorded=False, report="酒吧视频", submissions=None) == ("「酒吧视频」帮手回报了，现在没有在做。", False)
    assert line(lang="en", submissions=[{"task_id": "t1", "title": "Bar video", "state": "claimed"}])[0] == \
        "Tasks: Bar video: in progress."


def notes(provider):
    return [json.loads(text[text.index("{"):]) for _, text in provider.commands("note")]


async def handed(words="帮我做一条酒吧视频"):
    bridge, provider, link, _ = routed({words: True})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript(words, "u1"), event("user_stopped"),
                 started("r1"), audio("r1"), done("r1"))  # the front desk's own short reply
    assert len(link.started) == 1
    return bridge, provider, link


async def test_a_receipt_is_told_from_the_fact_line_with_the_assistants_words_as_reference():
    bridge, provider, link = await handed()
    link.finish(link.started[0].provider_call_id, speech="好的，我已经全部安排好了，马上就能出片。",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "queued"}])
    await replay(bridge)
    result = notes(provider)[-1]["results"][0]
    assert list(result)[:2] == ["source", "say"]  # the verified line comes first
    assert result["say"] == "任务单：「酒吧视频」已交给帮手，排着队还没开始。"
    assert result["reference"].startswith("好的，我已经全部安排好了") and "answer" not in result
    assert "task_submission_facts" not in result and "state_note" not in result
    instructions = provider.commands("create")[-1][1]
    assert "只按它说" in instructions and "先说状态和一句结论" in instructions
    await bridge.close()


async def test_a_plain_answer_keeps_the_assistants_words_as_the_answer():
    bridge, provider, link = await handed("帮我查一下上海明天的天气")
    link.started[0].submissions = []
    link.finish(link.started[0].provider_call_id, speech="上海明天多云，十二到十八度。")
    await replay(bridge)
    result = notes(provider)[-1]["results"][0]
    assert result["answer"] == "上海明天多云，十二到十八度。" and "这一轮没有交办任何任务" in result["say"]
    await bridge.close()


async def test_a_user_who_chose_detail_is_not_told_to_keep_to_the_conclusion():
    assert "细节等用户追问再讲" in phrases.fact_delivery("zh")
    assert "细节等用户追问再讲" not in phrases.fact_delivery("zh", detailed=True)
    assert "details wait" not in phrases.fact_delivery("en", detailed=True)
    # A call on model dispatch is told exactly as before.
    assert phrases.delivery_instructions("x", "zh") == phrases._delivery("zh")
    assert phrases.card_instructions("zh").startswith(phrases._delivery("zh"))


async def test_the_opened_task_card_is_told_while_the_request_is_still_with_the_assistant():
    bridge, provider, link = await handed()
    await bridge.control_note("t1", "酒吧视频", created_text("酒吧视频", "running", "zh"), 1, kind="created")
    await replay(bridge)
    result = notes(provider)[-1]["results"][0]
    assert result["say"] == "任务单「酒吧视频」开好了，帮手在做。" and "reference" not in result
    assert "酒吧视频" in bridge._handoff_facts()  # "is it being done?" is now answered yes, from a row
    assert bridge._ack_at is None
    # The same card again, or one opened elsewhere with nothing of this call pending, is not announced.
    await bridge.control_note("t1", "酒吧视频", "重复", 1, kind="created")
    await replay(bridge)
    assert len(provider.commands("note")) == 1
    await bridge.close()


async def test_a_card_opened_elsewhere_is_not_announced_as_this_calls_work():
    bridge, provider, link, _ = routed({})
    await greeted(bridge, provider)
    await bridge.control_note("t9", "别处开的", created_text("别处开的", "queued", "zh"), 1, kind="created")
    assert not bridge.deliveries and "别处开的" not in bridge._handoff_facts()
    await bridge.close()


async def test_the_receipt_after_the_announcement_is_not_said_again_unless_it_asks_something():
    bridge, provider, link = await handed()
    await bridge.control_note("t1", "酒吧视频", created_text("酒吧视频", "running", "zh"), 1, kind="created")
    await replay(bridge, started("d1"), audio("d1"), done("d1"))
    told = len(provider.commands("note"))
    link.finish(link.started[0].provider_call_id, speech="已经交给帮手在做了。",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "running"}])
    await replay(bridge)
    assert len(provider.commands("note")) == told and not bridge.deliveries
    assert link.records[-1] == (link.started[0].id, {"outcome": "delivered", "delivered": False})
    await bridge.close()

    bridge, provider, link = await handed()
    await bridge.control_note("t1", "酒吧视频", created_text("酒吧视频", "running", "zh"), 1, kind="created")
    await replay(bridge, started("d1"), audio("d1"), done("d1"))
    told = len(provider.commands("note"))
    link.finish(link.started[0].provider_call_id, speech="帮手在做了。你要横版还是竖版？",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "running"}])
    await replay(bridge)
    assert len(provider.commands("note")) == told + 1
    await bridge.close()


async def test_an_announcement_still_waiting_gives_way_to_the_receipt():
    bridge, provider, link = await handed()
    bridge.state = "user_speaking"
    await bridge.control_note("t1", "酒吧视频", created_text("酒吧视频", "queued", "zh"), 1, kind="created")
    assert [ref.reason for ref in bridge.deliveries] == ["control"]
    link.finish(link.started[0].provider_call_id, speech="交给帮手了。",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "queued"}])
    await replay(bridge)
    assert [ref.reason for ref in bridge.deliveries] == ["running"]
    await bridge.close()


def test_created_text_follows_the_tasks_own_state():
    assert created_text("酒吧视频", "queued", "zh") == "任务单「酒吧视频」开好了，排着队，还没开始。"
    assert created_text("酒吧视频", "waiting_input", "zh").endswith("在等你。")
    assert "is open" in created_text("Bar video", "running", "en")


async def route_with(monkeypatch, answer, questions):
    seen = {}

    async def complete(model, system, text, timeout):
        seen.update(payload=json.loads(text))
        return answer

    monkeypatch.setattr("voice.summary.complete", complete)
    return await handoff.route("可以的，但是换成专业档来做", "", (), (), questions=questions), seen


async def test_a_change_to_a_shown_task_card_is_routed_as_its_revision(monkeypatch):
    shown = ({"request_id": "q-1", "conversation": "酒吧视频", "questions": [
        {"question": "按这个方案做吗", "options": ["确认", "取消"]}]},)
    result, seen = await route_with(monkeypatch, '{"route":"dispatch","card":"q-1"}', shown)
    assert result == handoff.Revision("q-1")
    assert seen["payload"]["open_cards"] == [{"card": "q-1", "title": "酒吧视频", "questions": [
        {"question": "按这个方案做吗", "options": ["确认", "取消"]}]}]
    # Work that names no shown card, or a card the call never showed, is a plain handover.
    assert (await route_with(monkeypatch, '{"route":"dispatch","card":""}', shown))[0] is True
    assert (await route_with(monkeypatch, '{"route":"dispatch","card":"q-9"}', shown))[0] is True
    # A choice on a task card is the front desk's own task_question_answer: never forwarded as a request.
    assert (await route_with(monkeypatch, '{"route":"card","card":"q-1"}', shown))[0] is False


async def test_the_server_hands_a_card_change_over_bound_to_that_card():
    words = "可以的，但是换成专业档来做"
    bridge, provider, link, _ = routed({words: handoff.Revision(FORM["id"])})
    await greeted(bridge, provider)
    bridge.desk.show_question(FORM, "zh")
    await replay(bridge, event("user_started"), item("u1"), transcript(words, "u1"), event("user_stopped"))
    context = link.started[0].context
    assert context["revision_request"]["request_id"] == FORM["id"]
    assert [q["request_id"] for q in context["task_questions"]] == [FORM["id"]]
    assert FORM["id"] in bridge._revisions_in_flight()
    await bridge.close()


async def test_a_change_said_before_the_card_was_shown_is_a_plain_handover():
    words = "换成专业档来做"
    bridge, provider, link, _ = routed({words: handoff.Revision("gone")})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript(words, "u1"), event("user_stopped"))
    assert len(link.started) == 1 and "revision_request" not in link.started[0].context
    await bridge.close()


async def test_model_dispatch_notes_are_unchanged():
    bridge, provider, link = make()
    assert not bridge.server_dispatch
    await bridge.control_note("t1", "酒吧视频", "x", 1, kind="created")  # no pending request: nothing
    assert not bridge.deliveries
    await bridge.close()


async def test_a_yes_after_a_home_entry_reply_carries_the_request_it_agrees_to():
    bridge, provider, link, _ = routed({"帮我做一条酒吧视频": True, "要": True, "再做张海报": True})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("帮我做一条酒吧视频", "u1"),
                 event("user_stopped"), started("r1"), audio("r1"), done("r1"))
    link.finish(link.started[0].provider_call_id, speech="可以到首页做视频入口来做。", light_redirect=True)
    await replay(bridge, started("r2"), audio("r2"), done("r2"))
    await replay(bridge, event("user_started"), item("u2"), transcript("要", "u2"), event("user_stopped"),
                 started("r3"), audio("r3"), done("r3"))
    yes = link.started[1]
    assert yes.transcript == "要"  # the conversation shows what the user said
    assert "帮我做一条酒吧视频" in yes.text and "直接" in yes.text
    link.finish(yes.provider_call_id, speech="好的。", running=[{"task_id": "t1", "title": "酒吧视频", "state": "queued"}])
    await replay(bridge, started("r4"), audio("r4"), done("r4"))
    await replay(bridge, event("user_started"), item("u3"), transcript("再做张海报", "u3"), event("user_stopped"))
    assert link.started[2].text == "再做张海报"  # only the words right after the entry reply carry it

