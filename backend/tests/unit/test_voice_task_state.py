"""A live call speaks the current state of a task changed elsewhere, never an outdated one.

Pausing, cancelling, resuming or editing a task in the personal assistant or
its conversation, or the task moving on by itself, reaches the call as a new
shared snapshot (voice/freshness.py). The turn's task rows follow it, a reply
still telling the old state stops and the user's question is answered again;
progress messages and other tasks leave speech alone.
"""
import asyncio
import json

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, outbox,
                                       started, transcript)
from voice import phrases
from voice.assistant_link import VoiceTurnRef
from voice.bridge import Bridge
from voice.freshness import work_claim, work_status
from voice.progress import Progress
from voice.reply_focus import ReplyFocus
from voice.shared_context import SharedSnapshot
from voice.tools import CallScope

SCOPE = CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x")
SUNSET, ORANGE = "s-sunset", "s-orange"
SUNRISE = "s-sunrise"
TITLES = {SUNSET: ("30秒日落海浪横屏短视频方案", "日落海浪短视频"), ORANGE: ("橙光鲜果周末上新宣传视频脚本", "橙光鲜果新品宣传"),
          SUNRISE: ("日出海浪竖屏短视频", "日出海浪短视频")}
STATUS = "The sunset video is still generating its second segment."
CONCEPTS = (("sunset", "日落"), ("orange", "橙光"))


def row(session, state="running", *, revision=1, control=1, message="生成第二段"):
    title, project = TITLES[session]
    return {"session_id": session, "task_id": "t-" + session, "title": title, "project": project, "state": state,
            "intent_revision": revision, "control_revision": control, "task_version": f"{revision}:{control}:{state}",
            "result_id": None, "updated_at": "2026-10-27T10:00:00+00:00",
            "recent_messages": [{"text": message, "truncated": False}], "version": f"{revision}:{control}:{state}:{message}"}


def snapshot(*rows, sequence):
    return SharedSnapshot("work", list(rows), sequence)


def meaning(text):
    lowered = text.casefold()
    return [float(any(word in lowered for word in words)) for words in CONCEPTS] + [0.3]


async def embed(texts):
    return [meaning(text) for text in texts]


@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="en", late_after=12, clock=clock, scope=SCOPE,
                    progress=Progress(user_id="u1", main_session_id="main-1", clock=clock), instructions="BASE")
    bridge.focus = ReplyFocus(embed=embed)
    return bridge, provider, link, clock


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def settled(bridge):
    for _ in range(50):
        task = bridge._focus_task
        if task is None or task.done():
            break
        await asyncio.sleep(0.01)
    await drain()


def telling_call(focus_embed=embed):
    """A call as it runs: native memory prepares each turn (tests/unit/test_voice_memory_context.py)."""
    from tests.unit.test_voice_memory_context import call as memory_call
    bridge, provider, clock = memory_call()
    bridge.focus = ReplyFocus(embed=focus_embed)
    return bridge, provider, clock


async def telling(bridge, provider, text=STATUS, response="status"):
    """Two tasks running; the user asks how things are going and the reply is playing ``text``."""
    from tests.unit.test_voice_memory_context import _heard
    await bridge.refresh_shared_context(snapshot(row(SUNSET), row(ORANGE), sequence=1))
    await _heard(bridge, "How is the video going?", "u-ask")
    await bridge.fill_idle()
    await drain()
    outbox(bridge)
    provider.sent.clear()
    await replay(bridge, started(response), audio(response, size=480000),
                 event("assistant_transcribing", response_id=response, text=text))
    await settled(bridge)


def cleared(bridge):
    return any(isinstance(value, dict) and value.get("type") == "playback.clear" for value in outbox(bridge))


def test_a_claim_ignores_progress_messages_but_preserves_distinct_states():
    queued, running, later = row(SUNSET, "queued"), row(SUNSET, "running"), row(SUNSET, "running", message="生成第三段")
    assert work_claim(queued) != work_claim(running)  # "not started yet" is no longer true
    assert work_claim(running) == work_claim(later)  # a message alone is not a state transition
    assert work_claim(row(SUNSET, "paused", control=2)) != work_claim(running)
    assert work_claim(row(SUNSET, revision=2)) != work_claim(running)  # requirements changed
    assert work_status(later) == work_status(running) != work_status(queued)  # rows follow a state, not a message


