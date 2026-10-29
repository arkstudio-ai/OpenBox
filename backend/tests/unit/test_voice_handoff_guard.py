"""Missed handoffs are repaired once, from the exact human utterance."""
import asyncio
import json

import pytest

from tests.support.voice_fakes import audio, done, drain, event, item, started, tool_call, transcript
from tests.unit.test_voice_routing import greeted, make, replay, said, utterance
from voice.handoff import HandoffGuard, required


def guarded(checker):
    bridge, provider, link = make()
    bridge.handoff = HandoffGuard(bridge, checker)
    return bridge, provider, link


async def needs_handoff(words, context, *request_words):
    return True


@pytest.mark.parametrize("status,applied,expected", [
    ("conflict", False, 1), ("target_mismatch", False, 1), ("invalid_arguments", False, 1),
    ("decision_recorded", True, 0), ("effect_unknown", False, 0), ("unavailable", False, 0),
])
async def test_failed_card_attempt_does_not_swallow_explicit_task_request(monkeypatch, status, applied, expected):
    bridge, provider, link = guarded(needs_handoff)
    async def result(*args):
        return {"status": status, "this_call_applied": applied}
    monkeypatch.setattr("voice.tools.run", result)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("水果店方案我确认了，直接开始写完整稿。", "u1"),
        started("r1"), tool_call("choose", name="task_question_answer", arguments="{}", response_id="r1"),
        said("好的", "r1"), audio("r1"), done("r1"))
    assert len(link.started) == expected
    if expected:
        assert link.started[0].transcript == "水果店方案我确认了，直接开始写完整稿。"
        await bridge.fill_idle()
        assert len(link.started) == 1
    await bridge.close()


async def test_reading_old_status_does_not_satisfy_a_modification(monkeypatch):
    bridge, provider, link = guarded(needs_handoff)
    async def overview(*args):
        return {"status": "ok", "tasks": [{"title": "海鲜视频", "state": "completed"}]}
    monkeypatch.setattr("voice.tools.run", overview)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("每样海鲜分开砂锅，最后加全家福，尽快提交", "u1"),
                 started("r1"), tool_call("read", name="tasks_overview", arguments="{}", response_id="r1"),
                 said("旧视频已经完成，我这就安排修改，还有什么补充吗？", "r1"), audio("r1"), done("r1"))
    if bridge.requested == "followup":  # the read can finish before the async intent check
        await replay(bridge, started("read-answer"), said("旧任务已经完成", "read-answer"), done("read-answer"))
    assert not any("handoff_retry" in str(command) for command in provider.sent)
    assert len(link.started) == 1
    assert link.started[0].transcript == "每样海鲜分开砂锅，最后加全家福，尽快提交"
    await bridge.close()


async def test_one_repair_and_late_native_tool_calls_share_the_same_request():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await utterance(bridge, "按刚才的新方案修改视频并提交", "我这就安排")
    assert len(link.started) == 1 and not provider.commands("create")
    await replay(bridge, tool_call("a", response_id="r1"), tool_call("b", response_id="r1"))
    assert len(link.started) == 1
    assert provider.commands("output")[-1][2]["request_id"] == link.started[0].id
    await bridge.fill_idle()
    # Acknowledge the accepted input, not the obsolete read/decision state;
    # never create a second business handover or a repair generation.
    assert bridge.requested == "progress"
    assert len(provider.commands("create")) == 1 and len(link.started) == 1
    await bridge.close()


@pytest.mark.parametrize("already_started", [True, False])
async def test_recovered_request_invalidates_an_older_read_reply(already_started):
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("现在按方案开始制作", "u1"))
    bridge.requested = "followup"
    if already_started:
        await replay(bridge, started("old-read"))
    # The semantic check completes after a reply to the old read was requested.
    bridge.handoff.required.add("u1")
    assert await bridge._recover_handoff("u1")
    if not already_started:
        await replay(bridge, started("old-read"))
    assert len(link.started) == 1 and provider.commands("cancel")
    await replay(bridge, said("暂时不制作", "old-read"), audio("old-read"),
                 done("old-read", status="cancelled"))
    assert "暂时不制作" not in " ".join(line.text for line in bridge.spoken.lines)
    assert bridge.requested == "progress"
    assert "原话已保存" in provider.commands("create")[-1][1]
    await bridge.close()


