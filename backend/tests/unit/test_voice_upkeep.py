"""Long calls (docs/ASSISTANT_VOICE_FIX_PLAN.md §5.3): summaries, deleted items, a fresh provider session."""
import asyncio
from datetime import datetime

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       outbox, provider_error, silent_reply, started, tool_call)
from voice import upkeep
from voice.bridge import KEEPALIVE_RETRY_SECONDS, REPLY_GRACE_SECONDS, Bridge
from voice.models import AUDIO_MODEL, OMNI_MODEL
from voice.transcript import CallTranscript
from voice.upkeep import KEEP_ITEMS, ContextKeeper

WALL = datetime(2026, 10, 7, 20, 30)


class Summaries:
    """Records what it was asked to fold; answers at once or when released."""

    def __init__(self, text="用户问了贪吃蛇的进展，已经告诉他做完了。", gate=None):
        self.text, self.gate, self.calls = text, gate, []

    async def __call__(self, transcript, previous):
        self.calls.append((transcript, previous))
        if self.gate is not None:
            await self.gate.wait()
        return self.text


@pytest.fixture
def call():
    clock, provider, link, summaries = FakeClock(), ScriptedProvider(), FakeLink(), Summaries()
    bridge = Bridge(provider, link, lang="zh", late_after=12, clock=clock, instructions="BASE",
                    summarizer=summaries, wall_clock=lambda: WALL)
    return bridge, provider, link, clock, summaries


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def exchange(bridge, number, *, tokens=1200):
    """One user utterance and the front desk's reply, with their provider items."""
    usage = {"input_tokens": tokens, "output_tokens": 60, "input_tokens_details": {"text_tokens": tokens,
             "audio_tokens": 0}, "output_tokens_details": {"text_tokens": 10, "audio_tokens": 50}}
    await replay(bridge, event("user_started"), item(f"u{number}"), event("user_stopped"),
                 event("user_transcript", text=f"第{number}句话", item_id=f"u{number}"),
                 started(f"r{number}"), item(f"a{number}", role="assistant"), audio(f"r{number}"),
                 event("assistant_transcript", text=f"第{number}句回答", item_id=f"a{number}"),
                 done(f"r{number}", usage=usage))


async def test_every_ten_turns_the_call_is_folded_and_early_items_deleted_oldest_first(call):
    bridge, provider, _, clock, summaries = call
    for number in range(1, 10):
        await exchange(bridge, number)
    assert not summaries.calls
    await exchange(bridge, 10)
    await drain()
    [(folded, previous)] = summaries.calls
    assert previous == "" and folded.splitlines()[0] == "20:30 用户：第1句话"
    assert folded.splitlines()[-1] == "20:30 前台：第10句回答"
    clock.advance(3)
    await bridge.fill_idle()
    early = [f"{kind}{number}" for number in range(1, 7) for kind in ("u", "a")][:20 - KEEP_ITEMS]
    assert [item_id for _, item_id in provider.commands("delete")] == early  # in creation order
    assert [entry.id for entry in bridge.spoken.items] == [f"{kind}{number}" for number in range(7, 11)
                                                          for kind in ("u", "a")]
    assert provider.commands("instructions")[-1][1] == "BASE\n本通电话到目前为止：" + summaries.text
    for number in range(11, 21):
        await exchange(bridge, number)
    await drain()
    assert len(summaries.calls) == 2 and summaries.calls[1][1] == summaries.text  # folds onto the memo
    assert summaries.calls[1][0].splitlines()[0] == "20:30 用户：第11句话"


async def test_a_large_context_is_folded_early_and_a_failed_summary_deletes_nothing(call):
    bridge, provider, _, clock, summaries = call
    for number in range(1, 3):
        await exchange(bridge, number, tokens=70_000)
    assert not summaries.calls  # too few turns since the last memo
    await exchange(bridge, 3, tokens=70_000)
    await drain()
    assert len(summaries.calls) == 1
    summaries.text = ""
    bridge.keeper.start_summary()
    await drain()
    clock.advance(3)
    await bridge.fill_idle()
    assert bridge.keeper.summary == "用户问了贪吃蛇的进展，已经告诉他做完了。"  # the earlier memo stays