@pytest.mark.parametrize("before,after,text", [
    ("paused", "canceled", "The sunset video is paused. You can resume it when ready."),
    ("queued", "running", "The sunset video is still queued and has not started processing."),
])
async def test_distinct_states_in_the_same_old_group_invalidate_a_playing_answer(before, after, text):
    from tests.unit.test_voice_memory_context import _heard
    bridge, provider, _ = telling_call()
    await bridge.refresh_shared_context(snapshot(row(SUNSET, before), row(ORANGE), sequence=1))
    await _heard(bridge, "日落海浪那个视频现在怎么样了？", "u-state")
    await bridge.fill_idle()
    await replay(bridge, started("status"), audio("status", size=480000),
                 event("assistant_transcribing", response_id="status", text=text))
    await settled(bridge)
    outbox(bridge)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, after, control=2), row(ORANGE), sequence=2))
    assert cleared(bridge) and provider.commands("cancel") and bridge.followup_due
    assert bridge._work_changes() == [{"task": TITLES[SUNSET][0], "state": after}]
    await bridge.close()


async def test_a_missing_cancel_completion_recovers_the_question_with_the_latest_task_state():
    from voice.bridge import CANCEL_TIMEOUT_SECONDS, RECOVERIES_PER_CALL
    from tests.unit.test_voice_memory_context import Provider, finish_tasks
    bridge, provider, clock = telling_call()
    fresh = Provider()
    async def opener(instructions):
        fresh.instructions = instructions
        return fresh
    bridge.opener = opener
    bridge.answered()
    # A deliberate cancellation recovery does not consume the network-drop budget.
    bridge.recoveries = RECOVERIES_PER_CALL
    await telling(bridge, provider)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), row(ORANGE), sequence=2))
    assert bridge._phase[0] == "thinking"
    clock.advance(CANCEL_TIMEOUT_SECONDS + .01)
    await bridge.fill_idle()
    await drain()
    assert provider.closed and not fresh.commands("create")
    # This is the provider pump's path when closing the stalled session ends its stream.
    assert await bridge.recover(provider)
    await finish_tasks(bridge)
    await bridge.fill_idle()
    assert bridge.provider is fresh and not bridge.failed.is_set()
    assert bridge.recoveries == RECOVERIES_PER_CALL
    assert "How is the video going?" in fresh.commands("note")[0][1]
    assert changes(fresh.commands("create")[-1][1]) == [{"task": TITLES[SUNSET][0], "state": "paused"}]
    assert not bridge.refs  # recovering speech never re-submits the business request
    await bridge.close()


async def test_a_reply_cancelled_before_its_first_audio_cannot_hold_the_call_forever():
    from voice.bridge import CANCEL_TIMEOUT_SECONDS
    from tests.unit.test_voice_memory_context import _heard
    bridge, provider, clock = telling_call()
    before = row(SUNSET)
    await bridge.refresh_shared_context(snapshot(before, sequence=1))
    await _heard(bridge, "日落海浪的视频现在怎么样？", "u-before-audio")
    await bridge.fill_idle()
    bridge._native_work_pending = {SUNSET: work_claim(before)}
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), sequence=2))
    outbox(bridge)
    await replay(bridge, started("obsolete-start"), audio("obsolete-start"))
    assert provider.commands("cancel") and not any(isinstance(item, bytes) for item in outbox(bridge))
    clock.advance(CANCEL_TIMEOUT_SECONDS + .01)
    await bridge.fill_idle()
    await drain()
    assert provider.closed and not bridge._can_inject()
    assert bridge._state_changed and not bridge.refs
    await bridge.close()


