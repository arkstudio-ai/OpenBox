"""Cloud VAD race, privacy, late outputs and acknowledged native voice context."""
import asyncio
from dataclasses import replace

import pytest

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


async def test_unconfirmed_speculative_cleanup_still_answers_the_utterance():
    # Measured 2026-10-26: a late deletion receipt ended the whole call and the
    # user's request with it. A never-played speculative reply is not a reason.
    bridge, provider, _ = call()
    async def failed_delete(identity):
        raise TimeoutError("No authoritative deletion receipt")
    provider.delete_item_confirmed = failed_delete
    await replay(bridge, event("user_started", item_id="human"), item("human"),
        event("user_stopped"), started("auto"),
        event("output_item", response_id="auto", item_id="unconfirmed-answer"),
        done("auto", status="cancelled"), event("user_transcript", item_id="human", text="最近有什么安排"))
    await finish_tasks(bridge)
    assert not bridge.failed.is_set()
    assert bridge.memory_applied_version == 1
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def test_unconfirmed_cleanup_under_an_explicit_restriction_never_answers_from_old_context():
    async def prepare(scope, text, recent):
        return packet(text, "current_only"), "updated instructions"
    bridge, provider, _ = call(prepare)
    async def failed_delete(identity):
        raise TimeoutError("No authoritative deletion receipt")
    provider.delete_item_confirmed = failed_delete
    await replay(bridge, item("earlier"), event("user_started", item_id="human"), item("human"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="只看我这句话，最近有什么安排"))
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


async def test_quarantined_tool_call_gets_one_void_output_and_never_executes():
    # Measured 2026-10-26: a speculative VAD reply called a tool; unanswered,
    # the provider never finished that reply and the call was ended.
    bridge, provider, _ = call()
    await replay(bridge, event("user_started", item_id="human"), item("human"), event("user_stopped"),
        started("auto"), tool_call("spec-1", name="assistant_ask", response_id="auto"),
        done("auto", status="cancelled"), event("user_transcript", item_id="human", text="帮我做个视频"))
    await finish_tasks(bridge)
    outputs = provider.commands("output")
    assert [(call_id, payload["status"]) for _, call_id, payload in outputs] == [("spec-1", "superseded")]
    assert outputs[0][2]["this_call_applied"] is False
    assert not bridge.link.started
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def test_a_stuck_quarantined_reply_is_abandoned_and_the_utterance_still_answered(monkeypatch):
    monkeypatch.setattr("voice.memory_context.QUARANTINE_SECONDS", 0.05)
    bridge, provider, _ = call()
    await replay(bridge, event("user_started", item_id="human"), item("human"), event("user_stopped"),
        started("auto"), event("output_item", response_id="auto", item_id="spec-answer"),
        event("user_transcript", item_id="human", text="再做一个五十秒的视频"))  # no response.done ever comes
    await finish_tasks(bridge)
    assert not bridge.failed.is_set()
    assert ("delete", "spec-answer") in provider.sent
    assert len(provider.commands("create")) == 1
    # Its late events stay quarantined: never played, never a second answer.
    await replay(bridge, audio("auto"), done("auto", status="cancelled"))
    assert not any(isinstance(value, bytes) for value in outbox(bridge))
    await bridge.close()


async def test_a_timed_out_read_decision_keeps_the_call_context_and_reads_nothing():
    async def prepare(scope, text, recent):
        value = packet(text, "uncertain")
        value["route"]["reason_code"] = "timeout"
        value["status"] = "unavailable"
        return value, "fresh"
    bridge, provider, _ = call(prepare)
    bridge.decision_context = "当前待决定事项：{\"task_questions\": [\"q1\"]}"
    await replay(bridge, item("earlier-request"), item("tool-result", item_type="function_call_output"),
        event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="竖屏的"))
    await finish_tasks(bridge)
    assert ("delete", "tool-result") in provider.sent
    assert ("delete", "earlier-request") not in provider.sent
    assert bridge.memory_turn.read_policy == "uncertain"
    assert "q1" in provider.commands("instructions")[-1][1]
    from memory.read_contract import permits
    assert not permits(bridge.memory_turn, "task_state") and not permits(bridge.memory_turn, "search")
    await bridge.close()