async def test_native_handoff_does_not_run_a_second_semantic_check():
    async def should_not_check(*args):
        raise AssertionError("a native handoff already exists")
    bridge, provider, link = guarded(should_not_check)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("修改视频", "u1"), started("r1"),
                 tool_call("a", response_id="r1"), done("r1"))
    assert len(link.started) == 1 and not provider.commands("create")
    await bridge.close()


@pytest.mark.parametrize("words", ["你说的独立砂锅是什么意思？", "先讨论方案，别开始做", "假如我要发视频呢？", "帮我看一下旧任务进度"])
async def test_conversation_and_read_requests_are_not_turned_into_execution(words):
    seen = []
    async def discuss(original, context):
        seen.append(original)
        return False
    bridge, provider, link = guarded(discuss)
    await greeted(bridge, provider)
    await utterance(bridge, words, "我马上安排")
    assert seen == [words] and not link.started and not provider.commands("create")
    await bridge.close()


async def test_recovery_hands_over_exact_input_once_without_an_extra_generated_reply():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await utterance(bridge, "立即按新方案重做", "我这就安排")
    assert len(link.started) == 1 and link.started[0].text == "立即按新方案重做"
    assert link.started[0].context["handover_source"] == "verified_missing_handoff"
    assert "revision_request" not in link.started[0].context
    await bridge._recover_handoff("u-1")
    assert len(link.started) == 1
    assert not provider.commands("create")
    await bridge.close()


async def test_late_invalid_native_arguments_cannot_replace_recovered_human_input():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await utterance(bridge, "按新方案修改", "我这就安排")
    await replay(bridge, tool_call("bad", arguments="{}", response_id="r1"))
    assert provider.commands("output")[-1][2]["status"] == "invalid_arguments"
    assert len(link.started) == 1 and link.started[0].text == "按新方案修改"
    await bridge.close()


async def test_new_human_speech_supersedes_a_slow_check():
    release = asyncio.Event()
    async def slow(*args):
        await release.wait()
        return True
    bridge, provider, link = guarded(slow)
    await greeted(bridge, provider)
    await utterance(bridge, "马上提交", "好的")
    await replay(bridge, event("user_started", item_id="u2"), item("u2"), transcript("先别提交了", "u2"))
    release.set()
    await drain()
    assert not provider.commands("create") and not link.started
    await bridge.close()




async def test_unverified_speech_cannot_use_raw_input_recovery():
    async def discussion(*args):
        return False
    bridge, provider, link = guarded(discussion)
    await greeted(bridge, provider)
    await utterance(bridge, "先聊聊，不开始做", "明白")
    assert not await bridge._recover_handoff("u-1")
    assert not link.started
    await bridge.close()


async def test_transcript_arriving_after_the_reply_is_checked_once():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), started("r1"), said("我这就安排", "r1"), audio("r1"), done("r1"))
    assert not provider.commands("create")
    await replay(bridge, transcript("按刚才方案立即修改", "u1"))
    assert len(link.started) == 1 and not provider.commands("create")
    await replay(bridge, transcript("按刚才方案立即修改", "u1"))
    assert len(link.started) == 1 and not provider.commands("create")
    await bridge.close()


async def test_check_failure_does_not_invent_submission_or_interrupt_an_answer():
    async def unavailable(*args):
        raise TimeoutError("check unavailable")
    bridge, provider, link = guarded(unavailable)
    await greeted(bridge, provider)
    await utterance(bridge, "尽快提交修改", "我这就安排")
    assert not provider.commands("create") and not link.started
    await bridge.close()


