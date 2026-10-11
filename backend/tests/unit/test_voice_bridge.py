"""The call state machine against a scripted provider: greeting, replies, interruptions, hang-up, limit.

Turns, notes and delivery: test_voice_turns.py. Summaries and rotation: test_voice_upkeep.py.
"""
import asyncio

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       of_type, outbox, provider_error, started, tool_call)
from voice import bridge as bridge_module, phrases, tools
from voice.bridge import Bridge, REPLY_GRACE_SECONDS, RECOVERIES_PER_CALL
from voice.provider import ProviderUnavailable


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
                 event("user_transcript", text="帮我看看贪吃蛇进展"), tool_call(call_id), done(response_id))


def phase_values(items):
    return [(item["value"], item["working"], item["late"]) for item in of_type(items, "phase")]


async def test_the_greeting_is_free_words_from_facts_and_its_audio_reaches_the_client(call):
    bridge, provider, _, _ = call
    await bridge.start()
    [(_, instructions)] = provider.commands("create")
    assert instructions == phrases.greeting_instructions("zh")
    assert "上次通话" in instructions and "不推测" in instructions  # a goal, never a sentence to repeat
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
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), tool_call("call-1"))  # the acknowledgement reply is still active
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
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack-2"), audio("ack-2"), tool_call("call-2"), done("ack-2"))
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
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), tool_call("call-1"))
    assert link.started[0].transcript == "帮我看看贪吃蛇进展"  # the actual transcript, never generated tool text
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
    # An Alembic migration test earlier in the run (fileConfig) disables loggers that already exist.
    was_disabled, logger.disabled = logger.disabled, False
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
        logger.disabled = was_disabled


async def test_the_greeting_is_made_before_the_answer_and_its_playback_is_never_cut_by_echo(call):
    """Made while the client rings; once answered, the microphone counts again only after it has played."""
    from voice.bridge import GREETING_MARGIN_SECONDS
    bridge, provider, link, clock = call
    await bridge.start()
    assert not bridge.greeted.is_set()
    await replay(bridge, started("greet"), audio("greet", size=48000), done("greet"))  # one second of speech
    assert bridge.greeted.is_set() and bridge.greeting_bytes == 48000
    bridge.answered()
    await bridge.feed_audio(b"\x10\x00" * 1600)
    clock.advance(1.0 + GREETING_MARGIN_SECONDS - 0.1)
    await bridge.feed_audio(b"\x10\x00" * 1600)
    clock.advance(0.2)
    await bridge.feed_audio(b"\x10\x00" * 1600)
    assert [command[2] for command in provider.commands("audio")] == [b"\0\0\0\0", b"\0\0\0\0", b"\x10\x00\x10\x00"]


async def test_a_refused_greeting_does_not_hold_the_call(call):
    bridge, provider, link, clock = call
    await bridge.start()
    await replay(bridge, provider_error("active_response"))
    assert bridge.greeted.is_set() and not bridge.greeting
    bridge.answered()
    await bridge.feed_audio(b"\x10\x00" * 1600)
    assert provider.commands("audio")[-1][2] == b"\x10\x00\x10\x00"


SPEECH = b"\x10\x00" * 1600


async def answered(bridge, provider, clock, failures=0):
    """Greeted and answered, the greeting played; ``fresh`` lists the sessions opened for the call after that."""
    fresh = []

    async def opener(instructions):
        nonlocal failures
        if failures:
            failures -= 1
            raise ProviderUnavailable("no session")
        fresh.append(ScriptedProvider())
        fresh[-1].instructions = instructions
        return fresh[-1]
    bridge.opener = opener
    await greeted(bridge, provider)
    bridge.answered()
    clock.advance(1)
    return fresh


async def test_a_dropped_session_is_replaced_and_the_call_goes_on_unnoticed(call):
    """2026-10-09: 13 of 62 calls ended when Audio closed an idle session; now a fresh one carries the call."""
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    await replay(bridge, event("user_transcript", text="先等一下", item_id="u1"))
    provider.drop()
    await bridge.feed_audio(SPEECH)  # the socket is gone: never raises, kept for the next session
    assert await bridge.recover(provider)
    assert bridge.provider is fresh[0] and bridge.recoveries == 1 and not (bridge.recovering or bridge.swapping)
    assert fresh[0].commands("audio") == [("audio", 3200, b"\x10\x00\x10\x00")]  # what the user said meanwhile
    assert "用户：先等一下" in fresh[0].instructions  # the call's last lines carry over, as in a rotation
    assert not fresh[0].commands("create") and not outbox(bridge)  # nobody was cut off: nothing said, no event
    await bridge.feed_audio(SPEECH)
    assert len(fresh[0].commands("audio")) == 2


