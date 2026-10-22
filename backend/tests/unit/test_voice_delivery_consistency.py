"""Regressions for stale task speech, dual observers and interrupted playback.

Provider output is injected: these prove transport/SQL gates, not model prose.
"""
import asyncio
import json

import pytest

from tests.support.voice_fakes import FakeClock, FakeLink, ScriptedProvider, audio, done, event, item, started, tool_call
from voice import phrases
from voice.assistant_link import VoiceTurnRef
from voice.bridge import Bridge, REPLY_GAP_SECONDS
from voice.reports import Report
from voice.shared_context import SharedSnapshot
from voice.tools import CallScope


def make(*, ack=False, paced=False, reader=None):
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, clock=clock, instructions="Test", context_reader=reader,
        reply_gap=REPLY_GAP_SECONDS if paced else None,
        scope=CallScope(user_id="u", workspace_id="w", main_session_id="m", call_id="c"))
    bridge.greeting, bridge.playback_ack = False, ack
    return bridge, provider, link, clock


def facts(provider):
    return [entry["facts"] for _, note in provider.commands("note")
            for entry in json.loads(note.removeprefix(phrases.NOTE_PREFIX["zh"]))["results"]]


async def finish(bridge, provider, name="speech", size=4800):
    await bridge.on_provider_event(item("note-" + name, text=provider.commands("note")[-1][1]))
    await bridge.on_provider_event(started(name))
    await bridge.on_provider_event(audio(name, size))
    await bridge.on_provider_event(done(name))


def snapshot(state="running", revision=1, result="result-1", sequence=0):
    return SharedSnapshot("Current work", [{"session_id": "s", "task_id": "task", "state": state,
        "intent_revision": revision, "result_id": result, "task_version": f"{revision}:{state}",
        "version": f"{revision}:{state}:{result}"}], sequence)


@pytest.mark.parametrize("arrival", ["follow_first", "report_first", "both_waiting"])
async def test_same_inbox_enters_speech_once_across_observers(arrival):
    bridge, provider, link, _ = make()
    ref = VoiceTurnRef("follow", "follow", "确认", "第一个", 1000, inbox_id="same-inbox")
    if arrival == "both_waiting":
        bridge.state = "user_speaking"
    if arrival == "report_first":
        await bridge.report("", "已确认，开始执行。", "same-inbox")
    await bridge._settle(ref, {"status": "ok", "speech": "已确认，开始执行。"})
    if arrival != "report_first":
        await bridge.report("", "已确认，开始执行。", "same-inbox")
    bridge.state = "idle"
    await bridge.fill_idle()
    await finish(bridge, provider)
    assert facts(provider) == ["已确认，开始执行。"]
    assert not bridge.deliveries
    assert ref.heard and ref.delivery == "done"


async def test_timeout_does_not_consume_later_success_and_result_retry_ids_deduplicate():
    bridge, provider, _, _ = make()
    ref = VoiceTurnRef("timeout", "timeout", "进展", "进展", 1000, inbox_id="same-inbox")
    await bridge._settle(ref, {"status": "timeout", "speech": "稍后查看结果。"})
    await finish(bridge, provider, "timeout")
    await bridge.report("任务", "已完成", "same-inbox", result_id="result")
    await finish(bridge, provider, "result")
    await bridge.report("任务", "完成了", "retry-inbox", result_id="result")
    assert facts(provider) == ["稍后查看结果。", "已完成。"]


async def test_idle_batch_is_combined_before_first_response_is_requested():
    bridge, provider, _, _ = make()
    await bridge.report_batch([Report(f"inbox-{i}", f"task-{i}", f"result-{i}") for i in range(3)])
    assert len(provider.commands("create")) == 1
    assert facts(provider) == ["result-0.", "result-1.", "result-2."]