@pytest.mark.parametrize("read_status", ["ok", "none", "unavailable", "restricted"])
async def test_check_timeout_does_not_reannounce_a_completed_card_read(monkeypatch, read_status):
    async def unavailable(*args):
        raise TimeoutError("check unavailable")
    release = asyncio.Event()
    async def read(*args):
        await release.wait()
        return {"status": read_status, "handled_decisions": (
            [{"request_id": "chosen", "answers": [["周末解压"]]}] if read_status == "ok" else [])}
    monkeypatch.setattr("voice.tools.run", read)
    bridge, provider, link = guarded(unavailable)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("刚才选的是哪个？", "u1"), started("lookup"),
                 tool_call("read", name="cards_pending", arguments="{}", response_id="lookup"), done("lookup"))
    release.set()
    async with asyncio.timeout(2):
        while bridge._direct:
            await asyncio.sleep(.001)
    assert bridge.requested == "followup"
    await replay(bridge, started("answer"), said("您选的是周末解压", "answer"), audio("answer"), done("answer"))
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1 and not link.started
    assert bridge.handoff.pending is None
    await bridge.close()


async def test_slow_checker_is_bounded_and_does_not_dispatch(monkeypatch):
    monkeypatch.setattr("voice.handoff.TIMEOUT_SECONDS", 0.01)
    async def slow(*args):
        await asyncio.Event().wait()
    bridge, provider, link = guarded(slow)
    await greeted(bridge, provider)
    await utterance(bridge, "尽快提交修改", "我这就安排")
    await asyncio.sleep(0.03)
    assert not provider.commands("create") and not link.started
    await bridge.close()


async def test_verified_recovery_failure_is_announced_once_without_retrying(monkeypatch):
    bridge, provider, link = guarded(needs_handoff)
    attempts = []
    async def unavailable(ref):
        attempts.append(ref)
        raise RuntimeError("unavailable")
    monkeypatch.setattr(link, "start", unavailable)
    await greeted(bridge, provider)
    await utterance(bridge, "尽快提交修改", "我这就安排")
    assert bridge.requested == "handoff_failed:u-1" and not link.started
    await replay(bridge, started("failure"), said("这次要求还没提交成功", "failure"), audio("failure"), done("failure"))
    await bridge.fill_idle()
    assert len(attempts) == 1 and len(provider.commands("create")) == 1
    await bridge.close()


async def test_semantic_check_uses_human_words_not_the_assistants_promise(monkeypatch):
    seen = []
    async def complete(model, instructions, body, timeout):
        seen.append((instructions, body, timeout))
        return json.dumps({"decision": "direct", "operation": "none", "authorization": ""})
    monkeypatch.setattr("voice.summary.complete", complete)
    assert not await required("先讨论，不要执行", "assistant: 我已经提交了")
    assert "先讨论，不要执行" in seen[0][1] and "前台承诺不是授权" in seen[0][0]


@pytest.mark.parametrize("answer", [
    "required", "[]", "null",
    '{"decision":"handoff","operation":"artifact","authorization":"我已交办"}',
    '{"decision":"handoff","operation":"none","authorization":"制作视频"}',
    '{"decision":"handoff","operation":"artifact","authorization":""}',
    '{"decision":"direct","operation":"artifact","authorization":"制作视频"}',
])
async def test_semantic_check_rejects_unbound_or_invalid_authorization(monkeypatch, answer):
    async def complete(*args):
        return answer
    monkeypatch.setattr("voice.summary.complete", complete)
    with pytest.raises(ValueError):
        await required("用那张图制作视频，先让我看方案", "assistant: 我已交办")


async def test_semantic_check_accepts_work_bound_to_original_user_words(monkeypatch):
    async def complete(*args):
        return json.dumps({"decision": "handoff", "operation": "artifact", "authorization": "用那张图制作视频"})
    monkeypatch.setattr("voice.summary.complete", complete)
    assert await required("用那张图制作视频，先让我看方案", "assistant: 我已交办")


