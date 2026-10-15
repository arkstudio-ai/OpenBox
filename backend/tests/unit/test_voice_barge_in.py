import pytest

from tests.support.voice_fakes import audio, done, event, of_type, outbox, started
from tests.unit.test_voice_bridge import call, greeted, replay  # noqa: F401
from voice.provider import translate


async def test_microphone_noise_or_echo_without_a_user_turn_cannot_cancel_the_opening(call):
    """2026-10-15: raw VAD cancelled both real browser openings without a valid user utterance."""
    bridge, provider, _, _ = call
    await bridge.start()
    await replay(bridge, started("opening"), audio("opening", size=48000))
    outbox(bridge)
    for pcm in (bytes(3200), b"\xe8\x03\x18\xfc" * 800, b"\xff\x7f\x01\x80" * 800):
        await bridge.feed_audio(pcm)
        assert provider.commands("audio")[-1][2] == pcm[:4]
    assert not provider.commands("cancel") and not of_type(outbox(bridge), "playback.clear")
    await replay(bridge, audio("opening"), done("opening"))
    assert any(isinstance(item, bytes) for item in outbox(bridge)) and bridge.greeted.is_set()


@pytest.mark.parametrize("purpose", ["opening", "answer", "report"])
async def test_confirmed_partial_speech_cuts_any_playback_without_authorizing_work(call, purpose):
    bridge, provider, _, _ = call
    if purpose == "opening":
        await bridge.start()
    else:
        await greeted(bridge, provider)
        if purpose == "report":
            await bridge.report("已有任务", "真实后台结果", "inbox-result")
    await replay(bridge, started(purpose), audio(purpose, size=48000))
    outbox(bridge)
    partial = translate({"type": "conversation.item.input_audio_transcription.delta",
                         "item_id": "actual-user", "text": "先停一下", "stash": "未确定"})
    await replay(bridge, partial)
    assert of_type(outbox(bridge), "playback.clear") and provider.commands("cancel")
    assert bridge.interrupted and bridge.greeted.is_set()
    assert bridge.heard_count == 0 and not bridge.utterance and not bridge._last_user_item
    await replay(bridge, audio(purpose), done(purpose, status="cancelled"))
    assert not any(isinstance(item, bytes) for item in outbox(bridge))
    assert not bridge._can_inject()
    await replay(bridge, started("too-early"), audio("too-early"), done("too-early", status="cancelled"))
    assert not any(isinstance(item, bytes) for item in outbox(bridge))
    await replay(bridge, event("user_started", item_id="actual-user"), event("user_stopped"))
    assert bridge._last_user_item == "actual-user"
    assert not of_type(outbox(bridge), "playback.clear")  # no second clear for the same utterance
    await replay(bridge, started("answer"), audio("answer"))
    assert any(isinstance(item, bytes) for item in outbox(bridge))


def test_ambient_or_unconfirmed_asr_does_not_claim_the_user_has_interrupted():
    for kind in ("conversation.item.ambient_audio_transcription.delta",
                 "conversation.item.ambient_audio_transcription.completed"):
        assert translate({"type": kind, "item_id": "ambient", "text": "环境声", "transcript": "环境声"}) is None
    assert translate({"type": "conversation.item.input_audio_transcription.delta", "item_id": "u",
                      "text": "", "stash": "尚未确定"}) is None
    assert translate({"type": "conversation.item.input_audio_transcription.delta", "text": "无音频项"}) is None
    assert translate({"type": "conversation.item.input_audio_transcription.delta", "item_id": "u",
                      "delta": "stop"}).kind == "user_transcribing"


async def test_late_asr_for_a_finished_turn_cannot_cancel_its_answer(call):
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started", item_id="finished-user"), event("user_stopped"),
                 started("answer"), audio("answer", size=48000))
    provider.sent.clear()
    outbox(bridge)
    await replay(bridge, translate({"type": "conversation.item.input_audio_transcription.delta",
                                   "item_id": "finished-user", "text": "迟到的转写"}), audio("answer"))
    items = outbox(bridge)
    assert not provider.commands("cancel") and not of_type(items, "playback.clear")
    assert any(isinstance(item, bytes) for item in items)