@pytest.mark.parametrize("reverse", [False, True])
async def test_current_sql_version_supersedes_old_running_in_both_arrival_orders(reverse):
    bridge, provider, _, _ = make()
    bridge.state = "user_speaking"
    await bridge.refresh_shared_context(snapshot())
    if not reverse:
        await bridge.report("task", "还在执行", "old", "task",
            running=[{"task_id": "task", "title": "task", "task_version": "1:running"}])
    await bridge.refresh_shared_context(snapshot("completed", result="result-2"))
    await bridge.report("task", "已完成", "new", "task", result_id="result-2", task_revision=1)
    if reverse:
        await bridge.report("task", "还在执行", "old", "task",
            running=[{"task_id": "task", "title": "task", "task_version": "1:running"}])
    bridge.state = "idle"
    await bridge.fill_idle()
    assert facts(provider) == ["已完成。"]
    # A legitimate new revision can run again after completion.
    await finish(bridge, provider)
    await bridge.refresh_shared_context(snapshot("running", revision=2, result="result-2"))
    await bridge.report("task", "修改已开始", "revision-2", "task", task_revision=2,
        running=[{"task_id": "task", "title": "task", "task_version": "2:running"}])
    assert facts(provider)[-1] == "修改已开始。"


async def test_native_answer_refreshes_before_audio_and_rereads_after_poll_hint_is_lost():
    current = snapshot()
    async def read():
        return current
    bridge, provider, _, _ = make(reader=read)
    await bridge.refresh_shared_context(current)
    bridge._native_context_changed = False
    current = snapshot("completed", result="result-2")  # no watcher signal
    await bridge.on_provider_event(started("stale-native"))
    await bridge.on_provider_event(audio("stale-native"))
    assert not any(isinstance(value, bytes) for value in bridge.outbox._queue)
    await bridge.on_provider_event(done("stale-native", status="cancelled"))
    assert bridge.requested == "followup"
    await bridge.on_provider_event(started("fresh-native"))
    await bridge.on_provider_event(audio("fresh-native"))
    assert bridge._work["s"]["state"] == "completed"
    assert any(isinstance(value, bytes) for value in bridge.outbox._queue)


async def test_out_of_order_context_delivery_does_not_regress_state():
    bridge, _, _, _ = make()
    await bridge.refresh_shared_context(snapshot("completed", sequence=2))
    await bridge.refresh_shared_context(snapshot("running", sequence=1))
    assert bridge._work["s"]["state"] == "completed"


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("read_fails", [False, True])
async def test_new_report_ahead_of_context_watcher_is_not_discarded(batch, read_fails):
    async def read():
        if read_fails:
            raise TimeoutError("temporarily unavailable")
        return snapshot("completed", result="new-result", sequence=2)
    bridge, provider, _, _ = make(reader=read)
    bridge.state = "user_speaking"
    await bridge.refresh_shared_context(snapshot(result="old-result", sequence=1))
    report = Report("new-inbox", "task", "新结果已完成", "task", result_id="new-result", task_revision=1)
    if batch:
        await bridge.report_batch([report])
    else:
        await bridge.report(report.title, report.text, report.inbox_id, report.task_id,
                            result_id=report.result_id, task_revision=report.task_revision)
    assert len(bridge.deliveries) == 1  # failed reads cannot silently consume the notification
    read_fails = False
    bridge.state = "idle"
    await bridge.fill_idle()
    assert facts(provider) == ["新结果已完成。"]


@pytest.mark.parametrize("size", [960, 4800, 12000, 48000])
@pytest.mark.parametrize("after_provider_done", [False, True])
async def test_partial_playback_is_not_delivery_and_late_ack_cannot_finish_it(size, after_provider_done):
    bridge, provider, _, _ = make(ack=True)
    await bridge.report("task", "结果已就绪。", "inbox")
    ref = bridge.deliveries[0]
    await bridge.on_provider_event(started("cut"))
    await bridge.on_provider_event(audio("cut", size))
    if after_provider_done:
        await bridge.on_provider_event(done("cut"))
        assert ref.delivery == "awaiting_playback"
    await bridge.on_provider_event(event("user_started"))
    if not after_provider_done:
        await bridge.on_provider_event(done("cut", status="cancelled"))
    await bridge.playback_completed("0:cut")
    assert ref.delivery == "paused" and ref.interrupted
    await bridge.on_provider_event(event("user_stopped", invalid=True))
    assert "不要重复称呼" in provider.commands("create")[-1][1]
    assert ref.delivery != "done"


