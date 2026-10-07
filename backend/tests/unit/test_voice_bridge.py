"""The call state machine against a scripted provider: greeting, replies, interruptions, hang-up, limit.

Turns, notes and delivery: test_voice_turns.py. Summaries and rotation: test_voice_upkeep.py.
"""
import asyncio

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, of_type,
                                       outbox, provider_error, started, tool_call)
from voice import phrases
from voice.bridge import Bridge, REPLY_GRACE_SECONDS


@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", late_after=12, clock=clock)
    return bridge, provider, link, clock


async def replay(bridge, *events):
    for item in events:
        await bridge.on_provider_event(item)
    await drain()


async def greeted(bridge, provider):
    """Connected and greeted: the state every other case starts from."""
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    provider.sent.clear()
    outbox(bridge)


async def asked(bridge, call_id="call-1", response_id="ack"):
    """The front desk says its acknowledgement and calls assistant_ask in the same reply."""
    await replay(bridge, event("user_started"), event("user_stopped"), started(response_id), audio(response_id),
                 tool_call(call_id), done(response_id))


def phase_values(items):
    return [(item["value"], item["working"], item["late"]) for item in of_type(items, "phase")]


async def test_the_greeting_is_free_words_from_facts_and_its_audio_reaches_the_client(call):
    bridge, provider, _, _ = call
    await bridge.start()
    [(_, instructions)] = provider.commands("create")
    assert instructions == phrases.greeting_instructions("zh")
    assert "上次通话" in instructions and "不要编造" in instructions  # a goal, never a sentence to repeat
    assert "嗨，我在" not in instructions and "只说这一句" not in instructions
    await replay(bridge, started("greet"), audio("greet", 4800), done("greet"))
    items = outbox(bridge)
    assert of_type(items, "phrase") == [{"type": "phrase", "key": "greeting"}]
    assert [item for item in items if isinstance(item, bytes)] == [b"\x01\x00" * 2400]
    assert phase_values(items)[0] == ("greeting", False, False)
    assert phase_values(items)[-1] == ("listening", False, False)
    assert of_type(items, "cost")[-1]["settled_rounds"] == 1


async def test_no_reply_is_requested_while_responding_or_while_the_user_talks(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await replay(bridge, started("ack"), tool_call("call-1"))  # the acknowledgement reply is still active
    link.finish("call-1")
    await drain()
    assert not provider.commands("create") and not provider.commands("note")
    await replay(bridge, event("user_started"))  # the reply is interrupted and still not done
    await replay(bridge, done("ack", status="cancelled"))
    assert not provider.commands("create")  # the user is still talking
    await replay(bridge, event("user_stopped", invalid=True))  # a filtered backchannel: nobody answers it
    assert [command[0] for command in provider.sent if command[0] in ("note", "create")] == ["note", "create"]


async def test_the_vad_reply_to_a_finished_utterance_is_not_preempted(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, event("user_started"), event("user_stopped"))  # the VAD answers this by itself
    link.finish("call-1")
    await drain()
    assert not provider.commands("create") and not provider.commands("note")
    assert phase_values(outbox(bridge))[-1][0] == "thinking"
    await replay(bridge, started("vad"), audio("vad"), done("vad"))
    assert len(provider.commands("create")) == 1  # told right after the VAD's own reply
    await replay(bridge, started("deliver-1"), audio("deliver-1"), done("deliver-1"))
    provider.sent.clear()
    # When the VAD does not answer, the result is told once the grace period has passed.
    await replay(bridge, started("ack-2"), audio("ack-2"), tool_call("call-2"), done("ack-2"))
    await replay(bridge, event("user_started"), event("user_stopped"))
    link.finish("call-2")
    await drain()
    assert not provider.commands("create")
    clock.advance(REPLY_GRACE_SECONDS)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1


async def test_hang_up_reports_pending_turns_and_records_them_late(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge, "call-1", "ack-1")
    await asked(bridge, "call-2", "ack-2")
    link.finish("call-2")
    await drain()
    await bridge.stop()
    assert len(bridge.pending_calls) == 1
    link.finish("call-1")  # the observer stops at hang-up; the assistant keeps working
    await bridge.close()
    outcomes = {ref_id: fields.get("outcome") for ref_id, fields in link.records}
    assert outcomes[link.started[1].id] == "cancelled"  # settled but never told


async def test_user_speech_clears_playback_and_drops_the_old_reply(call):
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("one"), audio("one", 4800))
    await replay(bridge, event("user_started"), audio("one", 4800))  # arrives after the interruption
    items = outbox(bridge)
    assert [item for item in items if isinstance(item, bytes)] == [b"\x01\x00" * 2400]
    assert of_type(items, "playback.clear") == [{"type": "playback.clear"}]
    assert phase_values(items)[-1] == ("listening", False, False)
    await replay(bridge, done("one", status="cancelled"), event("user_stopped"), started("two"), audio("two", 960))
    assert [item for item in outbox(bridge) if isinstance(item, bytes)] == [b"\x01\x00" * 480]
    assert bridge.meter.responses["one"]["audio_bytes"] == 9600  # billed although never played


