"""Cloud VAD race, privacy, late outputs and acknowledged native voice context."""
import asyncio
from dataclasses import replace

from memory.packet import TurnContext
from tests.support.voice_fakes import ScriptedProvider, FakeLink, FakeClock, event, started, done, audio, tool_call, drain, outbox, item
from tests.unit.test_voice_bridge import replay
from voice.bridge import Bridge


class Provider(ScriptedProvider):
    def __init__(self):
        super().__init__()
        self.ack = asyncio.Event()
        self.ack.set()
        self.deletion = asyncio.Event()
        self.deletion.set()

    async def update_instructions_confirmed(self, instructions):
        self.sent.append(("instructions", instructions))
        await self.ack.wait()

    async def delete_item_confirmed(self, identity):
        self.sent.append(("delete", identity))
        await self.deletion.wait()


def packet(text, policy="allowed", version=1):
    return {"_semantic_packet": True, "_turn": TurnContext("user", "workspace", text),
        "route": {"read_policy": policy}, "context_version": version, "status": "complete",
        "settings": {"address": "林姐"}, "items": [], "stable_background": {"items": []}}


def call(prepare=None):
    async def default(scope, text, recent):
        return packet(text), "updated instructions"
    provider, clock = Provider(), FakeClock()
    bridge = Bridge(provider, FakeLink(), clock=clock, prepare_memory=prepare or default)
    bridge._greeting_over()
    return bridge, provider, clock


async def finish_tasks(bridge):
    if bridge._memory_task:
        await asyncio.wait_for(bridge._memory_task, 1)
    await drain()


async def test_current_language_wins_over_recalled_language_without_an_extra_response():
    bridge, provider, _ = call()
    bridge.base_instructions = "BASE"
    bridge._memory_text = "Old recalled preference: speak Chinese."
    bridge.memory_packet = {**packet("周末做点什么"), "settings": {"language": "English"}}
    await bridge._sync_instructions(force=True)
    instructions = provider.commands("instructions")[-1][1]
    assert instructions.index('Speak your reply in "English"') > instructions.index("Old recalled preference")
    assert "only for that duration" in instructions
    assert not provider.commands("create")
    # An idle update is rendered before memory_packet is committed after ACK.
    refreshed = {**packet("周末做点什么", version=2), "settings": {"language": ""}}
    new_instructions = bridge._instructions(memory_packet=refreshed)
    assert 'Speak your reply in "English"' not in new_instructions
    assert "No fixed language is selected" in new_instructions
    await bridge.close()


async def test_voice_policy_distinguishes_the_opening_from_later_replies():
    bridge, provider, _ = call()
    bridge.memory_packet = packet("query")
    bridge.greeting = True
    await bridge._sync_instructions(force=True)
    assert "opening greeting is already over" not in provider.commands("instructions")[-1][1]
    bridge._greeting_over()
    await bridge._sync_instructions(force=True)
    assert "opening greeting is already over" in provider.commands("instructions")[-1][1]
    assert "if the user asks about it" in provider.commands("instructions")[-1][1]
    assert not provider.commands("create")
    await bridge.close()


async def test_new_speech_does_not_wait_for_an_incomplete_instruction_update():
    bridge, provider, _ = call()
    bridge.memory_packet = packet("previous")
    bridge.base_instructions = "Current role"
    provider.ack.clear()
    await asyncio.wait_for(bridge.on_provider_event(event("user_started", item_id="new")), .2)
    assert bridge._memory_pending and bridge.memory_packet is None
    assert not provider.commands("instructions") and not provider.commands("create")
    # The usable packet must still be acknowledged, even though VAD did not
    # wait on an intermediate prompt without any recalled evidence/settings.
    bridge.memory_packet = packet("new", version=2)
    update = asyncio.create_task(bridge._sync_instructions(force=True))
    await drain()
    assert provider.commands("instructions") and not update.done()
    provider.ack.set()
    assert await update
    await bridge.close()