async def test_contextual_recovery_keeps_raw_words_and_complete_request(monkeypatch):
    request = "Continue both the opening video and the abstract short, retaining their confirmed choices."
    async def complete(*args):
        return json.dumps({"decision": "handoff", "operation": "task_change",
            "authorization": "都接着做吧", "request": request})
    monkeypatch.setattr("voice.summary.complete", complete)
    bridge, provider, link = guarded(required)
    await greeted(bridge, provider)
    await utterance(bridge, "都接着做吧", "我来安排")
    assert len(link.started) == 1
    assert link.started[0].transcript == "都接着做吧"
    assert link.started[0].text == request
    await bridge.close()


async def test_one_repair_and_duplicate_tool_calls_share_the_same_request():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await utterance(bridge, "按刚才的新方案修改视频并提交", "我这就安排")
    assert len(link.started) == 1
    await replay(bridge, started("late-native"), tool_call("a", response_id="late-native"),
                 tool_call("b", response_id="late-native"), done("late-native"))
    assert len(link.started) == 1
    assert provider.commands("output")[-1][2]["request_id"] == link.started[0].id
    assert provider.commands("output")[-1][2]["duplicate"] is True
    await bridge.close()


async def test_failed_repair_reports_unsubmitted_without_looping_or_dispatching():
    bridge, provider, link = guarded(needs_handoff)
    link.fail_start = True
    await greeted(bridge, provider)
    await utterance(bridge, "立即按新方案重做", "我这就安排")
    assert bridge.requested == "handoff_failed:u-1"
    assert "尚未确认提交成功" in provider.commands("create")[-1][1]
    await replay(bridge, started("failed"), tool_call("bad", response_id="failed"), done("failed"))
    assert provider.commands("output")[-1][2]["status"] == "not_user_request"
    assert len(provider.commands("create")) == 1 and not link.started
    await bridge.close()


async def test_recovery_preserves_the_original_requirements_in_context():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await replay(bridge, item("u0"), transcript("分两段各15秒，中文，无背景音乐，快速档", "u0"))
    await replay(bridge, item("u1"), transcript("按刚才方案立即开始", "u1"), started("reply"),
                 said("好的", "reply"), audio("reply"), done("reply"))
    assert len(link.started) == 1
    assert "分两段各15秒" in str(link.started[0].context["call"])
    assert link.started[0].text == "按刚才方案立即开始"
    await bridge.close()


async def test_new_speech_blocks_recovery_waiting_for_the_business_lock():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await bridge._business_lock.acquire()
    try:
        await utterance(bridge, "马上提交", "好的")
        await replay(bridge, event("user_started", item_id="u2"), item("u2"), transcript("先别提交", "u2"))
    finally:
        bridge._business_lock.release()
    await drain()
    assert not link.started
    await bridge.close()


async def test_check_failure_does_not_reopen_an_answered_question_or_infer_a_handoff():
    async def unavailable(*args):
        raise TimeoutError("check unavailable")
    bridge, provider, link = guarded(unavailable)
    await greeted(bridge, provider)
    await utterance(bridge, "现在任务进展怎么样了？", "分镜图片已生成，视频还在制作。")
    assert not bridge.requested and not link.started and not provider.commands("create")
    await bridge.fill_idle()
    assert not provider.commands("create")
    await bridge.close()


async def test_recovery_does_not_wait_for_spoken_playback_or_another_provider_reply():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("尽快提交修改", "u1"), started("r1"))
    bridge._playbacks["blocked"] = {"until": bridge.clock() + 900, "ack": True, "refs": []}
    await replay(bridge, said("我这就安排", "r1"), audio("r1"), done("r1"))
    assert len(link.started) == 1 and not provider.commands("create")
    await bridge.fill_idle()
    assert len(link.started) == 1
    await bridge.close()


async def test_speech_started_without_an_item_id_supersedes_a_slow_check():
    release = asyncio.Event()
    async def slow(*args):
        await release.wait()
        return True
    bridge, provider, link = guarded(slow)
    await greeted(bridge, provider)
    await utterance(bridge, '马上提交', '好的')
    await replay(bridge, event('user_started'))
    release.set()
    await drain()
    assert not link.started
    await bridge.close()


