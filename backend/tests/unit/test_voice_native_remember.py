"""Native remember observes authenticated ASR receipts without starting an Agent."""
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.voice import _provider_event
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionJob, MemoryTurnCompletion
from db.models.memory_v2 import MemorySource
from db.models.message import Message
from db.models.voice import VoiceCall, VoiceTurn
from memory import service, settings
from memory.extraction import MemoryExtractionWorker
from tests.support.voice_fakes import FakeClock, ScriptedProvider, done, event, started, tool_call
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_voice_memory_capture import SAID, extractor, link_for, seed
from voice import memory_capture as capture, prompt
from voice.assistant_link import VoiceTurnRef
from voice.bridge import Bridge


async def captured(monkeypatch, words=SAID):
    scope, _, config = await seed(monkeypatch)
    config.semantic_recall = True
    identity = await capture.capture_final(scope, text=words, item_id="asr-1", provider_session=0)
    link = link_for(scope)
    link.bind_native_source("asr-1", words, identity)
    return scope, config, link, identity


async def status(link, text=SAID, **kwargs):
    return await link.remember_current(**{"item_id": "asr-1", "text": text, "provider_session": 0, **kwargs})


async def no_agent_input():
    async with get_db_session() as db:
        for model in (Message, AgentInboxItem, AgentEvent, VoiceTurn):
            assert await db.scalar(select(func.count()).select_from(model)) == 0


async def bridge_with_words(monkeypatch, words=SAID, *, native=True):
    scope, _, config = await seed(monkeypatch)
    config.semantic_recall = native
    link, provider = link_for(scope), ScriptedProvider()
    async def no_handoff_check(*_):
        raise AssertionError("A handled remember must not trigger a second intent model")
    bridge = Bridge(provider, link, scope=scope, clock=FakeClock(), instructions="BASE",
                    handoff_checker=no_handoff_check)
    bridge.greeting = False
    await _provider_event(bridge, link, event("user_started", item_id="asr-1"))
    await _provider_event(bridge, link, event("user_transcript", item_id="asr-1", text=words))
    await bridge.on_provider_event(started("native-reply"))
    return bridge, provider, link, config


async def remember(bridge, provider, call_id="remember-1", **arguments):
    await bridge.on_provider_event(tool_call(call_id, response_id="native-reply", arguments=json.dumps({
        "request": "模型对意图的表述不作为用户原话", "mode": "remember", **arguments})))
    return provider.commands("output")[-1][2]


@pytest.mark.parametrize("words", ["以后打电话时请详细解释。", "我的店叫晚风茶馆。", "记住刚才那个。"])
async def test_native_remember_returns_capture_state_without_any_assistant_turn(monkeypatch, words):
    bridge, provider, link, _ = await bridge_with_words(monkeypatch, words)
    try:
        result = await remember(bridge, provider)
        assert result["status"] == "pending" and result["capture_confirmed"]
        assert not result["memory_saved"] and not result["execution_confirmed"] and not result["handoff_created"]
        assert not link.count and not bridge.refs and not bridge.pending_calls and not bridge._spoken_actions
        assert bridge.handoff.attempted == bridge.handoff.fulfilled == {"asr-1"}
        await bridge.on_provider_event(done("native-reply"))
        assert not bridge.handoff.checked and bridge.handoff.pending is None
        await no_agent_input()
        async with get_db_session() as db:
            assert (await db.scalar(select(MemorySource))).body == words
            assert await db.scalar(select(func.count()).select_from(MemoryExtractionJob)) == 1
    finally:
        await bridge.close()


async def test_repeated_remember_reads_fresh_state_without_reenqueuing(monkeypatch):
    bridge, provider, _, _ = await bridge_with_words(monkeypatch)
    try:
        assert (await remember(bridge, provider))["status"] == "pending"
        async with get_db_session() as db:
            (await db.scalar(select(MemoryExtractionJob))).state = "RUNNING"
        assert (await remember(bridge, provider, "remember-2"))["status"] == "processing"
        await no_agent_input()
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(MemoryExtractionJob)) == 1
    finally:
        await bridge.close()


@pytest.mark.parametrize("state, expected", [("PENDING", "pending"), ("RUNNING", "processing"),
    ("RETRY", "retrying"), ("DEAD", "failed"), ("CANCELLED", "cancelled"), ("SUCCEEDED", "not_saved")])
async def test_job_state_alone_never_proves_saved_memory(monkeypatch, state, expected):
    _, _, link, _ = await captured(monkeypatch)
    async with get_db_session() as db:
        (await db.scalar(select(MemoryExtractionJob))).state = state
    result = await status(link)
    assert result["status"] == expected and result["extraction_state"] == state
    assert result["capture_confirmed"] and not result["memory_saved"]
    await no_agent_input()