@pytest.mark.parametrize("judged, retired", [
    # Real restriction seen by a judge, even though the chain ended unresolved.
    ({"read_policy_answer": {"choice": "no_history", "restriction_role": "access"}}, True),
    ({"read_policy_answer": {"choice": "uncertain"}}, True),
    ({"primary_decision": {"restriction_role": {"choice": "access"}}}, True),
    # Measured: a short card answer, policy allowed / role none, only the
    # usefulness of reads unsure. The call's own context stays.
    ({"read_policy_answer": {"choice": "allowed", "restriction_role": "none"},
      "primary_decision": {"read_policy_answer": {"choice": "allowed"}, "restriction_role": {"choice": "none"}}}, False),
])
async def test_an_evaluated_uncertain_decision_retires_context_only_for_a_seen_restriction(judged, retired):
    async def prepare(scope, text, recent):
        value = packet(text, "uncertain")
        value["route"].update(reason_code="fallback_unresolved", **judged)
        return value, "fresh"
    bridge, provider, _ = call(prepare)
    await replay(bridge, item("earlier-request"), event("user_started", item_id="now"), item("now"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="快的那个就行"))
    await finish_tasks(bridge)
    assert (("delete", "earlier-request") in provider.sent) == retired
    from memory.read_contract import permits
    assert not permits(bridge.memory_turn, "task_state")  # reads stay gated either way
    await bridge.close()


async def test_failed_preparation_answers_without_memory_instead_of_ending_the_call():
    async def prepare(scope, text, recent):
        raise RuntimeError("memory service unavailable")
    bridge, provider, _ = call(prepare)
    bridge._memory_last_packet = packet("earlier", version=7)
    await replay(bridge, item("earlier-request"), event("user_started", item_id="now"), item("now"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="帮我再做一个短视频"))
    await finish_tasks(bridge)
    await drain()
    assert not bridge.failed.is_set()
    assert bridge.memory_turn.read_policy == "uncertain"
    assert bridge.memory_packet["status"] == "unavailable" and not bridge.memory_packet["items"]
    assert bridge.memory_packet["settings"] == {"address": "林姐"}
    assert ("delete", "earlier-request") not in provider.sent
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def test_overdue_preparation_degrades_after_the_watchdog():
    async def prepare(scope, text, recent):
        await asyncio.Event().wait()
    bridge, provider, clock = call(prepare)
    await replay(bridge, event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="做到哪了"))
    clock.advance(25)
    await bridge.fill_idle()
    await drain()
    assert not bridge.failed.is_set()
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def test_refused_prepared_answer_is_requested_again_not_fatal():
    from tests.support.voice_fakes import provider_error
    bridge, provider, clock = call()
    await replay(bridge, event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="你好"))
    await finish_tasks(bridge)
    assert bridge.requested == "model" and len(provider.commands("create")) == 1
    await replay(bridge, provider_error("active_response", param="response.create"))
    assert not bridge.failed.is_set() and bridge._memory_ready
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 1  # waits for the provider's own reply to end
    clock.advance(2)
    await bridge.fill_idle()
    assert len(provider.commands("create")) == 2
    await bridge.close()


async def test_missing_instruction_receipt_still_answers_an_unrestricted_turn_without_claiming_it():
    bridge, provider, _ = call()
    async def late_ack(text):
        provider.sent.append(("instructions", text))
        raise TimeoutError()
    provider.update_instructions_confirmed = late_ack
    await replay(bridge, event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="今天有什么安排"))
    await finish_tasks(bridge)
    assert not bridge.failed.is_set() and not provider.closed
    assert len(provider.commands("instructions")) >= 2  # unconfirmed text is sent again, never assumed
    assert len(provider.commands("create")) == 1  # a lost turn is worse than a late receipt
    assert bridge.memory_applied_version is None  # never reported as applied without a receipt
    await bridge.close()


def _question(identity="q-video", version="v1"):
    return {"id": identity, "version": version, "session_id": "s-video", "session_title": "抽象风格短视频",
            "questions": [{"header": "视频档位", "question": "选哪个视频档位？", "options": ["快速", "标准", "质量"],
                           "custom": False}],
            "assistant_may_answer": True, "card_kind": "question", "context": "分镜：五个镜头……" * 40}


async def test_a_card_shown_earlier_in_the_call_stays_answerable_on_the_next_turn():
    bridge, provider, _ = call()
    bridge.desk.show_question(_question(), "zh")
    await replay(bridge, item("cards-read", item_type="function_call_output"),
        event("user_started", item_id="answer"), item("answer"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="answer", text="快的那个就行"))
    await finish_tasks(bridge)
    assert ("delete", "cards-read") in provider.sent  # per-turn tool bodies still go
    instructions = provider.commands("instructions")[-1][1]
    assert '"request_id": "q-video"' in instructions and '"version": "v1"' in instructions
    assert "快速" in instructions and "抽象风格短视频" in instructions
    # Answered on screen: the watcher reconciles the desk and the card is gone.
    bridge.desk.reconcile([], [])
    assert "q-video" not in bridge._instructions()
    await bridge.close()