async def test_a_cancelled_old_transcript_is_not_carried_into_the_replacement_session():
    bridge, provider, _ = telling_call()
    await telling(bridge, provider)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), row(ORANGE), sequence=2))
    await replay(bridge, event("assistant_transcript", response_id="status", item_id="stale-status", text=STATUS))
    assert all(line.text != STATUS for line in bridge.spoken.lines)
    await replay(bridge, done("status"))  # some providers report completed despite the cancel
    assert bridge._answered_item != "u-ask"
    await bridge.close()


async def test_a_prompt_cancel_completion_does_not_reconnect_or_cancel_the_new_reply():
    from voice.bridge import CANCEL_TIMEOUT_SECONDS
    bridge, provider, clock = telling_call()
    await telling(bridge, provider)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), row(ORANGE), sequence=2))
    await replay(bridge, done("status", status="cancelled"), started("fresh"), audio("fresh"))
    clock.advance(CANCEL_TIMEOUT_SECONDS + .1)
    await bridge.fill_idle()
    await drain()
    assert not provider.closed and bridge.active_response == "fresh"
    assert bridge._cancel_wait is None and not bridge.interrupted
    await bridge.close()


async def test_repeated_interruptions_do_not_postpone_the_cancel_deadline():
    from voice.bridge import CANCEL_TIMEOUT_SECONDS
    bridge, provider, clock = telling_call()
    await telling(bridge, provider)
    await bridge._interrupt_playback()
    clock.advance(CANCEL_TIMEOUT_SECONDS / 2)
    await bridge._interrupt_playback()
    clock.advance(CANCEL_TIMEOUT_SECONDS / 2 + .01)
    await bridge.fill_idle()
    await drain()
    assert provider.closed
    await bridge.close()


async def test_stopping_obsolete_speech_does_not_wait_for_an_instruction_ack():
    bridge, provider, _ = telling_call()
    await telling(bridge, provider)
    provider.ack.clear()
    await asyncio.wait_for(bridge.refresh_shared_context(
        snapshot(row(SUNSET, "paused", control=2), row(ORANGE), sequence=2)), .2)
    assert cleared(bridge) and bridge._phase[0] == "thinking"
    assert provider.commands("cancel")
    provider.ack.set()
    await bridge.close()


@pytest.mark.parametrize("lang, guard", [
    ("zh", "不能把旧卡片消失说成用户确认了旧方案"),
    ("en", "not approval of the old plan"),
])
async def test_a_manual_conversation_update_replies_from_the_new_message_not_an_old_card_approval(lang, guard):
    from tests.unit.test_voice_memory_context import _heard
    bridge, provider, _ = telling_call()
    bridge.lang = lang
    before = {**row(SUNSET, "waiting_input"), "task_id": None, "intent_revision": None,
              "human_input": {"id": "old-input", "text": "做10秒视频", "truncated": False},
              "pending_request": {"request_id": "old-card", "version": "v1"}}
    await bridge.refresh_shared_context(snapshot(before, sequence=1))
    await _heard(bridge, "日落那个视频要我确认什么？", "u-manual")
    await bridge.fill_idle()
    await replay(bridge, started("old-plan"), audio("old-plan", size=480000),
                 event("assistant_transcribing", response_id="old-plan", text=STATUS))
    await settled(bridge)
    latest = {"id": "new-input", "text": "改成15秒，加一个店内人气场景", "truncated": False}
    after = {**before, "state": "busy", "pending_request": None, "human_input": latest, "version": "new"}
    provider.sent.clear(); outbox(bridge)
    await bridge.refresh_shared_context(snapshot(after, sequence=2))
    assert cleared(bridge)
    await replay(bridge, done("old-plan", status="cancelled"))
    instructions = provider.commands("create")[-1][1]
    [change] = changes(instructions)
    assert change["user_input_updated"]
    assert change["latest_user_input"] == {"text": latest["text"], "truncated": False}
    assert change["earlier_decision"] == "no longer pending"
    assert guard in instructions
    assert not bridge.refs
    await bridge.close()