async def test_a_request_spread_over_utterances_is_checked_and_recovered_with_all_its_words():
    # Measured 2026-10-26: "make me another short video" ... "you decide the
    # rest" got a spoken promise, no tool call, and no task.
    seen = []
    async def checker(words, context, request_words=()):
        seen.append((words, tuple(request_words)))
        return words.endswith("你定吧")  # the first turn was a clarifying question: direct
    bridge, provider, link = guarded(checker)
    await greeted(bridge, provider)
    await replay(bridge, item("u0"), transcript("再帮我做个五十秒左右的抽象风格短视频", "u0"), started("ask"),
                 said("想要什么主题？", "ask"), audio("ask"), done("ask"))
    seen.clear()
    await replay(bridge, item("u1"), transcript("颜色流动的就行，你定吧", "u1"), started("promise"),
                 said("好的，我来做", "promise"), audio("promise"), done("promise"))
    await drain()
    assert seen == [("颜色流动的就行，你定吧", ("再帮我做个五十秒左右的抽象风格短视频",))]
    [ref] = link.started
    assert ref.text == "颜色流动的就行，你定吧"
    assert ref.context["user_words"] == ["再帮我做个五十秒左右的抽象风格短视频", "颜色流动的就行，你定吧"]
    await bridge.close()


async def test_semantic_check_binds_authorization_to_an_earlier_utterance_of_the_request(monkeypatch):
    bodies = []
    async def complete(model, instructions, body, timeout):
        bodies.append(json.loads(body))
        return json.dumps({"decision": "handoff", "operation": "artifact", "authorization": "做个五十秒的抽象短视频"})
    monkeypatch.setattr("voice.summary.complete", complete)
    assert await required("你定吧", "assistant: 想要什么主题？", ("帮我做个五十秒的抽象短视频",))
    assert bodies[0]["same_request_user_words"] == ["帮我做个五十秒的抽象短视频"]
    # Words that only the front desk said still never authorize anything.
    with pytest.raises(ValueError):
        await required("你定吧", "assistant: 我来做个五十秒的抽象短视频")


async def test_a_check_that_times_out_once_is_retried_while_the_utterance_is_current(monkeypatch):
    monkeypatch.setattr("voice.handoff.TIMEOUT_SECONDS", 0.05)
    calls = []
    async def checker(words, context, *request_words):
        calls.append(words)
        if len(calls) == 1:
            await asyncio.sleep(1)
        return True
    bridge, provider, link = guarded(checker)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("帮我做个短视频，现在就开始", "u1"), started("promise"),
                 said("好的", "promise"), audio("promise"), done("promise"))
    for _ in range(40):
        if link.started:
            break
        await asyncio.sleep(0.02)
    assert len(calls) == 2 and len(link.started) == 1
    await bridge.close()


async def test_a_check_on_a_dropped_pooled_connection_is_retried_once():
    import httpx
    calls = []
    async def checker(words, context, *request_words):
        calls.append(words)
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return True
    bridge, provider, link = guarded(checker)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("帮我做个短视频，现在就开始", "u1"), started("promise"),
                 said("好的", "promise"), audio("promise"), done("promise"))
    for _ in range(40):
        if link.started:
            break
        await asyncio.sleep(0.02)
    assert len(calls) == 2 and len(link.started) == 1
    await bridge.close()


async def test_semantic_check_accepts_a_fenced_json_answer(monkeypatch):
    async def complete(*args):
        return '```json\n{"decision":"handoff","operation":"artifact","authorization":"制作视频"}\n```'
    monkeypatch.setattr("voice.summary.complete", complete)
    assert await required("用那张图制作视频", "assistant: 好的")


async def test_semantic_check_binding_ignores_punctuation_and_case_but_not_words(monkeypatch):
    answers = iter([
        '{"decision":"handoff","operation":"task_change","authorization":"just something colorful and flowing no pictures"}',
        '{"decision":"handoff","operation":"task_change","authorization":"something colorful with music"}',
    ])
    async def complete(*args):
        return next(answers)
    monkeypatch.setattr("voice.summary.complete", complete)
    words = "Just something colorful and flowing, no pictures. You decide the rest."
    assert await required(words, "assistant: Got it")
    with pytest.raises(ValueError):
        await required(words, "assistant: Got it")