@pytest.mark.parametrize("control", ["account", "session", "write", "extract"])
async def test_pause_or_disabled_writes_cannot_fall_back_to_an_agent(monkeypatch, control):
    scope, _, config = await seed(monkeypatch)
    config.semantic_recall = True
    if control == "account":
        await settings.update_settings(scope.user_id, auto_save=False)
    elif control == "session":
        await settings.update_settings(scope.user_id, session_id=scope.main_session_id, session_paused=True)
    else:
        setattr(config, "v2_write" if control == "write" else "auto_extract", False)
    identity = await capture.capture_final(scope, text=SAID, item_id="asr-1", provider_session=0)
    link = link_for(scope)
    link.bind_native_source("asr-1", SAID, identity)
    # Later resuming cannot turn this utterance into permission to backfill.
    await settings.update_settings(scope.user_id, auto_save=True, session_id=scope.main_session_id, session_paused=False)
    config.v2_write = config.auto_extract = True
    result = await status(link)
    assert result["status"] == ("paused" if control in {"account", "session"} else "disabled")
    assert result["capture_confirmed"] and not result["memory_saved"]
    await no_agent_input()


async def test_a_pause_after_capture_is_reported_before_the_worker_runs(monkeypatch):
    scope, _, link, _ = await captured(monkeypatch)
    await settings.update_settings(scope.user_id, auto_save=False)
    assert (await status(link))["status"] == "paused"
    await no_agent_input()


@pytest.mark.parametrize("replacement", [None, SimpleNamespace(run_id="other-run")],
                         ids=["missing", "not-native"])
async def test_receipt_lost_after_source_validation_returns_unavailable(monkeypatch, replacement):
    scope, _, _, identity = await captured(monkeypatch)
    original_get = AsyncSession.get
    receipt_reads = 0

    async def changed_get(db, model, key, *args, **kwargs):
        nonlocal receipt_reads
        if model is MemoryTurnCompletion:
            receipt_reads += 1
            if receipt_reads == 2:
                return replacement
        return await original_get(db, model, key, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "get", changed_get)
    # Keep the real source authority check. The later receipt read must itself
    # fail closed; remember_current's exception handler must not hide a crash.
    result = await capture.capture_status(user_id=scope.user_id, workspace_id=scope.workspace_id,
        session_id=scope.main_session_id, call_id=scope.call_id, provider_session=0,
        item_id="asr-1", identity=identity, text=SAID)
    assert receipt_reads == 2
    assert result == capture.unavailable_status()
    await no_agent_input()


async def test_only_current_verified_memory_results_can_be_reported_saved(monkeypatch):
    scope, _, link, identity = await captured(monkeypatch)
    seen = []
    assert await MemoryExtractionWorker(extractor=extractor(seen), verifier=Verifier()).run_once() == "SUCCEEDED"
    result = await status(link)
    assert result["status"] == "saved" and result["memory_saved"]
    assert result["saved_memories"][0]["summary"] == "用户的店叫晚风茶馆。"
    async with get_db_session() as db:
        row = await db.scalar(select(UserMemory))
        memory_id, revision = row.id, row.revision
    await service.forget_memory(user_id=scope.user_id, workspace_id=scope.workspace_id,
        memory_id=memory_id, expected_revision=revision, mode="sources", source_ids=[identity])
    withdrawn = await status(link)
    assert withdrawn["status"] == "unavailable" and not withdrawn["memory_saved"]
    assert "saved_memories" not in withdrawn
    await no_agent_input()


async def test_unverified_candidates_are_not_reported_as_saved(monkeypatch):
    _, config, link, _ = await captured(monkeypatch)
    config.automatic_knowledge = False
    assert await MemoryExtractionWorker(extractor=extractor([])).run_once() == "SUCCEEDED"
    result = await status(link)
    assert result["status"] == "not_saved" and not result["memory_saved"]


async def test_native_setting_requires_a_current_source_bound_write_receipt(monkeypatch):
    from db.models.preference import UserPreference
    from memory.preferences import REVISION_KEY
    text = "以后打电话时请详细解释。"
    _, _, link, _ = await captured(monkeypatch, text)
    def extract(_):
        return {"candidates": [{"type": "PREFERENCE", "summary": "用户希望电话中详细解释。",
            "fact_key": "personal.style.call_detail", "confidence": 90, "source_indexes": [0],
            "quotes": [{"source_index": 0, "quote": text}], "setting": "detailed"}]}
    assert await MemoryExtractionWorker(extractor=extract, verifier=Verifier()).run_once() == "SUCCEEDED"
    result = await status(link, text)
    assert result["status"] == "saved" and result["saved_settings"] == {"call_detail": "detailed"}
    async with get_db_session() as db:
        preference = await db.scalar(select(UserPreference).where(UserPreference.user_id == link.user_id))
        preference.extra = {**preference.extra, REVISION_KEY: preference.extra[REVISION_KEY] + 1}
    result = await status(link, text)
    assert result["status"] == "not_saved" and not result["memory_saved"]


@pytest.mark.parametrize("change", ["missing_binding", "wrong_text", "wrong_epoch", "wrong_source",
                                    "call_owner", "call_workspace", "call_session", "call_closed", "source_body"])