async def test_a_late_cancel_watch_cannot_close_a_replacement_provider():
    from voice.bridge import CANCEL_TIMEOUT_SECONDS
    from tests.unit.test_voice_memory_context import Provider
    bridge, provider, clock = telling_call()
    await telling(bridge, provider)
    await bridge._interrupt_playback()
    new = Provider()
    bridge._swap_in(new, "BASE")
    clock.advance(CANCEL_TIMEOUT_SECONDS + .1)
    await bridge.fill_idle()
    await drain()
    assert not new.closed and bridge._cancel_wait is None
    await bridge.close()


async def test_a_stalled_session_that_cannot_close_fails_visibly(monkeypatch):
    import voice.bridge as bridge_module
    bridge, provider, clock = telling_call()
    await telling(bridge, provider)
    async def blocked_close():
        await asyncio.Event().wait()
    provider.close = blocked_close
    monkeypatch.setattr(bridge_module, "CLOSE_WAIT_SECONDS", .01)
    await bridge._interrupt_playback()
    clock.advance(bridge_module.CANCEL_TIMEOUT_SECONDS + .1)
    await bridge.fill_idle()
    await asyncio.wait_for(bridge.failed.wait(), 1)
    assert bridge.provider is provider and not bridge._can_inject()
    await bridge.close()


async def test_a_reply_telling_a_task_stops_when_the_task_is_paused_and_the_question_is_answered_again():
    bridge, provider, _ = telling_call()
    await telling(bridge, provider)
    assert bridge._native_work_playing == {SUNSET: work_claim(row(SUNSET))}
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), row(ORANGE), sequence=2))
    assert cleared(bridge) and provider.commands("cancel")
    assert bridge.followup_due
    bridge.state, bridge.active_response, bridge.interrupted = "idle", None, False
    await bridge.fill_idle()
    await drain()
    [(_, instructions)] = provider.commands("create")
    assert phrases.state_changed_instructions(bridge.lang) in instructions
    assert changes(instructions) == [{"task": TITLES[SUNSET][0], "state": "paused"}]
    await bridge.close()


def changes(instructions):
    """The task changes the answer given again is told (phrases.state_changed_instructions), in either language."""
    for start, end in (("this is what they say now: ", ". First tell"), ("以下是现在的状态：", "。先用日常语言")):
        if start in instructions:
            return json.loads(instructions.split(start)[1].split(end)[0])
    return []


async def test_the_answer_after_new_requirements_is_told_they_changed_and_the_old_decision_went():
    """Measured 2026-10-27: told only that something changed, it asked for the replaced plan's approval again."""
    bridge, provider, _ = telling_call()
    card = {"request_id": "q-plan", "version": "v1", "questions": [{"header": "分镜方案与积分报价确认"}]}
    asking = {**row(SUNSET, "waiting_input"), "waiting_for_user": True, "pending_request": card}
    from tests.unit.test_voice_memory_context import _heard
    await bridge.refresh_shared_context(snapshot(asking, row(ORANGE), sequence=1))
    await _heard(bridge, "How is the sunset video going?", "u-ask")
    await bridge.fill_idle()
    await drain()
    outbox(bridge)
    provider.sent.clear()
    await replay(bridge, started("status"), audio("status", size=480000),
                 event("assistant_transcribing", response_id="status", text=STATUS))
    await settled(bridge)
    # The person typed new requirements on the task's page (voice/work.py: the card no longer waits).
    redone = {**row(SUNSET, "queued", revision=2, control=2), "waiting_for_user": False,
              "state_summary": "用户刚提交了新的要求，任务将按新要求重新处理；之前的待确认事项会被替换，不需要再确认。"}
    await bridge.refresh_shared_context(snapshot(redone, row(ORANGE), sequence=2))
    assert cleared(bridge) and bridge.followup_due
    bridge.state, bridge.active_response, bridge.interrupted = "idle", None, False
    await bridge.fill_idle()
    await drain()
    [(_, instructions)] = provider.commands("create")
    assert changes(instructions) == [{"task": TITLES[SUNSET][0], "state": "queued",
                                      "state_summary": redone["state_summary"], "requirements_updated": True,
                                      "earlier_decision": "no longer pending"}]
    assert "这些决定已经不再等用户" not in instructions  # nothing was answered
    await bridge.close()