SEND_CARD = {"card_id": "q-send", "kind": "assistant_confirm", "header": "确认修改任务",
             "prompt": "把这些修改发送到会话「春禾果店15秒竖屏宣传视频」吗？\\n\\n修改视频总时长为10秒。",
             "options": ["确认发送", "取消"], "created_at": "2026-10-27T03:04:20+00:00",
             "expires_at": "2026-10-27T03:14:20+00:00", "high_risk": True}


async def _told_card_then(checker, applies):
    """A card the call told, then the user's reply; the front desk claims a choice without cards_answer."""
    seen = {}
    async def answer(source, choice):
        seen[source] = choice
        return applies
    bridge, provider, link = guarded(checker)
    bridge._answer_card_for = answer
    await greeted(bridge, provider)
    bridge.desk.show(SEND_CARD)
    await replay(bridge, item("u1"), transcript("Yes, send that change.", "u1"), started("claim"),
                 said("I've confirmed the change.", "claim"), audio("claim"), done("claim"))
    for _ in range(40):
        if seen or link.started:
            break
        await asyncio.sleep(0.02)
    await drain()
    return bridge, link, seen


async def test_a_reply_to_a_told_card_is_applied_to_that_card_not_forwarded_as_a_new_request():
    calls = []
    async def checker(words, context, *request_words, cards=()):
        calls.append([card["card"] for card in cards])
        [card] = cards
        return {"card": card["card"], "version": card["version"], "option_id": card["options"][0]["id"]}
    bridge, link, seen = await _told_card_then(checker, applies=True)
    assert calls == [["1"]] and seen["u1"]["card"] == "1"  # the open card was offered to the check
    assert not link.started  # no second request, so no second confirmation
    assert "u1" in bridge.handoff.fulfilled
    await bridge.close()


async def test_a_card_that_no_longer_takes_the_choice_keeps_the_users_words_as_a_request():
    async def checker(words, context, *request_words, cards=()):
        [card] = cards
        return {"card": card["card"], "version": card["version"], "option_id": card["options"][0]["id"]}
    bridge, link, seen = await _told_card_then(checker, applies=False)
    assert seen and [ref.transcript for ref in link.started] == ["Yes, send that change."]
    await bridge.close()


async def test_the_card_check_binds_the_choice_to_the_users_words_and_the_shown_options(monkeypatch):
    shown = ({"card": "1", "title": "确认修改任务", "what": "修改视频总时长为10秒", "version": "v-1",
              "options": [{"id": "1", "label": "确认发送"}, {"id": "2", "label": "取消"}]},)
    answers = iter([
        '{"decision":"card","card":"1","option_id":"1","authorization":"yes send that change"}',
        '{"decision":"card","card":"1","option_id":"7","authorization":"yes send that change"}',
        '{"decision":"card","card":"1","option_id":"1","authorization":"I have confirmed the change"}',
    ])
    bodies = []
    async def complete(model, system, text, timeout):
        bodies.append(json.loads(text))
        return next(answers)
    monkeypatch.setattr("voice.summary.complete", complete)
    assert await required("Yes, send that change.", "assistant: ...", cards=shown) == {
        "card": "1", "version": "v-1", "option_id": "1"}
    assert bodies[0]["open_cards"][0]["options"][0] == {"id": "1", "label": "确认发送"}
    with pytest.raises(ValueError):  # an option the card never showed
        await required("Yes, send that change.", "assistant: ...", cards=shown)
    with pytest.raises(ValueError):  # the front desk's own claim is no authorization
        await required("Yes, send that change.", "assistant: I have confirmed the change", cards=shown)