async def test_open_decisions_are_withheld_under_an_explicit_restriction():
    async def prepare(scope, text, recent):
        return packet(text, "current_only"), "fresh"
    bridge, provider, _ = call(prepare)
    bridge.desk.show_question(_question(), "zh")
    await replay(bridge, event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="只根据这句话回答"))
    await finish_tasks(bridge)
    assert "q-video" not in provider.commands("instructions")[-1][1]
    await bridge.close()


async def test_a_carried_utterance_gets_a_prepared_answer_and_survives_a_restriction():
    seen = []
    async def prepare(scope, text, recent):
        seen.append(text)
        return packet(text, "current_only"), "fresh"
    bridge, provider, _ = call(prepare)
    bridge.spoken.add("user", "帮我再做一个五十秒的抽象短视频", "lost-audio")
    bridge._user_utterances["lost-audio"] = 1
    bridge._last_user_item = "lost-audio"
    await bridge._carry_utterance("lost-audio", "帮我再做一个五十秒的抽象短视频")
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("carried", text=note), item("stale", role="assistant"))
    await finish_tasks(bridge)
    assert seen == ["帮我再做一个五十秒的抽象短视频"]
    assert ("delete", "stale") in provider.sent and ("delete", "carried") not in provider.sent
    assert len(provider.commands("create")) == 1 and not bridge.failed.is_set()
    await bridge.close()


async def test_an_uncaptured_utterance_is_answered_without_any_recall():
    reads = []
    async def prepare(scope, text, recent):
        reads.append(text)
        return packet(text), "recalled"
    bridge, provider, _ = call(prepare)
    bridge.skip_recall("human")
    await replay(bridge, item("earlier"), event("user_started", item_id="human"), item("human"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="human", text="帮我做个视频"))
    await finish_tasks(bridge)
    assert reads == [] and not bridge.failed.is_set()
    assert bridge.memory_packet["status"] == "unavailable" and bridge.memory_turn.read_policy == "uncertain"
    assert ("delete", "earlier") not in provider.sent
    assert len(provider.commands("create")) == 1
    await bridge.close()


async def _refuse_every_answer(bridge, provider, clock, attempts):
    from tests.support.voice_fakes import provider_error
    for _ in range(attempts):
        assert bridge.requested == "model"
        await replay(bridge, provider_error("server_error", param="response.create"))
        clock.advance(2)
        await bridge.fill_idle()
        await drain()


async def _heard(bridge, text, item_id="now"):
    await replay(bridge, event("user_started", item_id=item_id), item(item_id), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id=item_id, text=text))
    await finish_tasks(bridge)


async def test_exhausted_answer_retries_pass_the_words_on_and_keep_the_turn_working():
    bridge, provider, clock = call()
    await _heard(bridge, "帮我把海浪视频改成竖屏")
    outbox(bridge)
    # The prepared answer and every retry are refused: before, the call went
    # back to listening and the request was gone.
    await _refuse_every_answer(bridge, provider, clock, 4)
    assert len(provider.commands("create")) == 4 and bridge.requested is None
    [ref] = bridge.link.started
    assert ref.transcript == "帮我把海浪视频改成竖屏"  # the user's own words, never generated text
    assert ref.context["handover_source"] == "unanswered_voice_reply"
    sent = outbox(bridge)
    assert {"type": "turn", "turn_id": ref.id, "state": "accepted"}.items() <= next(
        value for value in sent if value.get("type") == "turn").items()
    phase = [value for value in sent if value.get("type") == "phase"][-1]
    assert phase["value"] == "working" and phase["working"]
    assert not bridge.failed.is_set()
    # The prepared answer is not asked again; the call tells the user it was
    # passed on once the provider takes a reply, and submits nothing twice.
    clock.advance(10)
    await bridge.fill_idle()
    await drain()
    assert bridge.requested == "followup" and len(provider.commands("create")) == 5
    await replay(bridge, started("told"), audio("told"), done("told"))
    await drain()
    assert any(isinstance(value, bytes) for value in outbox(bridge))
    assert len(bridge.link.started) == 1
    await bridge.close()


async def test_exhausted_retries_end_the_call_visibly_when_the_words_cannot_be_kept():
    bridge, provider, clock = call()
    await _heard(bridge, "帮我把海浪视频改成竖屏")
    bridge.link.fail_start = True
    await _refuse_every_answer(bridge, provider, clock, 4)
    assert bridge.failed.is_set() and not bridge.link.started
    await bridge.close()


