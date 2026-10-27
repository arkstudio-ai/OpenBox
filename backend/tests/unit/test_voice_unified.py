"""Confirmed speech uses one business assistant; speculative speech never reaches the user."""
import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain,
    event, outbox, started, tool_call, transcript)
from tests.unit.test_voice_bridge import greeted, replay
from voice.bridge import Bridge
from voice.progress import Progress
from voice.turns import ACK_WAIT_SECONDS


async def test_reconnect_greeting_cannot_claim_stale_business_status():
    provider, link = ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", unified=True, recap=True)
    try:
        await bridge.start()
        [(_, instructions)] = provider.commands("create")
        assert "不要回顾旧通话" in instructions
        assert "业务回答等待主助手的最新结果" in instructions
        await replay(bridge, started("greet"), audio("greet"), done("greet"))
        assert any(isinstance(x, bytes) for x in outbox(bridge))
        assert not link.started  # connection alone is not a business request
    finally:
        await bridge.close()


async def test_every_final_transcript_is_submitted_once_without_tool_routing():
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    async def unexpected_check(*args):
        raise AssertionError("durable transcript handoff must not get a second dispatcher")
    bridge = Bridge(provider, link, lang="zh", clock=clock, unified=True, handoff_checker=unexpected_check)
    assert bridge.handoff is None
    await greeted(bridge, provider)
    try:
        await replay(bridge, transcript("先看看刚才的图片", "asr-1"), transcript("先看看刚才的图片", "asr-1"))
        assert len(link.started) == 1
        ref = link.started[0]
        assert ref.text == ref.transcript == "先看看刚才的图片"
        assert ref.context["handover_source"] == "confirmed_transcript"
        await replay(bridge, started("speculative"), audio("speculative"),
            event("assistant_transcript", response_id="speculative", text="已经帮你做好了"),
            tool_call("unexpected"), done("speculative", status="cancelled"))
        assert not any(isinstance(x, bytes) for x in outbox(bridge))
        assert len(link.started) == 1 and provider.commands("cancel")
        link.finish(ref.provider_call_id, speech="图片已找到，还没有开始制作视频。")
        await drain()
        clock.advance(10)
        await bridge.fill_idle()
        assert provider.commands("create")
        await replay(bridge, started("verified"), audio("verified"), done("verified"))
        assert any(isinstance(x, bytes) for x in outbox(bridge))
    finally:
        await bridge.close()


async def test_interruption_and_hangup_do_not_cancel_accepted_business_work():
    provider, link = ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", unified=True)
    await greeted(bridge, provider)
    await replay(bridge, transcript("帮我查进度", "asr-1"))
    ref = link.started[0]
    await replay(bridge, event("user_started"))
    assert ref.inbox_id and ref.provider_call_id in bridge.pending_calls
    await bridge.close()
    assert len(link.started) == 1 and ref.inbox_id


async def test_slow_main_acknowledges_received_speech_without_a_second_business_turn():
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    progress = Progress(user_id="user", main_session_id="main", clock=clock)
    bridge = Bridge(provider, link, clock=clock, unified=True, instructions="BASE", progress=progress)
    await greeted(bridge, provider)
    try:
        await replay(bridge, event("user_started", item_id="first"), event("user_stopped"),
                     transcript("截图", "first"))
        clock.advance(ACK_WAIT_SECONDS - .1)
        await bridge.fill_idle()
        assert not provider.commands("create")
        clock.advance(.1)
        await bridge.fill_idle()
        assert bridge.requested == "progress"
        assert len(provider.commands("create")) == 1
        assert len(link.started) == 1 and bridge.pending_calls
        assert "截图" in progress.section(list(bridge.pending_calls.values()))
        assert "截图" not in provider.commands("create")[0][1]
        assert "还在办：“截图”" not in bridge._instructions()
        await replay(bridge, started("receipt"), audio("receipt"), done("receipt"))
        assert any(isinstance(value, bytes) for value in outbox(bridge))
        # Further speech is accepted once and acknowledged while the original
        # main turn is still pending; it does not restart or duplicate that work.
        await replay(bridge, event("user_started", item_id="second"), event("user_stopped"),
                     transcript("你好，听到我了吗？", "second"))
        clock.advance(ACK_WAIT_SECONDS)
        await bridge.fill_idle()
        assert len(provider.commands("create")) == 2
        assert [ref.transcript for ref in link.started] == ["截图", "你好，听到我了吗？"]
        assert len(bridge.pending_calls) == 2
    finally:
        await bridge.close()


async def test_fast_unified_answer_does_not_get_an_extra_waiting_acknowledgement():
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    bridge = Bridge(provider, link, clock=clock, unified=True)
    await greeted(bridge, provider)
    try:
        await replay(bridge, transcript("你好", "hello"))
        link.finish(link.started[0].provider_call_id, speech="你好，我在。")
        await drain()
        await replay(bridge, started("answer"), audio("answer"), done("answer"))
        clock.advance(ACK_WAIT_SECONDS + 1)
        await bridge.fill_idle()
        assert len(provider.commands("create")) == 1
        assert not bridge.pending_calls
    finally:
        await bridge.close()