@pytest.mark.parametrize("changed, stops", [(SUNSET, True), (SUNRISE, False)])
async def test_a_reply_to_words_naming_a_task_stops_when_that_task_changes_whatever_meaning_says(changed, stops):
    """Measured 2026-10-27: meaning could not tell 日落海浪 from 日出海浪, and Pause left "it is regenerating" playing."""
    async def alike(texts):  # every reply and both wave videos look the same to meaning
        return [[1.0, 0.2] if "海浪" in text or "waves" in text else [0.0, 1.0] for text in texts]
    from tests.unit.test_voice_memory_context import _heard
    bridge, provider, _ = telling_call(alike)
    await bridge.refresh_shared_context(snapshot(row(SUNSET), row(SUNRISE, "completed"), row(ORANGE), sequence=1))
    await _heard(bridge, "日落海浪那个视频现在进展到哪了？", "u-ask")
    await bridge.fill_idle()
    await drain()
    outbox(bridge)
    provider.sent.clear()
    await replay(bridge, started("status"), audio("status", size=480000),
                 event("assistant_transcribing", response_id="status", text="The waves video is regenerating its storyboard."))
    await settled(bridge)
    assert set(bridge._native_work_playing) == {SUNSET}  # named, not every close one
    paused = row(changed, "paused", control=2)
    rows = [paused if key == changed else row(key, "completed" if key == SUNRISE else "running")
            for key in (SUNSET, SUNRISE, ORANGE)]
    await bridge.refresh_shared_context(snapshot(*rows, sequence=2))
    assert cleared(bridge) is stops and bridge.followup_due is stops
    await bridge.close()


async def test_progress_messages_and_other_tasks_leave_the_reply_playing():
    bridge, provider, _ = telling_call()
    await telling(bridge, provider)
    assert bridge._native_work_playing
    await bridge.refresh_shared_context(snapshot(row(SUNSET, message="生成第三段"), row(ORANGE), sequence=2))
    await bridge.refresh_shared_context(snapshot(row(SUNSET, message="生成第三段"),
                                                 row(ORANGE, "paused", control=2), sequence=3))
    assert not cleared(bridge) and not bridge.followup_due
    await bridge.close()


async def test_a_handover_acknowledgement_is_not_bound_to_the_task_it_changes():
    from tests.support.voice_fakes import tool_call
    from tests.unit.test_voice_memory_context import _heard
    bridge, provider, _ = telling_call()
    await bridge.refresh_shared_context(snapshot(row(SUNSET), row(ORANGE), sequence=1))
    await _heard(bridge, "Make the sunset video thirty seconds long.", "u-edit")
    await bridge.fill_idle()
    await drain()
    outbox(bridge)
    await replay(bridge, started("ack"), audio("ack", size=480000),
                 tool_call("ask-1", text="把日落视频改成30秒", response_id="ack"),
                 event("assistant_transcribing", response_id="ack",
                       text="I've passed the new length on to the sunset video task."))
    await settled(bridge)
    assert bridge.link.started and bridge._native_response == "ack"
    assert bridge._native_work_playing == {}
    await bridge.refresh_shared_context(snapshot(row(SUNSET, revision=2), row(ORANGE), sequence=2))
    assert not cleared(bridge)  # its own edit changed the task: the acknowledgement stays true
    await bridge.close()


async def test_a_task_changed_before_the_reply_was_placed_stops_it_when_placed():
    gate = asyncio.Event()

    async def slow(texts):
        if not any(text.startswith("任务「") for text in texts):
            await gate.wait()  # the reply's comparison; task names were embedded beforehand
        return await embed(texts)
    bridge, provider, _ = telling_call(slow)
    await telling(bridge, provider)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "canceled", control=2), row(ORANGE), sequence=2))
    assert not cleared(bridge)
    gate.set()
    await settled(bridge)
    assert cleared(bridge) and bridge.followup_due
    await bridge.close()


