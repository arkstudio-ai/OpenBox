"""Missed handoffs persist the original human input once without a second voice-model turn."""
import asyncio

import pytest

from tests.support.voice_fakes import audio, done, drain, event, item, started, tool_call, transcript
from tests.unit.test_voice_routing import greeted, make, replay, said, utterance
from voice.handoff import HandoffGuard, required


def guarded(checker):
    bridge, provider, link = make()
    bridge.handoff = HandoffGuard(bridge, checker)
    return bridge, provider, link


async def needs_handoff(words, context):
    return True


async def test_reading_old_status_does_not_satisfy_a_modification(monkeypatch):
    bridge, provider, link = guarded(needs_handoff)
    async def overview(*args):
        return {"status": "ok", "tasks": [{"title": "海鲜视频", "state": "completed"}]}
    monkeypatch.setattr("voice.tools.run", overview)
    await greeted(bridge, provider)
    words = "每样海鲜分开砂锅，最后加全家福，尽快提交"
    await replay(bridge, item("u1"), transcript(words, "u1"), started("r1"),
                 tool_call("read", name="tasks_overview", arguments="{}", response_id="r1"),
                 said("旧视频已完成，我这就安排修改", "r1"), audio("r1"), done("r1"))
    if bridge.requested == "followup":
        await replay(bridge, started("read-answer"), said("旧任务已经完成", "read-answer"), done("read-answer"))
    assert len(link.started) == 1
    assert link.started[0].text == link.started[0].transcript == words
    assert link.started[0].context["handover_source"] == "missed_handoff"
    assert bridge.requested != "handoff_retry:u1"
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

async def test_transcript_arriving_after_the_reply_is_checked_once():
    bridge, provider, link = guarded(needs_handoff)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), started("r1"), said("我这就安排", "r1"), audio("r1"), done("r1"))
    assert not link.started
    await replay(bridge, transcript("按刚才方案立即修改", "u1"))
    assert len(link.started) == 1
    await replay(bridge, transcript("按刚才方案立即修改", "u1"))
    assert len(link.started) == 1 and not provider.commands("create")
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


async def test_slow_checker_is_bounded_and_does_not_dispatch(monkeypatch):
    monkeypatch.setattr("voice.handoff.TIMEOUT_SECONDS", 0.01)
    async def slow(*args):
        await asyncio.Event().wait()
    bridge, provider, link = guarded(slow)
    await greeted(bridge, provider)
    await utterance(bridge, "尽快提交修改", "我这就安排")
    await asyncio.sleep(0.03)
    assert not bridge.requested and not link.started and not provider.commands("create")
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

async def test_semantic_check_uses_human_words_not_the_assistants_promise(monkeypatch):
    seen = []
    async def complete(model, instructions, body, timeout):
        seen.append((instructions, body, timeout))
        return "none"
    monkeypatch.setattr("voice.summary.complete", complete)
    assert not await required("先讨论，不要执行", "assistant: 我已经提交了")
    assert "先讨论，不要执行" in seen[0][1] and "前台承诺不是授权" in seen[0][0]


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
