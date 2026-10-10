"""Long calls (docs/ASSISTANT_VOICE_FIX_PLAN.md §5.3): summaries, deleted items, a fresh provider session."""
import asyncio
from datetime import datetime

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, item,
                                       outbox, started, tool_call)
from voice import upkeep
from voice.bridge import Bridge
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
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-1"), done("ack"))
    bridge.keeper.session_started -= upkeep.ROTATE_SECONDS
    clock.advance(30)
    await bridge.fill_idle()
    assert bridge.rotating
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
    await replay(bridge, started("ack"), audio("ack"), tool_call("call-1"), done("ack"))
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