async def test_a_retry_the_provider_accepts_is_spoken_without_a_handover():
    bridge, provider, clock = call()
    await _heard(bridge, "海浪视频做到哪了")
    await _refuse_every_answer(bridge, provider, clock, 2)
    assert len(provider.commands("create")) == 3 and bridge.requested == "model"
    await replay(bridge, started("answer"), audio("answer"), done("answer"))
    await drain()
    assert not bridge.link.started and not bridge.failed.is_set()
    assert any(isinstance(value, bytes) for value in outbox(bridge))  # the user hears the answer
    await bridge.close()


async def test_words_already_with_the_assistant_are_not_submitted_again():
    bridge, provider, clock = call()
    await _heard(bridge, "帮我把海浪视频改成竖屏")
    bridge._remember_spoken_action("now", {"status": "accepted", "request_id": "earlier"})
    await _refuse_every_answer(bridge, provider, clock, 4)
    assert not bridge.link.started and not bridge.failed.is_set()
    await bridge.close()


async def test_a_newer_utterance_supersedes_an_unanswered_one():
    bridge, provider, clock = call()
    await _heard(bridge, "帮我把海浪视频改成竖屏")
    await _refuse_every_answer(bridge, provider, clock, 3)
    await replay(bridge, event("user_started", item_id="next"))
    from tests.support.voice_fakes import provider_error
    await replay(bridge, provider_error("server_error", param="response.create"))
    await drain()
    assert not bridge.link.started and not bridge.failed.is_set()
    await bridge.close()


def _restricted_by_primary(text, reason):
    """The primary judge heard "don't use my history" below its bar; the fallback never answered."""
    value = packet(text, "uncertain")
    value["route"].update(reason_code=reason, fallback_reason="jev_uncertain_read_scope", primary_decision={
        "read_policy": "no_history", "read_policy_answer": {"choice": "no_history", "confidence": 0.82},
        "restriction_role": {"choice": "access", "confidence": 0.95}})
    value["status"] = "unavailable"
    return value


@pytest.mark.parametrize("reason", ["timeout", "network_error", "invalid_response"])
async def test_a_primary_restriction_survives_a_failed_fallback_in_both_assistants_context(reason):
    from voice.memory_context import keeps_call_context
    packets = iter([packet("帮我做个城市夜景视频"), None])
    async def prepare(scope, text, recent):
        return (next(packets) or _restricted_by_primary(text, reason)), "fresh"
    bridge, provider, _ = call(prepare)
    await _heard(bridge, "帮我做个城市夜景视频", "first")
    await replay(bridge, started("a1"), tool_call("ask-1", text="做城市夜景视频", response_id="a1"), done("a1"))
    bridge.decision_context = "当前待决定事项：{\"task_questions\": [\"q1\"]}"
    bridge.desk.show_question(_question(), "zh")
    bridge.keeper.summary = "用户之前要过城市夜景视频"
    await _heard(bridge, "别用之前的记录，重新做一个火车的视频")
    assert not keeps_call_context(bridge.memory_packet)
    # The voice model keeps this utterance only, without earlier decisions.
    assert ("delete", "first") in provider.sent and ("delete", "now") not in provider.sent
    instructions = provider.commands("instructions")[-1][1]
    assert "q1" not in instructions and "q-video" not in instructions
    # The text assistant gets the current words, never the call's history.
    await replay(bridge, started("a2"), tool_call("ask-2", text="重新做一个火车的视频", response_id="a2"))
    first, second = bridge.link.started
    assert "call" in first.context
    assert second.transcript == "别用之前的记录，重新做一个火车的视频"
    assert not {"call", "summary", "user_words", "earlier_user_words"} & set(second.context)
    await bridge.close()


async def test_a_restriction_seen_before_preparation_was_overdue_stays_in_force():
    from voice.memory_context import keeps_call_context
    async def prepare(scope, text, recent):
        return _restricted_by_primary(text, "timeout"), "fresh"
    bridge, provider, clock = call(prepare)
    bridge.desk.show_question(_question(), "zh")
    provider.deletion.clear()  # the restricted cleanup is slow: the watchdog degrades the turn
    await replay(bridge, item("earlier-request"), event("user_started", item_id="now"), item("now"),
        event("user_stopped"), started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="不要参考以前的内容，直接说说今天的天气"))
    await drain()
    clock.advance(25)
    await bridge.fill_idle()
    await drain()
    provider.deletion.set()
    for _ in range(5):
        await drain()
    assert not bridge.failed.is_set()
    assert bridge.memory_packet["degraded_reasons"] == ["voice_preparation_overdue"]
    assert not keeps_call_context(bridge.memory_packet)
    assert ("delete", "earlier-request") in provider.sent
    assert "q-video" not in provider.commands("instructions")[-1][1]
    await bridge.close()


