"""The call state machine against a scripted provider (docs/VOICE_CALL_SPEC.md §4.2 rules 1-7)."""
import asyncio

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, of_type,
                                       outbox, provider_error, started, tool_call)
from voice import phrases
from voice.bridge import Bridge, REPLY_GRACE_SECONDS


@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", late_after=20, clock=clock)
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


async def test_greeting_is_a_verbatim_phrase_reply_and_its_audio_reaches_the_client(call):
    bridge, provider, _, _ = call
    await bridge.start()
    assert provider.commands("create") == [("create", phrases.phrase_instructions("greeting", "zh"))]
    assert "嗨，我在，你说。" in provider.commands("create")[0][1]
    await replay(bridge, started("greet"), audio("greet", 4800), done("greet"))
    items = outbox(bridge)
    assert of_type(items, "phrase") == [{"type": "phrase", "key": "greeting"}]
    assert [item for item in items if isinstance(item, bytes)] == [b"\x01\x00" * 2400]
    assert phase_values(items)[0] == ("greeting", False, False)
    assert phase_values(items)[-1] == ("listening", False, False)
    assert of_type(items, "cost")[-1]["settled_rounds"] == 1


async def test_rule1_tool_call_accepts_a_turn_and_reports_working(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    items = outbox(bridge)
    [ref] = link.started
    assert ref.text == "帮我看看贪吃蛇进展" and ref.provider_call_id == "call-1"
    [turn] = of_type(items, "turn")
    assert turn == {"type": "turn", "turn_id": ref.id, "state": "accepted", "inbox_id": "inbox-1", "message_id": None}
    assert ("speaking", True, False) in phase_values(items)  # the acknowledgement is still playing
    assert phase_values(items)[-1] == ("working", True, False)
    assert len(bridge.pending_calls) == 1 and len(bridge.refs) == 1
    link.on_message(ref)  # the run claimed the input: the client can scroll to the message
    assert of_type(outbox(bridge), "turn")[0]["state"] == "working"


async def test_rule2_and_rule3_result_is_output_at_once_and_read_once_when_idle(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    link.finish("call-1", speech="贪吃蛇的收尾自检做完了。")
    await drain()
    assert provider.commands("output") == [("output", "call-1", {"status": "ok", "speech": "贪吃蛇的收尾自检做完了。"})]
    [create] = provider.commands("create")
    assert create[1] == phrases.delivery_instructions("贪吃蛇的收尾自检做完了。", "zh")
    assert create[1].endswith("我这边查到了，贪吃蛇的收尾自检做完了。")
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    items = outbox(bridge)
    [delivered] = [item for item in of_type(items, "turn") if item["state"] == "delivered"]
    assert delivered["turn_id"] == link.started[0].id
    assert len(provider.commands("create")) == 1 and not bridge.deliveries
    assert (link.started[0].id, {"delivered": True, "outcome": "delivered"}) in link.records
    assert phase_values(items)[-1] == ("listening", False, False)


async def test_rule3_no_reply_is_requested_while_responding_or_while_the_user_talks(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("ack"), tool_call("call-1"))  # the acknowledgement reply is still active
    link.finish("call-1")
    await drain()
    assert provider.commands("output") and not provider.commands("create")
    await replay(bridge, event("user_started"))  # the reply is interrupted and still not done
    await replay(bridge, done("ack", status="cancelled"))
    assert not provider.commands("create")  # the user is still talking
    await replay(bridge, event("user_stopped", invalid=True))  # a filtered backchannel: nobody answers it
    assert len(provider.commands("create")) == 1


async def test_rule3_the_vad_reply_to_a_finished_utterance_is_not_preempted(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, event("user_started"), event("user_stopped"))  # the VAD answers this by itself
    link.finish("call-1")
    await drain()
    assert not provider.commands("create")
    assert phase_values(outbox(bridge))[-1][0] == "thinking"
    await replay(bridge, started("vad"), audio("vad"), done("vad"))
    assert len(provider.commands("create")) == 1  # read right after the VAD's own reply
    await replay(bridge, started("deliver-1"), audio("deliver-1"), done("deliver-1"))
    provider.sent.clear()
    # When the VAD does not answer, the result is read once the grace period has passed.
    await replay(bridge, started("ack-2"), tool_call("call-2"), done("ack-2"))
    await replay(bridge, event("user_started"), event("user_stopped"))
    link.finish("call-2")
    await drain()
    assert not provider.commands("create")
    clock.advance(REPLY_GRACE_SECONDS)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1


async def test_rule3_active_response_refusal_requeues_and_retries_after_the_next_reply(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    link.finish("call-1")
    await drain()
    assert len(provider.commands("create")) == 1
    await replay(bridge, provider_error("active_response"))  # a VAD reply won the race
    assert bridge.requested is None and bridge.deliveries[0].delivery == "queued"
    await replay(bridge, started("vad"), audio("vad"), done("vad"))
    assert len(provider.commands("create")) == 2
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert [item["state"] for item in of_type(outbox(bridge), "turn")].count("delivered") == 1


async def test_rule3_each_call_id_gets_exactly_one_output(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    await replay(bridge, tool_call("call-1"))  # the provider repeats the same call
    assert len(link.started) == 1
    link.finish("call-1")
    await drain()
    await replay(bridge, provider_error("duplicate_output"))  # ignored: nothing to retry
    assert len(provider.commands("output")) == 1 and len(provider.commands("create")) == 1


async def test_rule4_still_working_once_per_turn_and_only_when_idle(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    outbox(bridge)
    clock.advance(19)
    await bridge.fill_idle()
    assert not provider.commands("create")
    await replay(bridge, started("chat"))  # the user is chatting about something else
    clock.advance(5)
    await bridge.fill_idle()
    assert not provider.commands("create")  # late, but not idle
    await replay(bridge, audio("chat"), done("chat"))
    assert provider.commands("create") == [("create", phrases.phrase_instructions("still_working", "zh"))]
    items = outbox(bridge)
    assert [item["state"] for item in of_type(items, "turn")] == ["late"]
    await replay(bridge, started("still"), audio("still"), done("still"))
    items += outbox(bridge)
    assert of_type(items, "phrase") == [{"type": "phrase", "key": "still_working"}]
    assert phase_values(items)[-1] == ("working", True, True)
    clock.advance(30)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1  # at most once per turn
    link.finish("call-1")
    await drain()
    assert phase_values(outbox(bridge))[-1] == ("working", True, False)  # no longer late; working until read out
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert [value[0] for value in phase_values(outbox(bridge))] == ["speaking", "listening"]


async def test_rule5_timeout_is_output_once_and_said_without_claiming_a_result(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    speech = phrases.speech_text("timeout", "zh")
    link.finish("call-1", status="timeout", speech=speech)
    await drain()
    assert provider.commands("output") == [("output", "call-1", {"status": "timeout", "speech": speech})]
    [create] = provider.commands("create")
    assert create[1] == phrases.notice_instructions(speech, "zh") and "查到了" not in create[1]
    assert [item["state"] for item in of_type(outbox(bridge), "turn")] == ["accepted", "timeout"]
    await replay(bridge, started("notice"), audio("notice"), done("notice"))
    assert not [item for item in of_type(outbox(bridge), "turn") if item["state"] == "delivered"]


async def test_rule6_hang_up_reports_pending_turns_and_records_them_late(call):
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
    assert outcomes[link.started[1].id] == "cancelled"  # settled but never read out


async def test_rule7_user_speech_clears_playback_and_drops_the_old_reply(call):
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


async def test_rule7_a_reply_created_while_the_user_talks_is_cancelled_and_read_later(call):
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
    assert len(provider.commands("create")) == 2  # nobody heard it, so it is read again


async def test_other_refusal_says_the_result_is_in_the_text(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await asked(bridge)
    link.finish("call-1")
    await drain()
    await replay(bridge, provider_error("other"))
    assert provider.commands("create")[-1] == ("create", phrases.phrase_instructions("result_in_text", "zh"))
    assert [item["state"] for item in of_type(outbox(bridge), "turn")] == ["accepted", "failed"]
    assert not bridge.deliveries


async def test_failed_acceptance_and_unknown_tools_are_answered(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await replay(bridge, started("ack"), tool_call("call-x", name="weather"), done("ack"))
    assert provider.commands("output") == [("output", "call-x", {"status": "unknown_tool"})]
    link.fail_start = True
    await replay(bridge, started("ack-2"), tool_call("call-1"), done("ack-2"))
    unavailable = phrases.speech_text("unavailable", "zh")
    assert provider.commands("output")[-1] == ("output", "call-1", {"status": "failed", "speech": unavailable})
    assert [item["state"] for item in of_type(outbox(bridge), "turn")] == ["failed"]
    assert provider.commands("create")[-1][1] == phrases.notice_instructions(unavailable, "zh")


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


async def test_a_request_answered_by_a_simultaneous_vad_reply_is_read_after_it(call):
    bridge, provider, link, clock = call
    await greeted(bridge, provider)
    await asked(bridge)
    clock.advance(REPLY_GRACE_SECONDS)
    link.finish("call-1")
    await drain()
    assert len(provider.commands("create")) == 1
    await replay(bridge, started("vad"), provider_error("active_response"), audio("vad"), done("vad"))
    assert not [item for item in of_type(outbox(bridge), "turn") if item["state"] == "delivered"]
    assert len(provider.commands("create")) == 2  # read after the VAD reply it lost to
    await replay(bridge, started("deliver"), audio("deliver"), done("deliver"))
    assert [item["state"] for item in of_type(outbox(bridge), "turn")] == ["delivered"]


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