async def test_a_told_note_leaves_three_replies_later_and_an_untold_one_stays():
    transcript = CallTranscript(now=lambda: WALL)
    for item_id in ("n1", "x1", "x2", "n2"):
        transcript.item_created(item_id, "user", "message")
    keeper = ContextKeeper(transcript, clock=FakeClock(), protected=lambda: {"n2"})
    keeper.note_delivered("n1")
    for _ in range(2):
        keeper.response_done(None)
        assert keeper.due_deletions() == []
    keeper.response_done(None)
    assert keeper.due_deletions() == ["n1"]
    keeper._deletions = ["n2", "x1"]
    assert keeper.due_deletions() == ["x1"] and keeper._deletions == ["n2"]  # an untold note waits


async def test_a_long_call_moves_to_a_fresh_provider_session_without_the_client_noticing(call):
    bridge, provider, link, clock, summaries = call
    fresh = []

    async def opener(instructions):
        fresh.append(ScriptedProvider())
        fresh[-1].instructions = instructions
        return fresh[-1]
    bridge.opener = opener
    await exchange(bridge, 1)
    outbox(bridge)
    clock.advance(upkeep.ROTATE_SECONDS)
    await bridge.fill_idle()
    await drain()
    assert bridge.provider is fresh[0] and provider.closed and not bridge.rotating
    assert fresh[0].instructions == ("BASE\n本通电话到目前为止：" + summaries.text
                                     + "\n刚才最后几句：\n20:30 用户：第1句话\n20:30 前台：第1句回答")
    assert not outbox(bridge)  # no ready, no phase, nothing
    assert bridge.spoken.items == [] and bridge.keeper.input_tokens == 0
    await bridge.feed_audio(b"\x01\x00" * 1600)
    assert fresh[0].commands("audio") and not provider.commands("audio")
    await replay(bridge, started("next"), audio("next"), done("next"))
    assert bridge.state == "idle" and bridge.meter.responses["next"]["tokens"]
    clock.advance(5)
    await bridge.fill_idle()
    assert not fresh[0].commands("instructions")  # the carried lines stay until the next memo


async def test_rotation_waits_for_a_quiet_moment_and_retries(call):
    bridge, provider, link, clock, summaries = call
    gate, fresh = asyncio.Event(), []

    async def opener(instructions):
        await gate.wait()
        fresh.append(ScriptedProvider())
        return fresh[-1]
    bridge.opener = opener
    await exchange(bridge, 1, tokens=130_000)  # far past the token limit
    await drain()
    assert bridge.rotating and bridge.swapping
    assert not await bridge.say_phrase("result_in_text")  # nothing of ours starts while it opens
    await replay(bridge, event("user_started"))  # the user talks while the new session opens
    gate.set()
    await drain()
    assert bridge.provider is provider and fresh[0].closed and not bridge.rotating
    await replay(bridge, event("user_stopped", invalid=True))
    assert bridge.provider is provider  # retried only after a pause
    clock.advance(upkeep.ROTATE_RETRY_SECONDS)
    await bridge.fill_idle()
    await drain()
    assert bridge.provider is fresh[1]


async def test_a_result_arriving_while_the_new_session_opens_is_told_there(call):
    bridge, provider, link, clock, summaries = call
    gate, fresh = asyncio.Event(), []

    async def opener(instructions):
        await gate.wait()
        fresh.append(ScriptedProvider())
        return fresh[-1]
    bridge.opener, bridge.late_after = opener, 600  # a progress reply would come first otherwise
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), audio("ack"), tool_call("call-1"), done("ack"))
    bridge.keeper.session_started -= upkeep.ROTATE_SECONDS
    clock.advance(30)
    await bridge.fill_idle()
    assert bridge.rotating
    await drain()
    assert bridge.swapping  # wait for the summary before checking the new-session window
    link.finish("call-1")
    await drain()
    assert not provider.commands("note")  # nothing new goes into the old session
    gate.set()
    await drain()
    assert bridge.provider is fresh[0]
    await bridge.fill_idle()
    assert [command[0] for command in fresh[0].sent] == ["note", "create"]