async def test_a_user_cut_off_by_a_drop_is_asked_to_say_it_again(call):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    await replay(bridge, event("user_started"))
    outbox(bridge)
    provider.drop()
    await bridge.feed_audio(SPEECH)
    assert await bridge.recover(provider)
    assert not fresh[0].commands("audio")  # the rest of a sentence whose start went with the old session
    assert fresh[0].commands("create") == [("create", phrases.phrase_instructions("reconnected", "zh"))]
    assert phrases.phrase_text("reconnected", "zh") == "刚才线路断了一下，您再说一遍？"
    await replay(bridge, started("again"), audio("again"), done("again"))
    assert of_type(outbox(bridge), "phrase") == [{"type": "phrase", "key": "reconnected"}]


async def test_a_reply_cut_off_is_billed_once_and_the_user_asked_again(call):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    await replay(bridge, event("user_started"), event("user_stopped"), started("answer"), audio("answer", 48000))
    provider.drop()
    assert await bridge.recover(provider)
    row = bridge.meter.responses["answer"]
    assert row["status"] == "incomplete" and row["audio_bytes"] == 48000 and bridge.settled.is_set()
    assert bridge.meter.snapshot()["unreported_rounds"] == 1  # estimated from the second of audio received
    assert bridge.meter.snapshot()["provisional_yuan"] == "0.000150"
    assert (bridge.state, bridge.active_response) == ("idle", None)
    assert fresh[0].commands("create") == [("create", phrases.phrase_instructions("reconnected", "zh"))]
    bridge.meter.finish()
    assert bridge.meter.snapshot()["unreported_rounds"] == 1  # counted once


async def test_a_result_nobody_heard_is_told_again_in_the_new_session(call):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    await asked(bridge)
    clock.advance(REPLY_GRACE_SECONDS)
    link.finish("call-1")
    await drain()
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("note-1", text=note), started("deliver"))  # its reply began; nothing heard yet
    outbox(bridge)
    provider.drop()
    assert await bridge.recover(provider)
    assert [command[0] for command in fresh[0].sent] == ["note", "create"]  # silently told there instead
    assert fresh[0].commands("note") == [("note", note)] and len(bridge.deliveries) == 1
    await replay(bridge, item("note-2", text=note), started("again"), audio("again"), done("again"))
    assert of_type(outbox(bridge), "turn")[-1]["state"] == "delivered" and not bridge.deliveries


async def test_a_session_that_cannot_be_reopened_ends_the_call(call, monkeypatch):
    monkeypatch.setattr(bridge_module, "RECOVERY_PAUSE_SECONDS", 0)
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock, failures=2)
    provider.drop()
    assert not await bridge.recover(provider)
    assert not fresh and bridge.recoveries == 0 and not bridge.recovering  # two attempts, both failed
    await bridge.feed_audio(SPEECH)  # still never raises while the call ends


@pytest.mark.parametrize("situation", ["ringing", "hanging_up", "limit", "denied", "too_many"])
async def test_no_recovery_where_the_call_ends_anyway(call, situation):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    if situation == "ringing":
        bridge.answered_at = None
    elif situation == "hanging_up":
        await bridge.stop()
    elif situation == "limit":
        await bridge.begin_limit("max_duration", 1800)
    elif situation == "denied":
        await replay(bridge, provider_error("denied", code="Arrearage"))
    else:
        bridge.recoveries = RECOVERIES_PER_CALL
    provider.drop()
    assert not await bridge.recover(provider) and not fresh


async def test_a_session_closing_error_ends_the_stream_for_recovery(call):
    bridge, provider, link, clock = call
    await answered(bridge, provider, clock)
    await replay(bridge, provider_error("session_closed", code="response_idle_timeout"))
    assert provider.closed and bridge.requested is None


async def test_a_rotation_under_way_gives_way_to_the_recovery(call):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    gate = asyncio.Event()

    async def slow_summary():
        await gate.wait()
        return ""
    bridge.keeper.summary_now = slow_summary
    bridge.keeper.session_started -= 25 * 60
    await bridge.fill_idle()
    assert bridge.rotating
    provider.drop()
    assert await bridge.recover(provider)
    assert bridge.provider is fresh[0] and len(fresh) == 1 and not bridge.rotating and bridge._rotation.cancelled()