async def test_code_owned_binding_and_current_actor_call_source_are_required(monkeypatch, change):
    scope, _, link, identity = await captured(monkeypatch)
    arguments = {}
    if change == "missing_binding":
        link._native_sources.clear()
    elif change == "wrong_text":
        arguments["text"] = "另一句原话"
    elif change == "wrong_epoch":
        arguments["provider_session"] = 1
    elif change == "wrong_source":
        link.bind_native_source("asr-1", SAID, "msv_other_actors_source")
    else:
        async with get_db_session() as db:
            call = await db.get(VoiceCall, scope.call_id)
            if change == "source_body":
                (await db.get(MemorySource, identity)).body = "修改后的原话"
            elif change == "call_closed":
                call.status = "ended"
            else:
                setattr(call, {"call_owner": "user_id", "call_workspace": "workspace_id",
                               "call_session": "main_session_id"}[change], "wrong")
    result = await status(link, **arguments)
    assert result == capture.unavailable_status()
    await no_agent_input()


async def test_model_supplied_source_is_rejected_and_old_utterance_is_not_reused(monkeypatch):
    bridge, provider, link, _ = await bridge_with_words(monkeypatch)
    try:
        identity = link._native_sources["asr-1"][0]
        invalid = await remember(bridge, provider, source_id=identity)
        assert invalid["status"] == "invalid_arguments"
        bridge._last_user_item = "newer-audio"
        stale = await remember(bridge, provider, "old-source")
        assert stale == capture.unavailable_status()
        await no_agent_input()
    finally:
        await bridge.close()


async def test_missing_or_failed_status_never_creates_a_fallback_turn(monkeypatch):
    bridge, provider, link, _ = await bridge_with_words(monkeypatch)
    async def broken(**_):
        raise RuntimeError("source store unavailable")
    monkeypatch.setattr(capture, "capture_status", broken)
    try:
        assert (await remember(bridge, provider))["status"] == "unavailable"
        await no_agent_input()
    finally:
        await bridge.close()


async def test_native_link_refuses_an_accidental_legacy_remember_start(monkeypatch):
    _, _, link, _ = await captured(monkeypatch)
    ref = VoiceTurnRef(id="remember", provider_call_id="tool", text=SAID, transcript=SAID,
                      requested=1, lane="remember")
    with pytest.raises(ValueError, match="does not create"):
        await link.start(ref)
    await no_agent_input()


@pytest.mark.parametrize("native, mode", [(False, "remember"), (True, "request"), (True, "revise")])
async def test_legacy_remember_and_real_work_keep_the_existing_handover(monkeypatch, native, mode):
    from tests.unit.test_voice_cards import FORM
    bridge, provider, link, _ = await bridge_with_words(monkeypatch, native=native)
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    args = {"mode": mode}
    if mode == "revise":
        bridge.desk.show_question({**FORM, "version": "a" * 64}, "zh")
        args["question_id"] = "q-video"
    try:
        result = await remember(bridge, provider, **args)
        assert result["status"] == "accepted" and link.count == 1
        assert len(bridge.refs) == 1 and bridge.refs[0].inbox_id
        assert bridge.refs[0].lane == ("remember" if mode == "remember" else "assistant")
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem)) == 1
    finally:
        await bridge.close()


def test_native_prompt_distinguishes_capture_from_verified_persistence():
    from voice.tools import ASK_SCHEMA
    facts = prompt.FrontFacts(native_memory=True, ask_address=True)
    text = prompt.front_instructions(facts, "zh", datetime.now(timezone.utc))
    assert prompt.NATIVE_MEMORY_GUIDANCE in text and prompt.LEGACY_MEMORY_GUIDANCE not in text
    assert "原话交给助理记到设置里" not in text and "memory_saved=true" in text
    assert "事实和设置仍须经过" in text and "不会转交个人助理" in text
    schema = ASK_SCHEMA["function"]
    assert "成功转交才返回 accepted" in schema["description"]
    assert "不表示长期记忆已保存" in schema["description"]
    assert "不创建执行任务" in schema["parameters"]["properties"]["mode"]["description"]
    legacy = prompt.front_instructions(replace(facts, native_memory=False), "zh", datetime.now(timezone.utc))
    assert prompt.LEGACY_MEMORY_GUIDANCE in legacy and prompt.NATIVE_MEMORY_GUIDANCE not in legacy


async def test_current_front_facts_and_per_turn_prepare_keep_native_prompt(monkeypatch):
    scope, _, _, _ = await captured(monkeypatch)
    facts = await prompt.front_context(user_id=scope.user_id, workspace_id=scope.workspace_id,
                                       main_session_id=scope.main_session_id)
    assert facts.native_memory
    assert (await prompt.user_facts(scope.user_id, scope.workspace_id))["native_memory"]
    async def packet(_):
        # prepare() now re-applies the shared budget after attaching the
        # viewed conversation. Keep the stub on that real packet contract.
        return {"settings": {}, "stable_background": {"items": []},
                "items": [], "known_gaps": [], "degraded_reasons": []}
    monkeypatch.setattr("memory.packet.prepare_context", packet)
    from voice.memory_context import prepare
    _, instructions = await prepare(scope, SAID, [])
    assert prompt.NATIVE_MEMORY_GUIDANCE in instructions and prompt.LEGACY_MEMORY_GUIDANCE not in instructions