async def test_results_are_still_told_while_the_memo_for_a_rotation_is_made():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    gate, fresh = asyncio.Event(), []
    summaries = Summaries(gate=gate)

    async def opener(instructions):
        fresh.append(ScriptedProvider())
        return fresh[-1]
    bridge = Bridge(provider, link, clock=clock, instructions="BASE", summarizer=summaries, opener=opener,
                    wall_clock=lambda: WALL)
    bridge.late_after = 600
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), audio("ack"), tool_call("call-1"), done("ack"))
    await exchange(bridge, 1, tokens=130_000)
    await drain()
    assert bridge.rotating and not bridge.swapping  # waiting for the memo, the call goes on
    link.finish("call-1")
    await drain()
    assert [command[0] for command in provider.sent if command[0] in ("note", "create")] == ["note", "create"]
    await replay(bridge, item("note-1", text=provider.commands("note")[0][1]), started("deliver"),
                 audio("deliver"), done("deliver"))
    gate.set()
    await drain()
    assert bridge.provider is fresh[0] and not bridge.rotating


def kept_call(model):
    """A call whose provider closes an idle session: the bridge keeps it open for this model."""
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="zh", late_after=12, clock=clock, instructions="BASE",
                    summarizer=Summaries(), wall_clock=lambda: WALL,
                    keepalive_after=upkeep.keepalive_seconds(model))
    return bridge, provider, link, clock


def test_keepalive_thresholds_stay_a_minute_below_each_providers_limit():
    assert upkeep.keepalive_seconds(AUDIO_MODEL) == 120  # Audio closes a session after 180 s without a response
    assert upkeep.keepalive_seconds(OMNI_MODEL) == 240   # Omni after 300 s
    assert upkeep.keepalive_seconds(AUDIO_MODEL, 90) == upkeep.keepalive_seconds(OMNI_MODEL, 90) == 90
    keeper = ContextKeeper(CallTranscript(now=lambda: WALL), clock=FakeClock())
    keeper.clock.advance(10_000)
    assert not keeper.keepalive_due()  # without a threshold: never (tests, scripts)


@pytest.mark.parametrize("model, after", [(AUDIO_MODEL, 120), (OMNI_MODEL, 240)])
async def test_a_silent_reply_keeps_an_idle_session_open(model, after):
    bridge, provider, _, clock = kept_call(model)
    await exchange(bridge, 1)
    clock.advance(after - 1)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.commands("keepalive") == [("keepalive", False)]  # the user has spoken in this session
    await bridge.fill_idle()
    assert len(provider.commands("keepalive")) == 1 and bridge.requested == "keepalive"  # one at a time


async def test_the_silent_reply_is_invisible_billed_and_deleted():
    bridge, provider, link, clock = kept_call(AUDIO_MODEL)
    await exchange(bridge, 1)
    outbox(bridge)
    lines, items, replies = list(bridge.spoken.lines), list(bridge.spoken.items), bridge.keeper.replies
    clock.advance(120)
    await bridge.fill_idle()
    await replay(bridge, *silent_reply("keep", "item_keep"))
    assert not outbox(bridge)  # no phase, no audio, no cost event: nothing reaches the client
    assert bridge.spoken.lines == lines and bridge.spoken.items == items  # nor the transcript and summaries
    assert bridge.keeper.replies == replies and not bridge._replies and not bridge._answering
    assert not bridge.followup_due and not link.started and not bridge.deliveries  # no turn logic either
    assert provider.commands("delete") == [("delete", "item_keep")]  # no later reply reads "嗯"
    assert bridge.meter.responses["keep"]["tokens"]["output_text"] == 2  # billed like any reply
    assert (bridge.state, bridge.active_response, bridge._keepalives) == ("idle", None, {})
    clock.advance(119)
    await bridge.fill_idle()
    assert len(provider.commands("keepalive")) == 1  # its own done restarted the clock
    clock.advance(1)
    await bridge.fill_idle()
    assert len(provider.commands("keepalive")) == 2