async def test_vad_output_is_never_played_or_executed_and_human_audio_survives():
    bridge, provider, _ = call()
    await replay(bridge, event("user_started", item_id="human"), item("human"), event("user_stopped"),
        started("auto"), audio("auto"), tool_call("forbidden", response_id="auto"),
        event("output_item", response_id="auto", item_id="old-answer"),
        event("assistant_transcript", response_id="auto", item_id="old-answer", text="stale secret"),
        done("auto", status="cancelled"), event("user_transcript", item_id="human", text="我刚才说的事情呢"))
    await finish_tasks(bridge)
    assert provider.commands("cancel") and ("delete", "old-answer") in provider.sent
    assert ("delete", "human") not in provider.sent
    assert not bridge.link.started and not any(isinstance(v, bytes) for v in outbox(bridge))
    assert not any("stale secret" in line.text for line in bridge.spoken.lines)
    assert len(provider.commands("create")) == 1
    assert len(provider.commands("response_policy")) == 1
    assert "stale secret" not in provider.commands("create")[0][1]
    assert bridge.memory_applied_version == 1
    await bridge.close()


async def test_asr_before_vad_waits_for_cancel_and_exact_instruction_ack():
    bridge, provider, _ = call()
    await replay(bridge, event("user_started", item_id="human"), event("user_stopped"),
        event("user_transcript", item_id="human", text="你好"))
    assert not provider.commands("create")
    provider.ack.clear()
    await replay(bridge, started("auto"), done("auto", status="cancelled"))
    assert not provider.commands("create")
    provider.ack.set()
    await finish_tasks(bridge)
    assert len(provider.commands("create")) == 1
    assert len(provider.commands("response_policy")) == 1
    await bridge.close()


async def test_shared_task_changes_do_not_restart_a_prepared_memory_answer_or_leak_context():
    from voice.shared_context import SharedSnapshot
    async def prepare(scope, text, recent):
        return packet(text, "current_only"), "fresh bounded instructions"
    bridge, provider, _ = call(prepare)
    async def read():
        return SharedSnapshot("unrelated private task details", [
            {"session_id": "other", "version": "changed"}], sequence=1)
    bridge.context_reader = read
    await bridge.refresh_shared_context(await read())  # the background watcher still refreshes work
    bridge._native_work_read = True
    bridge._native_work_pending = {"other": "previous-turn"}
    bridge._native_work_decisions = {"old-card": "old-version"}
    bridge._native_work_stale = True
    await replay(bridge, event("user_started", item_id="human"), item("human"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="只根据我这句话回答，今天怎么安排运动"))
    await finish_tasks(bridge)
    assert len(provider.commands("create")) == 1
    await replay(bridge, started("prepared"), audio("prepared"), done("prepared"))
    assert any(isinstance(value, bytes) for value in outbox(bridge))
    assert len(provider.commands("cancel")) == 1  # only the speculative VAD reply
    assert len(provider.commands("create")) == 1
    assert all("unrelated private task details" not in text
               for _, text in provider.commands("instructions"))
    await bridge.close()


async def test_memory_asr_does_not_wait_for_a_slow_work_snapshot():
    bridge, provider, _ = call()
    calls = []
    async def read():
        calls.append("read")
        await asyncio.Event().wait()
    bridge.context_reader = read
    await replay(bridge, event("user_started", item_id="human"))
    await asyncio.wait_for(bridge.on_provider_event(event("user_stopped")), .2)
    assert not calls  # memory answers have their own acknowledged preparation barrier
    assert bridge._memory_pending
    await replay(bridge, item("human"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="你好"))
    await finish_tasks(bridge)
    await asyncio.wait_for(bridge.on_provider_event(started("prepared")), .2)
    await replay(bridge, audio("prepared"), done("prepared"))
    assert not calls and any(isinstance(value, bytes) for value in outbox(bridge))
    await bridge.close()


async def test_an_explicit_empty_work_read_still_requires_freshness(monkeypatch):
    from voice.tools import CallScope
    bridge, provider, _ = call()
    bridge.scope = CallScope("user", "workspace", "main")
    bridge.memory_packet = packet("我有待办吗")
    bridge.memory_turn = replace(bridge.memory_packet["_turn"], read_policy="allowed")
    reads = []
    async def empty(*_):
        return {"status": "ok", "tasks": []}
    async def unavailable():
        reads.append("snapshot")
        raise TimeoutError("work state unavailable")
    monkeypatch.setattr("voice.tools.run", empty)
    bridge.context_reader = unavailable
    await bridge._complete_direct_call(tool_call("read-work", name="tasks_overview", arguments="{}"))
    await bridge.fill_idle()
    assert reads == ["snapshot"]
    assert bridge.requested == "followup"
    assert "暂时无法核实进展" in provider.commands("create")[-1][1]
    await bridge.close()