async def test_a_card_choice_from_the_check_is_applied_by_the_cards_answer_tool_with_the_users_words(monkeypatch):
    from voice.tools import CallScope
    calls = []
    async def run(name, scope, arguments):
        calls.append((name, scope.transcript, scope.desk is bridge.desk, json.loads(arguments)))
        return {"status": "decision_recorded", "this_call_applied": True}
    monkeypatch.setattr("voice.tools.run", run)
    bridge, provider, link = guarded(needs_handoff)
    bridge.scope = CallScope(user_id="owner", workspace_id="workspace", main_session_id="main", call_id="call")
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("Yes, send that change.", "u1"))
    choice = {"card": "1", "version": "v-1", "option_id": "1"}
    assert await bridge._answer_card_for("u1", choice)
    assert calls == [("cards_answer", "Yes, send that change.", True, choice)]
    await bridge.close()


async def test_the_check_never_declines_a_card_for_the_user(monkeypatch):
    """Measured 2026-10-27: "这个先放一放，我再想想" came back as the cancel option and withdrew a revision."""
    shown = ({"card": "1", "title": "确认修改任务", "what": "背景音乐换成轻快的钢琴曲", "version": "v-1",
              "options": [{"id": "option_0", "label": "确认发送"}, {"id": "option_1", "label": "取消"}]},)
    answers = iter(['{"decision":"card","card":"1","option_id":"option_1","authorization":"这个先放一放"}',
                    '{"decision":"card","card":"1","option_id":"option_1","authorization":"我没这么说"}'])
    async def complete(model, system, text, timeout):
        return next(answers)
    monkeypatch.setattr("voice.summary.complete", complete)
    assert await required("这个先放一放，我再想想。", "assistant: 要把修改发出去吗？", cards=shown) is False
    with pytest.raises(ValueError):  # an unbound choice is still refused, decline or not
        await required("这个先放一放，我再想想。", "assistant: 要把修改发出去吗？", cards=shown)


async def test_a_deferral_read_as_a_decline_leaves_the_told_card_waiting(monkeypatch):
    async def complete(model, system, text, timeout):
        [card] = json.loads(text)["open_cards"]
        return json.dumps({"decision": "card", "card": card["card"], "option_id": card["options"][-1]["id"],
                           "authorization": "Hold on"}, ensure_ascii=False)
    async def warm(model):
        return None
    monkeypatch.setattr("voice.summary.complete", complete)
    monkeypatch.setattr("voice.summary.warm", warm)
    seen = {}
    async def answer(source, choice):
        seen[source] = choice
        return True
    bridge, provider, link = guarded(required)
    bridge._answer_card_for = answer
    await greeted(bridge, provider)
    bridge.desk.show(SEND_CARD)
    await replay(bridge, item("u1"), transcript("Hold on, let me think about it.", "u1"), started("r1"),
                 said("Sure, I'll wait.", "r1"), audio("r1"), done("r1"))
    for _ in range(20):
        await asyncio.sleep(0.01)
    await drain()
    assert not seen and not link.started  # no card answered, nothing forwarded
    assert "u1" in bridge.handoff.checked and "u1" not in bridge.handoff.fulfilled
    await bridge.close()


async def test_a_card_answer_the_check_applied_is_followed_told_and_never_submitted_again(monkeypatch):
    """Review 2026-10-27: the check's card path skipped the turn bookkeeping. The confirmed card's resumed turn
    was never followed or told, and a late handover of the same words was not recognised as a duplicate."""
    from tests.unit.test_voice_cards import answering
    from voice.tools import CallScope
    replies = answering(monkeypatch, waiting=(SEND_CARD,))
    async def checker(words, context, *request_words, cards=()):
        [card] = cards
        return {"card": card["card"], "version": card["version"], "option_id": card["options"][0]["id"]}
    bridge, provider, link = guarded(checker)
    bridge.scope = CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x")
    await greeted(bridge, provider)
    bridge.desk.show(SEND_CARD)
    await replay(bridge, item("u1"), transcript("Yes, send that change.", "u1"), started("claim"),
                 said("I've confirmed the change.", "claim"), audio("claim"), done("claim"))
    for _ in range(50):
        if link.followed:
            break
        await asyncio.sleep(0.02)
    await drain()
    assert [request_id for request_id, *_ in replies] == ["q-send"] and not bridge.desk.answers
    [(follow, after)] = link.followed  # the confirmed card's resumed turn is followed...
    assert after == "inbox_0001" and follow.transcript == "Yes, send that change."
    link.finish(follow.provider_call_id, speech="修改已经发到「春禾果店15秒竖屏宣传视频」，正在重新处理。")
    for _ in range(50):
        if provider.commands("note"):
            break
        await asyncio.sleep(0.01)
    await bridge.fill_idle()
    await drain()
    assert any("正在重新处理" in note for _, note in provider.commands("note"))  # ...and its result told
    # A late handover of the same words is a duplicate, never a second submission.
    await replay(bridge, tool_call("late-ask", text="把修改发出去", response_id="claim"))
    assert not link.started
    [(_, _, output)] = [entry for entry in provider.commands("output") if entry[1] == "late-ask"]
    assert output["duplicate"] is True and output["this_call_applied"] is False
    await bridge.close()


