"""Idle updates are local, acknowledged, silent and fenced against new speech."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.support.voice_fakes import drain
from tests.unit.test_voice_memory_context import call, packet
from tests.unit.test_memory_read_protocol import context
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def test_idle_refresh_waits_for_ack_without_answer_or_handoff():
    bridge, provider, _ = call()
    provider.ack.clear()
    async def refresh(scope, previous):
        fresh = packet("last", version=2)
        fresh["settings"].update(call_reports=False, call_detail="detailed", call_recap=False)
        return fresh, "new settings"
    bridge.refresh_memory = refresh
    update = asyncio.create_task(bridge.refresh_idle_memory())
    await drain()
    assert bridge.memory_applied_version is None and provider.commands("instructions")
    assert bridge.tell_reports and bridge.detail == "brief"
    provider.ack.set()
    await update
    assert bridge.memory_applied_version == 2
    assert not bridge.tell_reports and not bridge.recap and bridge.detail == "detailed"
    assert not provider.commands("create") and not bridge.link.started
    await bridge.close()


async def test_each_native_answer_and_report_end_with_current_spoken_policy():
    bridge, provider, _ = call()
    bridge.base_instructions = "DURABLE ROLE"
    bridge._memory_text = "VERIFIED MEMORY"
    bridge.memory_packet = packet("last", version=2)
    bridge.memory_packet["settings"].update(language="中文", call_detail="brief")
    try:
        await bridge._create(None)
        policy = provider.commands("response_policy")[-1][1]
        assert 'Speak your reply in "中文"' in policy
        assert "opening greeting is already over" in policy
        assert "VERIFIED MEMORY" not in policy
        full = provider.commands("create")[-1][1]
        assert "DURABLE ROLE" in full and "VERIFIED MEMORY" in full
        await bridge._create("Deliver this background report, preserving its facts.")
        report = provider.commands("create")[-1][1]
        assert report.index("Deliver this background report") < report.index("# Current spoken response policy")
        assert report.count("# Current spoken response policy") == 1
        assert len(provider.commands("response_policy")) == 2
        report_policy = provider.commands("response_policy")[-1][1]
        assert "Deliver this background report" in report_policy
        assert "Omit its greeting or salutation" in report_policy
        assert "opening greeting is already over" in report_policy
        assert "VERIFIED MEMORY" not in report_policy and "DURABLE ROLE" not in report_policy
    finally:
        await bridge.close()


async def test_notice_before_first_utterance_still_has_no_repeat_greeting_policy():
    bridge, provider, _ = call()
    bridge.base_instructions = "DURABLE ROLE AND SAVED LANGUAGE"
    assert bridge.memory_packet is None
    try:
        await bridge._create("Ask about the newly displayed task review.")
        policy = provider.commands("response_policy")[-1][1]
        assert "opening greeting is already over" in policy
        assert "Omit its greeting or salutation" in policy
        assert "DURABLE ROLE AND SAVED LANGUAGE" in provider.commands("create")[-1][1]
        assert not bridge.link.started
    finally:
        await bridge.close()


@pytest.mark.parametrize("address,text,expected", [
    ("林姐", "林姐，照片已收到，原方案还没确认。", "照片已收到，原方案还没确认。"),
    ("Dr. Li", "Dr. Li, the photo is ready.", "the photo is ready."),
    ("林姐", "林姐水果店的照片已收到。", "林姐水果店的照片已收到。"),
    ("林姐", "你的称呼已保存为林姐。", "你的称呼已保存为林姐。"),
    ("", "林姐，照片已收到。", "林姐，照片已收到。"),
])
async def test_background_note_omits_only_the_configured_salutation(address, text, expected):
    from voice.assistant_link import VoiceTurnRef
    bridge, provider, _ = call()
    bridge.memory_packet = packet("last", version=2)
    bridge.memory_packet["settings"]["address"] = address
    ref = VoiceTurnRef(id="report", provider_call_id="report:1", text="task", transcript="", requested=0)
    ref.speech, ref.status, ref.reason = text, "ok", "ok"
    try:
        assert bridge._note_speech(ref) == expected
        assert ref.speech == text  # Written and audit facts are never rewritten.
        assert expected in bridge._note_body(ref)
    finally:
        await bridge.close()


async def test_new_speech_supersedes_idle_read_before_it_applies():
    bridge, provider, _ = call()
    reading, release = asyncio.Event(), asyncio.Event()
    async def refresh(scope, previous):
        reading.set()
        await release.wait()
        return packet("old", version=1), "old read"
    bridge.refresh_memory = refresh
    update = asyncio.create_task(bridge.refresh_idle_memory())
    await reading.wait()
    bridge._memory_new_speech()
    release.set()
    await update
    assert bridge.memory_packet is None and bridge.memory_applied_version is None
    assert not provider.commands("instructions")
    await bridge.close()


async def test_speech_during_idle_ack_does_not_mark_old_version_applied():
    bridge, provider, _ = call()
    provider.ack.clear()
    async def refresh(scope, previous):
        return packet("old", version=1), "old settings"
    bridge.refresh_memory = refresh
    update = asyncio.create_task(bridge.refresh_idle_memory())
    await drain()
    assert provider.commands("instructions")
    bridge._memory_new_speech()
    provider.ack.set()
    await update
    assert bridge.memory_packet is None and bridge.memory_applied_version is None
    assert not provider.commands("create")
    await bridge.close()


async def test_busy_call_defers_idle_refresh():
    bridge, provider, _ = call()
    async def forbidden(*args):
        raise AssertionError("Must not read while speaking")
    bridge.refresh_memory = forbidden
    bridge.active_response = "speaking"
    await bridge.refresh_idle_memory()
    assert not provider.commands("instructions")
    bridge.active_response = None
    await bridge.close()


async def test_change_during_playback_wait_is_applied_before_response_create():
    bridge, provider, _ = call()
    bridge.memory_packet = packet("last", version=1)
    bridge._memory_ready = True
    async def refresh(scope, previous):
        assert previous["context_version"] == 1
        return packet("last", version=3), "current settings"
    bridge.refresh_memory = refresh
    provider.ack.clear()
    answer = asyncio.create_task(bridge._say_memory_answer())
    await drain()
    assert not provider.commands("create")
    provider.ack.set()
    await answer
    assert bridge.memory_applied_version == 3
    assert len(provider.commands("create")) == 1
    await bridge.close()


@pytest.mark.parametrize("language", ["中文", ""])
async def test_final_refresh_ack_contains_the_new_language_before_version_advances(language):
    bridge, provider, _ = call()
    old = packet("last", version=1)
    old["settings"]["language"] = "English"
    bridge.memory_packet, bridge.memory_applied_version = old, 1
    fresh = packet("last", version=2)
    fresh["settings"]["language"] = language

    async def refresh(scope, previous):
        return fresh, "Current SQL settings"

    bridge.refresh_memory = refresh
    provider.ack.clear()
    answer = asyncio.create_task(bridge._say_memory_answer())
    try:
        await drain()
        instructions = provider.commands("instructions")[-1][1]
        assert 'Speak your reply in "English"' not in instructions
        assert ('Speak your reply in "中文"' if language else "No fixed language is selected") in instructions
        assert bridge.memory_packet is old and bridge.memory_applied_version == 1
        assert not provider.commands("create")
    finally:
        provider.ack.set()
        await answer
        await bridge.close()
    assert bridge.memory_packet is fresh and bridge.memory_applied_version == 2
    assert len(provider.commands("create")) == 1


async def test_failed_final_ack_keeps_the_old_version_and_never_answers():
    bridge, provider, _ = call()
    old = packet("last", version=1)
    bridge.memory_packet, bridge.memory_applied_version = old, 1

    async def refresh(*args):
        return packet("last", version=2), "new setting"

    async def failed_ack(text):
        raise TimeoutError()

    bridge.refresh_memory = refresh
    provider.update_instructions_confirmed = failed_ack
    await bridge._say_memory_answer()
    assert bridge.memory_packet is old and bridge.memory_applied_version == 1
    assert not provider.commands("create")
    # The call goes on: the same answer is requested again shortly.
    assert not bridge.failed.is_set() and bridge._memory_ready and bridge._memory_retries == 1
    await bridge.close()


async def test_speech_during_final_ack_supersedes_the_prepared_answer():
    bridge, provider, _ = call()
    bridge.memory_packet, bridge.memory_applied_version = packet("last", version=1), 1

    async def refresh(*args):
        return packet("last", version=2), "new setting"

    bridge.refresh_memory = refresh
    provider.ack.clear()
    answer = asyncio.create_task(bridge._say_memory_answer())
    await drain()
    assert provider.commands("instructions")
    bridge._memory_new_speech()
    provider.ack.set()
    await answer
    assert bridge.memory_packet is None and bridge.memory_applied_version == 1
    assert not provider.commands("create")
    await bridge.close()


async def test_failed_ack_keeps_old_packet_and_retries():
    bridge, provider, _ = call()
    old = packet("previous", version=1)
    bridge.memory_packet, bridge.memory_applied_version = old, 1
    seen = []
    async def refresh(scope, previous):
        seen.append(previous)
        return packet("new", version=2), "new settings"
    async def missing_ack(text):
        raise TimeoutError()
    bridge.refresh_memory = refresh
    confirmed = provider.update_instructions_confirmed
    provider.update_instructions_confirmed = missing_ack
    await bridge.refresh_idle_memory()
    assert bridge.memory_packet is old and bridge.memory_applied_version == 1
    provider.update_instructions_confirmed = confirmed
    await bridge.refresh_idle_memory()
    assert seen == [old, old] and bridge.memory_applied_version == 2
    assert not provider.commands("create")
    await bridge.close()


async def test_new_and_restricted_call_refreshes_settings_without_historical_reads(monkeypatch):
    from db.base import get_db_session
    from memory.versions import changed
    from voice.memory_context import refresh
    _, turn = await context(monkeypatch)
    scope = SimpleNamespace(user_id=turn.user_id, workspace_id=turn.workspace_id,
        main_session_id=turn.session_id, call_id="call", lang="zh")
    async def forbidden(*args, **kwargs):
        raise AssertionError("Idle refresh must not authorize history or call a model")
    monkeypatch.setattr("memory.orchestrator._stable_background", forbidden)
    monkeypatch.setattr("memory.read_routing.route", forbidden)
    first, _ = await refresh(scope, None)
    assert first["route"]["read_policy"] == "uncertain"
    assert await refresh(scope, first) is None
    async with get_db_session() as db:
        revision = await changed(db, turn.user_id)
    second, _ = await refresh(scope, first)
    assert second["context_version"] == revision
    assert second["items"] == second["stable_background"]["items"] == []
    assert await refresh(scope, second) is None


async def test_live_main_and_task_changes_refresh_without_memory_version_change(monkeypatch):
    from dataclasses import replace
    from db.base import get_db_session
    from db.models.session import Session
    from memory import packet as packets
    from memory.orchestrator import render_memory_context
    from session.session import create_session, create_user_message
    from tests.unit.test_memory_read_protocol import allowed
    from voice.memory_context import refresh
    from voice.tools import CallScope
    from voice.view_context import attach
    config, turn = await context(monkeypatch)
    turn = replace(turn, entry="voice", query="刚才在文字里改好了，现在进展怎么样")
    task = await create_session(user_id=turn.user_id, workspace_id=turn.workspace_id, title="社区读书会")
    scope = CallScope(turn.user_id, turn.workspace_id, turn.session_id, call_id="call")
    async def evaluate(*args, **kwargs):
        response = allowed()
        response["answers"]["task_needed"] = {"choice": "read", "confidence": 1}
        return response
    previous = await packets.prepare_context(turn, config, evaluator=evaluate)
    await attach(scope, previous)
    async with get_db_session() as db:
        (await db.get(Session, task.id)).status = "busy"
    await create_user_message(turn.session_id, "改到周日下午三点，不要沿用周六方案", user_id=turn.user_id,
        agent="assistant", origin="human", origin_ref={"actor_user_id": turn.user_id})
    async def forbidden(*args, **kwargs):
        raise AssertionError("Live refresh must not call a model or search")
    monkeypatch.setattr("memory.read_routing.route", forbidden)
    fresh, _ = await refresh(scope, previous)
    assert fresh["context_version"] == previous["context_version"]
    assert "周日下午三点" in render_memory_context(fresh)
    assert fresh["task_state"]["sessions"][0]["status"] == "busy"
    assert await refresh(scope, fresh) is None  # no repeated updates for observed_at alone