@pytest.mark.parametrize("route, kept", [
    ({"read_policy": "uncertain", "reason_code": "timeout", "primary_decision": {
        "read_policy_answer": {"choice": "no_history"}}}, False),
    ({"read_policy": "uncertain", "reason_code": "network_error", "primary_decision": {
        "restriction_role": {"choice": "access"}}}, False),
    ({"read_policy": "uncertain", "reason_code": "timeout"}, True),  # no decision arrived at all
    ({"read_policy": "uncertain", "reason_code": "preparation_unavailable"}, True),
    ({"read_policy": "no_history"}, False),
    ({"read_policy": "allowed"}, True),
])
def test_keeps_call_context_checks_every_judge_before_the_failure_reason(route, kept):
    from voice.memory_context import keeps_call_context
    assert keeps_call_context({"route": route}) is kept


def _unconfirmed(provider, *, confirm_after=None):
    """Instruction updates the provider never confirms (or confirms from the given attempt on)."""
    attempts = []
    async def update(text):
        attempts.append(text)
        provider.sent.append(("instructions", text))
        if confirm_after is None or len(attempts) < confirm_after:
            raise TimeoutError("No authoritative session.updated receipt")
    provider.update_instructions_confirmed = update
    return attempts


async def test_a_restricted_turn_never_answers_from_instructions_the_provider_did_not_confirm():
    from voice.memory_context import keeps_call_context
    decisions = iter([_restricted_by_primary, lambda text, _: packet(text)])
    async def prepare(scope, text, recent):
        return next(decisions)(text, "timeout"), "fresh"
    bridge, provider, _ = call(prepare)
    fresh = []
    async def opener(instructions):
        fresh.append(Provider())
        fresh[-1].instructions = instructions
        return fresh[-1]
    bridge.opener = opener
    bridge.answered()
    bridge.desk.show_question(_question(), "zh")
    attempts = _unconfirmed(provider)
    await replay(bridge, item("earlier"), event("user_started", item_id="now"), item("now"), event("user_stopped"),
        started("auto"), done("auto", status="cancelled"),
        event("user_transcript", item_id="now", text="别用之前的记录，重新做一个火车的视频"))
    await finish_tasks(bridge)
    # Audio would answer from whatever instructions the service still holds.
    assert len(attempts) == 2 and not provider.commands("create")
    assert bridge.memory_applied_version is None and provider.closed and not bridge.failed.is_set()
    # The provider pump recovers the call: a fresh session opened with this
    # turn's restricted instructions, the user's words carried into it.
    assert await bridge.recover(provider)
    [new] = fresh
    assert "q-video" not in new.instructions
    [(_, note)] = new.commands("note")
    assert "火车" in note
    await replay(bridge, item("carried", text=note))
    await finish_tasks(bridge)
    # The second decision said "allowed"; the words' restriction stays pinned.
    assert not keeps_call_context(bridge.memory_packet)
    assert bridge.memory_packet["degraded_reasons"] == ["voice_preparation_pinned_restriction"]
    assert "q-video" not in new.commands("instructions")[-1][1]
    assert len(new.commands("create")) == 1 and not bridge.failed.is_set()
    await bridge.close()


async def test_a_late_receipt_confirms_restricted_instructions_on_the_second_attempt():
    async def prepare(scope, text, recent):
        return _restricted_by_primary(text, "timeout"), "fresh"
    bridge, provider, _ = call(prepare)
    attempts = _unconfirmed(provider, confirm_after=2)
    await _heard(bridge, "别用之前的记录，说个咖啡店名字")
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert bridge.memory_applied_version == 1 and len(provider.commands("create")) == 1
    assert not provider.closed and not bridge.failed.is_set()
    await bridge.close()


async def test_without_a_fresh_session_a_restricted_turn_is_passed_on_and_the_call_ends_visibly():
    async def prepare(scope, text, recent):
        return _restricted_by_primary(text, "timeout"), "fresh"
    bridge, provider, _ = call(prepare)  # no opener: the session cannot be replaced
    bridge.answered()
    bridge.keeper.summary = "用户之前要过城市夜景视频"
    _unconfirmed(provider)
    await replay(bridge, item("earlier"))
    await _heard(bridge, "别用之前的记录，帮我写一份咖啡店开业清单")
    assert not provider.commands("create")
    [ref] = bridge.link.started
    assert ref.transcript == "别用之前的记录，帮我写一份咖啡店开业清单"
    assert not {"call", "summary", "user_words", "earlier_user_words"} & set(ref.context)
    assert bridge.failed.is_set()
    await bridge.close()