async def test_a_card_answered_mid_reply_is_answered_again_from_the_new_state(call):
    from tests.unit.test_voice_reply_focus import GULLS, ORANGE as COPY_CARD, SUNSET as GULL_CARD, Embedding
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding())
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    bridge.seed_waiting_cards([], [GULL_CARD, COPY_CARD])
    await replay(bridge, event("user_started"), event("user_stopped"), transcript("What do you need?", "u-ask"))
    outbox(bridge)
    provider.sent.clear()
    await replay(bridge, started("explain"), audio("explain", size=480000),
                 event("assistant_transcribing", response_id="explain", text=GULLS))
    await settled(bridge)
    await bridge.waiting_cards([], [COPY_CARD])
    assert cleared(bridge) and bridge.followup_due and bridge._state_changed
    await bridge.close()


async def test_the_turns_task_rows_follow_a_state_change_but_not_a_progress_message():
    from tests.unit.test_voice_memory_context import _heard, call as memory_call
    bridge, provider, _ = memory_call()
    await bridge.refresh_shared_context(snapshot(row(SUNSET), sequence=1))
    await _heard(bridge, "那个日落视频怎么样了？", "now")
    assert '"state": "running"' in bridge._turn_work
    before = len(provider.commands("instructions"))
    await bridge.refresh_shared_context(snapshot(row(SUNSET, message="生成第三段"), sequence=2))
    assert len(provider.commands("instructions")) == before  # no churn on progress
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), sequence=3))
    assert '"state": "paused"' in bridge._turn_work
    assert '"state": "paused"' in provider.commands("instructions")[-1][1]
    await bridge.close()


def _ref(**fields):
    ref = VoiceTurnRef(id="r", provider_call_id="p", text="", transcript="", requested=0.0, **fields)
    ref.status = "ok"
    return ref


async def test_a_report_read_before_a_pause_or_retry_is_no_longer_told(call):
    bridge, _, _, _ = call
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "failed"), sequence=1))
    report = _ref(task_id="t-" + SUNSET, task_revision=1, control_revision=1)
    assert not bridge._stale_work(report)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "running", control=2), sequence=2))  # retried
    assert bridge._stale_work(report)


async def test_a_handed_over_request_stays_true_while_its_task_moves_from_queued_to_running(call):
    bridge, _, _, _ = call
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "queued"), sequence=1))
    passed_on = _ref(running=[{"task_id": "t-" + SUNSET, "title": "sunset", "intent_revision": 1,
                               "task_version": "1:1:queued"}])
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "running"), sequence=2))
    assert not bridge._stale_work(passed_on)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "paused", control=2), sequence=3))
    assert bridge._stale_work(passed_on)
    await bridge.refresh_shared_context(snapshot(row(SUNSET, "running", revision=2, control=3), sequence=4))
    assert bridge._stale_work(passed_on)  # resumed with new requirements: not what was handed over


async def test_a_dropped_session_stops_the_audio_it_had_sent(call):
    from tests.unit.test_voice_bridge import answered
    bridge, provider, _, clock = call
    bridge.focus = None
    fresh = await answered(bridge, provider, clock)
    await replay(bridge, event("user_started"), event("user_stopped"), transcript("Tell me a story.", "u1"))
    await replay(bridge, started("story"), audio("story", size=480000))
    outbox(bridge)
    provider.drop()
    assert await bridge.recover(provider)
    assert cleared(bridge)  # not heard to the end and then told again
    assert fresh
    await bridge.close()


async def test_the_turns_rows_say_what_a_waiting_task_is_waiting_on():
    """Measured 2026-10-27: told only that a task was waiting, a reply named a decision from old history."""
    from tests.unit.test_voice_memory_context import _heard, call as memory_call
    bridge, provider, _ = memory_call()
    waiting = {**row(SUNSET, "waiting_input"), "waiting_for_user": True, "pending_request": {
        "request_id": "q-adjust", "version": "v1", "questions": [{"header": "明确分镜或Prompt调整内容", "question": "改哪里？"}]}}
    await bridge.refresh_shared_context(snapshot(waiting, sequence=1))
    await _heard(bridge, "日落那个视频现在要我决定什么？", "now")
    assert "明确分镜或Prompt调整内容" in bridge._turn_work
    assert "明确分镜或Prompt调整内容" in provider.commands("instructions")[-1][1]
    await bridge.close()


