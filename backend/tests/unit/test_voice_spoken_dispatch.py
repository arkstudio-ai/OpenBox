"""Replay natural duplicate routes from the ztqz production call."""
import asyncio
import json

from tests.support.voice_fakes import done, started, tool_call
from tests.unit.test_voice_cards import call, greeted, replay, said, FORM  # noqa: F401
from voice import tools


async def test_same_utterance_handoff_then_direct_answer_does_not_execute_twice(call, monkeypatch):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "重拍一二四五段，其他不改变，不要出现穿模。", "redo")
    dispatched = []
    async def direct(*args):
        dispatched.append(args)
        return {"status": "decision_recorded", "this_call_applied": True}
    monkeypatch.setattr(tools, "run", direct)
    await replay(bridge, started("main"), tool_call("handoff", arguments=json.dumps({
        "request": "重拍第1、2、4、5段，其他不变，避免穿模。"}), response_id="main"), done("main"))
    await bridge._complete_direct_call(tool_call("also-answer", name="task_question_answer",
        arguments=json.dumps({"request_id": "old-card"})))
    assert len(link.started) == 1 and not dispatched
    assert provider.commands("output")[-1][2]["duplicate"] is True
    # A second phrasing of the same original utterance must reuse its receipt.
    await bridge._ask(tool_call("duplicate-handoff", arguments=json.dumps({"request": "帮用户重做视频。"})))
    assert len(link.started) == 1
    await bridge.close()


async def test_native_answer_then_handoff_preserves_answer_without_new_job(call, monkeypatch):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "重拍一二四五段。", "redo")
    calls = []
    async def direct(*args):
        calls.append(args)
        await asyncio.sleep(.01)
        return {"status": "decision_recorded", "this_call_applied": True, "request_id": "old-card"}
    monkeypatch.setattr(tools, "run", direct)
    await bridge._complete_direct_call(tool_call("answer", name="task_question_answer",
        arguments=json.dumps({"request_id": "old-card"})))
    await bridge._ask(tool_call("also-handoff", arguments=json.dumps({"request": "重拍第1、2、4、5段。"})))
    assert len(calls) == 1 and not link.started
    assert provider.commands("output")[-1][2]["duplicate"] is True
    # New human input remains actionable; deduplication is not text matching.
    await said(bridge, "再做一个水果店视频。", "new-task")
    await bridge._ask(tool_call("new-handoff", arguments=json.dumps({"request": "再做一个水果店视频。"})))
    assert len(link.started) == 1
    await bridge.close()


async def test_normal_handoff_owns_target_card_while_other_cards_remain_actionable(call, monkeypatch):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    bridge.desk.show_question({**FORM, "version": "a" * 64}, "zh")
    await said(bridge, "重拍一二四五段。", "redo")
    await bridge._ask(tool_call("handoff", arguments=json.dumps({
        "request": "重拍第1、2、4、5段。", "question_id": "q-video"})))
    await said(bridge, "是的。", "yes")
    calls = []
    async def direct(name, scope, arguments):
        calls.append(json.loads(arguments)["request_id"])
        return {"status": "conflict"}
    monkeypatch.setattr(tools, "run", direct)
    for identity in ("q-video", "unrelated"):
        await bridge._complete_direct_call(tool_call("answer-" + identity, name="task_question_answer",
            arguments=json.dumps({"request_id": identity})))
    assert calls == ["unrelated"]
    assert len(link.started) == 1
    await bridge.close()