async def test_words_already_acted_on_are_neither_answered_on_a_card_again_nor_forwarded(monkeypatch):
    from voice.tools import CallScope
    runs = []
    async def run(name, scope, arguments):
        runs.append(name)
        return {"status": "decision_recorded", "this_call_applied": True}
    monkeypatch.setattr("voice.tools.run", run)
    bridge, provider, link = guarded(needs_handoff)
    bridge.scope = CallScope(user_id="owner", workspace_id="workspace", main_session_id="main", call_id="call")
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("Yes, send that change.", "u1"))
    bridge._remember_spoken_action("u1", {"status": "accepted", "request_id": "turn-1"})
    assert await bridge._answer_card_for("u1", {"card": "1", "version": "v-1", "option_id": "1"}) is None
    assert runs == []
    await bridge.close()

    async def checker(words, context, *request_words, cards=()):
        [card] = cards
        return {"card": card["card"], "version": card["version"], "option_id": card["options"][0]["id"]}
    bridge, link, seen = await _told_card_then(checker, applies=None)
    assert seen and not link.started and "u1" not in bridge.handoff.required
    await bridge.close()


async def test_invalid_check_retries_and_forwards_the_human_request_only_once():
    calls = []
    async def checker(words, context, *args):
        calls.append(words)
        if len(calls) == 1:
            raise ValueError('unbound handoff authorization')
        return True
    bridge, provider, link = guarded(checker)
    await greeted(bridge, provider)
    await utterance(bridge, '刚才说错了，用灵活档做这个视频', '好的，我来安排')
    assert len(calls) == 2 and len(link.started) == 1
    assert link.started[0].transcript == '刚才说错了，用灵活档做这个视频'
    await bridge.fill_idle()
    assert len(link.started) == 1
    await bridge.close()


async def test_failed_validation_is_visible_but_never_authorizes_work():
    from tests.support.voice_fakes import outbox, of_type
    calls = []
    async def checker(*args):
        calls.append(1)
        raise ValueError('unknown_authorization_source')
    bridge, provider, link = guarded(checker)
    await greeted(bridge, provider)
    await utterance(bridge, '帮我做一份完整欢迎词文档', '我来安排')
    assert len(calls) == 2 and not link.started
    assert [e['code'] for e in of_type(outbox(bridge), 'notice')] == ['handoff_check_unavailable']
    assert not provider.commands('create')
    await bridge.close()


@pytest.mark.parametrize('source,earlier,accepted', [
    ('latest_user', (), True), ('prior_0', ('制作一个短视频',), True),
    ('prior_0', (), False), ('assistant', (), False), ('context', (), False),
])
async def test_authorization_source_resolves_only_to_permitted_human_words(monkeypatch, source, earlier, accepted):
    async def complete(*args):
        return json.dumps({'decision': 'handoff', 'operation': 'artifact',
                           'authorization_source': source, 'request': '按用户选定的档位制作短视频'})
    monkeypatch.setattr('voice.summary.complete', complete)
    if accepted:
        assert await required('用灵活档制作', '', earlier)
    else:
        with pytest.raises(ValueError, match='unknown_authorization_source'):
            await required('用灵活档制作', 'assistant: 制作一个短视频', earlier)