async def test_any_reply_restarts_the_clock_and_none_is_sent_while_something_is_going_on():
    bridge, provider, _, clock = kept_call(AUDIO_MODEL)
    await exchange(bridge, 1)
    clock.advance(100)
    await replay(bridge, started("vad"), audio("vad"), done("vad", status="cancelled"))  # cut short: still counts
    clock.advance(100)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")
    clock.advance(20)
    await replay(bridge, event("user_started"))
    await bridge.fill_idle()
    assert not provider.commands("keepalive")  # the user is speaking
    await replay(bridge, event("user_stopped"))
    await bridge.fill_idle()
    assert not provider.commands("keepalive")  # the VAD answers by itself: its grace
    await replay(bridge, started("answer"), audio("answer"))
    clock.advance(REPLY_GRACE_SECONDS)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")  # a reply is playing
    await replay(bridge, done("answer"))
    clock.advance(119)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")
    bridge.closing = True
    clock.advance(1)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")  # hanging up
    bridge.closing = False
    await bridge.begin_limit("max_duration", 1800)
    assert not provider.commands("keepalive")  # the goodbye instead
    assert provider.commands("create")[-1][1].endswith("这通电话到时间了，我们文字里继续。")


async def test_a_task_still_running_does_not_hold_the_silent_reply_back():
    """2026-10-09: users waited silently for a delegated task; Audio closed the call after 180 s."""
    bridge, provider, link, clock = kept_call(AUDIO_MODEL)
    await replay(bridge, event("user_transcript", text="帮我看看贪吃蛇进展"), started("ack"), audio("ack"), tool_call("call-1"), done("ack"))
    for number in range(3):  # the one progress line a request gets, then other replies
        clock.advance(30)
        await bridge.fill_idle()
        await replay(bridge, started(f"p{number}"), audio(f"p{number}"), done(f"p{number}"))
    assert len(provider.commands("create")) == 1 and bridge.pending_calls
    clock.advance(119)
    await bridge.fill_idle()
    assert not provider.commands("keepalive")
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.commands("keepalive") == [("keepalive", True)]  # no user item in this session yet
    await replay(bridge, *silent_reply("keep", "item_keep"))
    assert bridge.pending_calls and not bridge.deliveries
    link.finish("call-1")  # the result comes in as usual and is told
    await drain()
    assert [command[0] for command in provider.sent[-2:]] == ["note", "create"]


async def test_a_refused_silent_reply_is_skipped_and_tried_again_later():
    bridge, provider, _, clock = kept_call(AUDIO_MODEL)
    await exchange(bridge, 1)
    clock.advance(120)
    await bridge.fill_idle()
    await replay(bridge, provider_error("active_response", param="response.create"))
    assert bridge.requested is None
    clock.advance(KEEPALIVE_RETRY_SECONDS - 1)
    await bridge.fill_idle()
    assert len(provider.commands("keepalive")) == 1
    clock.advance(1)
    await bridge.fill_idle()
    assert provider.commands("keepalive")[-1] == ("keepalive", False)
    # Audio found no user message left (it keeps max_history_turns): the next one brings its prompt item.
    await replay(bridge, provider_error("no_input", param="response.create"))
    clock.advance(KEEPALIVE_RETRY_SECONDS)
    await bridge.fill_idle()
    assert provider.commands("keepalive")[-1] == ("keepalive", True)


async def test_a_vad_reply_taken_for_the_silent_one_is_heard():
    """The user's answer won the race with our request: it is the model's reply after all."""
    bridge, provider, _, clock = kept_call(AUDIO_MODEL)
    await exchange(bridge, 1)
    clock.advance(120)
    await bridge.fill_idle()
    outbox(bridge)
    await replay(bridge, started("vad"), provider_error("active_response", param="response.create"),
                 audio("vad"), event("assistant_transcript", text="好的", item_id="a-vad", response_id="vad"))
    items = outbox(bridge)
    assert [value for value in items if isinstance(value, bytes)] and bridge.state == "responding"
    await replay(bridge, done("vad"))
    assert bridge.spoken.lines[-1].text == "好的" and bridge.state == "idle"