async def test_a_reply_created_while_the_user_talks_is_cancelled_and_told_later(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    clock.advance(REPLY_GRACE_SECONDS)
    link.finish("call-1")
    await drain()
    assert len(provider.commands("create")) == 1
    outbox(bridge)
    await replay(bridge, event("user_started"), started("deliver"), audio("deliver"))
    assert provider.commands("cancel") and not [i for i in outbox(bridge) if isinstance(i, bytes)]
    await replay(bridge, done("deliver", status="cancelled"), event("user_stopped", invalid=True))
    assert len(provider.commands("create")) == 2  # nobody heard it, so it is told again


async def test_stop_cancels_the_reply_and_waits_for_its_usage(call):
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("talk"), audio("talk"))
    outbox(bridge)
    stopping = asyncio.create_task(bridge.stop())
    await drain()
    assert provider.commands("cancel") and not stopping.done()
    await replay(bridge, done("talk", status="cancelled"))
    await asyncio.wait_for(stopping, 1)
    assert bridge.meter.responses["talk"]["tokens"]["output_audio"] == 50
    assert not outbox(bridge)  # nothing more for the client after hang-up; the socket sends ended


async def test_unsupported_voice_fails_the_call(call):
    bridge, provider, _, _ = call
    await bridge.start()
    await replay(bridge, provider_error("voice_unsupported"))
    assert bridge.failed.is_set()


async def test_limit_cancels_the_reply_says_the_phrase_and_silences_the_microphone(call):
    bridge, provider, _, clock = call
    await greeted(bridge, provider)
    await replay(bridge, started("talk"), audio("talk"))
    await bridge.begin_limit("max_duration", 1800.4)
    assert provider.commands("cancel")
    assert of_type(outbox(bridge), "limit") == [{"type": "limit", "reason": "max_duration", "elapsed_seconds": 1800}]
    await bridge.feed_audio(b"\x05\x00" * 1600)
    assert provider.commands("audio")[-1] == ("audio", 3200, b"\x00\x00\x00\x00")
    await replay(bridge, done("talk", status="cancelled"))
    assert provider.commands("create") == [("create", phrases.phrase_instructions("limit_reached", "zh"))]
    # A VAD reply created in the same instant wins; the provider refuses ours.
    await replay(bridge, started("vad"), provider_error("active_response"))
    assert len(provider.commands("cancel")) == 2 and not bridge.limit_done.is_set()
    await replay(bridge, done("vad", status="cancelled"))
    assert len(provider.commands("create")) == 2
    await replay(bridge, started("bye"), audio("bye", 48000), done("bye"))
    assert bridge.limit_done.is_set() and 1 < bridge.playback_left() <= 1.3
    clock.advance(2)
    assert bridge.playback_left() == 0


async def test_a_turn_keeps_the_words_of_its_own_utterance(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"),
                 event("user_transcript", text="今天几号"), started("chat"), done("chat"))
    await replay(bridge, event("user_started"), event("user_stopped"), started("ack"), tool_call("call-1"))
    assert link.started[0].transcript == "帮我看看贪吃蛇进展"  # its transcription had not arrived yet
    await replay(bridge, done("ack"), event("user_started"), event("user_stopped"),
                 event("user_transcript", text="我有什么待办"), started("ack-2"), tool_call("call-2", text="我有什么待办？"))
    assert link.started[1].transcript == "我有什么待办"


async def test_transcripts_are_kept_for_the_call_and_logged_with_the_call_id_only_in_qa():
    import logging
    from voice.tools import CallScope
    lines = []
    handler = logging.Handler()
    handler.emit = lambda record: lines.append(record.getMessage())
    logger = logging.getLogger("openbox.voice.bridge")  # OpenBox loggers do not propagate to caplog
    logger.addHandler(handler)
    try:
        clock = FakeClock()
        bridge = Bridge(ScriptedProvider(), FakeLink(), clock=clock, debug_transcripts=True,
                        scope=CallScope(user_id="u", workspace_id="w", main_session_id="m", call_id="call-9"))
        await replay(bridge, event("user_transcript", text="你好", item_id="i1"),
                     event("assistant_transcript", text="你好呀", item_id="i2"),
                     event("assistant_transcript", text="你好呀", item_id="i2"))  # the text twin of the audio transcript
        assert [(line.role, line.text) for line in bridge.spoken.lines] == [("user", "你好"), ("assistant", "你好呀")]
        assert "voice transcript call=call-9 user text=你好" in lines
        assert lines.count("voice transcript call=call-9 assistant text=你好呀") == 1
        quiet = Bridge(ScriptedProvider(), FakeLink(), clock=clock)
        lines.clear()
        await replay(quiet, event("user_transcript", text="秘密"))
        assert not [line for line in lines if "秘密" in line] and quiet.spoken.lines[0].text == "秘密"
    finally:
        logger.removeHandler(handler)