async def test_only_completed_playback_ack_finishes_group_once():
    bridge, provider, _, _ = make(ack=True)
    await bridge.report_batch([Report("a", "one", "结果一"), Report("b", "two", "结果二")])
    await bridge.playback_completed("0:speech")  # too early
    await finish(bridge, provider)
    assert len(bridge.deliveries) == 2
    await bridge.playback_completed("unknown")
    assert len(bridge.deliveries) == 2
    await bridge.playback_completed("0:speech")
    await bridge.playback_completed("0:speech")
    assert not bridge.deliveries and len(provider.commands("create")) == 1


@pytest.mark.parametrize("after_provider_done", [False, True])
async def test_interrupted_batch_yields_to_native_answer_without_replaying_or_claiming_delivery(after_provider_done):
    bridge, provider, _, clock = make(ack=True)
    await bridge.report_batch([Report("a", "one", "结果一", result_id="r1"),
                               Report("b", "two", "结果二", result_id="r2")])
    refs = list(bridge.deliveries)
    await bridge.on_provider_event(started("cut"))
    await bridge.on_provider_event(audio("cut"))
    if after_provider_done:
        await bridge.on_provider_event(done("cut"))
    await bridge.on_provider_event(event("user_started", item_id="interrupt"))
    if not after_provider_done:
        await bridge.on_provider_event(done("cut", status="cancelled"))
    await bridge.on_provider_event(event("user_stopped", invalid=False))
    await bridge.on_provider_event(event("user_transcript", item_id="interrupt", text="只说重点"))
    await bridge.on_provider_event(started("answer"))
    await bridge.on_provider_event(audio("answer"))
    await bridge.on_provider_event(done("answer"))
    await bridge.playback_completed("0:answer")
    await bridge.playback_completed("0:cut")
    clock.advance(60)
    await bridge.fill_idle()
    assert all(ref.delivery == "paused" and not ref.heard for ref in refs)
    assert len(provider.commands("create")) == 1
    # A later noise event or provider reconnect must not revive the old report.
    await bridge.on_provider_event(event("user_started", item_id="noise"))
    await bridge.on_provider_event(event("user_stopped", invalid=True))
    await bridge._lose_session()
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1
    assert bridge._quiet_for_swap()
    assert bridge._phase[0] == "listening"
    # The same result stays deduplicated, while an actually new result can speak.
    await bridge.report("one", "结果一", "retry-a", result_id="r1")
    assert len(provider.commands("create")) == 1
    await bridge.report("three", "新结果", "c", result_id="r3")
    assert facts(provider)[-1] == "新结果。"
    assert len(provider.commands("create")) == 2


async def test_state_withdrawal_keeps_unrelated_report_retryable():
    bridge, provider, _, _ = make(ack=True)
    await bridge.report("one", "结果", "a")
    await finish(bridge, provider)
    await bridge._interrupt_playback(source="work_changed")
    assert bridge.deliveries[0].delivery == "queued"
    assert not bridge.deliveries[0].paused_by_user


async def test_user_requested_result_still_retries_after_interruption():
    bridge, provider, _, _ = make(ack=True)
    ref = VoiceTurnRef("answer", "answer", "查询", "查询", 1000)
    await bridge._settle(ref, {"status": "ok", "speech": "用户请求的结果。"})
    await finish(bridge, provider)
    await bridge.on_provider_event(event("user_started"))
    assert ref.delivery == "queued" and not ref.paused_by_user