@pytest.mark.parametrize("newer_speech", [False, True])
async def test_completed_asr_releases_listening_state_without_closing_newer_speech(newer_speech):
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    bridge = Bridge(provider, link, clock=clock, unified=True)
    await greeted(bridge, provider)
    try:
        await replay(bridge, event("user_started", item_id="first"))
        if newer_speech:
            await replay(bridge, event("user_started", item_id="second"))
        # A provider may finish ASR without delivering the earlier VAD stop.
        await replay(bridge, transcript("听到我说话了吗？", "first"))
        assert bridge.state == ("user_speaking" if newer_speech else "idle")
        clock.advance(ACK_WAIT_SECONDS)
        await bridge.fill_idle()
        assert bool(provider.commands("create")) is not newer_speech
        assert len(link.started) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("stage", ["working", "speaking", "buffered"])
async def test_manual_click_withdraws_unified_voice_explanation(stage):
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    bridge = Bridge(provider, link, lang="zh", clock=clock, unified=True)
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [{"id": "placement", "version": "v1"}])
    try:
        await replay(bridge, transcript("接下来还有什么需要我确认？", "ask-about-card"))
        ref = link.started[0]
        if stage != "working":
            link.finish(ref.provider_call_id, speech="还需要确认新建项目。", decision_versions={"placement": "v1"})
            await drain()
            clock.advance(10)
            await bridge.fill_idle()
            await replay(bridge, started("explanation"), audio("explanation", size=480000))
            if stage == "buffered":
                # The provider can finish before the browser's audio buffer does.
                await replay(bridge, done("explanation"))
        outbox(bridge)
        await bridge.waiting_cards([], [])
        if stage == "working":
            link.finish(ref.provider_call_id, speech="还需要确认新建项目。",
                        decision_versions={"placement": "v1"}, speech_stale=True)
            await drain()
            # Only the note that the card went (voice/phrases.closed_decisions_note), never the stale explanation.
            assert not any("还需要确认新建项目" in note for _, note in provider.commands("note"))
            assert all("no_longer_waiting" in note for _, note in provider.commands("note"))
        else:
            assert {"type": "playback.clear"} in outbox(bridge)
            if stage == "speaking":
                assert provider.commands("cancel")
                await replay(bridge, audio("explanation"), done("explanation", status="cancelled"))
        assert not any(isinstance(value, bytes) for value in outbox(bridge))
        assert ref.delivery == "superseded" and not bridge.deliveries
        assert ref.inbox_id  # only stale speech is withdrawn; accepted work stays durable
    finally:
        await bridge.close()


@pytest.mark.parametrize("stage", ["working", "speaking", "buffered"])
@pytest.mark.parametrize("explains_another_card", [False, True])
async def test_unrelated_card_click_preserves_voice_answer(stage, explains_another_card):
    provider, link, clock = ScriptedProvider(), FakeLink(), FakeClock()
    bridge = Bridge(provider, link, lang="zh", clock=clock, unified=True)
    await greeted(bridge, provider)
    kept = {"id": "image-placement", "version": "v1"}
    bridge.seed_waiting_cards([], [kept, {"id": "unrelated-video-quote", "version": "v2"}])
    dependencies = {kept["id"]: kept["version"]} if explains_another_card else {}
    try:
        await replay(bridge, transcript("图片那件事现在怎么样了？", "image-answer"))
        ref = link.started[0]
        if stage != "working":
            link.finish(ref.provider_call_id, speech="图片是白底红色圆形。", decision_versions=dependencies)
            await drain()
            await replay(bridge, started("answer"), audio("answer", size=480000))
            if stage == "buffered":
                await replay(bridge, done("answer"))
        outbox(bridge)
        await bridge.waiting_cards([], [kept])
        if stage == "working":
            link.finish(ref.provider_call_id, speech="图片是白底红色圆形。", decision_versions=dependencies)
            await drain()
        assert ref.delivery != "superseded"
        assert {"type": "playback.clear"} not in outbox(bridge)
        assert not provider.commands("cancel")
        assert ref.inbox_id
    finally:
        await bridge.close()


async def test_unrelated_card_does_not_interrupt_completed_task_report():
    provider, link = ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", unified=True)
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [{"id": "another-task", "version": "v1"}])
    try:
        await bridge.report("图片整理", "已经把三张图片整理好了。", "finished-image-task")
        await replay(bridge, started("report"), audio("report", size=480000))
        outbox(bridge)
        await bridge.waiting_cards([], [])
        assert {"type": "playback.clear"} not in outbox(bridge)
        assert not provider.commands("cancel")
    finally:
        await bridge.close()