async def test_new_utterance_cancels_old_preparation_even_if_asr_arrives_immediately():
    calls = []
    async def prepare(scope, text, recent):
        calls.append(text)
        if text == "old":
            await asyncio.Event().wait()
        return packet(text, version=2), "new instructions"
    bridge, provider, _ = call(prepare)
    await replay(bridge, event("user_started", item_id="a"), started("auto1"), done("auto1", status="cancelled"),
        event("user_transcript", item_id="a", text="old"))
    await replay(bridge, event("user_started", item_id="b"), event("user_transcript", item_id="b", text="new"),
        started("auto2"), done("auto2", status="cancelled"))
    await finish_tasks(bridge)
    assert calls == ["old", "new"] and bridge.memory_turn.query == "new"
    assert bridge.memory_applied_version == 2
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def test_restricted_turn_removes_provider_history_and_allowed_turn_removes_old_tool_results():
    for policy in ("current_only", "allowed"):
        async def prepare(scope, text, recent):
            return packet(text, policy), "fresh"
        bridge, provider, _ = call(prepare)
        await replay(bridge, item("previous"), item("tool-result", item_type="function_call_output"),
            event("user_started", item_id="now"), item("now"), event("user_stopped"),
            started("auto"), done("auto", status="cancelled"),
            event("user_transcript", item_id="now", text="只看我这句话"))
        await finish_tasks(bridge)
        assert ("delete", "tool-result") in provider.sent
        assert (("delete", "previous") in provider.sent) == (policy == "current_only")
        assert ("delete", "now") not in provider.sent
        await bridge.close()


async def test_long_human_speech_does_not_trip_preparation_watchdog():
    bridge, provider, clock = call()
    await replay(bridge, event("user_started", item_id="human"))
    clock.advance(45)
    await bridge.fill_idle()
    assert not bridge.failed.is_set()
    await replay(bridge, event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="一段比较长的说明"))
    await finish_tasks(bridge)
    assert not bridge.failed.is_set() and provider.commands("create")
    await bridge.close()


async def test_unconfirmed_cleanup_cannot_apply_memory_or_start_an_answer():
    bridge, provider, _ = call()
    async def failed_delete(identity):
        raise TimeoutError("No authoritative deletion receipt")
    provider.delete_item_confirmed = failed_delete
    await replay(bridge, event("user_started", item_id="human"), item("human"),
        event("user_stopped"), started("auto"),
        event("output_item", response_id="auto", item_id="unconfirmed-answer"),
        done("auto", status="cancelled"), event("user_transcript", item_id="human", text="最近有什么安排"))
    await finish_tasks(bridge)
    assert bridge.failed.is_set()
    assert bridge.memory_applied_version is None
    assert not provider.commands("create")
    assert all("updated instructions" not in value for _, value in provider.commands("instructions"))
    await bridge.close()


async def test_transport_prewarm_does_not_wait_for_asr_or_authorize_a_read_and_stops_at_hangup():
    started_warm, cancelled = asyncio.Event(), asyncio.Event()
    calls = []
    async def warm():
        calls.append("warm")
        started_warm.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
    bridge, provider, _ = call()
    bridge.prewarm_memory = warm
    await replay(bridge, event("user_started", item_id="human"))
    await asyncio.wait_for(started_warm.wait(), 1)
    bridge._prewarm_memory_connection()
    assert calls == ["warm"] and bridge.memory_packet is None
    assert not bridge.link.started and not provider.commands("create")
    await replay(bridge, event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="你好"))
    await finish_tasks(bridge)
    assert bridge.memory_applied_version == 1  # A slow warmup never blocks the actual read/ACK.
    await asyncio.wait_for(bridge.close(), 1)
    assert cancelled.is_set()