async def test_legacy_client_waits_for_estimated_playback_and_interruption_preserves_result():
    bridge, provider, _, clock = make(paced=True)
    await bridge.report("task", "结果", "inbox")
    ref = bridge.deliveries[0]
    clock.advance(.401)
    await bridge.fill_idle()
    await finish(bridge, provider, size=480000)
    assert ref.delivery == "awaiting_playback"
    clock.advance(5)
    await bridge.fill_idle()
    assert ref.delivery == "awaiting_playback"
    await bridge.on_provider_event(event("user_started"))
    assert ref.delivery == "paused" and ref.interrupted
    if bridge._wake:
        bridge._wake.cancel()


async def test_reports_from_separate_wakes_are_coalesced_in_production_pacing():
    bridge, provider, _, clock = make(paced=True)
    await bridge.report("one", "第一项完成。", "one")
    clock.advance(.2)
    await bridge.report("two", "第二项完成。", "two")
    assert not provider.commands("create")
    clock.advance(.201)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1
    assert facts(provider) == ["第一项完成。", "第二项完成。"]
    if bridge._wake:
        bridge._wake.cancel()


async def test_missing_ack_does_not_replay_or_mark_delivered_and_late_ack_still_works():
    bridge, provider, _, clock = make(ack=True)
    await bridge.report("task", "结果", "inbox")
    ref = bridge.deliveries[0]
    await finish(bridge, provider)
    clock.advance(60)
    await bridge.fill_idle()
    assert ref.delivery == "awaiting_playback" and not ref.heard
    assert len(provider.commands("create")) == 1
    await bridge.playback_completed("0:speech")
    assert ref.heard and not bridge.deliveries


async def test_superseding_one_playing_result_preserves_other_group_members():
    bridge, provider, _, _ = make(ack=True)
    await bridge.refresh_shared_context(snapshot("completed"))
    await bridge.report_batch([Report("a", "one", "旧结果。", "task", result_id="result-1", task_revision=1),
                               Report("b", "two", "另一个结果。", "other")])
    first, second = bridge.deliveries
    await finish(bridge, provider)
    await bridge.refresh_shared_context(snapshot("running", revision=2))
    assert first.delivery == "superseded"
    assert list(bridge.deliveries) == [second] and second.delivery == "queued"
    await bridge.playback_completed("0:speech")  # old grouped ACK is invalid after clear
    assert not second.heard
    await bridge.fill_idle()
    assert facts(provider)[-1] == "另一个结果。"


async def test_failed_freshness_read_cannot_play_old_status_or_execute_stale_tool_call():
    async def unavailable():
        raise TimeoutError("database unavailable")
    bridge, provider, link, _ = make(reader=unavailable)
    await bridge.on_provider_event(started("stale"))
    await bridge.on_provider_event(audio("stale"))
    await bridge.on_provider_event(done("stale", status="cancelled"))
    await bridge.on_provider_event(tool_call("late-tool", response_id="stale"))
    assert not link.started
    assert not any(isinstance(value, bytes) for value in bridge.outbox._queue)
    assert "当前状态核验失败" in provider.commands("create")[-1][1]
    assert provider.commands("output")[-1][2]["this_call_applied"] is False
    await bridge.on_provider_event(started("unavailable-notice"))
    await bridge.on_provider_event(audio("unavailable-notice"))
    assert any(isinstance(value, bytes) for value in bridge.outbox._queue)


async def test_user_speech_during_freshness_read_prevents_delivery_creation():
    reading, release = asyncio.Event(), asyncio.Event()
    async def read():
        reading.set()
        await release.wait()
        return snapshot()
    bridge, provider, _, _ = make(reader=read)
    pending = asyncio.create_task(bridge.report("task", "结果。", "inbox"))
    await reading.wait()
    await bridge.on_provider_event(event("user_started"))
    release.set()
    await pending
    assert not provider.commands("create") and bridge.deliveries[0].delivery == "queued"