async def test_the_turns_rows_say_what_was_chosen_on_a_decided_card():
    """Measured 2026-10-27: told only that a card was answered, a reply called "modify the plan" confirmed."""
    from tests.unit.test_voice_memory_context import _heard, call as memory_call
    bridge, provider, _ = memory_call()
    decided = {**row(SUNSET, "running"), "last_decision": {"request_id": "q-plan", "state": "answered",
                                                          "applied": True, "choice": "修改拆段或 prompt"}}
    await bridge.refresh_shared_context(snapshot(decided, sequence=1))
    await _heard(bridge, "日落那个视频现在怎么样了？", "now")
    assert '"last_decision": {"answered": "修改拆段或 prompt"}' in bridge._turn_work
    await bridge.close()


@pytest.mark.parametrize("route, shown", [
    # Measured 2026-10-27: "how is the sunset video now?" right after Resume got
    # no rows and "temporary lookup limitation" on an undecided read scope.
    ({"read_policy": "uncertain", "reason_code": "fallback_unresolved"}, True),
    ({"read_policy": "uncertain", "reason_code": "timeout", "primary_decision": {
        "read_policy_answer": {"choice": "current_only"}}}, False),  # a restriction any judge saw holds
    ({"read_policy": "current_only"}, False),
    ({"read_policy": "no_history"}, True),
])
async def test_an_undecided_turn_keeps_the_tasks_live_rows_only_while_no_restriction_was_seen(route, shown):
    from tests.unit.test_voice_memory_context import _heard, call as memory_call, packet
    async def prepare(scope, text, recent):
        return {**packet(text), "route": route}, "fresh"
    bridge, provider, _ = memory_call(prepare)
    await bridge.refresh_shared_context(snapshot(row(SUNSET), sequence=1))
    await _heard(bridge, "那个日落视频现在怎么样了？", "now")
    assert ('"state": "running"' in bridge._turn_work) is shown
    assert ('"state": "running"' in provider.commands("instructions")[-1][1]) is shown
    await bridge.close()


async def test_a_reply_about_a_tasks_card_stops_when_the_task_is_paused_with_the_card_still_pending():
    """Measured 2026-10-27: "the sunset video is waiting for your confirmation" played on after Pause."""
    from tests.unit.test_voice_memory_context import _heard

    async def gulls(texts):  # the reply matches the card's question, not the task's name
        return [[float("gull" in text.casefold() or "海鸥" in text), 0.3] for text in texts]
    bridge, provider, _ = telling_call(gulls)
    card = {"id": "q-gulls", "version": "v1", "session_id": SUNSET, "session_title": TITLES[SUNSET][0],
            "questions": [{"header": "海鸥镜头位置", "question": "海鸥放在哪个镜头？", "options": ["收尾", "中间"]}]}
    bridge.seed_waiting_cards([], [card])
    # As voice/work.py reads it: the pending card stays on the row before and after Pause.
    pending = {"waiting_for_user": True, "pending_request": {"request_id": "q-gulls", "version": "v1",
                                                             "questions": [{"header": "海鸥镜头位置"}]}}
    await bridge.refresh_shared_context(snapshot({**row(SUNSET, "waiting_input"), **pending}, sequence=1))
    await _heard(bridge, "日落那个要我决定什么？", "u-ask")
    await bridge.fill_idle()
    await drain()
    outbox(bridge)
    await replay(bridge, started("explain"), audio("explain", size=480000),
                 event("assistant_transcribing", response_id="explain", text="Would you like the gulls at the end?"))
    await settled(bridge)
    assert bridge._native_meaning == {"q-gulls": "v1"} and SUNSET in bridge._native_work_playing
    await bridge.refresh_shared_context(snapshot({**row(SUNSET, "paused", control=2), **pending}, sequence=2))
    assert cleared(bridge) and bridge.followup_due
    await bridge.close()