async def test_a_direct_read_for_the_dropped_session_is_not_answered_in_the_new_one(call, monkeypatch):
    bridge, provider, link, clock = call
    fresh = await answered(bridge, provider, clock)
    gate = asyncio.Event()

    async def overview(scope, arguments):
        await gate.wait()
        return {"status": "ok", "tasks": []}
    monkeypatch.setitem(tools.DIRECT, "tasks_overview", tools.DirectTool("…", overview))
    bridge.scope = tools.CallScope(user_id="u", workspace_id="w", main_session_id="m", call_id="c")
    await replay(bridge, event("user_started"), event("user_stopped"), started("ask"),
                 tool_call("call-9", name="tasks_overview", arguments="{}"), done("ask"))
    provider.drop()
    assert await bridge.recover(provider)
    assert fresh[0].commands("create") == [("create", phrases.phrase_instructions("reconnected", "zh"))]
    gate.set()
    await drain()
    assert not fresh[0].commands("output") and not provider.commands("output")


async def test_a_reply_of_ours_waits_until_the_client_has_played_the_last_one(call):
    """2026-10-09: a result went in at response.done while the client still played the reply before it, and the
    two came out back to back. Ours now start once that reply has played, and a short gap after it."""
    from voice.bridge import PLAYBACK_MARGIN_SECONDS, REPLY_GAP_SECONDS
    bridge, provider, link, clock = call
    bridge.reply_gap = REPLY_GAP_SECONDS
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"), event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), audio("ack", 48000),
                 audio("ack", 48000), tool_call("call-1"), done("ack"))  # two seconds, sent faster than played
    link.finish("call-1")
    await drain()
    assert not provider.commands("note")  # the client is still playing the acknowledgement
    held = 2 + PLAYBACK_MARGIN_SECONDS + REPLY_GAP_SECONDS
    assert held - 0.5 < bridge._wake.when() - asyncio.get_running_loop().time() <= held  # looked at again then
    clock.advance(held - 0.1)
    await bridge.fill_idle()
    assert not provider.commands("note")
    clock.advance(0.1)
    await bridge.fill_idle()
    assert [command[0] for command in provider.sent if command[0] in ("note", "create")] == ["note", "create"]
    await replay(bridge, item("note-1", text=provider.commands("note")[0][1]), started("tell"),
                 audio("tell", 48000), done("tell"))
    await bridge.report("制作iPhone 18口播视频", "做好了。", "inbox-9")
    assert len(provider.commands("note")) == 1  # held: the result is still playing
    await replay(bridge, event("user_started"), event("user_stopped", invalid=True))  # the user cut in: it stopped
    assert len(provider.commands("note")) == 2  # nothing playing any more, nothing to wait for
    await replay(bridge, started("tell-2"), audio("tell-2", 48000), done("tell-2"))
    await bridge.begin_limit("max_duration", 1800)  # the goodbye never waits
    assert provider.commands("create")[-1] == ("create", phrases.phrase_instructions("limit_reached", "zh"))
    await bridge.close()
    assert bridge._wake is None


async def test_the_greeting_made_while_ringing_is_paced_from_the_answer(call):
    from voice.bridge import PLAYBACK_MARGIN_SECONDS, REPLY_GAP_SECONDS
    bridge, provider, link, clock = call
    bridge.reply_gap = REPLY_GAP_SECONDS
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet", 48000), done("greet"))  # one second, made while ringing
    clock.advance(3)
    bridge.answered()  # the client starts playing it only now
    await bridge.report("制作iPhone 18口播视频", "做好了。", "inbox-9")
    assert not provider.commands("note")
    clock.advance(1 + PLAYBACK_MARGIN_SECONDS + REPLY_GAP_SECONDS)
    await bridge.fill_idle()
    assert provider.commands("note")
    await bridge.close()


async def test_without_pacing_a_result_goes_in_as_soon_as_the_reply_is_done(call):
    bridge, provider, link, clock = call
    assert bridge.reply_gap is None  # what the other unit tests run with; api/voice.py paces every call
    await greeted(bridge, provider)
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), audio("ack", 96000), tool_call("call-1"), done("ack"))
    link.finish("call-1")
    await drain()
    assert [command[0] for command in provider.sent if command[0] in ("note", "create")] == ["note", "create"]
